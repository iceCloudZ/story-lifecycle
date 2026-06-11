# Explicit Story Execution Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make ordinary stories run in visible per-story Claude PTYs while preserving headless execution only for explicitly configured benchmark and CI profiles.

**Architecture:** Resolve a typed execution mode from profile data and pass it
through graph tool arguments. Interactive dispatch records a persistent marker
and yields the graph until a done-file watcher resumes it; headless dispatch
keeps the existing synchronous subprocess path.

**Tech Stack:** Python 3.10+, FastAPI lifespan tasks, LangGraph, YAML profiles,
cross-platform PTY registry, pytest.

---

### Task 1: Lock Execution Mode Selection With Tests

**Files:**
- Modify: `tests/test_entry_decisions.py`
- Modify: `tests/test_smart_orchestrator.py`
- Modify: `tests/test_profile_resolution.py`

- [ ] Add a regression test proving default Claude execution uses the PTY path.
- [ ] Add a regression test proving `execution_mode: headless` uses subprocess.
- [ ] Add graph routing tests for the interactive waiting state.
- [ ] Run the focused tests and verify they fail for the current global-headless behavior.

### Task 2: Add Explicit Profile Resolution

**Files:**
- Create: `src/story_lifecycle/orchestrator/execution.py`
- Modify: `src/story_lifecycle/orchestrator/nodes/profile_loader.py`
- Modify: `src/story_lifecycle/orchestrator/nodes/graph_nodes.py`
- Modify: `profiles/*.yaml`
- Modify: `src/story_lifecycle/profiles/*.yaml`

- [ ] Define `interactive_pty` and `headless` execution modes with strict validation.
- [ ] Resolve profile defaults and stage overrides.
- [ ] Pass the resolved mode in stage tool arguments.
- [ ] Mark SWE-bench and Headless Smoke as explicitly headless.
- [ ] Run profile and graph tests.

### Task 3: Implement Interactive Agent PTY Dispatch

**Files:**
- Modify: `src/story_lifecycle/terminal/pty.py`
- Modify: `src/story_lifecycle/adapters/base.py`
- Modify: `src/story_lifecycle/adapters/claude.py`
- Modify: `src/story_lifecycle/orchestrator/tools/base.py`
- Modify: `src/story_lifecycle/orchestrator/api.py`

- [ ] Add PTY purpose metadata and reuse helpers.
- [ ] Add adapter argv for an interactive process.
- [ ] Start or reuse the per-story agent PTY and inject the prompt.
- [ ] Persist execution metadata and expose it in execute events.
- [ ] Make terminal spawn reuse an existing agent PTY.
- [ ] Run focused PTY and tool tests.

### Task 4: Restore Interactive Waiting And Resume

**Files:**
- Modify: `src/story_lifecycle/orchestrator/nodes/state.py`
- Modify: `src/story_lifecycle/orchestrator/nodes/graph_nodes.py`
- Modify: `src/story_lifecycle/orchestrator/nodes/routing.py`
- Modify: `src/story_lifecycle/orchestrator/graph.py`
- Modify: `src/story_lifecycle/orchestrator/api.py`

- [ ] Skip repeated planning for an active or completed current-stage execution.
- [ ] End graph invocation cleanly while the agent is running.
- [ ] Add a done-file watcher that resumes only matching active interactive stories.
- [ ] Clear the execution marker after consuming the done file.
- [ ] Run graph, restart, and watcher tests.

### Task 5: Verify And Ship

**Files:**
- Review all modified files.

- [ ] Run focused regression tests.
- [ ] Run the full pytest suite.
- [ ] Run `ruff check src tests`.
- [ ] Review `git diff` for unrelated changes.
- [ ] Commit only this feature and its documentation.
- [ ] Push the current branch.
