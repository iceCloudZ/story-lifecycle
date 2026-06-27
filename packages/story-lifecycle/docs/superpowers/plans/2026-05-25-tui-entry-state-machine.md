# TUI Entry State Machine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expand the TUI entry state machine from 6 states/6 actions to 11 states/15 actions per the design doc at `docs/design-tui-entry-state-machine.md`. Make all state resolution pure and testable, add CLI exit marker handling, session state granularity, and fix TUI handlers for all keys.

**Architecture:** Three-layer design stays: resolver (read-only fact gathering) → decider (pure function) → handler (side effects). New enums added to `entry.py` for `SessionState`, `CliExitState`, `WorkspaceState`. `SessionBackend` gains `resolve_session_state()` method. `ttyd.py` gains session state parsing. TUI handlers rewritten to use expanded decision table.

**Tech Stack:** Python 3.11+, pytest, Textual (TUI layer only)

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `src/story_lifecycle/orchestrator/entry.py` | Modify | Expand enums, resolver, decider, notice for 11 states / 15 actions |
| `src/story_lifecycle/terminal/ttyd.py` | Modify | Add `SessionState` enum, `resolve_session_state()` function |
| `src/story_lifecycle/orchestrator/graph.py` | Modify | Add `is_workspace_locked()` helper |
| `src/story_lifecycle/cli/tui.py` | Modify | Rewrite e/r handlers, fix q/s/f/a/x, decompose watchdog |
| `tests/test_entry_decisions.py` | Modify | Expand tests for all new states, actions, resolver, decider |

---

### Task 1: Add `SessionState` enum and `resolve_session_state()` to ttyd.py

**Files:**
- Modify: `src/story_lifecycle/terminal/ttyd.py` (after `session_alive`, ~line 159)
- Test: `tests/test_entry_decisions.py` (add new test class)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_entry_decisions.py`:

```python
from story_lifecycle.terminal.ttyd import SessionState, resolve_session_state


class TestResolveSessionState:
    def test_live_session(self, monkeypatch):
        import subprocess
        import story_lifecycle.terminal.ttyd as ttyd_mod

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
        import story_lifecycle.terminal.ttyd as ttyd_mod

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
        import story_lifecycle.terminal.ttyd as ttyd_mod

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
        import story_lifecycle.terminal.ttyd as ttyd_mod

        monkeypatch.setattr(ttyd_mod, "_MPLEX", None)
        assert resolve_session_state("s-TEST-001") == SessionState.UNKNOWN

    def test_tmux_live(self, monkeypatch):
        import subprocess
        import story_lifecycle.terminal.ttyd as ttyd_mod

        monkeypatch.setattr(ttyd_mod, "_MPLEX", "tmux")
        monkeypatch.setattr(
            ttyd_mod,
            "_run",
            lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, "", ""),
        )
        assert resolve_session_state("s-TEST-001") == SessionState.LIVE
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_entry_decisions.py::TestResolveSessionState -v`
Expected: FAIL — `ImportError: cannot import name 'SessionState'`

- [ ] **Step 3: Add `SessionState` enum and `resolve_session_state` to ttyd.py**

Insert after the `session_alive` function (~line 158 in `src/story_lifecycle/terminal/ttyd.py`):

```python
class SessionState:
    """Granular session state for TUI entry decisions."""
    LIVE = "live"
    EXITED = "exited"
    MISSING = "missing"
    UNKNOWN = "unknown"


def resolve_session_state(name: str) -> str:
    """Resolve the detailed state of a session: live/exited/missing/unknown."""
    if not _MPLEX:
        return SessionState.UNKNOWN

    if _MPLEX == "zellij":
        r = _run(["zellij", "list-sessions"], text=True, timeout=5)
        if r.returncode != 0:
            return SessionState.MISSING
        for line in _strip_ansi(r.stdout).splitlines():
            parts = line.strip().split()
            if not parts:
                continue
            if parts[0] == name:
                return SessionState.EXITED if "EXITED" in line else SessionState.LIVE
        return SessionState.MISSING

    if _MPLEX == "tmux":
        r = _run(["tmux", "has-session", "-t", name])
        return SessionState.LIVE if r.returncode == 0 else SessionState.MISSING

    return SessionState.UNKNOWN
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_entry_decisions.py::TestResolveSessionState -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/story_lifecycle/terminal/ttyd.py tests/test_entry_decisions.py
git commit -m "feat: add SessionState enum and resolve_session_state to ttyd"
```

---

### Task 2: Expand enums in entry.py — `StageEntryState`, `StageEntryAction`, `CliExitState`, `WorkspaceState`

**Files:**
- Modify: `src/story_lifecycle/orchestrator/entry.py` (replace enum definitions)
- Test: `tests/test_entry_decisions.py` (update imports and parametrized tests)

- [ ] **Step 1: Replace `StageEntryState` with expanded 11-state enum**

In `src/story_lifecycle/orchestrator/entry.py`, replace lines 103-109:

```python
class StageEntryState(Enum):
    STORY_FINISHED = "story_finished"
    DONE_CORRUPTED = "done_corrupted"
    DONE_OK = "done_ok"
    CLI_EXITED_WITHOUT_DONE = "cli_exited_without_done"
    BLOCKED_BY_WORKSPACE = "blocked_by_workspace"
    RUNNING_WITH_LIVE_SESSION = "running_with_live_session"
    RUNNING_WITH_DEAD_SESSION = "running_with_dead_session"
    RUNNING_WITH_UNKNOWN_SESSION = "running_with_unknown_session"
    IDLE_WITH_LIVE_SESSION = "idle_with_live_session"
    IDLE_WITH_DEAD_SESSION = "idle_with_dead_session"
    IDLE = "idle"
    UNKNOWN = "unknown"
```

- [ ] **Step 2: Replace `StageEntryAction` with expanded 15-action enum**

In `src/story_lifecycle/orchestrator/entry.py`, replace lines 112-118:

```python
class StageEntryAction(Enum):
    ATTACH = "attach"
    START_OR_RESUME = "start_or_resume"
    CONSUME_DONE_RESUME = "consume_done_resume"
    CLEANUP_DEAD_AND_START = "cleanup_dead_and_start"
    CLEANUP_DEAD_AND_RESTART = "cleanup_dead_and_restart"
    PROMPT_KEY_EXISTS = "prompt_key_exists"
    CONFIRM_AND_DESTROY = "confirm_and_destroy"
    PROMPT_PRESS_R = "prompt_press_r"
    PROMPT_FIX_DONE = "prompt_fix_done"
    SHOW_STATUS = "show_status"
    SHOW_RUNNING = "show_running"
    SHOW_WORKSPACE_BUSY = "show_workspace_busy"
    SHOW_SESSION_UNKNOWN = "show_session_unknown"
    SHOW_CLI_EXIT_ERROR = "show_cli_exit_error"
    NOOP = "noop"
```

- [ ] **Step 3: Add `CliExitState` and `WorkspaceState` enums**

Append after `DoneValidationResult` class (~line 37 in entry.py):

```python
class CliExitState(Enum):
    EXITED_WITHOUT_DONE = "exited_without_done"
    NONE = "none"
    UNKNOWN = "unknown"


class WorkspaceState(Enum):
    LOCKED_BY_SELF = "locked_by_self"
    LOCKED_BY_OTHER = "locked_by_other"
    FREE = "free"
    UNKNOWN = "unknown"
```

- [ ] **Step 4: Add CLI exit marker helper**

Append after the `validate_stage_done` function:

```python
def cli_exit_marker_path(story_key: str) -> Path:
    """Path to the CLI exit marker file for a story."""
    from tempfile import gettempdir

    return Path(gettempdir()) / f"story-exit-{story_key}"


def resolve_cli_exit_state(story: dict) -> CliExitState:
    """Check if the CLI process exited without writing .done."""
    marker = cli_exit_marker_path(story.get("story_key", ""))
    if not marker.exists():
        return CliExitState.NONE
    done = validate_stage_done(story)
    if done.status == DoneStatus.OK:
        return CliExitState.NONE
    return CliExitState.EXITED_WITHOUT_DONE
```

- [ ] **Step 5: Update existing tests to use new state names**

In `tests/test_entry_decisions.py`, update all references from old state names to new ones. Key changes:

- `StageEntryState.DONE` → `StageEntryState.DONE_OK`
- `StageEntryState.RUNNING_HEALTHY` → `StageEntryState.RUNNING_WITH_LIVE_SESSION`
- `StageEntryState.RUNNING_DEAD` → `StageEntryState.RUNNING_WITH_DEAD_SESSION`
- `StageEntryAction.PROMPT_DONE_PRESS_R` → `StageEntryAction.PROMPT_PRESS_R` (DONE_OK + e now maps to PROMPT_PRESS_R with different notice)

Update `_ACTION_TABLE` in entry.py too (done in Task 3). For now, just make the enum renames compile.

- [ ] **Step 6: Run lint**

Run: `ruff check src/story_lifecycle/orchestrator/entry.py`
Expected: No errors (may have unused imports, fix as needed)

- [ ] **Step 7: Commit**

```bash
git add src/story_lifecycle/orchestrator/entry.py
git commit -m "feat: expand StageEntryState to 11 states, StageEntryAction to 15 actions"
```

---

### Task 3: Update `SessionBackend` protocol and `resolve_stage_state`

**Files:**
- Modify: `src/story_lifecycle/orchestrator/entry.py` (SessionBackend, resolver)
- Modify: `src/story_lifecycle/cli/tui.py` (update backend usage)
- Test: `tests/test_entry_decisions.py` (update resolver tests)

- [ ] **Step 1: Add `resolve_session_state` to `SessionBackend` protocol**

In `entry.py`, update the `SessionBackend` protocol:

```python
class SessionBackend(Protocol):
    def is_healthy(self, session_id: str) -> bool: ...
    def resolve_session_state(self, session_id: str) -> str: ...
    def attach_foreground(self, session_id: str) -> list[str]: ...
    def launch_independent_terminal(
        self, story_key: str, workspace: str, launch_cmd: str, prompt_file: str
    ) -> None: ...
```

Add implementation in `TtydSessionBackend`:

```python
def resolve_session_state(self, session_id: str) -> str:
    from ..terminal import ttyd

    return ttyd.resolve_session_state(session_id)
```

- [ ] **Step 2: Rewrite `resolve_stage_state` for 11 states**

Replace the existing `resolve_stage_state` function:

```python
def resolve_stage_state(
    story: dict,
    backend: SessionBackend,
    is_running: bool,
    cli_exit_state: CliExitState | None = None,
    workspace_state: WorkspaceState | None = None,
) -> StageEntryState:
    status = story.get("status", "")

    # Priority 1: terminal story states
    if status in _FINISHED_STATUSES:
        return StageEntryState.STORY_FINISHED

    # Priority 2: .done corrupted (blocks everything except STORY_FINISHED)
    validation = validate_stage_done(story)
    if validation.status == DoneStatus.CORRUPTED:
        return StageEntryState.DONE_CORRUPTED

    # Priority 3: .done ok
    if validation.status == DoneStatus.OK:
        return StageEntryState.DONE_OK

    # Priority 4: CLI exited without .done
    if cli_exit_state is None:
        cli_exit_state = resolve_cli_exit_state(story)
    if cli_exit_state == CliExitState.EXITED_WITHOUT_DONE:
        return StageEntryState.CLI_EXITED_WITHOUT_DONE

    # Priority 5: workspace blocked
    if workspace_state is None:
        workspace_state = WorkspaceState.FREE
    if workspace_state == WorkspaceState.LOCKED_BY_OTHER:
        return StageEntryState.BLOCKED_BY_WORKSPACE

    # Priority 6-7: graph running/not running + session state
    session_id = _session_id_for_story(story)
    session = backend.resolve_session_state(session_id)

    if is_running:
        if session == "live":
            return StageEntryState.RUNNING_WITH_LIVE_SESSION
        if session == "exited":
            return StageEntryState.RUNNING_WITH_DEAD_SESSION
        if session == "missing":
            return StageEntryState.RUNNING_WITH_DEAD_SESSION
        return StageEntryState.RUNNING_WITH_UNKNOWN_SESSION

    # graph not running
    if session == "live":
        return StageEntryState.IDLE_WITH_LIVE_SESSION
    if session == "exited":
        return StageEntryState.IDLE_WITH_DEAD_SESSION
    if session == "missing":
        return StageEntryState.IDLE

    return StageEntryState.UNKNOWN
```

- [ ] **Step 3: Update `FakeBackend` in tests**

In `tests/test_entry_decisions.py`, update `FakeBackend`:

```python
from story_lifecycle.terminal.ttyd import SessionState


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
```

- [ ] **Step 4: Update resolver tests for new states**

Replace `TestResolveStageState` with expanded tests covering all 11 states:

```python
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

    def test_done_corrupted(self, tmp_path):
        story = _make_story(str(tmp_path), status="active")
        done = stage_done_file(story)
        done.parent.mkdir(parents=True, exist_ok=True)
        done.write_text("BROKEN{{{", encoding="utf-8")
        assert (
            resolve_stage_state(story, FakeBackend(), is_running=False)
            == StageEntryState.DONE_CORRUPTED
        )

    def test_done_ok(self, tmp_path):
        story = _make_story(str(tmp_path), status="active")
        done = stage_done_file(story)
        done.parent.mkdir(parents=True, exist_ok=True)
        done.write_text('{"summary": "ok"}', encoding="utf-8")
        assert (
            resolve_stage_state(story, FakeBackend(), is_running=True)
            == StageEntryState.DONE_OK
        )

    def test_cli_exited_without_done(self, tmp_path):
        story = _make_story(str(tmp_path), status="active")
        marker = cli_exit_marker_path(story["story_key"])
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("1", encoding="utf-8")
        assert (
            resolve_stage_state(story, FakeBackend(), is_running=False)
            == StageEntryState.CLI_EXITED_WITHOUT_DONE
        )
        marker.unlink()

    def test_blocked_by_workspace(self, tmp_path):
        story = _make_story(str(tmp_path), status="active")
        assert (
            resolve_stage_state(
                story,
                FakeBackend(),
                is_running=False,
                workspace_state=WorkspaceState.LOCKED_BY_OTHER,
            )
            == StageEntryState.BLOCKED_BY_WORKSPACE
        )

    def test_running_with_live_session(self, tmp_path):
        story = _make_story(str(tmp_path), status="active")
        assert (
            resolve_stage_state(story, FakeBackend(SessionState.LIVE), is_running=True)
            == StageEntryState.RUNNING_WITH_LIVE_SESSION
        )

    def test_running_with_dead_session(self, tmp_path):
        story = _make_story(str(tmp_path), status="active")
        assert (
            resolve_stage_state(
                story, FakeBackend(SessionState.EXITED), is_running=True
            )
            == StageEntryState.RUNNING_WITH_DEAD_SESSION
        )

    def test_running_with_missing_session(self, tmp_path):
        story = _make_story(str(tmp_path), status="active")
        assert (
            resolve_stage_state(
                story, FakeBackend(SessionState.MISSING), is_running=True
            )
            == StageEntryState.RUNNING_WITH_DEAD_SESSION
        )

    def test_running_with_unknown_session(self, tmp_path):
        story = _make_story(str(tmp_path), status="active")
        assert (
            resolve_stage_state(
                story, FakeBackend(SessionState.UNKNOWN), is_running=True
            )
            == StageEntryState.RUNNING_WITH_UNKNOWN_SESSION
        )

    def test_idle_with_live_session(self, tmp_path):
        story = _make_story(str(tmp_path), status="active")
        assert (
            resolve_stage_state(
                story, FakeBackend(SessionState.LIVE), is_running=False
            )
            == StageEntryState.IDLE_WITH_LIVE_SESSION
        )

    def test_idle_with_dead_session(self, tmp_path):
        story = _make_story(str(tmp_path), status="active")
        assert (
            resolve_stage_state(
                story, FakeBackend(SessionState.EXITED), is_running=False
            )
            == StageEntryState.IDLE_WITH_DEAD_SESSION
        )

    def test_idle(self, tmp_path):
        story = _make_story(str(tmp_path), status="active")
        assert (
            resolve_stage_state(story, FakeBackend(SessionState.MISSING), is_running=False)
            == StageEntryState.IDLE
        )

    def test_done_ok_takes_priority_over_running_dead(self, tmp_path):
        """Even if session is dead, valid .done means DONE_OK state."""
        story = _make_story(str(tmp_path), status="active")
        done = stage_done_file(story)
        done.parent.mkdir(parents=True, exist_ok=True)
        done.write_text('{"summary": "ok"}', encoding="utf-8")
        assert (
            resolve_stage_state(
                story, FakeBackend(SessionState.EXITED), is_running=True
            )
            == StageEntryState.DONE_OK
        )

    def test_cli_exit_ignored_when_done_ok(self, tmp_path):
        """If .done exists and is valid, CLI exit marker is irrelevant."""
        story = _make_story(str(tmp_path), status="active")
        done = stage_done_file(story)
        done.parent.mkdir(parents=True, exist_ok=True)
        done.write_text('{"summary": "ok"}', encoding="utf-8")
        marker = cli_exit_marker_path(story["story_key"])
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("1", encoding="utf-8")
        assert (
            resolve_stage_state(story, FakeBackend(), is_running=False)
            == StageEntryState.DONE_OK
        )
        marker.unlink()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_entry_decisions.py::TestResolveStageState -v`
Expected: All 14 resolver tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/story_lifecycle/orchestrator/entry.py tests/test_entry_decisions.py
git commit -m "feat: rewrite resolve_stage_state for 11 states with CLI exit + workspace checks"
```

---

### Task 4: Rewrite decision table for full 11-state × 2-action coverage

**Files:**
- Modify: `src/story_lifecycle/orchestrator/entry.py` (replace `_ACTION_TABLE`)
- Test: `tests/test_entry_decisions.py` (replace `DECISION_TABLE`)

- [ ] **Step 1: Write the failing tests**

Replace `DECISION_TABLE` in `tests/test_entry_decisions.py`:

```python
DECISION_TABLE = [
    # (state, user_action, expected_action)
    (StageEntryState.STORY_FINISHED, "e", StageEntryAction.SHOW_STATUS),
    (StageEntryState.STORY_FINISHED, "r", StageEntryAction.SHOW_STATUS),
    (StageEntryState.DONE_CORRUPTED, "e", StageEntryAction.PROMPT_FIX_DONE),
    (StageEntryState.DONE_CORRUPTED, "r", StageEntryAction.PROMPT_FIX_DONE),
    (StageEntryState.DONE_OK, "e", StageEntryAction.PROMPT_PRESS_R),
    (StageEntryState.DONE_OK, "r", StageEntryAction.CONSUME_DONE_RESUME),
    (StageEntryState.CLI_EXITED_WITHOUT_DONE, "e", StageEntryAction.SHOW_CLI_EXIT_ERROR),
    (StageEntryState.CLI_EXITED_WITHOUT_DONE, "r", StageEntryAction.START_OR_RESUME),
    (StageEntryState.BLOCKED_BY_WORKSPACE, "e", StageEntryAction.SHOW_WORKSPACE_BUSY),
    (StageEntryState.BLOCKED_BY_WORKSPACE, "r", StageEntryAction.SHOW_WORKSPACE_BUSY),
    (StageEntryState.RUNNING_WITH_LIVE_SESSION, "e", StageEntryAction.ATTACH),
    (StageEntryState.RUNNING_WITH_LIVE_SESSION, "r", StageEntryAction.SHOW_RUNNING),
    (StageEntryState.RUNNING_WITH_DEAD_SESSION, "e", StageEntryAction.PROMPT_PRESS_R),
    (StageEntryState.RUNNING_WITH_DEAD_SESSION, "r", StageEntryAction.CLEANUP_DEAD_AND_RESTART),
    (StageEntryState.RUNNING_WITH_UNKNOWN_SESSION, "e", StageEntryAction.SHOW_SESSION_UNKNOWN),
    (StageEntryState.RUNNING_WITH_UNKNOWN_SESSION, "r", StageEntryAction.SHOW_SESSION_UNKNOWN),
    (StageEntryState.IDLE_WITH_LIVE_SESSION, "e", StageEntryAction.ATTACH),
    (StageEntryState.IDLE_WITH_LIVE_SESSION, "r", StageEntryAction.START_OR_RESUME),
    (StageEntryState.IDLE_WITH_DEAD_SESSION, "e", StageEntryAction.PROMPT_PRESS_R),
    (StageEntryState.IDLE_WITH_DEAD_SESSION, "r", StageEntryAction.CLEANUP_DEAD_AND_START),
    (StageEntryState.IDLE, "e", StageEntryAction.PROMPT_PRESS_R),
    (StageEntryState.IDLE, "r", StageEntryAction.START_OR_RESUME),
    (StageEntryState.UNKNOWN, "e", StageEntryAction.SHOW_SESSION_UNKNOWN),
    (StageEntryState.UNKNOWN, "r", StageEntryAction.SHOW_SESSION_UNKNOWN),
]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_entry_decisions.py::TestDecideAction -v`
Expected: FAIL — old table doesn't cover new states

- [ ] **Step 3: Replace `_ACTION_TABLE` in entry.py**

```python
_ACTION_TABLE: dict[tuple[StageEntryState, str], StageEntryAction] = {
    # STORY_FINISHED
    (StageEntryState.STORY_FINISHED, "e"): StageEntryAction.SHOW_STATUS,
    (StageEntryState.STORY_FINISHED, "r"): StageEntryAction.SHOW_STATUS,
    # DONE_CORRUPTED
    (StageEntryState.DONE_CORRUPTED, "e"): StageEntryAction.PROMPT_FIX_DONE,
    (StageEntryState.DONE_CORRUPTED, "r"): StageEntryAction.PROMPT_FIX_DONE,
    # DONE_OK
    (StageEntryState.DONE_OK, "e"): StageEntryAction.PROMPT_PRESS_R,
    (StageEntryState.DONE_OK, "r"): StageEntryAction.CONSUME_DONE_RESUME,
    # CLI_EXITED_WITHOUT_DONE
    (StageEntryState.CLI_EXITED_WITHOUT_DONE, "e"): StageEntryAction.SHOW_CLI_EXIT_ERROR,
    (StageEntryState.CLI_EXITED_WITHOUT_DONE, "r"): StageEntryAction.START_OR_RESUME,
    # BLOCKED_BY_WORKSPACE
    (StageEntryState.BLOCKED_BY_WORKSPACE, "e"): StageEntryAction.SHOW_WORKSPACE_BUSY,
    (StageEntryState.BLOCKED_BY_WORKSPACE, "r"): StageEntryAction.SHOW_WORKSPACE_BUSY,
    # RUNNING_WITH_LIVE_SESSION
    (StageEntryState.RUNNING_WITH_LIVE_SESSION, "e"): StageEntryAction.ATTACH,
    (StageEntryState.RUNNING_WITH_LIVE_SESSION, "r"): StageEntryAction.SHOW_RUNNING,
    # RUNNING_WITH_DEAD_SESSION
    (StageEntryState.RUNNING_WITH_DEAD_SESSION, "e"): StageEntryAction.PROMPT_PRESS_R,
    (StageEntryState.RUNNING_WITH_DEAD_SESSION, "r"): StageEntryAction.CLEANUP_DEAD_AND_RESTART,
    # RUNNING_WITH_UNKNOWN_SESSION
    (StageEntryState.RUNNING_WITH_UNKNOWN_SESSION, "e"): StageEntryAction.SHOW_SESSION_UNKNOWN,
    (StageEntryState.RUNNING_WITH_UNKNOWN_SESSION, "r"): StageEntryAction.SHOW_SESSION_UNKNOWN,
    # IDLE_WITH_LIVE_SESSION
    (StageEntryState.IDLE_WITH_LIVE_SESSION, "e"): StageEntryAction.ATTACH,
    (StageEntryState.IDLE_WITH_LIVE_SESSION, "r"): StageEntryAction.START_OR_RESUME,
    # IDLE_WITH_DEAD_SESSION
    (StageEntryState.IDLE_WITH_DEAD_SESSION, "e"): StageEntryAction.PROMPT_PRESS_R,
    (StageEntryState.IDLE_WITH_DEAD_SESSION, "r"): StageEntryAction.CLEANUP_DEAD_AND_START,
    # IDLE
    (StageEntryState.IDLE, "e"): StageEntryAction.PROMPT_PRESS_R,
    (StageEntryState.IDLE, "r"): StageEntryAction.START_OR_RESUME,
    # UNKNOWN
    (StageEntryState.UNKNOWN, "e"): StageEntryAction.SHOW_SESSION_UNKNOWN,
    (StageEntryState.UNKNOWN, "r"): StageEntryAction.SHOW_SESSION_UNKNOWN,
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_entry_decisions.py::TestDecideAction -v`
Expected: All 24 parametrized tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/story_lifecycle/orchestrator/entry.py tests/test_entry_decisions.py
git commit -m "feat: expand decision table to 11 states × 2 actions = 24 entries"
```

---

### Task 5: Expand `entry_action_notice` for all non-executable actions

**Files:**
- Modify: `src/story_lifecycle/orchestrator/entry.py` (expand `entry_action_notice`)
- Test: `tests/test_entry_decisions.py` (expand notice tests)

- [ ] **Step 1: Write the failing tests**

Replace `TestEntryActionNotice`:

```python
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
        assert "workspace" in notice.lower() or "工作区" in notice

    def test_show_running_notice(self, tmp_path):
        story = _make_story(str(tmp_path), "TEST-001", "design")
        notice = entry_action_notice(StageEntryAction.SHOW_RUNNING, story)
        assert "运行" in notice

    def test_show_status_notice(self, tmp_path):
        story = _make_story(str(tmp_path), "TEST-001", "design")
        notice = entry_action_notice(StageEntryAction.SHOW_STATUS, story)
        assert notice is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_entry_decisions.py::TestEntryActionNotice -v`
Expected: FAIL — new actions return None

- [ ] **Step 3: Rewrite `entry_action_notice` in entry.py**

```python
def entry_action_notice(action: StageEntryAction, story: dict) -> str | None:
    """Return a short user-visible notice for non-terminal entry actions."""
    stage = story.get("current_stage", "")
    key = story.get("story_key", "")

    return {
        StageEntryAction.PROMPT_PRESS_R: "没有运行中的 session，按 r 启动或恢复执行。",
        StageEntryAction.PROMPT_FIX_DONE: ".done 文件损坏，请修复或删除后重试。",
        StageEntryAction.SHOW_STATUS: f"Story {key} 已结束（{story.get('status', '')}），不可操作。",
        StageEntryAction.SHOW_RUNNING: f"Story {key} 正在运行中，AI session 健康，无需重复启动。",
        StageEntryAction.SHOW_WORKSPACE_BUSY: f"Workspace 被其他 story 占用，请等待完成后再试。",
        StageEntryAction.SHOW_SESSION_UNKNOWN: f"无法确定 session 状态，请检查 Zellij/tmux 是否正常。",
        StageEntryAction.SHOW_CLI_EXIT_ERROR: f"CLI 进程异常退出（stage: {stage}），按 r 重新启动。",
        StageEntryAction.PROMPT_KEY_EXISTS: f"Story key {key} 已存在，请使用现有 story 或换 key。",
        StageEntryAction.NOOP: "当前状态无需操作。",
    }.get(action)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_entry_decisions.py::TestEntryActionNotice -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/story_lifecycle/orchestrator/entry.py tests/test_entry_decisions.py
git commit -m "feat: expand entry_action_notice for all 9 non-executable actions"
```

---

### Task 6: Update TUI `action_enter_terminal` for new states

**Files:**
- Modify: `src/story_lifecycle/cli/tui.py` (lines ~964-1057)

- [ ] **Step 1: Update imports in tui.py**

Add new imports (update the existing import block from entry.py):

```python
from ..orchestrator.entry import (
    StageEntryAction,
    StageEntryState,
    TtydSessionBackend,
    resolve_stage_state,
    decide_action,
    entry_action_notice,
    has_stage_done,
    validate_stage_done,
    DoneStatus,
    CliExitState,
    WorkspaceState,
    cli_exit_marker_path,
    resolve_cli_exit_state,
)
```

- [ ] **Step 2: Rewrite `action_enter_terminal` method**

Replace the existing `action_enter_terminal` method in `StoryBoardApp`:

```python
def action_enter_terminal(self):
    if not self.stories:
        _tui_debug("enter_terminal_no_stories")
        return
    s = self.stories[self.selected_index]
    story_key = s["story_key"]
    session = ttyd.session_name(story_key)

    from ..orchestrator.graph import is_story_running

    is_running = is_story_running(story_key)
    state = resolve_stage_state(s, self._session_backend, is_running)
    action = decide_action(state, "e")
    _tui_debug(
        "enter_terminal_decision",
        story_key=story_key,
        state=state.value,
        action=action.value,
    )

    if action == StageEntryAction.ATTACH:
        attach_args = self._session_backend.attach_foreground(session)
        if os.name == "nt":
            self._pending_attach_args = attach_args
            self.exit()
            return
        try:
            with self.suspend():
                subprocess.run(attach_args, check=False)
        except Exception:
            self.exit()
            subprocess.run(attach_args, check=False)
            os.system("story board")
    else:
        # All non-ATTACH actions: show notice in detail panel
        notice = entry_action_notice(action, s)
        if notice:
            severity = "error" if action in (
                StageEntryAction.PROMPT_FIX_DONE,
                StageEntryAction.SHOW_CLI_EXIT_ERROR,
                StageEntryAction.SHOW_SESSION_UNKNOWN,
            ) else "warning"
            self.notify(notice, severity=severity)
        panel = self.query_one("#detail-panel")
        panel.update(f"[bold yellow]{notice or '不可操作'}[/]")
        panel.set_class(True, "visible")
        self._show_detail = True
```

- [ ] **Step 3: Run lint**

Run: `ruff check src/story_lifecycle/cli/tui.py`
Expected: No errors

- [ ] **Step 4: Commit**

```bash
git add src/story_lifecycle/cli/tui.py
git commit -m "feat: rewrite e handler for 11-state entry state machine"
```

---

### Task 7: Update TUI `action_resume_story` for new states

**Files:**
- Modify: `src/story_lifecycle/cli/tui.py` (lines ~1244-1296)

- [ ] **Step 1: Rewrite `action_resume_story` method**

Replace the existing `action_resume_story` method:

```python
def action_resume_story(self):
    if not self.stories:
        return
    s = self.stories[self.selected_index]
    key = s["story_key"]
    session = ttyd.session_name(key)

    from ..orchestrator.graph import is_story_running, start_story_async

    is_running = is_story_running(key)
    state = resolve_stage_state(s, self._session_backend, is_running)
    action = decide_action(state, "r")
    _tui_debug(
        "resume_story_decision",
        story_key=key,
        state=state.value,
        action=action.value,
    )

    if action == StageEntryAction.START_OR_RESUME:
        db.update_story(key, status="active", last_error=None)
        if not is_story_running(key):
            start_story_async(key)
        self.refresh_stories()

    elif action == StageEntryAction.CONSUME_DONE_RESUME:
        db.update_story(key, status="active", last_error=None)
        if not is_story_running(key):
            start_story_async(key)
        self.refresh_stories()

    elif action == StageEntryAction.CLEANUP_DEAD_AND_START:
        _tui_debug("cleanup_dead_and_start", story_key=key)
        ttyd.delete_exited_session(session)
        # Clean exit marker if present
        marker = cli_exit_marker_path(key)
        if marker.exists():
            marker.unlink()
        db.update_story(key, status="active", last_error=None)
        if not is_story_running(key):
            start_story_async(key)
        self.refresh_stories()

    elif action == StageEntryAction.CLEANUP_DEAD_AND_RESTART:
        # Requires confirmation — graph still thinks it's running
        def on_restart_confirm(confirmed):
            if not confirmed:
                return
            _tui_debug("cleanup_dead_and_restart", story_key=key)
            ttyd.delete_exited_session(session)
            marker = cli_exit_marker_path(key)
            if marker.exists():
                marker.unlink()
            db.update_story(key, status="active", last_error=None)
            if not is_story_running(key):
                start_story_async(key)
            self.refresh_stories()

        self.push_screen(
            ConfirmDialog(
                f"Story {key} 的 graph 仍在运行但 session 已退出。\n"
                f"强制重启将中止当前后台执行。是否继续？"
            ),
            on_restart_confirm,
        )

    else:
        # SHOW_* / PROMPT_* / NOOP — show notice
        notice = entry_action_notice(action, s)
        if notice:
            severity = "error" if action == StageEntryAction.PROMPT_FIX_DONE else "warning"
            self.notify(notice, severity=severity)
        panel = self.query_one("#detail-panel")
        panel.update(f"[bold yellow]{notice or '不可操作'}[/]")
        panel.set_class(True, "visible")
        self._show_detail = True
```

- [ ] **Step 2: Run lint and tests**

Run: `ruff check src/story_lifecycle/cli/tui.py`
Run: `python -m pytest tests/test_entry_decisions.py -v`
Expected: No lint errors, all tests pass

- [ ] **Step 3: Commit**

```bash
git add src/story_lifecycle/cli/tui.py
git commit -m "feat: rewrite r handler for CONSUME_DONE, CLEANUP_DEAD_AND_START/RESTART"
```

---

### Task 8: Fix `action_quit`, `action_skip_stage`, `action_fail_story`, `action_abort_story`, `action_delete_story`

**Files:**
- Modify: `src/story_lifecycle/cli/tui.py`

- [ ] **Step 1: Change `action_quit` to not auto-pause**

Replace the existing `action_quit` method:

```python
def action_quit(self):
    _tui_debug("quit_tui")
    self.exit()
```

- [ ] **Step 2: Add graph-running protection to `action_skip_stage`**

Replace the existing `action_skip_stage` method:

```python
def action_skip_stage(self):
    if not self.stories:
        return
    s = self.stories[self.selected_index]
    key = s["story_key"]

    from ..orchestrator.graph import is_story_running

    if is_story_running(key):
        self.notify(
            f"Story {key} 的 graph 正在运行，不能跳过。请先等待完成或 abort。",
            severity="warning",
        )
        return

    skip_stage(key, s["current_stage"])
    self.refresh_stories()
```

- [ ] **Step 3: Add graph-running protection to `action_fail_story`**

Replace the existing `action_fail_story` method:

```python
def action_fail_story(self):
    if not self.stories:
        return
    s = self.stories[self.selected_index]
    key = s["story_key"]

    from ..orchestrator.graph import is_story_running

    if is_story_running(key):
        self.notify(
            f"Story {key} 的 graph 正在运行。如需标记失败，请先按 [a] abort。",
            severity="warning",
        )
        return

    fail_story(key)
    self.refresh_stories()
```

- [ ] **Step 4: Update `action_abort_story` with session cleanup hint**

Replace the existing `action_abort_story` method:

```python
def action_abort_story(self):
    if not self.stories:
        return
    s = self.stories[self.selected_index]
    key = s["story_key"]
    session = ttyd.session_name(key)

    from ..orchestrator.service import abort_story

    try:
        abort_story(key)
        # Clean up session if it exists
        if ttyd.session_alive(session):
            ttyd.kill_session(session)
        ttyd.stop_ttyd(key)
    except ValueError as e:
        panel = self.query_one("#detail-panel")
        panel.update(f"[red]{e}[/]")
        panel.set_class(True, "visible")
        self._show_detail = True
    self.refresh_stories()
```

- [ ] **Step 5: Update `action_delete_story` with full cleanup sequence**

Replace the existing `action_delete_story` method:

```python
def action_delete_story(self):
    if not self.stories:
        return
    s = self.stories[self.selected_index]
    key = s["story_key"]
    session = ttyd.session_name(key)

    from ..orchestrator.graph import is_story_running

    is_running = is_story_running(key)
    warning = ""
    if is_running:
        warning = "\n\n[bold red]Story graph 正在运行，删除将中止执行。[/]"

    def on_confirm(confirmed):
        if not confirmed:
            return
        _tui_debug("delete_story", story_key=key)
        # 1. Kill session (live or exited)
        ttyd.kill_session(session)
        # 2. Stop ttyd and release port
        ttyd.stop_ttyd(key)
        # 3. Delete DB record
        delete_story(key)
        # 4. Clean CLI exit marker
        marker = cli_exit_marker_path(key)
        if marker.exists():
            marker.unlink()
        self.refresh_stories()

    self.push_screen(
        ConfirmDialog(f"Delete story {key}?{warning}"),
        on_confirm,
    )
```

- [ ] **Step 6: Run lint and tests**

Run: `ruff check src/story_lifecycle/cli/tui.py`
Run: `python -m pytest tests/ -v`
Expected: No lint errors, all tests pass

- [ ] **Step 7: Commit**

```bash
git add src/story_lifecycle/cli/tui.py
git commit -m "fix: q no longer auto-pauses; s/f/x gain graph-running protection; delete cleans up sessions"
```

---

### Task 9: Add Zellij foreground failure handling in `run_tui()`

**Files:**
- Modify: `src/story_lifecycle/cli/tui.py` (lines ~1704-1764)

- [ ] **Step 1: Add user-visible error when Zellij foreground command fails**

In `run_tui()`, replace the `subprocess.run(attach_args, ...)` block (inside the `try` block) to add failure handling:

Replace the section from `result = subprocess.run(...)` through the end of the `try` block:

```python
            result = subprocess.run(
                attach_args,
                check=False,
                stdin=sys.__stdin__,
                stdout=sys.__stdout__,
                stderr=sys.__stderr__,
            )
            _tui_debug("run_tui_attach_return", returncode=result.returncode)

            if result.returncode != 0:
                # Show error to user before returning to TUI
                sys.__stdout__.write(
                    f"\n\x1b[31mZellij/terminal command failed (exit code: {result.returncode})\x1b[0m\n"
                    f"Command: {' '.join(attach_args)}\n"
                    f"Press Enter to return to TUI...\n"
                )
                sys.__stdout__.flush()
                try:
                    input()
                except EOFError:
                    pass

            # Signal that terminal was opened for foreground Zellij execution
            if len(attach_args) >= 3 and "--session" in attach_args:
                from ..orchestrator.graph import emit_terminal_opened

                session_name = attach_args[attach_args.index("--session") + 1]
                if session_name.startswith("s-"):
                    emit_terminal_opened(session_name[2:])
```

- [ ] **Step 2: Run lint**

Run: `ruff check src/story_lifecycle/cli/tui.py`
Expected: No errors

- [ ] **Step 3: Commit**

```bash
git add src/story_lifecycle/cli/tui.py
git commit -m "fix: show user-visible error when Zellij foreground command fails"
```

---

### Task 10: Integrate CLI exit marker cleanup into watchdog and startup sweep

**Files:**
- Modify: `src/story_lifecycle/cli/tui.py` (watchdog_check, _startup_sweep)

- [ ] **Step 1: Update `_startup_sweep` to clean stale CLI exit markers**

In the `_startup_sweep` method, add marker cleanup after done-file sweep:

```python
def _startup_sweep(self):
    """On startup, check all non-terminal stories for existing done files and resume."""
    from ..orchestrator.graph import start_story_async, is_story_running

    for s in self.stories:
        if s["status"] in _FINISHED_STATUSES:
            continue
        key = s["story_key"]

        # Clean stale CLI exit markers for stories that are not running
        marker = cli_exit_marker_path(key)
        if marker.exists() and not is_story_running(key):
            _tui_debug("startup_sweep_clean_marker", story_key=key)

        if has_stage_done(s):
            validation = validate_stage_done(s)
            if validation.status == DoneStatus.OK and not is_story_running(key):
                db.update_story(key, status="active", last_error=None)
                start_story_async(key)
```

- [ ] **Step 2: Update watchdog to use CLI exit state**

In `watchdog_check`, update the `.done` file watching section to also check CLI exit markers:

```python
    async def watchdog_check(self):
        from ..orchestrator.graph import (
            resume_story,
            is_story_running,
            take_terminal_request,
        )
        from ..db import models as db

        # Check for foreground terminal execution requests
        for s in self.stories:
            key = s["story_key"]
            args = take_terminal_request(key)
            if args:
                _tui_debug(
                    "watchdog_terminal_request",
                    story_key=key,
                    args=args,
                )
                self._pending_attach_args = args
                self.exit()
                return

        active = [s for s in self.stories if s["status"] in ("active", "paused")]
        for s in active:
            key = s["story_key"]

            if has_stage_done(s) and not is_story_running(key):
                validation = validate_stage_done(s)
                if validation.status == DoneStatus.OK:
                    db.update_story(key, status="active")
                    try:
                        resume_story(key)
                    except Exception:
                        pass
            elif not has_stage_done(s) and not is_story_running(key):
                # Check CLI exit marker — show error state in UI
                cli_state = resolve_cli_exit_state(s)
                if cli_state == CliExitState.EXITED_WITHOUT_DONE:
                    _tui_debug(
                        "watchdog_cli_exit_detected",
                        story_key=key,
                    )

        # Unblock sub-stories whose dependencies are complete
        from ..orchestrator.graph import start_story_async

        blocked_stories = [
            s for s in self.stories if s["status"] == "blocked" and s.get("parent_key")
        ]
        for story in blocked_stories:
            parent_key = story["parent_key"]
            siblings = db.get_sub_stories(parent_key)
            completed_keys = {
                c["story_key"] for c in siblings if c["status"] in ("completed",)
            }
            events = db.get_story_events(parent_key)
            deps = []
            for ev in events:
                if ev["event_type"] == "delegate" and ev.get("payload"):
                    import json as _json

                    payload = (
                        _json.loads(ev["payload"])
                        if isinstance(ev["payload"], str)
                        else ev["payload"]
                    )
                    if payload.get("sub_key") == story["story_key"]:
                        deps = payload.get("depends_on", [])
                        break
            required = {f"{parent_key}-{d}" for d in deps}
            if required and required.issubset(completed_keys):
                db.update_story(story["story_key"], status="active")
                db.log_event(
                    story["story_key"], "", "unblocked", {"deps_met": list(required)}
                )
                start_story_async(story["story_key"])

        # Check for parents waiting on subtasks
        TERMINAL_STATES = ("completed", "failed", "blocked")
        pending_parents = db.get_pending_parents()
        for parent in pending_parents:
            children = db.get_sub_stories(parent["story_key"])
            incomplete = [c for c in children if c["status"] not in TERMINAL_STATES]
            if not incomplete:
                conn = db.get_conn()
                updated = conn.execute(
                    "UPDATE story SET status = 'active' WHERE story_key = ? AND status = 'waiting_subtasks'",
                    (parent["story_key"],),
                ).rowcount
                conn.commit()
                conn.close()
                if updated:
                    db.log_event(
                        parent["story_key"],
                        "",
                        "subtasks_completed",
                        {
                            "children": [c["story_key"] for c in children],
                        },
                    )
                    try:
                        resume_story(parent["story_key"])
                    except Exception:
                        pass

        new_interval = 3 if active else 30
        if new_interval != self._watchdog_interval:
            self._watchdog_interval = new_interval
```

- [ ] **Step 3: Run lint and tests**

Run: `ruff check src/story_lifecycle/cli/tui.py`
Run: `python -m pytest tests/test_entry_decisions.py -v`
Expected: No lint errors, all tests pass

- [ ] **Step 4: Commit**

```bash
git add src/story_lifecycle/cli/tui.py
git commit -m "feat: integrate CLI exit marker into watchdog and startup sweep"
```

---

### Task 11: Final verification — full test run + lint + import check

**Files:**
- All modified files

- [ ] **Step 1: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 2: Run lint on all source**

Run: `ruff check src/`
Expected: No errors

- [ ] **Step 3: Verify imports are clean**

Run: `python -c "from story_lifecycle.orchestrator.entry import resolve_stage_state, decide_action, TtydSessionBackend, entry_action_notice, CliExitState, WorkspaceState, resolve_cli_exit_state, cli_exit_marker_path; print('OK')"`
Expected: prints `OK`

Run: `python -c "from story_lifecycle.terminal.ttyd import SessionState, resolve_session_state; print('OK')"`
Expected: prints `OK`

- [ ] **Step 4: Verify test count**

Run: `python -m pytest tests/test_entry_decisions.py -v --co`
Expected: 35+ tests (original ~20 + new SessionState tests + expanded resolver/decider/notice tests)

- [ ] **Step 5: Run entry module as standalone import check**

Run: `python -c "from story_lifecycle.orchestrator.entry import StageEntryState, StageEntryAction; print(f'{len(StageEntryState)} states, {len(StageEntryAction)} actions')"`
Expected: `11 states, 15 actions`
