# packages/testing — Dedicated Testing Package

Shared test infrastructure for the dev-flywheel monorepo's **real-AI E2E** layer.

This package does **not** contain unit tests (those live next to each package:
`packages/story-lifecycle/tests`, `packages/story-miner/tests`,
`packages/knowledge/tests`). It provides the *harness, asserters, and scenarios*
needed to run a story end-to-end against a **real** Claude/Codex CLI — no mocks.

## Layout

```
src/testing/
  harness.py      run_real_story() — programmatically drive the story-lifecycle
                  orchestrator through a real AI adapter (design → implement → verify)
  workspace.py    reset_workspace() — git restore + clean .story/<key>, repeatable runs
  asserters.py    per-stage artifact assertions (design/implement/verify/done/miner)
  scenarios/
    calculator/   real E2E workspace: PRD + tests/test_calculator.py (17 tests),
                  calculator.py intentionally absent (red → green via the AI)
```

## How tests consume it

`tests/e2e/test_calculator_real_e2e.py` (marked `real_e2e`) calls
`testing.harness.run_real_story(...)` then `testing.asserters.*`.

## Marker discipline

`real_e2e` is **opt-in only**. The top-level `pytest` command excludes it
(`-m "not real_e2e"`), so it never runs in the default/CI suite. Run it
explicitly when a real AI CLI + key are available:

```bash
pytest -m real_e2e            # real AI, slow, costs tokens
```

## Honest status

The harness wires up the real story-lifecycle API and asserts the contract at
every stage. Completing a full real-AI run requires the `claude` (or `codex`)
CLI on PATH plus API credentials and wall-clock time — that is the user's job,
not CI's.
