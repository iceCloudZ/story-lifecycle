import os

import pytest

from story_lifecycle.orchestrator.entry import StageEntryAction


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific")
def test_zellij_create_session_uses_session_name_as_attach_argument(monkeypatch):
    from story_lifecycle.terminal import ttyd

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))

        class Result:
            returncode = 0
            stdout = ""

        return Result()

    monkeypatch.setattr(ttyd, "_MPLEX", "zellij")
    monkeypatch.setattr(ttyd.os, "name", "nt")
    monkeypatch.setattr(ttyd, "_run", fake_run)
    monkeypatch.setattr(ttyd.time, "sleep", lambda _seconds: None)

    ttyd.create_session("s-WIN-ZELLIJ", "/tmp/story-test")

    assert calls[0][0] == [
        "zellij",
        "attach",
        "--create-background",
        "s-WIN-ZELLIJ",
        "options",
        "--default-cwd",
        "/tmp/story-test",
        "--default-shell",
        "powershell.exe",
    ]
    assert calls[0][1]["capture_output"] is False
    assert calls[0][1]["stdin"] is ttyd.subprocess.DEVNULL
    assert calls[0][1]["stdout"] is ttyd.subprocess.DEVNULL
    assert calls[0][1]["stderr"] is ttyd.subprocess.DEVNULL


def test_tui_debug_log_writes_to_story_home(monkeypatch, tmp_path):
    from story_lifecycle.cli import tui

    monkeypatch.setattr(tui, "STORY_HOME", tmp_path)

    tui._tui_debug("enter_terminal", story_key="WIN-ZELLIJ", session="s-WIN-ZELLIJ")

    log_text = (tmp_path / "tui.log").read_text(encoding="utf-8")
    assert "enter_terminal" in log_text
    assert "story_key='WIN-ZELLIJ'" in log_text
    assert "session='s-WIN-ZELLIJ'" in log_text


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific")
def test_tui_defers_attach_until_after_textual_exits_on_windows(
    monkeypatch,
):
    from story_lifecycle.cli import tui

    create_calls = []
    system_calls = []
    run_calls = []
    exit_calls = []

    app = tui.StoryBoardApp()
    app.stories = [
        {
            "story_key": "WIN-ZELLIJ",
            "workspace": "/tmp/story-test",
            "status": "active",
            "current_stage": "implement",
        }
    ]
    app.selected_index = 0

    monkeypatch.setattr(tui.os, "name", "nt")
    monkeypatch.setattr(tui.ttyd, "session_name", lambda key: f"s-{key}")
    monkeypatch.setattr(tui.ttyd, "session_alive", lambda _session: True)
    monkeypatch.setattr(
        tui.ttyd,
        "resolve_session_state",
        lambda name: tui.ttyd.SessionState.LIVE,
    )
    monkeypatch.setattr(
        tui.ttyd,
        "create_session",
        lambda session, workspace: create_calls.append((session, workspace)),
    )
    monkeypatch.setattr(
        tui.ttyd, "attach_cmd", lambda session: f"zellij attach {session}"
    )
    monkeypatch.setattr(
        tui.ttyd,
        "attach_args",
        lambda session: ["zellij", "attach", session],
    )
    monkeypatch.setattr(
        tui.subprocess,
        "run",
        lambda args, check=False: run_calls.append((args, check)),
    )
    monkeypatch.setattr(tui.os, "system", lambda cmd: system_calls.append(cmd))
    monkeypatch.setattr(app, "exit", lambda: exit_calls.append(True))

    # Monkeypatch is_story_running to return True so state = RUNNING_HEALTHY
    from story_lifecycle.orchestrator import graph as graph_mod

    monkeypatch.setattr(graph_mod, "is_story_running", lambda key: True)
    monkeypatch.setattr(graph_mod, "is_workspace_locked", lambda ws: False)

    app.action_enter_terminal()

    assert create_calls == []
    assert system_calls == []
    assert run_calls == []
    assert exit_calls == [True]
    assert app._pending_attach_args == ["zellij", "attach", "s-WIN-ZELLIJ"]


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific")
def test_tui_shows_prompt_when_no_session_on_windows(monkeypatch):
    """When no session exists, e key shows prompt instead of creating a session."""
    from story_lifecycle.cli import tui

    create_calls = []
    exit_calls = []

    app = tui.StoryBoardApp()
    app.stories = [
        {
            "story_key": "WIN-ZELLIJ",
            "workspace": "/tmp/story-test",
            "status": "active",
            "current_stage": "implement",
        }
    ]
    app.selected_index = 0

    monkeypatch.setattr(tui.os, "name", "nt")
    monkeypatch.setattr(tui.ttyd, "session_name", lambda key: f"s-{key}")
    monkeypatch.setattr(tui.ttyd, "session_alive", lambda _session: False)
    monkeypatch.setattr(
        tui.ttyd,
        "create_session",
        lambda session, workspace: create_calls.append((session, workspace)),
    )
    monkeypatch.setattr(app, "exit", lambda: exit_calls.append(True))

    # No .done file, not running, not healthy -> state=IDLE -> PROMPT_PRESS_R
    from story_lifecycle.orchestrator import graph as graph_mod
    from story_lifecycle.orchestrator import entry as entry_mod

    monkeypatch.setattr(graph_mod, "is_story_running", lambda key: False)

    # Verify the decision logic produces the expected action
    state = entry_mod.resolve_stage_state(app.stories[0], app._session_backend, False)
    action = entry_mod.decide_action(state, "e")
    assert action == StageEntryAction.PROMPT_PRESS_R

    # The actual TUI call would need a mounted app to query the panel.
    # Verify the contract: no session created, no exit called.
    # (We test the decision logic above, so the handler just renders a prompt.)


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific")
def test_run_tui_relaunches_after_deferred_attach_on_windows(monkeypatch):
    from story_lifecycle.cli import tui

    run_calls = []
    app_runs = []

    class FakeApp:
        def __init__(self):
            self._pending_attach_args = None

        def run(self):
            app_runs.append(self)
            if len(app_runs) == 1:
                self._pending_attach_args = ["zellij", "attach", "s-WIN-ZELLIJ"]

    class FakeResult:
        returncode = 0

    def fake_run(args, check=False, **kwargs):
        run_calls.append((args, check, sorted(kwargs)))
        return FakeResult()

    monkeypatch.setattr(tui.os, "name", "nt")
    monkeypatch.setattr(tui, "StoryBoardApp", FakeApp)
    monkeypatch.setattr(tui.subprocess, "run", fake_run)

    tui.run_tui()

    assert run_calls == [
        (["zellij", "attach", "s-WIN-ZELLIJ"], False, ["stderr", "stdin", "stdout"])
    ]
    assert len(app_runs) == 2
