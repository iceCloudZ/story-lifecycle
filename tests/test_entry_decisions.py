"""Tests for TUI entry decision logic — .done helpers, state resolver, action decider."""

import tempfile
import time

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


# ---------------------------------------------------------------------------
# Regression: BaseTool._launch_in_session must not call create_session
# ---------------------------------------------------------------------------


class TestBaseToolLaunchConstraint:
    """Lock down the constraint that _launch_in_session never calls
    ttyd.create_session and always falls back to launch_cli when no
    healthy session exists."""

    def test_no_create_session_no_healthy_session(self, monkeypatch, tmp_path):
        """When no session exists, must call launch_cli — never create_session."""
        import story_lifecycle.terminal.ttyd as ttyd_mod
        import story_lifecycle.orchestrator.graph as graph_mod
        import story_lifecycle.adapters as adapters_mod
        from story_lifecycle.orchestrator.tools.base import BaseTool
        from story_lifecycle.db import models as db_mod

        create_calls = []
        launch_cli_calls = []

        monkeypatch.setattr(ttyd_mod, "session_alive", lambda n: False)
        monkeypatch.setattr(
            ttyd_mod, "create_session", lambda *a, **kw: create_calls.append(a)
        )
        monkeypatch.setattr(
            ttyd_mod, "launch_cli", lambda *a, **kw: launch_cli_calls.append(a)
        )
        monkeypatch.setattr(ttyd_mod, "session_name", lambda k: f"s-{k}")
        monkeypatch.setattr(ttyd_mod, "send_keys", lambda *a, **kw: None)
        monkeypatch.setattr(ttyd_mod, "paste_text", lambda *a, **kw: None)
        monkeypatch.setattr(ttyd_mod, "_mplex_launched", set())
        # No Zellij available → must fall back to launch_cli
        monkeypatch.setattr(ttyd_mod, "_MPLEX", None)
        monkeypatch.setattr(ttyd_mod, "zellij_execution_args", lambda *a, **kw: None)

        class FakeAdapter:
            def launch_cmd(self, model):
                return "echo fake"

            def switch_provider(self, p):
                pass

        monkeypatch.setattr(adapters_mod, "get_adapter", lambda n: FakeAdapter())

        monkeypatch.setattr(graph_mod, "emit_terminal_opened", lambda k: None)
        monkeypatch.setattr(graph_mod, "emit_terminal_request", lambda k, a: None)
        monkeypatch.setattr(db_mod, "log_event", lambda *a, **kw: None)
        monkeypatch.setattr(db_mod, "update_story", lambda *a, **kw: None)

        tool = BaseTool()
        state = {
            "story_key": "T-1",
            "workspace": str(tmp_path),
            "current_stage": "design",
            "execution_count": 0,
        }
        tool._launch_in_session(
            state, {"adapter": "claude", "model": "sonnet"}, "prompt"
        )

        assert create_calls == [], "create_session must NOT be called"
        assert len(launch_cli_calls) == 1, "launch_cli must be called exactly once"

    def test_zellij_foreground_request_when_available(self, monkeypatch, tmp_path):
        """When Zellij available but no session, must emit terminal request."""
        import story_lifecycle.terminal.ttyd as ttyd_mod
        import story_lifecycle.orchestrator.graph as graph_mod
        import story_lifecycle.adapters as adapters_mod
        from story_lifecycle.orchestrator.tools.base import BaseTool
        from story_lifecycle.db import models as db_mod

        create_calls = []
        launch_cli_calls = []
        request_calls = []

        monkeypatch.setattr(ttyd_mod, "session_alive", lambda n: False)
        monkeypatch.setattr(
            ttyd_mod, "create_session", lambda *a, **kw: create_calls.append(a)
        )
        monkeypatch.setattr(
            ttyd_mod, "launch_cli", lambda *a, **kw: launch_cli_calls.append(a)
        )
        monkeypatch.setattr(ttyd_mod, "session_name", lambda k: f"s-{k}")
        monkeypatch.setattr(ttyd_mod, "send_keys", lambda *a, **kw: None)
        monkeypatch.setattr(ttyd_mod, "paste_text", lambda *a, **kw: None)
        monkeypatch.setattr(ttyd_mod, "_mplex_launched", set())
        monkeypatch.setattr(ttyd_mod, "_MPLEX", "zellij")
        monkeypatch.setattr(
            ttyd_mod,
            "zellij_execution_args",
            lambda *a, **kw: [
                "zellij",
                "--session",
                "s-T-3",
                "--new-session-with-layout",
                "/tmp/x.kdl",
            ],
        )

        class FakeAdapter:
            def launch_cmd(self, model):
                return "echo fake"

            def switch_provider(self, p):
                pass

        monkeypatch.setattr(adapters_mod, "get_adapter", lambda n: FakeAdapter())

        monkeypatch.setattr(graph_mod, "emit_terminal_opened", lambda k: None)
        monkeypatch.setattr(
            graph_mod,
            "emit_terminal_request",
            lambda k, a: request_calls.append((k, a)),
        )
        monkeypatch.setattr(db_mod, "log_event", lambda *a, **kw: None)
        monkeypatch.setattr(db_mod, "update_story", lambda *a, **kw: None)

        tool = BaseTool()
        state = {
            "story_key": "T-3",
            "workspace": str(tmp_path),
            "current_stage": "design",
            "execution_count": 0,
        }
        tool._launch_in_session(
            state, {"adapter": "claude", "model": "sonnet"}, "prompt"
        )

        assert create_calls == [], "create_session must NOT be called"
        assert launch_cli_calls == [], "launch_cli must NOT be called with Zellij"
        assert len(request_calls) == 1, "emit_terminal_request must be called"
        assert request_calls[0][0] == "T-3"

    def test_injects_into_healthy_session(self, monkeypatch, tmp_path):
        """When a healthy session exists, must inject via send_keys — no launch_cli."""
        import story_lifecycle.terminal.ttyd as ttyd_mod
        import story_lifecycle.orchestrator.graph as graph_mod
        import story_lifecycle.adapters as adapters_mod
        from story_lifecycle.orchestrator.tools.base import BaseTool
        from story_lifecycle.db import models as db_mod

        send_keys_calls = []
        launch_cli_calls = []

        monkeypatch.setattr(ttyd_mod, "session_alive", lambda n: True)
        monkeypatch.setattr(ttyd_mod, "create_session", lambda *a, **kw: None)
        monkeypatch.setattr(
            ttyd_mod, "send_keys", lambda *a, **kw: send_keys_calls.append(a)
        )
        monkeypatch.setattr(ttyd_mod, "paste_text", lambda *a, **kw: None)
        monkeypatch.setattr(ttyd_mod, "session_name", lambda k: f"s-{k}")
        monkeypatch.setattr(
            ttyd_mod, "launch_cli", lambda *a, **kw: launch_cli_calls.append(a)
        )
        monkeypatch.setattr(ttyd_mod, "_mplex_launched", set())

        monkeypatch.setattr(time, "sleep", lambda s: None)

        class FakeAdapter:
            def launch_cmd(self, model):
                return "echo fake"

            def switch_provider(self, p):
                pass

        monkeypatch.setattr(adapters_mod, "get_adapter", lambda n: FakeAdapter())

        monkeypatch.setattr(graph_mod, "emit_terminal_opened", lambda k: None)
        monkeypatch.setattr(graph_mod, "emit_terminal_request", lambda k, a: None)
        monkeypatch.setattr(db_mod, "log_event", lambda *a, **kw: None)
        monkeypatch.setattr(db_mod, "update_story", lambda *a, **kw: None)

        tool = BaseTool()
        state = {
            "story_key": "T-2",
            "workspace": str(tmp_path),
            "current_stage": "design",
            "execution_count": 0,
        }
        tool._launch_in_session(
            state, {"adapter": "claude", "model": "sonnet"}, "prompt"
        )

        assert launch_cli_calls == [], (
            "launch_cli must NOT be called when session is healthy"
        )
        assert len(send_keys_calls) >= 2, "send_keys must be called to inject prompt"


# ---------------------------------------------------------------------------
# zellij_execution_args direct tests
# ---------------------------------------------------------------------------


class TestZellijExecutionArgs:
    def test_returns_none_when_no_zellij(self, monkeypatch, tmp_path):
        import story_lifecycle.terminal.ttyd as ttyd_mod

        monkeypatch.setattr(ttyd_mod, "_MPLEX", None)

        result = ttyd_mod.zellij_execution_args("KEY", str(tmp_path), "claude", "/p.md")
        assert result is None

    def test_returns_none_when_no_git_bash_on_windows(self, monkeypatch, tmp_path):
        import os

        if os.name != "nt":
            pytest.skip("Windows-only test")

        import story_lifecycle.terminal.ttyd as ttyd_mod
        import story_lifecycle.terminal.platform_ops as po_mod

        monkeypatch.setattr(ttyd_mod, "_MPLEX", "zellij")
        monkeypatch.setattr(po_mod, "_find_git_bash", lambda: None)

        result = ttyd_mod.zellij_execution_args("KEY", str(tmp_path), "claude", "/p.md")
        assert result is None

    def test_generates_layout_and_argv(self, monkeypatch, tmp_path):
        import os
        import pathlib

        import story_lifecycle.terminal.ttyd as ttyd_mod

        monkeypatch.setattr(ttyd_mod, "_MPLEX", "zellij")

        # On Windows, ensure _find_git_bash returns a path
        if os.name == "nt":
            import story_lifecycle.terminal.platform_ops as po_mod

            monkeypatch.setattr(
                po_mod, "_find_git_bash", lambda: "C:/Program Files/Git/bin/bash.exe"
            )

        result = ttyd_mod.zellij_execution_args(
            "1065520", str(tmp_path), "claude --model sonnet", "/tmp/prompt.md"
        )
        assert result is not None
        assert result[0] == "zellij"
        assert "--session" in result
        assert "s-1065520" in result
        assert "--new-session-with-layout" in result

        # Verify KDL layout file
        kdl_files = list(
            pathlib.Path(tempfile.gettempdir()).glob("story-zellij-1065520.kdl")
        )
        assert len(kdl_files) == 1
        kdl_content = kdl_files[0].read_text(encoding="utf-8")
        assert "story-launch-1065520.sh" in kdl_content
        if os.name == "nt":
            assert "bash.exe" in kdl_content
        else:
            assert 'pane command="bash"' in kdl_content

        # Verify launch script
        script_files = list(
            pathlib.Path(tempfile.gettempdir()).glob("story-launch-1065520.sh")
        )
        assert len(script_files) == 1
        script_content = script_files[0].read_text(encoding="utf-8")
        assert "claude --model sonnet" in script_content
        assert "prompt.md" in script_content

    def test_windows_uses_git_bash_path(self, monkeypatch, tmp_path):
        import os

        if os.name != "nt":
            pytest.skip("Windows-only test")

        import pathlib

        import story_lifecycle.terminal.ttyd as ttyd_mod
        import story_lifecycle.terminal.platform_ops as po_mod

        monkeypatch.setattr(ttyd_mod, "_MPLEX", "zellij")
        monkeypatch.setattr(
            po_mod,
            "_find_git_bash",
            lambda: "C:/Program Files/Git/bin/bash.exe",
        )

        result = ttyd_mod.zellij_execution_args("KEY", str(tmp_path), "claude", "/p.md")
        assert result is not None

        kdl_files = list(
            pathlib.Path(tempfile.gettempdir()).glob("story-zellij-KEY.kdl")
        )
        assert len(kdl_files) == 1
        kdl_content = kdl_files[0].read_text(encoding="utf-8")
        # Must use resolved Git Bash path, not bare "bash"
        assert "bash.exe" in kdl_content
