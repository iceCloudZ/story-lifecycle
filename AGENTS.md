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

## Conventions

- **Chinese content**: story-lifecycle's stage templates and prompts are in Chinese — maintain this when editing.
- **No ORM**: DB access uses raw SQL (`db/models.py`), zero ORM.
- **Editable installs**: packages are always editable-installed from `packages/`; never build wheels for local dev.
- **Do not commit runtime artifacts**: `ws/`, `*.db`, `dist/`, `.venv*/`, `.story*/`, `.claude/` (zcode workspace) are gitignored — leave them ignored.
