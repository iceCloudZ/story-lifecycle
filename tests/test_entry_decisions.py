"""Tests for TUI entry decision logic — .done helpers, state resolver, action decider."""

import pytest

from story_lifecycle.orchestrator.entry import (
    stage_done_file,
    has_stage_done,
    validate_stage_done,
    DoneStatus,
    TtydSessionBackend,
    StageEntryState,
    StageEntryAction,
    resolve_stage_state,
    decide_action,
)


def _make_story(
    workspace: str,
    story_key: str = "TEST-001",
    stage: str = "design",
    status: str = "active",
):
    return {
        "story_key": story_key,
        "current_stage": stage,
        "workspace": workspace,
        "status": status,
    }


# ---------------------------------------------------------------------------
# Layer 1 tests: .done helpers
# ---------------------------------------------------------------------------


class TestStageDoneFile:
    def test_returns_correct_path(self, tmp_path):
        story = _make_story(str(tmp_path), "FEAT-42", "implement")
        result = stage_done_file(story)
        assert result == tmp_path / ".story-done" / "FEAT-42" / "implement.json"


class TestHasStageDone:
    def test_exists(self, tmp_path):
        story = _make_story(str(tmp_path))
        done = stage_done_file(story)
        done.parent.mkdir(parents=True, exist_ok=True)
        done.write_text("{}", encoding="utf-8")
        assert has_stage_done(story) is True

    def test_missing(self, tmp_path):
        story = _make_story(str(tmp_path))
        assert has_stage_done(story) is False


class TestValidateStageDone:
    def test_ok(self, tmp_path):
        story = _make_story(str(tmp_path))
        done = stage_done_file(story)
        done.parent.mkdir(parents=True, exist_ok=True)
        done.write_text('{"summary": "done"}', encoding="utf-8")
        result = validate_stage_done(story)
        assert result.status == DoneStatus.OK
        assert result.data == {"summary": "done"}
        assert result.error is None

    def test_corrupted(self, tmp_path):
        story = _make_story(str(tmp_path))
        done = stage_done_file(story)
        done.parent.mkdir(parents=True, exist_ok=True)
        done.write_text("NOT JSON AT ALL {{{", encoding="utf-8")
        result = validate_stage_done(story)
        assert result.status == DoneStatus.CORRUPTED
        assert result.data is None
        assert result.error is not None

    def test_missing(self, tmp_path):
        story = _make_story(str(tmp_path))
        result = validate_stage_done(story)
        assert result.status == DoneStatus.MISSING
        assert result.data is None

    def test_markdown_wrapped_json(self, tmp_path):
        story = _make_story(str(tmp_path))
        done = stage_done_file(story)
        done.parent.mkdir(parents=True, exist_ok=True)
        done.write_text('```json\n{"summary": "wrapped"}\n```', encoding="utf-8")
        result = validate_stage_done(story)
        assert result.status == DoneStatus.OK
        assert result.data == {"summary": "wrapped"}

    def test_empty_file(self, tmp_path):
        story = _make_story(str(tmp_path))
        done = stage_done_file(story)
        done.parent.mkdir(parents=True, exist_ok=True)
        done.write_text("", encoding="utf-8")
        result = validate_stage_done(story)
        assert result.status == DoneStatus.CORRUPTED

    def test_empty_dict_rejected(self, tmp_path):
        story = _make_story(str(tmp_path))
        done = stage_done_file(story)
        done.parent.mkdir(parents=True, exist_ok=True)
        done.write_text("{}", encoding="utf-8")
        result = validate_stage_done(story)
        assert result.status == DoneStatus.CORRUPTED
        assert "no data" in result.error.lower()

    def test_non_dict_rejected(self, tmp_path):
        story = _make_story(str(tmp_path))
        done = stage_done_file(story)
        done.parent.mkdir(parents=True, exist_ok=True)
        done.write_text('"just a string"', encoding="utf-8")
        result = validate_stage_done(story)
        assert result.status == DoneStatus.CORRUPTED


# ---------------------------------------------------------------------------
# Layer 2 tests: TtydSessionBackend
# ---------------------------------------------------------------------------


class TestTtydSessionBackend:
    def test_is_healthy_delegates_to_ttyd(self, monkeypatch):
        called_with = {}

        def fake_session_alive(name):
            called_with["name"] = name
            return True

        import story_lifecycle.terminal.ttyd as ttyd_mod

        monkeypatch.setattr(ttyd_mod, "session_alive", fake_session_alive)

        backend = TtydSessionBackend()
        assert backend.is_healthy("s-TEST-001") is True
        assert called_with["name"] == "s-TEST-001"

    def test_is_healthy_false(self, monkeypatch):
        import story_lifecycle.terminal.ttyd as ttyd_mod

        monkeypatch.setattr(ttyd_mod, "session_alive", lambda n: False)

        backend = TtydSessionBackend()
        assert backend.is_healthy("s-TEST-001") is False

    def test_launch_independent_terminal_delegates(self, monkeypatch):
        calls = []

        def fake_launch_cli(story_key, workspace, launch_cmd, prompt_file):
            calls.append((story_key, workspace, launch_cmd, prompt_file))

        import story_lifecycle.terminal.ttyd as ttyd_mod

        monkeypatch.setattr(ttyd_mod, "launch_cli", fake_launch_cli)

        backend = TtydSessionBackend()
        backend.launch_independent_terminal("KEY", "/ws", "claude", "/p.md")
        assert calls == [("KEY", "/ws", "claude", "/p.md")]


# ---------------------------------------------------------------------------
# Layer 3 tests: resolve_stage_state + decide_action
# ---------------------------------------------------------------------------


class FakeBackend:
    """Mock SessionBackend for testing."""

    def __init__(self, healthy: bool = False):
        self._healthy = healthy

    def is_healthy(self, session_id: str) -> bool:
        return self._healthy

    def attach_foreground(self, session_id: str) -> list[str]:
        return ["echo", "attach", session_id]

    def launch_independent_terminal(
        self, story_key, workspace, launch_cmd, prompt_file
    ):
        pass


class TestResolveStageState:
    def test_story_finished_completed(self, tmp_path):
        story = _make_story(str(tmp_path), status="completed")
        assert (
            resolve_stage_state(story, FakeBackend(), is_running=False)
            == StageEntryState.STORY_FINISHED
        )

    def test_story_finished_failed(self, tmp_path):
        story = _make_story(str(tmp_path), status="failed")
        assert (
            resolve_stage_state(story, FakeBackend(), is_running=False)
            == StageEntryState.STORY_FINISHED
        )

    def test_story_finished_aborted(self, tmp_path):
        story = _make_story(str(tmp_path), status="aborted")
        assert (
            resolve_stage_state(story, FakeBackend(), is_running=False)
            == StageEntryState.STORY_FINISHED
        )

    def test_done_valid(self, tmp_path):
        story = _make_story(str(tmp_path), status="active")
        done = stage_done_file(story)
        done.parent.mkdir(parents=True, exist_ok=True)
        done.write_text('{"summary": "ok"}', encoding="utf-8")
        assert (
            resolve_stage_state(story, FakeBackend(), is_running=True)
            == StageEntryState.DONE
        )

    def test_done_corrupted(self, tmp_path):
        story = _make_story(str(tmp_path), status="active")
        done = stage_done_file(story)
        done.parent.mkdir(parents=True, exist_ok=True)
        done.write_text("BROKEN{{{", encoding="utf-8")
        assert (
            resolve_stage_state(story, FakeBackend(), is_running=False)
            == StageEntryState.DONE_CORRUPTED
        )

    def test_running_healthy(self, tmp_path):
        story = _make_story(str(tmp_path), status="active")
        assert (
            resolve_stage_state(story, FakeBackend(healthy=True), is_running=True)
            == StageEntryState.RUNNING_HEALTHY
        )

    def test_running_dead(self, tmp_path):
        story = _make_story(str(tmp_path), status="active")
        assert (
            resolve_stage_state(story, FakeBackend(healthy=False), is_running=True)
            == StageEntryState.RUNNING_DEAD
        )

    def test_idle(self, tmp_path):
        story = _make_story(str(tmp_path), status="active")
        assert (
            resolve_stage_state(story, FakeBackend(healthy=False), is_running=False)
            == StageEntryState.IDLE
        )

    def test_done_takes_priority_over_running_dead(self, tmp_path):
        """Even if session is dead, valid .done means DONE state."""
        story = _make_story(str(tmp_path), status="active")
        done = stage_done_file(story)
        done.parent.mkdir(parents=True, exist_ok=True)
        done.write_text('{"summary": "ok"}', encoding="utf-8")
        assert (
            resolve_stage_state(story, FakeBackend(healthy=False), is_running=True)
            == StageEntryState.DONE
        )


# Decision table: (state, user_action) -> expected action
DECISION_TABLE = [
    (StageEntryState.DONE, "e", StageEntryAction.PROMPT_DONE_PRESS_R),
    (StageEntryState.DONE, "r", StageEntryAction.START_OR_RESUME),
    (StageEntryState.DONE_CORRUPTED, "e", StageEntryAction.PROMPT_FIX_DONE),
    (StageEntryState.DONE_CORRUPTED, "r", StageEntryAction.PROMPT_FIX_DONE),
    (StageEntryState.RUNNING_HEALTHY, "e", StageEntryAction.ATTACH),
    (StageEntryState.RUNNING_HEALTHY, "r", StageEntryAction.NOOP),
    (StageEntryState.RUNNING_DEAD, "e", StageEntryAction.PROMPT_PRESS_R),
    (StageEntryState.RUNNING_DEAD, "r", StageEntryAction.START_OR_RESUME),
    (StageEntryState.IDLE, "e", StageEntryAction.PROMPT_PRESS_R),
    (StageEntryState.IDLE, "r", StageEntryAction.START_OR_RESUME),
    (StageEntryState.STORY_FINISHED, "e", StageEntryAction.NOOP),
    (StageEntryState.STORY_FINISHED, "r", StageEntryAction.NOOP),
]


class TestDecideAction:
    @pytest.mark.parametrize("state,user_action,expected", DECISION_TABLE)
    def test_decision_table(self, state, user_action, expected):
        assert decide_action(state, user_action) == expected

    def test_invalid_user_action_raises(self):
        with pytest.raises(ValueError):
            decide_action(StageEntryState.IDLE, "x")
