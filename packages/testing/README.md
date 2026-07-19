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

---

## WebBridge UI E2E (`testing.web`) — real browser, full UI path

The `harness` above drives story-lifecycle **in-process** (it calls the Python
API directly, bypassing HTTP/WebSocket/the frontend). `testing.web` is a
**second e2e channel** that drives the *real* user path: a real uvicorn server
+ the SPA in a real Chrome browser driven by **Kimi WebBridge**. Everything a
human would do — open the intake modal, fill the form, click "确认规划", advance
stage gates, answer clarify — is done through the browser over the real
network surface.

### Why a second channel

The in-process harness covers "does the AI flywheel work". The WebBridge
channel covers what it *can't*:

- real HTTP/WebSocket/SSE (TestClient is in-process ASGI, invisible to a
  browser)
- the SPA's intake/gate/clarify UI flows
- plan actually generating (not silently degrading to default actions)
- the AI CLI *receiving* its prompt (kimi's "opened but instruction not pasted"
  symptom is invisible to in-process)

Judgement stays pure Python (per AGENTS.md "Decider must be pure"): the browser
only *drives*, pass/fail is decided by `testing.asserters` over backend
artifacts.

### Layout

```
src/testing/web/
  server.py      boot a real uvicorn server in-process (webbridge_server /
                 real_webbridge_server fixtures)
  webbridge.py   Kimi WebBridge daemon client (127.0.0.1:10086): navigate /
                 snapshot / click / fill + DOM-click fallback
  api_client.py  httpx client mirroring the FastAPI contract + SSE/WS helpers
  scenario.py    run_scenario(): full-UI orchestration (no API detour for any
                 state-changing op); WorkspacePrep / CalculatorPrep /
                 InjectedSpecPrep for seeding + cleanup
  runner.py      pluggable test runner: MavenTestRunner (-pl + -am +
                 -DfailIfNoTests + maven_root) / PytestRunner
  judge.py       ScenarioJudge base + CalculatorJudge + HcAllJavaJudge
```

### Two ready scenarios

- **calculator** (Python, isolated DB): same red→green as the in-process
  harness, now over HTTP + browser.
- **hc_config** (Java, real `D:\hc-all\hc-config` Maven subproject): a
  trivial `WebBridgeDemoUtil` impl. `InjectedSpecPrep` copies the JUnit test
  into the real tree before the run and removes it (plus the AI-written impl)
  after — the real git tree is left clean.

### How to run

```bash
# prerequisites: WebBridge daemon running + Chrome extension connected,
# claude CLI on PATH, story-lifecycle LLM configured.
pytest -m real_web_e2e tests/e2e/test_calculator_webbridge_e2e.py   # Python
pytest -m real_web_e2e tests/e2e/test_hc_config_webbridge_e2e.py     # Java (hc-all)
```

`real_web_e2e` is opt-in only — excluded from the default suite. The
`hc_config` test uses `real_webbridge_server` (connects to the real
`~/.story-lifecycle` DB, because the intake modal's workspace dropdown needs
registered workspaces). The `calculator` test uses the isolated
`webbridge_server` fixture.

### Honest status

- **End-to-end verified**: a full run (UI intake → plan → design → build →
  verify → completed) has been observed producing `WebBridgeDemoUtil.java`
  with kimi actually writing the code, then `completed`. ~15 min wall-clock
  per run, real LLM cost.
- **Stability is NOT yet "always passes"**: the same code can occasionally
  stall at a planning/gate transition (nondeterministic timing across LLM
  plan generation, gate-button render, WebBridge snapshot). A red run must be
  triaged by a human — it may be a real regression or a timing flake.
- **Right use today**: a manual / nightly acceptance tool for "did this
  change to planner / PTY / adapter / llm_client break the full user path".
  Not yet a hard CI gate that "red == bug".
- See `docs/webbridge-e2e-runbook.md` for the self-contained execution manual.

