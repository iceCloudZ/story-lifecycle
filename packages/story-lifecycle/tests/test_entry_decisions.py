"""Tests for entry decision logic — .done helpers, action decider."""

import tempfile
import time

import pytest

from story_lifecycle.orchestrator.entry import (
    stage_done_file,
    has_stage_done,
    validate_stage_done,
    DoneStatus,
    TtydSessionBackend,
    StageEntryAction,
    WorkspaceState,
    cli_exit_marker_path,
    decide_enter_action,
    decide_resume_action,
    entry_action_notice,
)
from story_lifecycle.infra.terminal.ttyd import SessionState, resolve_session_state


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
        assert result == tmp_path / ".story" / "done" / "FEAT-42" / "implement.json"


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

        import story_lifecycle.infra.terminal.ttyd as ttyd_mod

        monkeypatch.setattr(ttyd_mod, "session_alive", fake_session_alive)

        backend = TtydSessionBackend()
        assert backend.is_healthy("s-TEST-001") is True
        assert called_with["name"] == "s-TEST-001"

    def test_is_healthy_false(self, monkeypatch):
        import story_lifecycle.infra.terminal.ttyd as ttyd_mod

        monkeypatch.setattr(ttyd_mod, "session_alive", lambda n: False)

        backend = TtydSessionBackend()
        assert backend.is_healthy("s-TEST-001") is False

    def test_zellij_exited_session_is_not_healthy(self, monkeypatch):
        import subprocess

        import story_lifecycle.infra.terminal.ttyd as ttyd_mod

        monkeypatch.setattr(ttyd_mod, "_MPLEX", "zellij")
        monkeypatch.setattr(
            ttyd_mod,
            "_run",
            lambda *a, **kw: subprocess.CompletedProcess(
                a[0],
                0,
                "\x1b[32;1ms-TEST-001\x1b[m [Created 1m ago] "
                "(\x1b[31;1mEXITED\x1b[m - attach to resurrect)\n",
                "",
            ),
        )

        assert ttyd_mod.session_alive("s-TEST-001") is False

    def test_delete_exited_session_removes_only_dead_zellij_session(self, monkeypatch):
        import subprocess

        import story_lifecycle.infra.terminal.ttyd as ttyd_mod

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd == ["zellij", "list-sessions"]:
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    "s-TEST-001 [Created 1m ago] (EXITED - attach to resurrect)\n",
                    "",
                )
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(ttyd_mod, "_MPLEX", "zellij")
        monkeypatch.setattr(ttyd_mod, "_run", fake_run)

        assert ttyd_mod.delete_exited_session("s-TEST-001") is True
        assert ["zellij", "delete-session", "s-TEST-001"] in calls

    def test_delete_exited_session_does_not_remove_live_zellij_session(
        self, monkeypatch
    ):
        import subprocess

        import story_lifecycle.infra.terminal.ttyd as ttyd_mod

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd == ["zellij", "list-sessions"]:
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    "s-TEST-001 [Created 1m ago]\n",
                    "",
                )
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(ttyd_mod, "_MPLEX", "zellij")
        monkeypatch.setattr(ttyd_mod, "_run", fake_run)

        assert ttyd_mod.delete_exited_session("s-TEST-001") is False
        assert ["zellij", "delete-session", "s-TEST-001"] not in calls

    def test_launch_independent_terminal_delegates(self, monkeypatch):
        calls = []

        def fake_launch_cli(story_key, workspace, launch_cmd, prompt_file):
            calls.append((story_key, workspace, launch_cmd, prompt_file))

        import story_lifecycle.infra.terminal.ttyd as ttyd_mod

        monkeypatch.setattr(ttyd_mod, "launch_cli", fake_launch_cli)

        backend = TtydSessionBackend()
        backend.launch_independent_terminal("KEY", "/ws", "claude", "/p.md")
        assert calls == [("KEY", "/ws", "claude", "/p.md")]


# ---------------------------------------------------------------------------
# Layer 3 tests: decide_enter_action + decide_resume_action
# ---------------------------------------------------------------------------


class FakeBackend:
    """Mock SessionBackend for testing."""

    def __init__(self, session_state: str = SessionState.MISSING):
        self._session_state = session_state

    def is_healthy(self, session_id: str) -> bool:
        return self._session_state == SessionState.LIVE

    def resolve_session_state(self, session_id: str) -> str:
        return self._session_state

    def attach_foreground(self, session_id: str) -> list[str]:
        return ["echo", "attach", session_id]

    def launch_independent_terminal(
        self, story_key, workspace, launch_cmd, prompt_file
    ):
        pass


# --- decide_enter_action tests ---


class TestDecideEnterAction:
    def test_finished_returns_show_status(self, tmp_path):
        story = _make_story(str(tmp_path), status="completed")
        assert (
            decide_enter_action(story, FakeBackend(), is_running=False)
            == StageEntryAction.SHOW_STATUS
        )

    def test_workspace_blocked(self, tmp_path):
        story = _make_story(str(tmp_path), status="active")
        assert (
            decide_enter_action(
                story,
                FakeBackend(),
                is_running=False,
                workspace_state=WorkspaceState.LOCKED_BY_OTHER,
            )
            == StageEntryAction.SHOW_WORKSPACE_BUSY
        )

    def test_done_corrupted(self, tmp_path):
        story = _make_story(str(tmp_path), status="active")
        done = stage_done_file(story)
        done.parent.mkdir(parents=True, exist_ok=True)
        done.write_text("BROKEN{{{", encoding="utf-8")
        assert (
            decide_enter_action(story, FakeBackend(), is_running=False)
            == StageEntryAction.PROMPT_FIX_DONE
        )

    def test_gate_wait(self, tmp_path):
        import json

        story = _make_story(str(tmp_path), status="paused")
        story["context_json"] = json.dumps({"last_gate_decision_id": "abc"})
        assert (
            decide_enter_action(story, FakeBackend(), is_running=False)
            == StageEntryAction.SHOW_GATE_STATUS
        )

    def test_live_session_returns_attach(self, tmp_path):
        story = _make_story(str(tmp_path), status="active")
        assert (
            decide_enter_action(story, FakeBackend(SessionState.LIVE), is_running=True)
            == StageEntryAction.ATTACH
        )

    def test_idle_live_session_returns_attach(self, tmp_path):
        story = _make_story(str(tmp_path), status="active")
        assert (
            decide_enter_action(story, FakeBackend(SessionState.LIVE), is_running=False)
            == StageEntryAction.ATTACH
        )

    def test_running_missing_session_returns_starting(self, tmp_path):
        story = _make_story(str(tmp_path), status="active")
        assert (
            decide_enter_action(
                story, FakeBackend(SessionState.MISSING), is_running=True
            )
            == StageEntryAction.SHOW_STARTING
        )

    def test_unknown_session_returns_unknown(self, tmp_path):
        story = _make_story(str(tmp_path), status="active")
        assert (
            decide_enter_action(
                story, FakeBackend(SessionState.UNKNOWN), is_running=True
            )
            == StageEntryAction.SHOW_SESSION_UNKNOWN
        )

    def test_idle_no_session_returns_prompt(self, tmp_path):
        story = _make_story(str(tmp_path), status="active")
        assert (
            decide_enter_action(story, FakeBackend(), is_running=False)
            == StageEntryAction.PROMPT_PRESS_R
        )


# --- decide_resume_action tests ---


class TestDecideResumeAction:
    def test_finished_returns_show_status(self, tmp_path):
        story = _make_story(str(tmp_path), status="failed")
        assert (
            decide_resume_action(story, FakeBackend(), is_running=False)
            == StageEntryAction.SHOW_STATUS
        )

    def test_workspace_blocked(self, tmp_path):
        story = _make_story(str(tmp_path), status="active")
        assert (
            decide_resume_action(
                story,
                FakeBackend(),
                is_running=False,
                workspace_state=WorkspaceState.LOCKED_BY_OTHER,
            )
            == StageEntryAction.SHOW_WORKSPACE_BUSY
        )

    def test_done_corrupted(self, tmp_path):
        story = _make_story(str(tmp_path), status="active")
        done = stage_done_file(story)
        done.parent.mkdir(parents=True, exist_ok=True)
        done.write_text("BROKEN{{{", encoding="utf-8")
        assert (
            decide_resume_action(story, FakeBackend(), is_running=False)
            == StageEntryAction.PROMPT_FIX_DONE
        )

    def test_gate_wait_returns_retry_review(self, tmp_path):
        import json

        story = _make_story(str(tmp_path), status="paused")
        story["context_json"] = json.dumps({"last_gate_decision_id": "abc"})
        assert (
            decide_resume_action(story, FakeBackend(), is_running=False)
            == StageEntryAction.RETRY_REVIEW
        )

    def test_done_ok_returns_consume(self, tmp_path):
        story = _make_story(str(tmp_path), status="active")
        done = stage_done_file(story)
        done.parent.mkdir(parents=True, exist_ok=True)
        done.write_text('{"summary": "ok"}', encoding="utf-8")
        assert (
            decide_resume_action(story, FakeBackend(), is_running=False)
            == StageEntryAction.CONSUME_DONE_RESUME
        )

    def test_done_ok_takes_priority_over_session_state(self, tmp_path):
        """Even if session is dead, valid .done means CONSUME_DONE_RESUME."""
        story = _make_story(str(tmp_path), status="active")
        done = stage_done_file(story)
        done.parent.mkdir(parents=True, exist_ok=True)
        done.write_text('{"summary": "ok"}', encoding="utf-8")
        assert (
            decide_resume_action(
                story, FakeBackend(SessionState.EXITED), is_running=True
            )
            == StageEntryAction.CONSUME_DONE_RESUME
        )

    def test_cli_exited_returns_start(self, tmp_path):
        story = _make_story(str(tmp_path), status="active")
        marker = cli_exit_marker_path(story["story_key"])
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("1", encoding="utf-8")
        assert (
            decide_resume_action(story, FakeBackend(), is_running=False)
            == StageEntryAction.START_OR_RESUME
        )
        marker.unlink()

    def test_running_dead_session_returns_cleanup_restart(self, tmp_path):
        story = _make_story(str(tmp_path), status="active")
        assert (
            decide_resume_action(
                story, FakeBackend(SessionState.EXITED), is_running=True
            )
            == StageEntryAction.CLEANUP_DEAD_AND_RESTART
        )

    def test_running_live_session_returns_show_running(self, tmp_path):
        story = _make_story(str(tmp_path), status="active")
        assert (
            decide_resume_action(story, FakeBackend(SessionState.LIVE), is_running=True)
            == StageEntryAction.SHOW_RUNNING
        )

    def test_idle_dead_session_returns_cleanup_start(self, tmp_path):
        story = _make_story(str(tmp_path), status="active")
        assert (
            decide_resume_action(
                story, FakeBackend(SessionState.EXITED), is_running=False
            )
            == StageEntryAction.CLEANUP_DEAD_AND_START
        )

    def test_idle_no_session_returns_start(self, tmp_path):
        story = _make_story(str(tmp_path), status="active")
        assert (
            decide_resume_action(story, FakeBackend(), is_running=False)
            == StageEntryAction.START_OR_RESUME
        )


class TestEntryActionNotice:
    def test_all_non_executable_actions_have_notice(self, tmp_path):
        """Every action that is NOT ATTACH/START_OR_RESUME/CONSUME_DONE_RESUME must have a non-empty notice."""
        executable_actions = {
            StageEntryAction.ATTACH,
            StageEntryAction.START_OR_RESUME,
            StageEntryAction.CONSUME_DONE_RESUME,
            StageEntryAction.CLEANUP_DEAD_AND_START,
            StageEntryAction.CLEANUP_DEAD_AND_RESTART,
            StageEntryAction.CONFIRM_AND_DESTROY,
        }
        story = _make_story(str(tmp_path), "TEST-001", "design")
        for action in StageEntryAction:
            if action in executable_actions:
                continue
            notice = entry_action_notice(action, story)
            assert notice is not None and len(notice) > 0, (
                f"Action {action.value!r} must have a non-empty notice"
            )

    def test_prompt_press_r_notice(self, tmp_path):
        story = _make_story(str(tmp_path), "TEST-001", "design")
        notice = entry_action_notice(StageEntryAction.PROMPT_PRESS_R, story)
        assert "没有运行中的 session" in notice
        assert "按 r" in notice

    def test_prompt_fix_done_notice(self, tmp_path):
        story = _make_story(str(tmp_path), "TEST-001", "design")
        notice = entry_action_notice(StageEntryAction.PROMPT_FIX_DONE, story)
        assert ".done" in notice

    def test_show_cli_exit_error_notice(self, tmp_path):
        story = _make_story(str(tmp_path), "TEST-001", "design")
        notice = entry_action_notice(StageEntryAction.SHOW_CLI_EXIT_ERROR, story)
        assert "退出" in notice

    def test_show_workspace_busy_notice(self, tmp_path):
        story = _make_story(str(tmp_path), "TEST-001", "design")
        notice = entry_action_notice(StageEntryAction.SHOW_WORKSPACE_BUSY, story)
        assert "workspace" in notice.lower() or "Workspace" in notice

    def test_show_running_notice(self, tmp_path):
        story = _make_story(str(tmp_path), "TEST-001", "design")
        notice = entry_action_notice(StageEntryAction.SHOW_RUNNING, story)
        assert "运行" in notice

    def test_show_status_notice(self, tmp_path):
        story = _make_story(str(tmp_path), "TEST-001", "design")
        notice = entry_action_notice(StageEntryAction.SHOW_STATUS, story)
        assert notice is not None



# ---------------------------------------------------------------------------
# zellij_execution_args direct tests
# ---------------------------------------------------------------------------


class TestZellijExecutionArgs:
    def test_returns_none_when_no_zellij(self, monkeypatch, tmp_path):
        import story_lifecycle.infra.terminal.ttyd as ttyd_mod

        monkeypatch.setattr(ttyd_mod, "_MPLEX", None)

        result = ttyd_mod.zellij_execution_args("KEY", str(tmp_path), "claude", "/p.md")
        assert result is None

    def test_returns_none_when_no_git_bash_on_windows(self, monkeypatch, tmp_path):
        import os

        if os.name != "nt":
            pytest.skip("Windows-only test")

        import story_lifecycle.infra.terminal.ttyd as ttyd_mod
        import story_lifecycle.infra.terminal.platform_ops as po_mod

        monkeypatch.setattr(ttyd_mod, "_MPLEX", "zellij")
        monkeypatch.setattr(po_mod, "_find_git_bash", lambda: None)

        result = ttyd_mod.zellij_execution_args("KEY", str(tmp_path), "claude", "/p.md")
        assert result is None

    def test_generates_layout_and_argv(self, monkeypatch, tmp_path):
        import os
        import pathlib

        import story_lifecycle.infra.terminal.ttyd as ttyd_mod

        monkeypatch.setattr(ttyd_mod, "_MPLEX", "zellij")

        # On Windows, ensure _find_git_bash returns a path
        if os.name == "nt":
            import story_lifecycle.infra.terminal.platform_ops as po_mod

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

        import story_lifecycle.infra.terminal.ttyd as ttyd_mod
        import story_lifecycle.infra.terminal.platform_ops as po_mod

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


class TestResolveSessionState:
    def test_live_session(self, monkeypatch):
        import subprocess
        import story_lifecycle.infra.terminal.ttyd as ttyd_mod

        monkeypatch.setattr(ttyd_mod, "_MPLEX", "zellij")
        monkeypatch.setattr(
            ttyd_mod,
            "_run",
            lambda *a, **kw: subprocess.CompletedProcess(
                a[0], 0, "s-TEST-001 [Created 1m ago]\n", ""
            ),
        )
        assert resolve_session_state("s-TEST-001") == SessionState.LIVE

    def test_exited_session(self, monkeypatch):
        import subprocess
        import story_lifecycle.infra.terminal.ttyd as ttyd_mod

        monkeypatch.setattr(ttyd_mod, "_MPLEX", "zellij")
        monkeypatch.setattr(
            ttyd_mod,
            "_run",
            lambda *a, **kw: subprocess.CompletedProcess(
                a[0],
                0,
                "s-TEST-001 [Created 1m ago] (EXITED - attach to resurrect)\n",
                "",
            ),
        )
        assert resolve_session_state("s-TEST-001") == SessionState.EXITED

    def test_missing_session(self, monkeypatch):
        import subprocess
        import story_lifecycle.infra.terminal.ttyd as ttyd_mod

        monkeypatch.setattr(ttyd_mod, "_MPLEX", "zellij")
        monkeypatch.setattr(
            ttyd_mod,
            "_run",
            lambda *a, **kw: subprocess.CompletedProcess(
                a[0], 1, "No sessions found.\n", ""
            ),
        )
        assert resolve_session_state("s-TEST-001") == SessionState.MISSING

    def test_unknown_when_no_mplex(self, monkeypatch):
        import story_lifecycle.infra.terminal.ttyd as ttyd_mod

        monkeypatch.setattr(ttyd_mod, "_MPLEX", None)
        assert resolve_session_state("s-TEST-001") == SessionState.UNKNOWN
