# TUI n/r/e/.done/session 决策逻辑 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 TUI 中 n/r/e 入口行为提取为可单测纯决策逻辑，统一 .done 处理，修复 e 误启动 Claude 和 Windows 不可靠 session 创建问题。

**Architecture:** 新增 `orchestrator/entry.py`，包含三层：.done helpers → SessionBackend Protocol + TtydSessionBackend → 纯决策函数 resolve/decide。TUI 的 e/r handler 改为调用决策函数后执行对应 action。

**Tech Stack:** Python 3.11+, pytest, Textual (仅 TUI 层), 现有 ttyd 模块

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `src/story_lifecycle/orchestrator/entry.py` | Create | .done helpers, SessionBackend Protocol, TtydSessionBackend, resolve_stage_state, decide_action |
| `tests/test_entry_decisions.py` | Create | 决策表全量测试 + .done helper 测试 + SessionBackend mock 测试 |
| `src/story_lifecycle/cli/tui.py` | Modify | e/r handler 改用决策函数; startup_sweep/watchdog 用 has_stage_done |

---

### Task 1: .done helpers

**Files:**
- Create: `src/story_lifecycle/orchestrator/entry.py`
- Test: `tests/test_entry_decisions.py`

- [ ] **Step 1: Write the failing tests for .done helpers**

```python
# tests/test_entry_decisions.py
"""Tests for TUI entry decision logic — .done helpers, state resolver, action decider."""

import json
import pytest
from pathlib import Path

from story_lifecycle.orchestrator.entry import (
    stage_done_file,
    has_stage_done,
    validate_stage_done,
    DoneStatus,
)


def _make_story(workspace: str, story_key: str = "TEST-001", stage: str = "design"):
    return {
        "story_key": story_key,
        "current_stage": stage,
        "workspace": workspace,
        "status": "active",
    }


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
        done.write_text(
            '```json\n{"summary": "wrapped"}\n```', encoding="utf-8"
        )
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_entry_decisions.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'story_lifecycle.orchestrator.entry'`

- [ ] **Step 3: Write .done helper implementation**

```python
# src/story_lifecycle/orchestrator/entry.py
"""TUI entry decision logic — .done helpers, SessionBackend, state resolver, action decider."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal, Protocol


# ---------------------------------------------------------------------------
# Layer 1: .done helpers
# ---------------------------------------------------------------------------


def stage_done_file(story: dict) -> Path:
    ws = story.get("workspace", "")
    key = story.get("story_key", "")
    stage = story.get("current_stage", "")
    return Path(ws) / ".story-done" / key / f"{stage}.json"


def has_stage_done(story: dict) -> bool:
    return stage_done_file(story).exists()


class DoneStatus(Enum):
    OK = "ok"
    CORRUPTED = "corrupted"
    MISSING = "missing"


@dataclass
class DoneValidationResult:
    status: DoneStatus
    data: dict | None = None
    error: str | None = None


def validate_stage_done(story: dict) -> DoneValidationResult:
    done = stage_done_file(story)
    if not done.exists():
        return DoneValidationResult(status=DoneStatus.MISSING)

    from .nodes import robust_json_parse

    try:
        data = robust_json_parse(done)
        return DoneValidationResult(status=DoneStatus.OK, data=data)
    except Exception as exc:
        return DoneValidationResult(
            status=DoneStatus.CORRUPTED, error=str(exc)
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_entry_decisions.py -v`
Expected: All .done helper tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/story_lifecycle/orchestrator/entry.py tests/test_entry_decisions.py
git commit -m "feat: add .done helpers (stage_done_file, has_stage_done, validate_stage_done)"
```

---

### Task 2: SessionBackend Protocol + TtydSessionBackend

**Files:**
- Modify: `src/story_lifecycle/orchestrator/entry.py`
- Modify: `tests/test_entry_decisions.py`

- [ ] **Step 1: Write the failing tests for TtydSessionBackend**

Append to `tests/test_entry_decisions.py`:

```python
from story_lifecycle.orchestrator.entry import (
    TtydSessionBackend,
)


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_entry_decisions.py::TestTtydSessionBackend -v`
Expected: FAIL — `ImportError: cannot import name 'TtydSessionBackend'`

- [ ] **Step 3: Add SessionBackend Protocol + TtydSessionBackend to entry.py**

Append to `src/story_lifecycle/orchestrator/entry.py`:

```python
# ---------------------------------------------------------------------------
# Layer 2: SessionBackend
# ---------------------------------------------------------------------------


class SessionBackend(Protocol):
    def is_healthy(self, session_id: str) -> bool: ...
    def attach_foreground(self, session_id: str) -> list[str]: ...
    def launch_independent_terminal(
        self, story_key: str, workspace: str, launch_cmd: str, prompt_file: str
    ) -> None: ...


class TtydSessionBackend:
    """Default implementation wrapping the ttyd module."""

    def is_healthy(self, session_id: str) -> bool:
        from ..terminal import ttyd

        return ttyd.session_alive(session_id)

    def attach_foreground(self, session_id: str) -> list[str]:
        from ..terminal import ttyd

        return ttyd.attach_args(session_id)

    def launch_independent_terminal(
        self, story_key: str, workspace: str, launch_cmd: str, prompt_file: str
    ) -> None:
        from ..terminal import ttyd

        ttyd.launch_cli(story_key, workspace, launch_cmd, prompt_file)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_entry_decisions.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/story_lifecycle/orchestrator/entry.py tests/test_entry_decisions.py
git commit -m "feat: add SessionBackend Protocol and TtydSessionBackend"
```

---

### Task 3: 纯决策函数 — resolve_stage_state + decide_action

**Files:**
- Modify: `src/story_lifecycle/orchestrator/entry.py`
- Modify: `tests/test_entry_decisions.py`

- [ ] **Step 1: Write the failing tests for resolve_stage_state and decide_action**

Append to `tests/test_entry_decisions.py`:

```python
from story_lifecycle.orchestrator.entry import (
    StageEntryState,
    StageEntryAction,
    resolve_stage_state,
    decide_action,
)


class FakeBackend:
    """Mock SessionBackend for testing."""

    def __init__(self, healthy: bool = False):
        self._healthy = healthy

    def is_healthy(self, session_id: str) -> bool:
        return self._healthy

    def attach_foreground(self, session_id: str) -> list[str]:
        return ["echo", "attach", session_id]

    def launch_independent_terminal(self, story_key, workspace, launch_cmd, prompt_file):
        pass


FINISHED_STATUSES = ("completed", "failed", "aborted")


class TestResolveStageState:
    def test_story_finished_completed(self, tmp_path):
        story = _make_story(str(tmp_path), status="completed")
        assert resolve_stage_state(story, FakeBackend(), is_running=False) == StageEntryState.STORY_FINISHED

    def test_story_finished_failed(self, tmp_path):
        story = _make_story(str(tmp_path), status="failed")
        assert resolve_stage_state(story, FakeBackend(), is_running=False) == StageEntryState.STORY_FINISHED

    def test_story_finished_aborted(self, tmp_path):
        story = _make_story(str(tmp_path), status="aborted")
        assert resolve_stage_state(story, FakeBackend(), is_running=False) == StageEntryState.STORY_FINISHED

    def test_done_valid(self, tmp_path):
        story = _make_story(str(tmp_path), status="active")
        done = stage_done_file(story)
        done.parent.mkdir(parents=True, exist_ok=True)
        done.write_text('{"summary": "ok"}', encoding="utf-8")
        assert resolve_stage_state(story, FakeBackend(), is_running=True) == StageEntryState.DONE

    def test_done_corrupted(self, tmp_path):
        story = _make_story(str(tmp_path), status="active")
        done = stage_done_file(story)
        done.parent.mkdir(parents=True, exist_ok=True)
        done.write_text("BROKEN{{{", encoding="utf-8")
        assert resolve_stage_state(story, FakeBackend(), is_running=False) == StageEntryState.DONE_CORRUPTED

    def test_running_healthy(self, tmp_path):
        story = _make_story(str(tmp_path), status="active")
        assert resolve_stage_state(story, FakeBackend(healthy=True), is_running=True) == StageEntryState.RUNNING_HEALTHY

    def test_running_dead(self, tmp_path):
        story = _make_story(str(tmp_path), status="active")
        assert resolve_stage_state(story, FakeBackend(healthy=False), is_running=True) == StageEntryState.RUNNING_DEAD

    def test_idle(self, tmp_path):
        story = _make_story(str(tmp_path), status="active")
        assert resolve_stage_state(story, FakeBackend(healthy=False), is_running=False) == StageEntryState.IDLE

    def test_done_takes_priority_over_running_dead(self, tmp_path):
        """Even if session is dead, valid .done means DONE state."""
        story = _make_story(str(tmp_path), status="active")
        done = stage_done_file(story)
        done.parent.mkdir(parents=True, exist_ok=True)
        done.write_text('{"summary": "ok"}', encoding="utf-8")
        assert resolve_stage_state(story, FakeBackend(healthy=False), is_running=True) == StageEntryState.DONE


# Decision table: (state, user_action) -> expected action
DECISION_TABLE = [
    # state                    user_action  expected_action
    (StageEntryState.DONE,             "e", StageEntryAction.PROMPT_DONE_PRESS_R),
    (StageEntryState.DONE,             "r", StageEntryAction.START_OR_RESUME),
    (StageEntryState.DONE_CORRUPTED,   "e", StageEntryAction.PROMPT_FIX_DONE),
    (StageEntryState.DONE_CORRUPTED,   "r", StageEntryAction.PROMPT_FIX_DONE),
    (StageEntryState.RUNNING_HEALTHY,  "e", StageEntryAction.ATTACH),
    (StageEntryState.RUNNING_HEALTHY,  "r", StageEntryAction.NOOP),
    (StageEntryState.RUNNING_DEAD,     "e", StageEntryAction.PROMPT_PRESS_R),
    (StageEntryState.RUNNING_DEAD,     "r", StageEntryAction.START_OR_RESUME),
    (StageEntryState.IDLE,             "e", StageEntryAction.PROMPT_PRESS_R),
    (StageEntryState.IDLE,             "r", StageEntryAction.START_OR_RESUME),
    (StageEntryState.STORY_FINISHED,   "e", StageEntryAction.NOOP),
    (StageEntryState.STORY_FINISHED,   "r", StageEntryAction.NOOP),
]


class TestDecideAction:
    @pytest.mark.parametrize("state,user_action,expected", DECISION_TABLE)
    def test_decision_table(self, state, user_action, expected):
        assert decide_action(state, user_action) == expected

    def test_invalid_user_action_raises(self):
        with pytest.raises(ValueError):
            decide_action(StageEntryState.IDLE, "x")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_entry_decisions.py::TestResolveStageState tests/test_entry_decisions.py::TestDecideAction -v`
Expected: FAIL — `ImportError: cannot import name 'StageEntryState'`

- [ ] **Step 3: Add enums, resolve_stage_state, and decide_action to entry.py**

Append to `src/story_lifecycle/orchestrator/entry.py`:

```python
# ---------------------------------------------------------------------------
# Layer 3: State resolver + action decider
# ---------------------------------------------------------------------------


_FINISHED_STATUSES = frozenset({"completed", "failed", "aborted"})


class StageEntryState(Enum):
    DONE = "done"
    DONE_CORRUPTED = "done_corrupted"
    RUNNING_HEALTHY = "running_healthy"
    RUNNING_DEAD = "running_dead"
    IDLE = "idle"
    STORY_FINISHED = "story_finished"


class StageEntryAction(Enum):
    ATTACH = "attach"
    START_OR_RESUME = "start_or_resume"
    PROMPT_PRESS_R = "prompt_press_r"
    PROMPT_DONE_PRESS_R = "prompt_done_press_r"
    PROMPT_FIX_DONE = "prompt_fix_done"
    NOOP = "noop"


def resolve_stage_state(
    story: dict,
    backend: SessionBackend,
    is_running: bool,
) -> StageEntryState:
    status = story.get("status", "")

    # Priority 1: terminal story states
    if status in _FINISHED_STATUSES:
        return StageEntryState.STORY_FINISHED

    # Priority 2: .done file (overrides running/session state)
    validation = validate_stage_done(story)
    if validation.status == DoneStatus.OK:
        return StageEntryState.DONE
    if validation.status == DoneStatus.CORRUPTED:
        return StageEntryState.DONE_CORRUPTED

    # Priority 3: running state + session health
    if is_running:
        session_id = _session_id_for_story(story)
        if backend.is_healthy(session_id):
            return StageEntryState.RUNNING_HEALTHY
        return StageEntryState.RUNNING_DEAD

    # Default: idle
    return StageEntryState.IDLE


def _session_id_for_story(story: dict) -> str:
    from ..terminal import ttyd

    return ttyd.session_name(story.get("story_key", ""))


# Decision table lookup
_ACTION_TABLE: dict[tuple[StageEntryState, str], StageEntryAction] = {
    (StageEntryState.DONE, "e"): StageEntryAction.PROMPT_DONE_PRESS_R,
    (StageEntryState.DONE, "r"): StageEntryAction.START_OR_RESUME,
    (StageEntryState.DONE_CORRUPTED, "e"): StageEntryAction.PROMPT_FIX_DONE,
    (StageEntryState.DONE_CORRUPTED, "r"): StageEntryAction.PROMPT_FIX_DONE,
    (StageEntryState.RUNNING_HEALTHY, "e"): StageEntryAction.ATTACH,
    (StageEntryState.RUNNING_HEALTHY, "r"): StageEntryAction.NOOP,
    (StageEntryState.RUNNING_DEAD, "e"): StageEntryAction.PROMPT_PRESS_R,
    (StageEntryState.RUNNING_DEAD, "r"): StageEntryAction.START_OR_RESUME,
    (StageEntryState.IDLE, "e"): StageEntryAction.PROMPT_PRESS_R,
    (StageEntryState.IDLE, "r"): StageEntryAction.START_OR_RESUME,
    (StageEntryState.STORY_FINISHED, "e"): StageEntryAction.NOOP,
    (StageEntryState.STORY_FINISHED, "r"): StageEntryAction.NOOP,
}


def decide_action(
    state: StageEntryState,
    user_action: Literal["e", "r"],
) -> StageEntryAction:
    key = (state, user_action)
    if key not in _ACTION_TABLE:
        raise ValueError(f"No action for state={state.value!r} user_action={user_action!r}")
    return _ACTION_TABLE[key]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_entry_decisions.py -v`
Expected: All 12 decision table parametrized tests PASS + resolve tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/story_lifecycle/orchestrator/entry.py tests/test_entry_decisions.py
git commit -m "feat: add resolve_stage_state and decide_action pure decision functions"
```

---

### Task 4: TUI action_enter_terminal — e 键改为纯观察

**Files:**
- Modify: `src/story_lifecycle/cli/tui.py` (lines 879-949)

This is the core behavioral change: `e` no longer creates sessions or launches CLI. It delegates to the decision function and only attaches or shows a prompt.

- [ ] **Step 1: Add imports at top of tui.py**

In `src/story_lifecycle/cli/tui.py`, add to existing imports (after line 31):

```python
from ..orchestrator.entry import (
    StageEntryAction,
    TtydSessionBackend,
    resolve_stage_state,
    decide_action,
    has_stage_done,
    validate_stage_done,
    DoneStatus,
)
```

- [ ] **Step 2: Add backend instance to StoryBoardApp.__init__**

In `src/story_lifecycle/cli/tui.py`, inside `StoryBoardApp.__init__` (around line 718), add:

```python
        self._session_backend = TtydSessionBackend()
```

- [ ] **Step 3: Replace action_enter_terminal with decision-logic driven version**

Replace the entire `action_enter_terminal` method (lines 879-949) in `src/story_lifecycle/cli/tui.py`:

```python
    def action_enter_terminal(self):
        if not self.stories:
            _tui_debug("enter_terminal_no_stories")
            return
        s = self.stories[self.selected_index]
        story_key = s["story_key"]
        session = ttyd.session_name(story_key)
        workspace = s.get("workspace", os.getcwd())

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
            # Only attach to existing healthy session
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

        elif action == StageEntryAction.PROMPT_DONE_PRESS_R:
            panel = self.query_one("#detail-panel")
            panel.update(
                f"[bold yellow]Stage {s.get('current_stage', '')} 已完成[/]\n\n"
                f"  .done 文件已存在，按 [bold cyan]r[/] 继续推进。"
            )
            panel.set_class(True, "visible")
            self._show_detail = True

        elif action == StageEntryAction.PROMPT_PRESS_R:
            panel = self.query_one("#detail-panel")
            panel.update(
                "[bold yellow]没有运行中的 session[/]\n\n"
                "  按 [bold cyan]r[/] 启动或恢复执行。"
            )
            panel.set_class(True, "visible")
            self._show_detail = True

        elif action == StageEntryAction.PROMPT_FIX_DONE:
            validation = validate_stage_done(s)
            panel = self.query_one("#detail-panel")
            panel.update(
                f"[bold red].done 文件损坏[/]\n\n"
                f"  {validation.error}\n\n"
                f"  请修复或删除: {validation.error}\n"
                f"  路径: {s.get('workspace', '')}/.story-done/{story_key}/{s.get('current_stage', '')}.json"
            )
            panel.set_class(True, "visible")
            self._show_detail = True

        # NOOP: do nothing (story finished)
```

- [ ] **Step 4: Remove _launch_cli_direct method from StoryBoardApp**

The `_launch_cli_direct` method (lines 951-995) is no longer called from `action_enter_terminal`. It may still be referenced elsewhere — verify with grep:

Run: `grep -n "_launch_cli_direct" src/story_lifecycle/cli/tui.py`

If only referenced in `action_enter_terminal` (now removed), delete the method entirely. If referenced elsewhere, keep it but add a comment that it should only be used for execution (r), not observation (e).

- [ ] **Step 5: Run lint and existing tests**

Run: `ruff check src/story_lifecycle/cli/tui.py`
Run: `python -m pytest tests/ -v`

Expected: No lint errors, existing tests still pass (they don't test TUI directly)

- [ ] **Step 6: Commit**

```bash
git add src/story_lifecycle/cli/tui.py
git commit -m "feat: rewrite e handler to use decision logic — observation only, never starts AI"
```

---

### Task 5: TUI action_resume_story — r 键走决策函数

**Files:**
- Modify: `src/story_lifecycle/cli/tui.py` (lines 1172-1182)

- [ ] **Step 1: Replace action_resume_story with decision-logic driven version**

Replace the entire `action_resume_story` method (lines 1172-1182) in `src/story_lifecycle/cli/tui.py`:

```python
    def action_resume_story(self):
        if not self.stories:
            return
        s = self.stories[self.selected_index]
        key = s["story_key"]

        from ..orchestrator.graph import is_story_running

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
            from ..orchestrator.graph import start_story_async

            if not is_story_running(key):
                start_story_async(key)
            self.refresh_stories()

        elif action == StageEntryAction.PROMPT_FIX_DONE:
            validation = validate_stage_done(s)
            panel = self.query_one("#detail-panel")
            panel.update(
                f"[bold red].done 文件损坏，无法恢复[/]\n\n"
                f"  {validation.error}\n\n"
                f"  请修复或删除后重试。"
            )
            panel.set_class(True, "visible")
            self._show_detail = True

        # NOOP: already running or story finished — do nothing
```

- [ ] **Step 2: Run lint and tests**

Run: `ruff check src/story_lifecycle/cli/tui.py`
Run: `python -m pytest tests/ -v`

Expected: No lint errors, all tests pass

- [ ] **Step 3: Commit**

```bash
git add src/story_lifecycle/cli/tui.py
git commit -m "feat: rewrite r handler to use decision logic — execution entry point"
```

---

### Task 6: TUI _startup_sweep 和 watchdog_check 统一 .done 判断

**Files:**
- Modify: `src/story_lifecycle/cli/tui.py` (lines 1249-1282)

- [ ] **Step 1: Replace _startup_sweep to use has_stage_done + validate_stage_done**

Replace the `_startup_sweep` method (lines 1249-1264):

```python
    def _startup_sweep(self):
        """On startup, check all non-terminal stories for existing done files and resume."""
        from ..orchestrator.graph import start_story_async, is_story_running

        for s in self.stories:
            if s["status"] in _FINISHED_STATUSES:
                continue
            key = s["story_key"]
            if has_stage_done(s):
                validation = validate_stage_done(s)
                if validation.status == DoneStatus.OK and not is_story_running(key):
                    db.update_story(key, status="active", last_error=None)
                    start_story_async(key)
```

Add the `_FINISHED_STATUSES` constant at module level in tui.py (near the top, after imports):

```python
_FINISHED_STATUSES = frozenset({"completed", "failed", "aborted"})
```

- [ ] **Step 2: Replace watchdog_check to use has_stage_done + validate_stage_done**

Replace the watchdog `.done` check section (lines 1269-1282, inside `watchdog_check`):

```python
            if has_stage_done(s) and not is_story_running(key):
                validation = validate_stage_done(s)
                if validation.status == DoneStatus.OK:
                    db.update_story(key, status="active")
                    try:
                        resume_story(key)
                    except Exception:
                        pass
```

Note: only replace the `.done` checking block. Keep the sub-story unblocking and parent completion logic that follows unchanged.

- [ ] **Step 3: Run lint and tests**

Run: `ruff check src/story_lifecycle/cli/tui.py`
Run: `python -m pytest tests/ -v`

Expected: No lint errors, all tests pass

- [ ] **Step 4: Commit**

```bash
git add src/story_lifecycle/cli/tui.py
git commit -m "refactor: unify .done checks in startup_sweep and watchdog via entry helpers"
```

---

### Task 7: Final verification — full test run + lint

**Files:**
- All modified files

- [ ] **Step 1: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: All tests pass including `tests/test_entry_decisions.py`

- [ ] **Step 2: Run lint on all source**

Run: `ruff check src/`
Expected: No errors

- [ ] **Step 3: Verify imports are clean — entry.py has no circular deps**

Run: `python -c "from story_lifecycle.orchestrator.entry import resolve_stage_state, decide_action, TtydSessionBackend, has_stage_done, validate_stage_done; print('OK')"`
Expected: prints `OK`

- [ ] **Step 4: Verify test count matches spec**

Run: `python -m pytest tests/test_entry_decisions.py -v --co`
Expected: ~20+ tests (6 .done helper + 3 backend + 8 resolve + 12 decide_action + 1 error case)
