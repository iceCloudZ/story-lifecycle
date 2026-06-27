# Story Board TUI Design (v4 — Smart Orchestrator)

## Goal

Replace the static `story board` (Rich Table) with an interactive Textual TUI that:
1. Does not require `story serve` — directly uses orchestrator via Python imports
2. Provides keyboard-driven navigation and actions (Claude Code-style)
3. Uses LangGraph `interrupt()` + Watchdog for async stage polling
4. Single Writer: LangGraph CheckpointSaver owns all state
5. **Smart Orchestrator integration**: shows plan/review status from编排 LLM

## Architecture

```
story board (Textual App)
├── Service layer (story_lifecycle.orchestrator.service)
│   └── create_and_start_story() — shared by TUI and server
├── Smart Orchestrator (story_lifecycle.orchestrator.planner)
│   ├── plan_stage_node — 编排 LLM 规划（选 adapter、生成指令）
│   └── review_stage_node — 编排 LLM 审查产出质量
├── LangGraph graph
│   ├── State init via graph.update_state() (never raw db.create_story)
│   └── interrupt() in poll nodes — yields worker thread
├── Watchdog (asyncio task)
│   ├── Scans .story-done/ files + tmux liveness
│   ├── graph.invoke(None, config) to resume from interrupt
│   └── Dynamic interval: 3s active / 30s idle
├── DB read-only (for board rendering only)
├── Background workers (Textual Worker API)
└── No HTTP server needed
```

## Runtime Architecture

### 1. Service layer — single entry point for TUI and server

All story creation and mutations go through a shared Service, never direct DB writes:

```python
# story_lifecycle/orchestrator/service.py
def create_and_start_story(story_key, title, profile, workspace, prd_path=None):
    # 1. Init LangGraph State (writes to CheckpointSaver)
    initial_state = StoryState(
        story_key=story_key,
        title=title,
        workspace=workspace,
        profile=profile,
        current_stage=get_first_stage(profile),
        status="active",
        context={"prd_content": read_prd(prd_path)} if prd_path else {},
    )
    config = {"configurable": {"thread_id": story_key}}
    graph.update_state(config, initial_state, as_node="__start__")

    # 2. Upsert business DB (for board quick-read)
    db.upsert_story(story_key, title=title, workspace=workspace, profile=profile,
                    current_stage=initial_state["current_stage"], status="active")

    # 3. Start execution
    return story_key  # caller submits to worker thread
```

TUI and `story serve` both call this. No `db.create_story()` anywhere.

### 2. interrupt() in poll_completion_node — LangGraph-native async

Worker threads don't block. `poll_completion_node` uses LangGraph's `interrupt()`:

```python
from langgraph.types import interrupt

def poll_completion_node(state: StoryState) -> StoryState:
    done_file = Path(state["workspace"]) / ".story-done" / f"{state['current_stage']}.json"

    if not done_file.exists():
        # Saves checkpoint perfectly, releases worker thread
        interrupt({"reason": "waiting_for_done_file"})

    # Resumes here when Watchdog calls graph.invoke(None, config)
    data = robust_json_parse(done_file)
    return {"context": data}
```

**Flow:**
1. Worker thread: `graph.invoke(initial_state, config)` → executes nodes → hits `poll_completion_node` → `interrupt()` → worker thread freed
2. Watchdog: scans `.story-done/` → finds file → `graph.invoke(None, config)` → LangGraph resumes from interrupt → `advance_node` → next stage → hits poll again → `interrupt()` → freed
3. Repeat until END

### 3. Watchdog with dynamic interval

```python
async def watchdog_check(self):
    active = [s for s in self.stories if s["status"] == "active"]
    for story in active:
        done_file = Path(ws) / ".story-done" / f"{stage}.json"
        if done_file.exists():
            graph.invoke(None, config)  # resume from interrupt

    # Dynamic interval
    if active:
        self.watchdog_interval = 3
    else:
        self.watchdog_interval = 30
```

### 4. Graceful shutdown

```python
def on_quit(self):
    for worker in self.workers:
        worker.cancel()
    for story in active_stories:
        graph.update_state(config, {"status": "paused"})
```

Active stories resume on next `story board` launch.

## Layout

```
┌─ Story Lifecycle ─────────────────────────────────────────────┐
│ Orchestrator: enabled (deepseek)       Stories: 3 active       │  Header
├───────────────────────────────────────────────────────────────┤
│                                                               │
│  STORY-123  Add login feature                                 │
│  ● design → ◉ implement → ○ test        [active] retries: 0  │
│  CLI: claude · Model: sonnet                                  │
│  Plan: 根据设计文档实现认证模块...                              │
│                                                               │
│  STORY-456  Fix payment bug                                   │
│  ... → ◉ backend_dev → ○ frontend_dev → ...  [blocked]       │
│  CLI: codex · Model: gpt-4o                                   │
│  ↳ Review: 实现缺少错误处理                                    │
│                                                               │
│  STORY-789  Update user API                                   │
│  ◉ design → implement → test            [active] retries: 0  │
│  CLI: claude · Model: opus                                    │
│                                                               │
├───────────────────────────────────────────────────────────────┤
│ [n] new  [e] enter  [s] skip  [f] fail  [R] refresh  [?] help│
│ > STORY-123 selected. Press Enter for actions.                │
└───────────────────────────────────────────────────────────────┘
```

## Components

### Header
- Smart Orchestrator status (enabled/disabled + provider name)
- Active story count

### Story Cards
- Story key (bold cyan) + title
- Stage progress bar (truncated for long profiles: current ± 1, `...` for rest)
- CLI tool + model: `CLI: claude · Model: sonnet`
- Status badge: `[active]` green, `[blocked]` red, `[paused]` yellow, `[completed]` dim green
- Retry count (if > 0)
- Last error (one line, dim red) if blocked
- **Plan summary** (one line, dim): shows the `extra_instructions` from `plan_stage` (if available)
- **Review feedback** (one line, dim yellow): shows review feedback if quality is `revise`

### Footer
- Key bindings bar
- Single log line for last action result

### Action Menu (Enter)

```
┌─ STORY-123: Add login feature ──────────┐
│  [e] Enter terminal                      │
│  [s] Skip current stage                  │
│  [f] Mark as failed                      │
│  [r] Resume                              │
│  [x] Delete story                        │
│  [Esc] Cancel                            │
└──────────────────────────────────────────┘
```

### Enter Terminal — app.suspend()

```python
def enter_terminal(self, story_key):
    session = f"s-{story_key}"
    self.app.suspend()
    subprocess.run(["tmux", "attach", "-t", session])
    # Ctrl+b d → TUI resumes
```

### New Story Dialog (`n`)

- Story key, title, profile selection
- PRD: **file path input** (`PRD file path (Enter to skip): ./docs/prd-123.md`)
- Calls `service.create_and_start_story()` → worker thread

### Detail Panel (`d`)

- Full stage list with status
- Full error stacktrace (if blocked)
- Context JSON: top-level keys only, values truncated at 500 chars
- Timestamps

## Key Bindings

| Key | Action |
|-----|--------|
| `↑` / `k` | Move up |
| `↓` / `j` | Move down |
| `Enter` | Action menu |
| `n` | New story |
| `e` | Enter terminal (suspend + tmux) |
| `d` | Toggle detail panel |
| `s` | Skip stage |
| `f` | Mark failed |
| `r` | Resume |
| `R` / `F5` | Manual refresh |
| `?` | Help |
| `q` / `Ctrl+c` | Quit (graceful) |

## File Changes

| File | Change |
|------|--------|
| `src/story_lifecycle/orchestrator/service.py` | New — shared service layer |
| `src/story_lifecycle/orchestrator/planner.py` | New — Smart Orchestrator (plan + review via DeepSeek) |
| `src/story_lifecycle/orchestrator/nodes.py` | Modify: add plan_stage/review_stage nodes, poll uses `interrupt()` |
| `src/story_lifecycle/orchestrator/graph.py` | Modify: insert plan/review nodes, update edges |
| `src/story_lifecycle/cli/tui.py` | New — Textual App (~450 lines) |
| `src/story_lifecycle/cli/main.py` | Modify `board()` to launch TUI |
| `src/story_lifecycle/db/models.py` | Add `upsert_story()` |
| `pyproject.toml` | Add `textual>=3.0` |

## Dependencies

- `textual>=3.0` (~2MB)
- `langgraph` already has `interrupt()` support (no new dep)

## Implementation Order

1. **Service layer** — `service.py` + `upsert_story()` (core artery)
2. **Smart Orchestrator** — `planner.py` + plan/review nodes (brain)
3. **interrupt() in poll nodes** — modify `nodes.py` (blood circulation)
4. **Graph update** — insert plan/review nodes, update edges
5. **Textual skeleton** — read-only board + keyboard nav (the skin)
6. **Watchdog** — file scanning + graph.invoke resume
7. **Action menu + e/s/f/r key handlers**
8. **New story dialog**
9. **Graceful shutdown**
10. **`--no-tui` fallback**

## Backward Compatibility

- `story serve` still works (refactored to use same service layer)
- `story board --no-tui` falls back to static table
- All existing subcommands unchanged

## Out of Scope

- Real-time log streaming from tmux
- Embedded terminal emulator
- Mouse interaction (v1 keyboard only)
- Remote server connection from TUI
