# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Run Commands

```bash
pip install -e .                # install package
pip install -e ".[dev]"         # install with dev deps (pytest, ruff)
ruff check src/                 # lint
ruff check src/ --fix           # lint with auto-fix
python -m story_lifecycle       # run as module (debugging)
story serve                     # start orchestrator server (port 8180)
story setup                     # first-run LLM config wizard
story doctor                    # check system deps
pytest                          # run tests (none exist yet)
```

## Architecture

**Story Lifecycle Manager** orchestrates AI coding assistants through multi-stage workflows (design → implement → test). A story represents a unit of work that progresses through stages defined by YAML profiles.

### Core Flow (LangGraph State Machine)

The orchestration engine is a `StateGraph` in `src/story_lifecycle/orchestrator/graph.py`. Each story runs in a background thread via `ThreadPoolExecutor`. The graph nodes:

1. **execute_stage** — launches the AI CLI (e.g. Claude Code) inside a tmux session, injects a rendered prompt
2. **poll_completion** — waits for the AI to write `.story-done/{stage}.json` in the workspace, with 30-min timeout
3. **router_node** — decides next action: `advance` (happy path), `retry`, `skip`, `fail`, or `wait_confirm`
4. **advance/retry/skip/fail/wait_confirm** — action nodes that update state and loop back or end

### Module Layout

- `src/story_lifecycle/cli/` — Click CLI (`story` command). All commands hit the FastAPI server via httpx, with fallback to direct DB reads.
- `src/story_lifecycle/orchestrator/` — graph.py (StateGraph), nodes.py (node implementations + prompt rendering), api.py (FastAPI REST server), router.py (LLM routing decisions)
- `src/story_lifecycle/adapters/` — adapter pattern for AI CLI tools. `BaseAdapter` defines the interface; `ClaudeAdapter` implements it. New tools need a subclass registered in `__init__.py::get_adapter`.
- `src/story_lifecycle/db/models.py` — SQLite with raw SQL, zero ORM. Tables: `story`, `stage_log`, `gate_result`. DB lives at `~/.story-lifecycle/story.db`.
- `src/story_lifecycle/terminal/ttyd.py` — manages per-story tmux sessions and ttyd web terminal instances (Unix only; Windows CLI works without AI execution).
- `profiles/` — YAML files defining stage sequences. `minimal.yaml` is the default 3-stage profile.
- `prompts/` — markdown prompt templates per stage, with `{variable}` substitution.

### Key Design Patterns

- **Handshake protocol**: The AI CLI signals completion by writing `.story-done/{stage}.json`. The orchestrator polls for this file. The JSON is parsed with `robust_json_parse` that handles markdown-wrapped output.
- **LLM Router dual mode**: If `STORY_LLM_API_KEY` is set, routing decisions (retry/skip/fail on errors) use an LLM call. Otherwise, rule-based fallback with provider rotation from `allowed_providers` in profile config.
- **Config**: `~/.story-lifecycle/config.yaml` stores LLM provider/key/model. Loaded to env vars (`STORY_LLM_API_KEY`, `STORY_LLM_BASE_URL`, `STORY_LLM_MODEL`) on CLI startup.
- **Profile resolution order**: `.story/profiles/` (project-local) → `~/.story-lifecycle/profiles/` → `profiles/` (package built-in).

### Conventions

- All stage templates and prompts are in Chinese — maintain this when editing.
- The `StoryState` TypedDict in `nodes.py` is the shared state object passed between all graph nodes.
- `db.log_stage()` records every action for auditability.
- Stories are recovered on server restart via `recover_orphan_stories()`.
