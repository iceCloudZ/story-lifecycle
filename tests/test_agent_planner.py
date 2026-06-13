"""Tests for Agent planner — run_orchestrator_agent and continue_orchestrator_agent."""

import json
from unittest.mock import patch, MagicMock

import pytest

from story_lifecycle.orchestrator.planner import (
    run_orchestrator_agent,
    continue_orchestrator_agent,
    _build_agent_system_prompt,
    _build_agent_user_message,
)


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Set up isolated DB for testing."""
    from story_lifecycle.db import models as db

    db_path = tmp_path / "story.db"
    monkeypatch.setattr(db, "get_db_path", lambda: db_path)
    db.init_db()
    return db


def _make_story(isolated_db, monkeypatch, tmp_path, **overrides):
    """Create a test story in DB."""
    from story_lifecycle.db import models as db

    defaults = {
        "story_key": "TEST-001",
        "title": "Test Story",
        "profile": "minimal",
        "workspace": str(tmp_path / "workspace"),
        "status": "planning",
        "intake_state": "ready",
    }
    defaults.update(overrides)
    db.upsert_story(**defaults)
    return defaults


class TestBuildPrompts:
    def test_system_prompt_contains_stages(self):
        prompt = _build_agent_system_prompt(
            profile_stages={"design": {"description": "设计方案", "cli": "codex"}},
            story_title="Auth",
            story_key="AUTH-001",
        )
        assert "design" in prompt
        assert "codex" in prompt
        assert "AUTH-001" in prompt

    def test_user_message_contains_title(self):
        msg = _build_agent_user_message(
            story_key="AUTH-001",
            title="实现登录",
            content="需要 JWT",
            workspace="/tmp/ws",
        )
        assert "实现登录" in msg
        assert "JWT" in msg


class TestRunOrchestratorAgent:
    def test_collects_plan_step_actions(self, isolated_db, monkeypatch, tmp_path):
        _make_story(isolated_db, monkeypatch, tmp_path)
        mock_llm = MagicMock()

        # Round 1: agent calls plan_step
        call_count = [0]

        def mock_invoke(messages, tools, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "c1",
                                "type": "function",
                                "function": {
                                    "name": "plan_step",
                                    "arguments": '{"adapter":"claude","stage":"design","focus":"需求澄清"}',
                                },
                            }
                        ],
                    },
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": "plan_step",
                                "arguments": {
                                    "adapter": "claude",
                                    "stage": "design",
                                    "focus": "需求澄清",
                                },
                            },
                        }
                    ],
                    "content": "",
                }
            else:
                # Round 2: agent stops
                return {
                    "message": {"role": "assistant", "content": "规划完成"},
                    "tool_calls": [],
                    "content": "规划完成",
                }

        mock_llm.invoke_with_tools = mock_invoke
        actions_received = []

        with patch(
            "story_lifecycle.orchestrator.planner.get_llm", return_value=mock_llm
        ):
            result = run_orchestrator_agent(
                "TEST-001", on_action=lambda e: actions_received.append(e)
            )

        assert result["status"] == "planning"
        assert len(result["actions"]) == 1
        assert result["actions"][0]["action"] == "launch"
        assert result["actions"][0]["adapter"] == "claude"
        assert result["actions"][0]["stage"] == "design"
        # Callback was called
        assert len(actions_received) == 1

    def test_writes_actions_to_db(self, isolated_db, monkeypatch, tmp_path):
        from story_lifecycle.db import models as db

        _make_story(isolated_db, monkeypatch, tmp_path)
        mock_llm = MagicMock()

        def mock_invoke(messages, tools, **kwargs):
            return {
                "message": {"role": "assistant", "content": "done"},
                "tool_calls": [],
                "content": "done",
            }

        mock_llm.invoke_with_tools = mock_invoke

        with patch(
            "story_lifecycle.orchestrator.planner.get_llm", return_value=mock_llm
        ):
            run_orchestrator_agent("TEST-001")

        story = db.get_story("TEST-001")
        ctx = json.loads(story["context_json"])
        assert "_agent_actions" in ctx
        assert ctx["_plan_confirmed"] is False
        assert story["status"] == "planning"

    def test_skip_stage_action(self, isolated_db, monkeypatch, tmp_path):
        _make_story(isolated_db, monkeypatch, tmp_path)
        mock_llm = MagicMock()
        call_count = [0]

        def mock_invoke(messages, tools, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                tc = {
                    "id": "c1",
                    "type": "function",
                    "function": {
                        "name": "skip_stage",
                        "arguments": {"reason": "not needed", "stage": "test"},
                    },
                }
                return {
                    "message": {"role": "assistant", "content": "", "tool_calls": [tc]},
                    "tool_calls": [tc],
                    "content": "",
                }
            return {
                "message": {"role": "assistant", "content": "done"},
                "tool_calls": [],
                "content": "done",
            }

        mock_llm.invoke_with_tools = mock_invoke

        with patch(
            "story_lifecycle.orchestrator.planner.get_llm", return_value=mock_llm
        ):
            result = run_orchestrator_agent("TEST-001")

        assert result["actions"][0]["action"] == "skip"
        assert result["actions"][0]["reason"] == "not needed"

    def test_story_not_found_raises(self, isolated_db):
        with pytest.raises(ValueError, match="Story not found"):
            run_orchestrator_agent("NONEXISTENT-001")


class TestContinueOrchestratorAgent:
    def test_skip_action_records_event(self, isolated_db, monkeypatch, tmp_path):
        from story_lifecycle.db import models as db

        _make_story(isolated_db, monkeypatch, tmp_path)
        db.update_story(
            "TEST-001",
            context_json=json.dumps(
                {
                    "_agent_actions": [
                        {"action": "skip", "stage": "test", "reason": "not needed"}
                    ],
                    "_plan_confirmed": True,
                }
            ),
        )

        with patch("story_lifecycle.terminal.pty.ensure_agent_pty"):
            continue_orchestrator_agent("TEST-001")

        story = db.get_story("TEST-001")
        assert story["status"] == "completed"

    def test_no_actions_marks_failed(self, isolated_db, monkeypatch, tmp_path):
        from story_lifecycle.db import models as db

        _make_story(isolated_db, monkeypatch, tmp_path)
        db.update_story(
            "TEST-001",
            context_json=json.dumps({"_agent_actions": [], "_plan_confirmed": True}),
        )

        continue_orchestrator_agent("TEST-001")

        story = db.get_story("TEST-001")
        assert story["status"] == "failed"

    def test_story_not_found_raises(self, isolated_db):
        with pytest.raises(ValueError, match="Story not found"):
            continue_orchestrator_agent("NONEXISTENT-001")
