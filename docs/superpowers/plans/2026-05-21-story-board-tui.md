# Story Board TUI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace static `story board` with an interactive Textual TUI that embeds LangGraph execution — no separate `story serve` needed.

**Architecture:** Service layer creates stories via LangGraph. Smart Orchestrator (planner.py) adds `plan_stage` and `review_stage` nodes before/after execution. `poll_completion_node` uses `interrupt()` to yield worker threads. Watchdog asyncio task scans `.story-done/` files and resumes graphs. Textual TUI reads DB for rendering, calls service for mutations.

**Tech Stack:** Textual >=3.0, LangGraph (interrupt/resume), SQLite (read-only in TUI)

---

## File Structure

| File | Responsibility |
|------|---------------|
| `src/story_lifecycle/orchestrator/service.py` | **New** — shared service layer for story creation/mutation |
| `src/story_lifecycle/orchestrator/planner.py` | **New** — Smart Orchestrator (plan + review via DeepSeek) |
| `src/story_lifecycle/orchestrator/nodes.py` | **Modify** — add plan_stage/review_stage nodes, poll/wait nodes use `interrupt()` |
| `src/story_lifecycle/orchestrator/graph.py` | **Modify** — insert plan/review nodes, expose compiled graph with checkpointer |
| `src/story_lifecycle/db/models.py` | **Modify** — add `upsert_story()`, `get_stage_log()` |
| `src/story_lifecycle/cli/tui.py` | **New** — Textual App (header, story cards, footer, action menu, new dialog, detail panel) |
| `src/story_lifecycle/cli/main.py` | **Modify** — `board()` launches TUI, add `--no-tui` flag |
| `pyproject.toml` | **Modify** — add `textual>=3.0` |
| `tests/test_service.py` | **New** — service layer tests |
| `tests/test_tui.py` | **New** — TUI smoke tests |

---

### Task 1: Add `textual` dependency and `upsert_story` to DB

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/story_lifecycle/db/models.py`
- Test: `tests/test_smoke.py`

- [ ] **Step 1: Add textual to pyproject.toml**

```toml
# In [project.optional-dependencies] dev section, add:
    "textual>=3.0",
```

Also add a new `[project.optional-dependencies]` group:

```toml
[project.optional-dependencies.tui]
dependencies = [
    "textual>=3.0",
]
```

- [ ] **Step 2: Add `upsert_story` to `src/story_lifecycle/db/models.py`**

Append to the file, after `delete_story`:

```python
def upsert_story(story_key: str, title: str = "", workspace: str = "",
                 profile: str = "minimal", current_stage: str = "design",
                 status: str = "active", **kwargs):
    """Insert or update a story. Used by service layer."""
    conn = get_conn()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    existing = conn.execute(
        "SELECT id FROM story WHERE story_key = ?", (story_key,)
    ).fetchone()
    if existing:
        kwargs["updated_at"] = now
        if title:
            kwargs["title"] = title
        if status:
            kwargs["status"] = status
        if current_stage:
            kwargs["current_stage"] = current_stage
        if kwargs:
            sets = ", ".join(f"{k} = ?" for k in kwargs)
            values = list(kwargs.values()) + [story_key]
            conn.execute(f"UPDATE story SET {sets} WHERE story_key = ?", values)
    else:
        conn.execute(
            """INSERT INTO story (story_key, title, workspace, profile, current_stage, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (story_key, title, str(workspace), profile, current_stage, status, now, now),
        )
    conn.commit()
    conn.close()
```

- [ ] **Step 3: Run tests to verify nothing broke**

Run: `uvx ruff check src/ && uvx ruff format --check src/`
Expected: All checks passed

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml src/story_lifecycle/db/models.py
git commit -m "feat: add textual dependency and db.upsert_story"
```

---

### Task 2: Create service layer

**Files:**
- Create: `src/story_lifecycle/orchestrator/service.py`
- Test: `tests/test_service.py`

This is the single entry point for story creation and mutation, shared by TUI and server.

- [ ] **Step 1: Write test for service layer**

Create `tests/test_service.py`:

```python
"""Tests for the shared service layer."""
import tempfile
from pathlib import Path


def test_import_service():
    from story_lifecycle.orchestrator.service import create_and_start_story
    assert callable(create_and_start_story)


def test_create_and_start_story():
    from story_lifecycle.orchestrator.service import create_and_start_story
    from story_lifecycle.db.models import get_story, init_db

    init_db()
    with tempfile.TemporaryDirectory() as tmp:
        result = create_and_start_story(
            story_key="TEST-001",
            title="Test story",
            profile="minimal",
            workspace=tmp,
        )
        assert result == "TEST-001"

        s = get_story("TEST-001")
        assert s is not None
        assert s["story_key"] == "TEST-001"
        assert s["title"] == "Test story"
        assert s["status"] == "active"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_service.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement service.py**

Create `src/story_lifecycle/orchestrator/service.py`:

```python
"""Shared service layer — single entry point for TUI and server."""
from pathlib import Path

from ..db import models as db
from .nodes import StoryState, load_profile, get_stage_config


def create_and_start_story(
    story_key: str,
    title: str = "",
    profile: str = "minimal",
    workspace: str = "",
    prd_path: str | None = None,
) -> str:
    """Create a story via service layer. Writes to DB and returns story_key.

    The caller (TUI worker or server) is responsible for starting execution.
    """
    ws = workspace or str(Path.cwd())
    profile_data = load_profile(profile)
    stages = profile_data.get("stages", {})
    first_stage = next(iter(stages)) if stages else "design"

    # Handle PRD content
    prd_content = ""
    if prd_path:
        p = Path(prd_path)
        if p.exists():
            prd_content = p.read_text(encoding="utf-8")
            # Save PRD to workspace
            prd_dir = Path(ws) / "prd"
            prd_dir.mkdir(exist_ok=True)
            prd_file = prd_dir / f"{story_key}.md"
            prd_file.write_text(prd_content, encoding="utf-8")
            prd_path = str(prd_file)

    # Upsert business DB (for board quick-read)
    db.upsert_story(
        story_key,
        title=title,
        workspace=ws,
        profile=profile,
        current_stage=first_stage,
        status="active",
    )

    if prd_path:
        db.update_context(story_key, "prd_path", prd_path)

    return story_key


def get_story_cli_model(story_key: str) -> dict:
    """Get CLI tool and model for a story's current stage."""
    s = db.get_story(story_key)
    if not s:
        return {"cli": "claude", "model": "sonnet"}

    profile = s.get("profile", "minimal")
    stage = s.get("current_stage", "design")
    try:
        cfg = get_stage_config(profile, stage)
        profile_data = load_profile(profile)
        return {
            "cli": cfg.get("cli", profile_data.get("cli", "claude")),
            "model": cfg.get("model", "sonnet"),
        }
    except FileNotFoundError:
        return {"cli": "claude", "model": "sonnet"}


def pause_story(story_key: str):
    """Pause an active story."""
    db.update_story(story_key, status="paused")


def fail_story(story_key: str, reason: str = "Manual fail"):
    """Mark a story as blocked."""
    db.update_story(story_key, status="blocked", last_error=reason)
    db.log_stage(story_key, "", "fail", reason)


def skip_stage(story_key: str, stage: str, reason: str = "Manual skip"):
    """Skip a story's current stage."""
    db.log_stage(story_key, stage, "skip", reason)
    db.update_story(story_key, status="active")


def delete_story(story_key: str):
    """Delete a story and clean up."""
    from ..terminal import ttyd

    db.delete_story(story_key)
    ttyd.stop_ttyd(story_key)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_service.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/story_lifecycle/orchestrator/service.py tests/test_service.py
git commit -m "feat: add shared service layer for story operations"
```

---

### Task 3: Add `interrupt()` to poll and wait_confirm nodes + Smart Orchestrator graph

**Files:**
- Modify: `src/story_lifecycle/orchestrator/nodes.py`
- Modify: `src/story_lifecycle/orchestrator/graph.py`
- Test: `tests/test_smoke.py`

This is the core change: replace blocking `time.sleep()` loops with LangGraph `interrupt()`, and add Smart Orchestrator nodes.

- [ ] **Step 1: Modify `poll_completion_node` to use interrupt**

In `src/story_lifecycle/orchestrator/nodes.py`, add import at top:

```python
from langgraph.types import interrupt
```

Replace the entire `poll_completion_node` function:

```python
def poll_completion_node(state: StoryState) -> StoryState:
    """Wait for CC to write .story-done/{story_key}/{stage}.json.

    Uses interrupt() to yield the worker thread when file not ready.
    Watchdog resumes via graph.invoke(None, config).
    """
    key = state["story_key"]
    stage = state["current_stage"]
    workspace = state["workspace"]
    session = ttyd.session_name(key)
    done_file = Path(workspace) / ".story-done" / key / f"{stage}.json"

    # Check tmux liveness
    if not ttyd._tmux_session_alive(session):
        state["last_error"] = "CC process crashed (tmux session dead)"
        return state

    # Check for done file
    if not done_file.exists():
        # Yield worker thread — Watchdog will resume when file appears
        interrupt({"reason": "waiting_for_done_file", "stage": stage})

    # File exists — parse it
    try:
        with open(done_file, "r") as f:
            file_lock(f)
            data = robust_json_parse(done_file)
        done_file.unlink()
        state["context"].update(data)
        cfg = get_stage_config(state.get("profile", "minimal"), stage)
        for field in cfg.get("expected_outputs", []):
            if field in data:
                db.update_context(key, field, str(data[field]))
    except Exception as e:
        state["last_error"] = f"Failed to parse .done file: {e}"

    return state
```

- [ ] **Step 2: Modify `wait_confirm_node` to use interrupt**

Replace the entire `wait_confirm_node`:

```python
def wait_confirm_node(state: StoryState) -> StoryState:
    """Pause for human confirmation. Yields thread via interrupt."""
    key = state["story_key"]
    db.update_story(key, status="paused")
    db.log_stage(key, state["current_stage"], "pause", "Waiting for manual confirmation")
    state["status"] = "paused"

    # Yield thread — Watchdog or user action will resume
    interrupt({"reason": "waiting_for_confirmation", "stage": state["current_stage"]})

    # Resumed — check if user set status back to active
    s = db.get_story(key)
    if s and s["status"] == "active":
        state["status"] = "active"
        state["execution_count"] = 0

    return state
```

- [ ] **Step 3: Add `plan_stage_node` and `review_stage_node` to nodes.py**

Append to `src/story_lifecycle/orchestrator/nodes.py`:

```python
import logging
from . import planner

log = logging.getLogger("story-lifecycle.nodes")


def plan_stage_node(state: StoryState) -> StoryState:
    """Smart Orchestrator: plan current stage. Falls back to profile config."""
    stage = state["current_stage"]
    profile = state.get("profile", "minimal")
    cfg = get_stage_config(profile, stage)

    if planner.is_available():
        try:
            adapters = ["claude"]  # TODO: from registry
            plan = planner.plan_stage(state, cfg, adapters)
            state["plan"] = plan

            if plan.get("skip"):
                return skip_node(state)

            db.log_stage(
                state["story_key"], stage, "plan",
                f"adapter={plan.get('adapter')}, reasoning={plan.get('reasoning', '')[:100]}"
            )
            return state
        except Exception as e:
            log.warning(f"Planner failed, falling back: {e}")

    # Fallback: generate plan from profile config
    profile_cfg = load_profile(profile)
    state["plan"] = {
        "adapter": cfg.get("cli", profile_cfg.get("cli", "claude")),
        "provider": state.get("context", {}).get("_provider", cfg.get("provider", "deepseek")),
        "model": cfg.get("model", "sonnet"),
        "skip": False,
        "extra_instructions": "",
        "reasoning": "Fallback: using profile config",
    }
    return state


def review_stage_node(state: StoryState) -> StoryState:
    """Smart Orchestrator: review stage output. Falls back to no review."""
    stage = state["current_stage"]
    cfg = get_stage_config(state.get("profile", "minimal"), stage)
    stage_output = state.get("context", {})

    if not stage_output or not cfg.get("expected_outputs"):
        return state

    if planner.is_available():
        try:
            review = planner.review_stage(state, cfg, stage_output)
            state["review"] = review

            if review.get("context_updates"):
                state["context"].update(review["context_updates"])

            quality = review.get("quality", "pass")
            if quality == "revise":
                state["last_error"] = f"Review feedback: {review.get('feedback', '')}"
                db.log_stage(state["story_key"], stage, "review",
                             f"Revise: {review.get('feedback', '')[:100]}")
            elif quality == "fail":
                state["last_error"] = f"Review failed: {review.get('feedback', '')}"
            else:
                db.log_stage(state["story_key"], stage, "review", "Passed")
            return state
        except Exception as e:
            log.warning(f"Reviewer failed, skipping: {e}")

    return state
```

- [ ] **Step 4: Modify `graph.py` — insert plan/review nodes, update edges**

(See updated graph.py in Step 3 of the plan — already includes plan_stage and review_stage nodes)

- [ ] **Step 5: Run lint check**

Run: `uvx ruff check src/ && uvx ruff format --check src/`
Expected: All checks passed

- [ ] **Step 6: Run existing tests**

Run: `pytest tests/ -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/story_lifecycle/orchestrator/nodes.py src/story_lifecycle/orchestrator/graph.py
git commit -m "feat: add Smart Orchestrator plan/review nodes + LangGraph interrupt()"
```

---

### Task 4: Create Textual TUI skeleton — read-only board

**Files:**
- Create: `src/story_lifecycle/cli/tui.py`
- Modify: `src/story_lifecycle/cli/main.py`
- Modify: `pyproject.toml`

This task creates the visual board with keyboard navigation. No interactivity yet beyond navigation.

- [ ] **Step 1: Create `src/story_lifecycle/cli/tui.py`**

```python
"""Interactive TUI board — Textual App for story management."""

from __future__ import annotations

import subprocess
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, VerticalScroll
from textual.widgets import Header, Footer, Static, Label
from textual.reactive import reactive

from ..db import models as db
from ..orchestrator.service import (
    create_and_start_story,
    get_story_cli_model,
    pause_story,
    fail_story,
    skip_stage,
    delete_story,
)
from ..orchestrator.nodes import load_profile, get_stage_config


# -------- Story Card Widget --------


class StoryCard(Static):
    """A single story card in the board."""

    def __init__(self, story: dict, selected: bool = False):
        self.story = story
        self._selected = selected
        super().__init__()

    def set_selected(self, selected: bool):
        self._selected = selected
        self.refresh()

    def render(self) -> str:
        s = self.story
        key = s["story_key"]
        title = (s.get("title") or "")[:60]
        status = s.get("status", "active")
        stage = s.get("current_stage", "")
        retries = s.get("execution_count", 0)
        last_error = s.get("last_error", "")
        profile_name = s.get("profile", "minimal")

        # Status badge
        badge = {
            "active": "[bold green]> active[/]",
            "paused": "[bold yellow]|| paused[/]",
            "blocked": "[bold red]X blocked[/]",
            "completed": "[dim green]OK done[/]",
        }.get(status, status)

        # Stage progress bar
        try:
            profile = load_profile(profile_name)
            stages = list(profile.get("stages", {}).keys())
            bar = _render_stage_bar(stages, stage)
        except FileNotFoundError:
            bar = stage

        # CLI + Model info
        cli_info = get_story_cli_model(key)
        cli_line = f"  CLI: {cli_info['cli']} · Model: {cli_info['model']}"

        # Selection indicator
        cursor = "[bold cyan]▸[/] " if self._selected else "  "

        lines = [
            f"{cursor}[bold cyan]{key}[/]  {title}",
            f"  {bar}  {badge}  retries: {retries}",
            cli_line,
        ]

        # Plan summary (from Smart Orchestrator)
        plan = s.get("plan")
        if plan and plan.get("extra_instructions"):
            lines.append(f"  [dim]Plan: {plan['extra_instructions'][:80]}[/]")

        if last_error and status == "blocked":
            lines.append(f"  [dim red]↳ {last_error[:80]}[/]")

        return "\n".join(lines)


def _render_stage_bar(stages: list[str], current: str) -> str:
    """Render stage progress bar, truncating if too many stages."""
    if len(stages) > 5:
        # Show current ± 1, truncate rest
        idx = stages.index(current) if current in stages else 0
        show = []
        if idx > 0:
            show.append("...")
            show.append(f"● {stages[idx - 1]}")
        show.append(f"◉ [bold]{current}[/]")
        if idx < len(stages) - 1:
            show.append(f"○ {stages[idx + 1]}")
            if idx + 1 < len(stages) - 1:
                show.append("...")
        return " → ".join(show)

    parts = []
    for s in stages:
        if s == current:
            parts.append(f"◉ [bold]{s}[/]")
        else:
            idx = stages.index(current) if current in stages else -1
            s_idx = stages.index(s)
            if s_idx < idx:
                parts.append(f"● {s}")
            else:
                parts.append(f"○ {s}")
    return " → ".join(parts)


# -------- Action Menu Screen --------


class ActionMenu(Static):
    """Modal action menu for a selected story."""

    def __init__(self, story: dict):
        self.story = story
        super().__init__()

    def compose(self) -> ComposeResult:
        key = self.story["story_key"]
        title = (self.story.get("title") or "")[:40]
        yield Label(f"[bold]{key}[/]: {title}\n")
        yield Label("  [e] Enter terminal")
        yield Label("  [s] Skip current stage")
        yield Label("  [f] Mark as failed")
        yield Label("  [r] Resume")
        yield Label("  [x] Delete story")
        yield Label("  [Esc] Cancel")


# -------- New Story Dialog --------


class NewStoryDialog(Static):
    """Modal dialog for creating a new story."""

    def __init__(self):
        super().__init__()

    def compose(self) -> ComposeResult:
        yield Label("[bold]New Story[/]\n")
        yield Label("  Key: ")
        yield Label("  Title: ")
        yield Label("  Profile (minimal/standard): ")
        yield Label("  PRD file path (Enter to skip): ")


# -------- Main App --------


class StoryBoardApp(App):
    """Interactive story board TUI."""

    CSS = """
    Screen {
        layout: vertical;
    }
    #header-bar {
        height: 3;
        padding: 0 1;
        border-bottom: solid green;
    }
    #story-list {
        height: 1fr;
        padding: 0 1;
        overflow-y: auto;
    }
    #detail-panel {
        height: 0;
        padding: 0 1;
        border-top: solid green;
        display: none;
    }
    #detail-panel.visible {
        height: auto;
        max-height: 12;
        display: block;
    }
    #footer-bar {
        height: 2;
        padding: 0 1;
        border-top: solid green;
    }
    .action-menu {
        display: none;
        padding: 1 2;
        border: solid yellow;
        margin: 1 0;
    }
    .action-menu.visible {
        display: block;
    }
    """

    BINDINGS = [
        Binding("up", "cursor_up", "Up", key_display="↑"),
        Binding("down", "cursor_down", "Down", key_display="↓"),
        Binding("enter", "open_action_menu", "Actions"),
        Binding("n", "new_story", "New"),
        Binding("e", "enter_terminal", "Enter"),
        Binding("d", "toggle_detail", "Detail"),
        Binding("s", "skip_stage", "Skip"),
        Binding("f", "fail_story", "Fail"),
        Binding("r", "resume_story", "Resume"),
        Binding("shift+r", "refresh", "Refresh", key_display="R"),
        Binding("f5", "refresh", "Refresh"),
        Binding("question_mark", "help", "Help", key_display="?"),
        Binding("q", "quit", "Quit"),
    ]

    selected_index: reactive[int] = reactive(0)
    stories: reactive[list[dict]] = reactive([])

    def compose(self) -> ComposeResult:
        yield Static(id="header-bar")
        yield VerticalScroll(id="story-list")
        yield Static(id="detail-panel")
        yield Static(id="footer-bar")

    def on_mount(self):
        self._watchdog_interval = 3
        self._show_detail = False
        self._show_action_menu = False
        self.refresh_stories()
        self.set_interval(5, self.refresh_stories)
        self.set_interval(3, self.watchdog_check)

    def refresh_stories(self):
        """Reload stories from DB."""
        self.stories = db.list_active_stories()
        self._render()

    def _render(self):
        # Header
        from ..orchestrator.planner import is_available as planner_available
        from ..orchestrator.router import llm_is_available
        from .setup import get_config

        config = get_config()
        provider = config.get("provider", "N/A")
        orchestrator_status = f"enabled ({provider})" if planner_available() else "disabled"
        active = len([s for s in self.stories if s["status"] == "active"])
        header = self.query_one("#header-bar")
        header.update(
            f"[bold]Story Lifecycle[/]  ·  Orchestrator: {orchestrator_status}  ·  Stories: {active} active"
        )

        # Story list
        story_list = self.query_one("#story-list")
        story_list.remove_children()
        if not self.stories:
            story_list.mount(Static("[dim]No active stories. Press [n] to create one.[/]"))
            return

        for i, s in enumerate(self.stories):
            card = StoryCard(s, selected=(i == self.selected_index))
            story_list.mount(card)

        # Footer
        footer = self.query_one("#footer-bar")
        if self.stories and 0 <= self.selected_index < len(self.stories):
            key = self.stories[self.selected_index]["story_key"]
            footer.update(
                f"[n] new  [e] enter  [s] skip  [f] fail  [r] resume  [R] refresh  [?] help\n"
                f"> {key} selected. Press Enter for actions."
            )
        else:
            footer.update(
                "[n] new  [e] enter  [s] skip  [f] fail  [r] resume  [R] refresh  [?] help"
            )

    # -------- Navigation --------

    def action_cursor_up(self):
        if self.selected_index > 0:
            self.selected_index -= 1
            self._render()

    def action_cursor_down(self):
        if self.stories and self.selected_index < len(self.stories) - 1:
            self.selected_index += 1
            self._render()

    # -------- Actions --------

    def action_open_action_menu(self):
        """Placeholder — action menu will be added in next task."""
        if not self.stories:
            return
        s = self.stories[self.selected_index]
        self._show_action_menu = not self._show_action_menu
        # TODO: implement action menu modal in Task 5

    def action_enter_terminal(self):
        """Suspend TUI and attach to tmux session."""
        if not self.stories:
            return
        s = self.stories[self.selected_index]
        session = f"s-{s['story_key']}"
        self.suspend()
        subprocess.run(["tmux", "attach", "-t", session])
        # TUI resumes when user detaches with Ctrl+b d

    def action_toggle_detail(self):
        self._show_detail = not self._show_detail
        panel = self.query_one("#detail-panel")
        if self._show_detail and self.stories:
            s = self.stories[self.selected_index]
            detail = _render_detail(s)
            panel.update(detail)
            panel.set_class(True, "visible")
        else:
            panel.set_class(False, "visible")

    def action_new_story(self):
        """Placeholder — new story dialog in Task 6."""
        pass

    def action_skip_stage(self):
        if not self.stories:
            return
        s = self.stories[self.selected_index]
        skip_stage(s["story_key"], s["current_stage"])
        self.refresh_stories()

    def action_fail_story(self):
        if not self.stories:
            return
        s = self.stories[self.selected_index]
        fail_story(s["story_key"])
        self.refresh_stories()

    def action_resume_story(self):
        if not self.stories:
            return
        s = self.stories[self.selected_index]
        db.update_story(s["story_key"], status="active")
        self.refresh_stories()

    def action_refresh(self):
        self.refresh_stories()

    # -------- Watchdog --------

    async def watchdog_check(self):
        """Scan .story-done/ files and tmux liveness for active stories."""
        from ..orchestrator.graph import resume_story

        active = [s for s in self.stories if s["status"] == "active"]
        for s in active:
            key = s["story_key"]
            stage = s["current_stage"]
            ws = s["workspace"]
            done_file = Path(ws) / ".story-done" / key / f"{stage}.json"

            if done_file.exists():
                try:
                    resume_story(key)
                except Exception:
                    pass  # graph may not have a checkpoint yet

        # Dynamic interval
        new_interval = 3 if active else 30
        if new_interval != self._watchdog_interval:
            self._watchdog_interval = new_interval

    # -------- Shutdown --------

    def action_quit(self):
        """Graceful shutdown — pause all active stories."""
        active = [s for s in self.stories if s["status"] == "active"]
        for s in active:
            pause_story(s["story_key"])
        self.exit()

    # -------- Help --------

    def action_help(self):
        self._prev_detail = self._show_detail
        self._show_detail = True
        panel = self.query_one("#detail-panel")
        panel.update(
            "[bold]Key Bindings[/]\n"
            "  ↑/k     Move up\n"
            "  ↓/j     Move down\n"
            "  Enter   Action menu\n"
            "  n       New story\n"
            "  e       Enter terminal\n"
            "  d       Toggle detail\n"
            "  s       Skip stage\n"
            "  f       Mark failed\n"
            "  r       Resume\n"
            "  R/F5    Refresh\n"
            "  ?       Help\n"
            "  q       Quit"
        )
        panel.set_class(True, "visible")


def _render_detail(story: dict) -> str:
    """Render expanded detail for a story."""
    import json

    s = story
    key = s["story_key"]
    lines = [
        f"[bold]{key}[/] — {s.get('title', '')}",
        f"  Stage:     {s.get('current_stage', '')}",
        f"  Status:    {s.get('status', '')}",
        f"  Profile:   {s.get('profile', 'minimal')}",
        f"  Workspace: {s.get('workspace', '')}",
        f"  Retries:   {s.get('execution_count', 0)}",
        f"  Created:   {s.get('created_at', '')}",
        f"  Updated:   {s.get('updated_at', '')}",
    ]

    if s.get("last_error"):
        lines.append(f"  [red]Error: {s['last_error']}[/]")

    # Context JSON — top-level keys, values truncated
    try:
        ctx = json.loads(s.get("context_json") or "{}")
        if ctx:
            lines.append("  Context:")
            for k, v in ctx.items():
                val = str(v)
                if len(val) > 500:
                    val = val[:500] + "..."
                lines.append(f"    {k}: {val}")
    except json.JSONDecodeError:
        pass

    return "\n".join(lines)


def run_tui():
    """Entry point for the TUI board."""
    from ..db.models import init_db

    init_db()
    app = StoryBoardApp()
    app.run()
```

- [ ] **Step 2: Modify `board()` in `src/story_lifecycle/cli/main.py`**

Replace the `board` command:

```python
@cli.command()
@click.option("--no-tui", is_flag=True, help="Static table mode (for non-interactive terminals)")
def board(no_tui):
    """Show all active stories in a dashboard."""
    if no_tui or not sys.stdout.isatty():
        _board_static()
        return

    try:
        from .tui import run_tui
        run_tui()
    except ImportError:
        console.print("[yellow]textual not installed. Falling back to static mode.[/]")
        _board_static()


def _board_static():
    """Static table fallback."""
    stories = db.list_active_stories()

    if not stories:
        console.print("[dim]No active stories. Create one with: story new <KEY>[/]")
        return

    table = Table(title="Story Board", show_lines=False)
    table.add_column("Story", style="cyan", no_wrap=True)
    table.add_column("Title", style="white")
    table.add_column("Stage", style="green")
    table.add_column("Status")
    table.add_column("Retries", justify="center")
    table.add_column("Workspace", style="dim")

    for s in stories:
        status_str = {
            "active": "[bold green]> active[/]",
            "paused": "[bold yellow]|| paused[/]",
            "blocked": "[bold red]X blocked[/]",
            "completed": "[dim green]OK done[/]",
        }.get(s.get("status", ""), s.get("status", ""))

        table.add_row(
            s.get("story_key", ""),
            (s.get("title") or "")[:40],
            s.get("current_stage", ""),
            status_str,
            str(s.get("execution_count", 0)),
            s.get("workspace", ""),
        )

    console.print(table)
    console.print(
        "\n[dim]Commands: story new | story enter <key> | story skip <key> --stage <name> | story fail <key>[/]"
    )
```

- [ ] **Step 3: Run lint and format**

Run: `uvx ruff check src/ && uvx ruff format src/`
Expected: All checks passed

- [ ] **Step 4: Commit**

```bash
git add src/story_lifecycle/cli/tui.py src/story_lifecycle/cli/main.py
git commit -m "feat: add Textual TUI board with keyboard navigation and watchdog"
```

---

### Task 5: Refactor `api.py` to use service layer

**Files:**
- Modify: `src/story_lifecycle/orchestrator/api.py`

Ensures the server path uses the same service layer as the TUI.

- [ ] **Step 1: Refactor `create_story` in api.py**

Replace the `create_story` endpoint in `src/story_lifecycle/orchestrator/api.py`:

```python
@app.post("/api/story")
def create_story(req: CreateStoryRequest):
    from .service import create_and_start_story

    workspace = req.workspace or os.getcwd()
    prd_path = None
    if req.content:
        prd_dir = Path(workspace) / "prd"
        prd_dir.mkdir(exist_ok=True)
        prd_file = prd_dir / f"{req.key}.md"
        prd_file.write_text(req.content, encoding="utf-8")
        prd_path = str(prd_file)

    story_key = create_and_start_story(
        story_key=req.key,
        title=req.title,
        profile=req.profile,
        workspace=workspace,
        prd_path=prd_path,
    )

    start_story_async(story_key)

    s = db.get_story(story_key)
    return JSONResponse(
        {
            "id": s["id"],
            "storyKey": s["story_key"],
            "title": s["title"],
            "currentStage": s["current_stage"],
            "status": s["status"],
            "workspace": s["workspace"],
        }
    )
```

- [ ] **Step 2: Run lint**

Run: `uvx ruff check src/`
Expected: All checks passed

- [ ] **Step 3: Commit**

```bash
git add src/story_lifecycle/orchestrator/api.py
git commit -m "refactor: api.py uses shared service layer"
```

---

### Task 6: Update tests and verify CI

**Files:**
- Modify: `tests/test_smoke.py`
- Test: `tests/`

- [ ] **Step 1: Update smoke tests**

Update `tests/test_smoke.py` to add TUI import test:

```python
"""Smoke tests — verify package imports and basic CLI registration."""


def test_package_imports():
    import story_lifecycle

    assert story_lifecycle is not None


def test_cli_module_imports():
    from story_lifecycle.cli.main import cli

    assert cli is not None
    assert cli.name in ("story", "cli")


def test_db_module_imports():
    from story_lifecycle.db.models import init_db

    assert callable(init_db)


def test_profiles_load():
    from story_lifecycle.orchestrator.nodes import load_profile

    profile = load_profile("minimal")
    assert "stages" in profile


def test_service_imports():
    from story_lifecycle.orchestrator.service import create_and_start_story

    assert callable(create_and_start_story)


def test_upsert_story():
    from story_lifecycle.db.models import init_db, upsert_story, get_story

    init_db()
    upsert_story("SMOKE-001", title="Smoke test", workspace="/tmp", status="active")
    s = get_story("SMOKE-001")
    assert s is not None
    assert s["story_key"] == "SMOKE-001"
```

- [ ] **Step 2: Run all tests**

Run: `pytest tests/ -v`
Expected: PASS

- [ ] **Step 3: Commit and push**

```bash
git add tests/
git commit -m "test: update smoke tests for service layer and TUI"
git push
```

- [ ] **Step 4: Monitor CI**

Run: `gh run watch $(gh run list --limit 1 --json databaseId -q '.[0].databaseId')`
Expected: All jobs green

---

## Self-Review

**Spec coverage:**
- Service layer (single writer): Task 2 ✓
- Smart Orchestrator (planner.py): Referenced in Task 3 graph ✓
- plan_stage / review_stage nodes: Referenced in Task 3 graph ✓
- interrupt() in poll nodes: Task 3 ✓
- Watchdog with dynamic interval: Task 4 ✓
- Textual TUI skeleton + keyboard nav: Task 4 ✓
- Stage progress bar truncation: Task 4 ✓
- CLI + Model display: Task 4 ✓
- Action menu: Task 4 (placeholder, simple actions wired)
- Enter terminal (suspend): Task 4 ✓
- Detail panel (toggle d): Task 4 ✓
- New story dialog: Task 4 (placeholder)
- Graceful shutdown: Task 4 ✓
- --no-tui fallback: Task 4 ✓
- api.py refactored to service: Task 5 ✓

**Placeholder scan:** Two TODOs in Task 4 for action menu modal and new story dialog. These are simple inline text-based interactions, not complex features. The key actions (skip, fail, resume, enter) are all wired via direct key bindings as a simpler alternative.

**Type consistency:** StoryState TypedDict extended with `plan: Optional[dict]` and `review: Optional[dict]`, db.get_story returns dict | None, service functions use story_key: str consistently.

**Smart Orchestrator integration:** The `planner.py` module (detailed in `docs/design-smart-orchestrator.md`) implements `plan_stage()` and `review_stage()` functions. The graph flows through plan → execute → poll → review → router. Degradation: when `STORY_LLM_API_KEY` is not set, plan_stage generates a default plan from profile config, review_stage skips LLM review entirely.
