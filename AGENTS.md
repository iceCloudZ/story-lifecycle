# AGENTS.md

This file provides guidance to AI coding assistants (Claude Code / Codex / Kimi Code …) working in this monorepo. **Read this before touching any package.**

## What this repo is

`dev-flywheel` — a Python monorepo where a unified knowledge flywheel connects four packages. One GitHub repo (`story-lifecycle`), one workspace root (`D:/github/story-lifecycle`).

| Package | Path | Role |
|---|---|---|
| `story-lifecycle` | `packages/story-lifecycle` | Core orchestrator: drives AI coding agents through story workflows (design → implement → test), FC-based, Python. |
| `story-miner` | `packages/story-miner` | Producer: normalizes coding-agent transcripts into SQLite, mines behavior/failure/cost knowledge. Uses flat `miner/` layout (not src/). |
| `story-knowledge` | `packages/knowledge` | Contract: unified knowledge schema (scenario/playbook/failure) consumed by both packages above. |
| `testing` | `packages/testing` | Real-AI E2E test harness + asserters + scenarios shared across packages. |

**Flywheel:** `story-miner` mines experience → `knowledge` defines the shared schema → `story-lifecycle` consumes it (via `knowledge/context_providers/`). The seam between packages is soft (try/except imports) so each package can run standalone.

## Setup

```bash
# Create/activate venv at the monorepo root (NOT inside a package)
python -m venv .venv-monorepo-test
source .venv-monorepo-test/Scripts/activate   # Windows Git Bash
# .venv-monorepo-test/bin/activate            # Linux/macOS

# Install dev tools at root
pip install -e ".[dev]"

# Install each package in editable mode (order does not matter, all are leaf-installable)
pip install -e packages/story-lifecycle
pip install -e packages/story-miner
pip install -e packages/knowledge
pip install -e packages/testing
```

## Build & Run

```bash
# Lint (run from a package dir or point at its src/)
ruff check packages/story-lifecycle/src/

# Run the orchestrator (story-lifecycle only)
story serve                  # FastAPI + uvicorn, 127.0.0.1:8180
story setup                  # first-run LLM config wizard
story doctor                 # check system deps

# story-miner ingest
python -m miner.store --since-days 1
python -m miner.story_ingest
python -m miner.link
```

## Tests

Test paths are configured at the monorepo root `pyproject.toml` `[tool.pytest.ini_options]`. Run **from the repo root** (not inside a package):

```bash
# All unit + contract tests (fast, default)
pytest

# Single package
pytest packages/story-lifecycle/tests

# Cross-package contract tests only
pytest tests/contracts tests/integration

# Real-AI E2E (slow/costly, opt-in — skipped by default)
pytest -m real_e2e tests/e2e
```

`testpaths` covers: each package's `tests/` + root `tests/contracts` + `tests/integration` + `tests/e2e`. Root `tests/` is the **cross-package layer** (contracts/integration/e2e) — it does NOT belong to any single package; do not move it into a package.

## Where things live

```
packages/<pkg>/             one package — src/, tests/, frontend/ (story-lifecycle only), docs/
packages/story-lifecycle/src/story_lifecycle/   physical 5-layer: entry/ sourcing/ orchestrator/ knowledge/ infra/
docs/                       monorepo-level docs (MIGRATION/INTEGRATION/ADOPTION + migration/)
tests/                      cross-package contract/integration/e2e
pyproject.toml              workspace root (dev deps + pytest config; packages=[[]] = no root wheel)
```

Each package has its own `pyproject.toml` and `docs/`. **Package-level docs stay in the package** (they describe that package's code); only monorepo-level concerns (migration, integration contracts) live at root `docs/`.

For package internals, read that package's docs first:
- `packages/story-lifecycle/docs/ARCHITECTURE.md` — the source of truth for story-lifecycle's layering
- `packages/story-miner/README.md` — miner's directory structure, db schema, adapter pattern

## Architecture Review Triggers

Use these as the project rule for deciding when a bugfix should stop being a local patch and become an architecture review. Applies across all packages.

Hard rules:

- If the same functional area has a third related bug, stop patching and write an architecture review or state-machine/protocol design first.
- If a cross-system state needs explanation beyond true/false, model it as an enum/tagged state instead of a boolean.
- TUI, CLI, workflow, and background orchestration changes must define `state x user_action -> action` before handler side effects are implemented.
- Resolver code must only read facts. Decider code must be pure. Handlers are the only layer allowed to update DB, start threads, open terminals, delete sessions, or show UI feedback.
- Every non-executable branch must produce visible user feedback and diagnostic logs.
- Every historical bug fixed in these areas must have a regression test.

Trigger checklist:

```text
1. Do these bugs share the same boundary?
2. Are multiple real states represented by one boolean?
3. Do multiple entry points make similar but inconsistent decisions?
4. Are side effects mixed into state checks?
5. Is a decision table, state machine, or protocol missing?
6. Is the fix spreading across multiple files?
7. Does the user need manual explanation for which action to take next?
```

If three or more answers are yes, pause implementation and design the state model first.

## Domain conventions (story-lifecycle)

These are durable design contracts — not implementation details. Changing them requires reading the linked commits and updating tests + docs together. Each came from a real incident; the contract is the fix.

### Adapter prompt delivery — `SessionSpec` + `start_session`

How an AI CLI (claude/codex/kimi) receives its seed prompt is the **adapter's** business, not the spawner's. All spawn paths (`continue_orchestrator_agent`, `_spawn_story_agent_pty`, `api_spawn_session`) go through one contract:

- `BaseAdapter.start_session(model, prompt, session_id, ...) -> SessionSpec`
- `SessionSpec` carries `command` + `pty_prompt` + `readiness_marker`
- ClaudeAdapter bakes the prompt into `command` (`claude "query"`), `pty_prompt=""`, `readiness_marker=None`
- ShellAdapter (kimi/codex) returns bare `command`, `pty_prompt=<seed>`, `readiness_marker=<CLI's ready banner>`
- Spawners do NOT branch on adapter type — they read the spec and execute mechanically

**Anti-pattern**: adding a `prompts_via_pty`/`isinstance(adapter, ClaudeAdapter)` branch in a spawner. That drifts again — happened twice before this contract landed. See commits `a32a00f6`, `c90474c5`.

### Per-story workspace — `worktrees_root` + LLM-decided slug

Code agents run in an isolated per-story workspace, not the main monorepo:

- Planning LLM returns `workspace_slug` in `PlanResult` (kebab-case title abbreviation, e.g. `mgm-app-version-limit`)
- Backend `mkdir <worktrees_root>/<slug>/` (default `D:/worktrees` on Windows, `~/worktrees` elsewhere; overridable via `config.yaml` `worktrees_root` or env `STORY_WORKTREES_ROOT`)
- `_build_cli_prompt` writes a `### 工作空间` section: agent's cwd is the workspace, it does `git worktree add` for each project it needs to touch
- Spawn `cwd = ctx.workspace_path`, not the main workspace

**LLM decides the slug, backend builds the dir** (no side effects in the model call — replayable). The agent decides *which projects* to bring in (it's closest to the need). See commit `8ddc3501`.

### Driver lifecycle — dead-PID recovery + passive done-file consumption

Two invariants that must hold, both learned from a stuck-story incident (commit `56583154`):

1. **A dead driver must not lock the story forever.** `claim_story_driver` checks `_driver_pid_alive(token)` before failing CAS — if the holding PID is gone, a new driver may seize. Windows uses `OpenProcess(SYNCHRONIZE)` via ctypes (`os.kill(pid, 0)` returns `WinError 87` regardless of liveness — do not use it). POSIX uses `os.kill(pid, 0)`.
2. **A CLI that self-completes while no driver is watching must still advance state.** `consume_orphan_done(story_key)` scans for done files not in `_completed_stages` and claims them. Triggered from `GET /api/story/{key}` — opening the detail page unsticks a story whose CLI finished after an emergency-stop. No-op when a driver is live (its poll loop owns that case) or the story is finished.

**Hard rule**: the driver assumes "CLI lifecycle ⊆ driver lifecycle". Any path that breaks this (interactive manual run, emergency-stop, crash) must have a reconciliation entry. `consume_orphan_done` is that entry; don't add a second one.

### `task_actions` drives stage semantics — not stage name, not prompt keywords

Stage constraints (what the agent may/may not do) come from the **structured `task_actions` list**, never from keyword-matching the assembled prompt text:

- `_build_exec_constraint(action_keys)` branches on `task_actions` content:
  - only `write_design_doc` (and no code/tests) → **no code edits** (design is investigation only)
  - contains `run_tests` → lightweight tests allowed (covers verify: `[run_tests, accept_review, write_test_report]` has no `write_code` but does write test code)
  - `write_code` without `run_tests` → write code, no tests
- All branches forbid heavy builds (mvn/gradle/yarn install).

**Anti-pattern**: judging stage semantics by grepping the prompt for "写代码"/"Edit"/"Write". Keyword matching misclassifies negations, synonyms, and API names. Use the structured field. See commits `bcabcc43`, `88b02033` — the second was a bug in the first found via the offline analysis export.

### Offline prompt analysis — no real-time prompt judge LLM

Prompt quality is **not** judged by an LLM at spawn time. Real-time judges waste tokens, misjudge structured conditions, and false-positive-block normal stories. Instead:

- `GET /api/analysis/prompts?status=&stage=&profile=&since=&limit=` exports `(prompt + outcome + events + llm_calls)` tuples per (story, stage), one row per assembled-prompt-and-its-result
- An external AI (or human) analyzes correlations offline (which prompt patterns correlate with failures / retries / long durations) and feeds findings back into template changes
- This module is `orchestrator/observability/prompt_export.py`

**Outcome gates stay LLM-judged** (`unified_gate.py` — judges *results*, not prompts): result quality is fuzzy, prompt template correctness is structural. Don't conflate them.

## Conventions

- **Chinese content**: story-lifecycle's stage templates and prompts are in Chinese — maintain this when editing.
- **No ORM**: DB access uses raw SQL (`db/models.py`), zero ORM.
- **Editable installs**: packages are always editable-installed from `packages/`; never build wheels for local dev.
- **Do not commit runtime artifacts**: `ws/`, `*.db`, `dist/`, `.venv*/`, `.story*/`, `.claude/` (zcode workspace) are gitignored — leave them ignored.
