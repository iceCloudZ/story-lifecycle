"""Unit tests for debug_packet.py -- packet building, stuck reasons, redaction."""

import pytest
from story_lifecycle.orchestrator.observability.debug_packet import (
    build_debug_packet,
    redact_text,
    redact_mapping,
)
from story_lifecycle.infra.db.models import init_db


class TestBuildDebugPacket:
    @pytest.fixture(autouse=True)
    def _isolated_db(self, tmp_path, monkeypatch):
        """Use a temporary database per test to avoid UNIQUE constraint conflicts."""
        monkeypatch.setenv("STORY_HOME", str(tmp_path))
        init_db()

    def test_nonexistent_story(self):
        result = build_debug_packet("NONEXISTENT-STORY")
        assert result == {"error": "Story not found"}

    def test_packet_schema_keys(self, tmp_path):
        """build_debug_packet returns all required top-level keys."""
        from story_lifecycle.infra.db.models import create_story

        ws = str(tmp_path)
        create_story("TEST-001", "Test Story", ws)
        (tmp_path / ".story" / "done" / "TEST-001").mkdir(parents=True, exist_ok=True)

        packet = build_debug_packet("TEST-001")
        required_keys = {
            "schema_version",
            "generated_at",
            "story",
            "done_state",
            "session_state",
            "terminal_output",
            "stuck_reason",
            "recent_events",
            "recent_stage_logs",
            "gate_results",
            "file_hints",
        }
        assert required_keys.issubset(set(packet.keys()))
        assert packet["schema_version"] == 1
        assert packet["story"]["story_key"] == "TEST-001"

    def test_missing_config_stuck_reason(self, tmp_path, monkeypatch):
        """If no LLM key configured, stuck_reason should be missing_config."""
        monkeypatch.setattr(
            "story_lifecycle.orchestrator.observability.debug_packet._check_llm_configured",
            lambda: False,
        )
        from story_lifecycle.infra.db.models import create_story

        ws = str(tmp_path)
        create_story("TEST-002", "Test", ws)
        (tmp_path / ".story" / "done" / "TEST-002").mkdir(parents=True, exist_ok=True)

        packet = build_debug_packet("TEST-002")
        assert packet["stuck_reason"]["code"] == "missing_config"


class TestStuckReasons:
    """Test _explain_stuck_reason via build_debug_packet."""

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "story_lifecycle.orchestrator.observability.debug_packet._check_llm_configured",
            lambda: True,
        )
        monkeypatch.setenv("STORY_HOME", str(tmp_path))
        init_db()

    def test_done_malformed(self, tmp_path):
        from story_lifecycle.infra.db.models import create_story

        ws = str(tmp_path)
        create_story("TEST-MAL", "Malformed Done", ws)
        done_dir = tmp_path / ".story" / "done" / "TEST-MAL"
        done_dir.mkdir(parents=True)
        done_dir.joinpath("design.json").write_text(
            "not valid json {{{", encoding="utf-8"
        )

        packet = build_debug_packet("TEST-MAL")
        assert packet["done_state"]["valid"] is False
        assert packet["stuck_reason"]["code"] == "done_malformed"

    def test_story_blocked(self, tmp_path):
        from story_lifecycle.infra.db.models import create_story, update_story

        ws = str(tmp_path)
        create_story("TEST-BLK", "Blocked", ws)
        # blocked 合并进 paused;子原因 manual_fail 写 ctx._pause_reason。
        update_story(
            "TEST-BLK",
            status="paused",
            context_json='{"_pause_reason": "manual_fail"}',
        )
        (tmp_path / ".story" / "done" / "TEST-BLK").mkdir(parents=True, exist_ok=True)

        packet = build_debug_packet("TEST-BLK")
        assert packet["stuck_reason"]["code"] == "story_blocked"

    def test_story_ok(self, tmp_path):
        from story_lifecycle.infra.db.models import create_story

        ws = str(tmp_path)
        create_story("TEST-OK", "Fine", ws)
        (tmp_path / ".story" / "done" / "TEST-OK").mkdir(parents=True, exist_ok=True)

        packet = build_debug_packet("TEST-OK")
        assert packet["stuck_reason"]["code"] == "none"


class TestRedaction:
    def test_redact_openai_key(self):
        text = "Authorization: Bearer sk-abc123def456ghijklmnopqrstuvwxyz"
        result = redact_text(text)
        assert "sk-abc" not in result
        assert "[REDACTED_API_KEY]" in result

    def test_redact_env_var(self):
        text = "export STORY_LLM_API_KEY=sk-ant-secret12345"
        result = redact_text(text)
        assert "secret12345" not in result
        assert "[REDACTED]" in result

    def test_redact_key_value(self):
        text = 'api_key: "my-secret-token-here"'
        result = redact_text(text)
        assert "my-secret-token-here" not in result
        assert "[REDACTED]" in result

    def test_redact_mapping_nested(self):
        data = {
            "config": {
                "api_key": "secret123",
                "url": "https://api.example.com",
                "nested": {"token": "abc123"},
            }
        }
        result = redact_mapping(data)
        assert result["config"]["api_key"] == "[REDACTED]"
        assert result["config"]["url"] == "https://api.example.com"
        assert result["config"]["nested"]["token"] == "[REDACTED]"

    def test_redact_anthropic_key(self):
        text = "using sk-ant-api03-abcdefghijklmnopqrstuvwxyz for auth"
        result = redact_text(text)
        assert "sk-ant-api03" not in result
        assert "[REDACTED_API_KEY]" in result
