"""Tests for P0 fixes: .story-done/ path structure and wait_confirm loop."""

import json
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

from story_lifecycle.orchestrator.nodes import (
    StoryState,
    poll_completion_node,
    wait_confirm_node,
    _render_prompt,
    robust_json_parse,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state(**overrides) -> StoryState:
    base: StoryState = {
        "story_key": "STORY-123",
        "title": "Test story",
        "workspace": "/tmp/test-ws",
        "profile": "minimal",
        "current_stage": "design",
        "status": "active",
        "complexity": "M",
        "context": {},
        "execution_count": 0,
        "last_error": None,
        "stage_start_time": time.time(),
    }
    base.update(overrides)
    return base


# ===========================================================================
# P0-1: .story-done/ path includes story_key subdirectory
# ===========================================================================


class TestDoneFilePath:
    """poll_completion_node must read from .story-done/{story_key}/{stage}.json."""

    @patch("story_lifecycle.orchestrator.nodes.ttyd")
    @patch("story_lifecycle.orchestrator.nodes.db")
    def test_reads_from_story_key_subdirectory(self, mock_db, mock_ttyd, tmp_path):
        """Done file at .story-done/STORY-123/design.json is found and parsed."""
        key = "STORY-123"
        stage = "design"
        done_dir = tmp_path / ".story-done" / key
        done_dir.mkdir(parents=True)
        done_file = done_dir / f"{stage}.json"
        done_file.write_text(
            '{"spec_path": "docs/spec.md", "complexity": "S", "summary": "ok"}\n',
            encoding="utf-8",
        )

        mock_ttyd._tmux_session_alive.return_value = True
        mock_ttyd.session_name.return_value = f"s-{key}"

        state = _make_state(story_key=key, current_stage=stage, workspace=str(tmp_path))
        result = poll_completion_node(state)

        assert result["last_error"] is None
        assert result["context"]["spec_path"] == "docs/spec.md"
        assert result["context"]["complexity"] == "S"
        # File should be cleaned up after read
        assert not done_file.exists()

    @patch("story_lifecycle.orchestrator.nodes.ttyd")
    @patch("story_lifecycle.orchestrator.nodes.db")
    def test_different_stories_do_not_collide(self, mock_db, mock_ttyd, tmp_path):
        """Two stories in the same workspace have separate .done directories."""
        # STORY-100 has a done file
        done_dir_100 = tmp_path / ".story-done" / "STORY-100"
        done_dir_100.mkdir(parents=True)
        (done_dir_100 / "design.json").write_text('{"summary": "story-100"}\n')

        # STORY-200 has no done file yet
        done_dir_200 = tmp_path / ".story-done" / "STORY-200"
        done_dir_200.mkdir(parents=True)

        mock_ttyd._tmux_session_alive.return_value = True
        mock_ttyd.session_name.return_value = "s-STORY-200"

        # STORY-200 should NOT see STORY-100's file
        state = _make_state(story_key="STORY-200", current_stage="design",
                            workspace=str(tmp_path))
        # Will timeout since no done file exists for STORY-200
        # Set stage_start_time to trigger timeout immediately
        state["stage_start_time"] = 0
        result = poll_completion_node(state)

        assert result["last_error"] is not None
        assert "timeout" in result["last_error"].lower()

        # STORY-100's file should still exist (untouched)
        assert (done_dir_100 / "design.json").exists()

    @patch("story_lifecycle.orchestrator.nodes.ttyd")
    @patch("story_lifecycle.orchestrator.nodes.db")
    def test_old_flat_path_not_found(self, mock_db, mock_ttyd, tmp_path):
        """A .done file at the old flat path (.story-done/design.json) is NOT picked up."""
        # Create file at old flat location (no story_key subdirectory)
        old_dir = tmp_path / ".story-done"
        old_dir.mkdir(parents=True)
        (old_dir / "design.json").write_text('{"summary": "stale"}\n')

        mock_ttyd._tmux_session_alive.return_value = False  # session dead → triggers error

        state = _make_state(story_key="STORY-123", current_stage="design",
                            workspace=str(tmp_path))
        # With session dead, poll exits immediately with crash error
        result = poll_completion_node(state)

        # Should NOT have parsed the old-format file
        assert "context" not in result or result["context"].get("summary") != "stale"
        assert result["last_error"] is not None


# ===========================================================================
# P0-2: wait_confirm loops back to execute_stage
# ===========================================================================


class TestWaitConfirmLoop:
    """wait_confirm_node must poll until status is set back to active, then return."""

    @patch("story_lifecycle.orchestrator.nodes.time")
    @patch("story_lifecycle.orchestrator.nodes.db")
    def test_blocks_until_resumed(self, mock_db, mock_time):
        """Node blocks until db.get_story returns status='active'."""
        # First call: still paused. Second call: resumed.
        paused_story = {"status": "paused"}
        active_story = {"status": "active"}
        mock_db.get_story.side_effect = [paused_story, paused_story, active_story]
        mock_time.sleep = MagicMock()

        state = _make_state(status="active")
        result = wait_confirm_node(state)

        assert result["status"] == "active"
        assert mock_db.get_story.call_count == 3
        mock_time.sleep.assert_called()  # did poll, not busy-wait

    @patch("story_lifecycle.orchestrator.nodes.time")
    @patch("story_lifecycle.orchestrator.nodes.db")
    def test_resets_execution_count(self, mock_db, mock_time):
        """execution_count is reset to 0 when resuming, so retry counter starts fresh."""
        mock_db.get_story.return_value = {"status": "active"}
        mock_time.sleep = MagicMock()

        state = _make_state(status="active", execution_count=3)
        result = wait_confirm_node(state)

        assert result["execution_count"] == 0

    @patch("story_lifecycle.orchestrator.nodes.time")
    @patch("story_lifecycle.orchestrator.nodes.db")
    def test_sets_paused_in_db(self, mock_db, mock_time):
        """Node immediately sets status to paused in DB."""
        mock_db.get_story.return_value = {"status": "active"}
        mock_time.sleep = MagicMock()

        state = _make_state(story_key="STORY-456")
        wait_confirm_node(state)

        mock_db.update_story.assert_any_call("STORY-456", status="paused")
        mock_db.log_stage.assert_called_once_with(
            "STORY-456", "design", "pause", "Waiting for manual confirmation"
        )


# ===========================================================================
# Prompt templates contain {story_key} in .story-done path
# ===========================================================================


class TestPromptPaths:
    """Rendered prompts must instruct CC to write to .story-done/{story_key}/{stage}.json."""

    def test_design_prompt_has_story_key_path(self):
        state = _make_state(current_stage="design", story_key="STORY-999")
        prompt = _render_prompt("design", state)
        assert ".story-done/STORY-999/design.json" in prompt

    def test_implement_prompt_has_story_key_path(self):
        state = _make_state(current_stage="implement", story_key="STORY-999")
        prompt = _render_prompt("implement", state)
        assert ".story-done/STORY-999/implement.json" in prompt

    def test_test_prompt_has_story_key_path(self):
        state = _make_state(current_stage="test", story_key="STORY-999")
        prompt = _render_prompt("test", state)
        assert ".story-done/STORY-999/test.json" in prompt

    def test_default_prompt_has_story_key_path(self):
        """Fallback prompt (no template file) also includes story_key subdirectory."""
        state = _make_state(current_stage="unknown_stage", story_key="STORY-ABC")
        with patch("story_lifecycle.orchestrator.nodes.Path.exists", return_value=False):
            prompt = _render_prompt("unknown_stage", state)
        assert ".story-done/STORY-ABC/unknown_stage.json" in prompt


# ===========================================================================
# robust_json_parse (existing feature, verify not broken)
# ===========================================================================


class TestRobustJsonParse:
    def test_raw_json(self, tmp_path):
        f = tmp_path / "ok.json"
        f.write_text('{"key": "value"}', encoding="utf-8")
        assert robust_json_parse(f) == {"key": "value"}

    def test_json_in_markdown_block(self, tmp_path):
        f = tmp_path / "wrapped.json"
        f.write_text(
            '好的，这是结果：\n```json\n{"complexity": "M"}\n```\n希望有帮助！',
            encoding="utf-8",
        )
        assert robust_json_parse(f) == {"complexity": "M"}

    def test_json_with_surrounding_text(self, tmp_path):
        f = tmp_path / "messy.json"
        f.write_text(
            'Here is the output:\n{"spec_path": "docs/spec.md", "complexity": "L"}\nDone.',
            encoding="utf-8",
        )
        result = robust_json_parse(f)
        assert result["spec_path"] == "docs/spec.md"

    def test_invalid_json_raises(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("this is not json at all", encoding="utf-8")
        with pytest.raises(ValueError, match="Cannot parse JSON"):
            robust_json_parse(f)
