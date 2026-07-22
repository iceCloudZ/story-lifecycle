"""WebBridge + real-story end-to-end testing toolkit.

Layered so the non-deterministic part (AI inside story-lifecycle + the browser
round-trips) stays separate from the deterministic judgement layer. The pytest
layer only orchestrates + judges; pass/fail is decided by pure Python over
backend artifacts, never by the LLM — per AGENTS.md "Decider code must be pure".

Modules
-------
server      — boots a real uvicorn server (same-process thread) against the
              isolated DB; the only way an external Chrome can reach the backend.
webbridge   — thin HTTP client over the Kimi WebBridge daemon (127.0.0.1:10086):
              navigate / snapshot / click / fill + text-matching helpers for the
              SPA's dynamic button labels.
api_client  — httpx client mirroring the FastAPI contract (create / start /
              plan-stream / confirm / advance / lifecycle-advance / clarify) +
              SSE/WS subscription helpers. Used for deterministic seeding and as
              a backend probe alongside the browser-driven path.
scenario    — run_calculator_scenario(): orchestrates one full real-user journey
              through the SPA. Drives only; does not judge.
judge       — Judge base + CalculatorJudge: pure-Python pass/fail by composing
              the existing testing.asserters over story artifacts.
"""

from testing.web.api_client import ApiError, StoryApiClient
from testing.web.judge import (
    CalculatorJudge,
    ConsultJudge,
    HcAllJavaJudge,
    HcOrderJudge,
    Judge,
    ScenarioJudge,
)
from testing.web.runner import (
    MavenTestRunner,
    PytestRunner,
    TestRunner,
    TestRunnerError,
)
from testing.web.scenario import (
    CalculatorPrep,
    ConsultPrep,
    InjectedSpecPrep,
    ScenarioError,
    ScenarioResult,
    WorkspacePrep,
    run_calculator_scenario,
    run_consult_scenario,
    run_scenario,
)
from testing.web.server import RunningServer, start_uvicorn_server
from testing.web.webbridge import WebBridgeClient, WebBridgeError

__all__ = [
    "ApiError",
    "CalculatorJudge",
    "CalculatorPrep",
    "ConsultJudge",
    "ConsultPrep",
    "HcAllJavaJudge",
    "HcOrderJudge",
    "InjectedSpecPrep",
    "Judge",
    "MavenTestRunner",
    "PytestRunner",
    "RunningServer",
    "ScenarioError",
    "ScenarioJudge",
    "ScenarioResult",
    "StoryApiClient",
    "TestRunner",
    "TestRunnerError",
    "WebBridgeClient",
    "WebBridgeError",
    "WorkspacePrep",
    "run_calculator_scenario",
    "run_consult_scenario",
    "run_scenario",
    "start_uvicorn_server",
]
