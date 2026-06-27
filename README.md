# dev-flywheel

Monorepo for `story-lifecycle` + `story-miner` (formerly `agent-transcript-miner`), unified under a single knowledge flywheel.

## Packages

| Package | Path | Description |
|---------|------|-------------|
| `story-lifecycle` | [`packages/story-lifecycle`](packages/story-lifecycle) | AI-powered development workflow orchestrator — from TAPD/Jira story to production. |
| `story-miner` | [`packages/story-miner`](packages/story-miner) | Transcript / stage-log mining tool that outputs LLM-ready structured knowledge (playbooks, failure modes, stage costs, retrospects). Can run standalone or as a provider for `story-lifecycle`. |

## Docs

- [`docs/MIGRATION.md`](docs/MIGRATION.md) — Monorepo migration plan and task cards (M1–M6) `[已完成]`.
- [`docs/INTEGRATION.md`](docs/INTEGRATION.md) — Cross-package contract specs; I1–I4 `[已完成]`.
- [`docs/ADOPTION.md`](docs/ADOPTION.md) — Adoption checklist for daily workflows.
- [`packages/story-miner/docs/ROADMAP.md`](packages/story-miner/docs/ROADMAP.md) — Mining roadmap (T1–T8).

## Quick start

```bash
# Create/activate a venv (recommended)
python -m venv .venv-monorepo-test
source .venv-monorepo-test/Scripts/activate   # Windows Git Bash
# .venv-monorepo-test/bin/activate            # Linux/macOS

# Install both packages in editable mode
pip install -e packages/story-lifecycle
pip install -e packages/story-miner
pip install -e packages/knowledge

# Run tests
python -m pytest packages/story-lifecycle/tests packages/story-miner/tests packages/knowledge/tests tests/contracts -q
```

## Releases

### v0.12.0 — Monorepo + Integration flywheel

- M1–M6: `story-lifecycle` + `story-miner` merged into monorepo under `packages/`, with a shared `packages/knowledge` layer.
- I1–I4: `story-miner` now runs incrementally via `packages/story-miner/scripts/refresh.sh`, binds sessions to stories via `story-lifecycle` anchors, injects `{transcript_context}` into prompts, and auto-generates story-level retrospects on `story done`.
- Docs refreshed: `docs/MIGRATION.md`, `docs/INTEGRATION.md`, `docs/ADOPTION.md`, `packages/story-miner/docs/ROADMAP.md`.
- Tests: `660 passed, 2 skipped`.

See [v0.12.0 release](https://github.com/iceCloudZ/story-lifecycle/releases/tag/v0.12.0) and the [releases page](https://github.com/iceCloudZ/story-lifecycle/releases) for assets and notes.

## License

MIT — see [`packages/story-lifecycle/LICENSE`](packages/story-lifecycle/LICENSE).
