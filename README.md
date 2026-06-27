# dev-flywheel

Monorepo for `story-lifecycle` + `story-miner` (formerly `agent-transcript-miner`), unified under a single knowledge flywheel.

## Packages

| Package | Path | Description |
|---------|------|-------------|
| `story-lifecycle` | [`packages/story-lifecycle`](packages/story-lifecycle) | AI-powered development workflow orchestrator — from TAPD/Jira story to production. |
| `story-miner` | [`packages/story-miner`](packages/story-miner) | Transcript / stage-log mining tool that outputs LLM-ready structured knowledge (playbooks, failure modes, stage costs, retrospects). Can run standalone or as a provider for `story-lifecycle`. |

## Docs

- [`docs/MIGRATION.md`](docs/MIGRATION.md) — Migration plan and task cards (M1–M6).
- [`docs/INTEGRATION.md`](docs/INTEGRATION.md) — Cross-package contract specs.
- [`docs/ADOPTION.md`](docs/ADOPTION.md) — Adoption checklist.

## Quick start

```bash
# Install both packages in editable mode
pip install -e packages/story-lifecycle
pip install -e packages/story-miner

# Run tests
pytest packages/story-lifecycle/tests
pytest packages/story-miner/tests
```

## License

MIT — see [`packages/story-lifecycle/LICENSE`](packages/story-lifecycle/LICENSE).
