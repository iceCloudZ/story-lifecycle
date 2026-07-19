"""Real WebBridge E2E: calculator 红→绿, driven over the real HTTP/WS/SSE surface
+ a real browser via Kimi WebBridge.

@real_web_e2e 默认跳过（见 conftest.py）；`pytest -m real_web_e2e` 才跑。
需要：story-lifecycle LLM 已配置 + claude/codex CLI 在 PATH + WebBridge daemon
在跑且浏览器扩展已连接。

和 tests/e2e/test_calculator_real_e2e.py（in-process 闭环）是叠加关系：那条
验证 AI 飞轮本身，这条验证真实网络面 + 浏览器真人视角。判定层复用同一套
asserters，两条通道对"什么算好 run"是一致的。
"""
import os
import subprocess
from pathlib import Path

import pytest

from testing.web import (
    CalculatorJudge,
    ScenarioError,
    StoryApiClient,
    WebBridgeClient,
    WebBridgeError,
    run_calculator_scenario,
)

_ROOT = Path(__file__).resolve().parents[2]
SCENARIO = _ROOT / "packages" / "testing" / "src" / "testing" / "scenarios" / "calculator"
DB = _ROOT / "packages" / "story-miner" / "data" / "transcripts.db"
STORY_KEY = "E2E-WEB-CALC"

_STAGES = ["design", "implement", "verify"]


def _claude_cli_present() -> bool:
    """The scenario runs real AI; skip (not fail) if the CLI isn't installed."""
    from shutil import which

    return which("claude") is not None or which("codex") is not None


@pytest.fixture
def webbridge(monkeypatch):
    """A WebBridgeClient bound to one tab-group session; skip if daemon/extension down.

    Uses a unique session name per test so tab groups don't collide. The daemon
    is auto-started if the binary is present (see webbridge._ensure_daemon).
    """
    try:
        wb = WebBridgeClient(session=f"e2e-{STORY_KEY}")
    except WebBridgeError as exc:
        pytest.skip(f"WebBridge unavailable: {exc}")
    # probe extension connection explicitly (status() is cheap)
    st = wb.status()
    if not st.get("extension_connected"):
        wb.close()
        pytest.skip("WebBridge extension not connected — open Chrome/Edge first")
    yield wb
    try:
        wb.close_session()
    except Exception:
        pass
    wb.close()


@pytest.mark.real_web_e2e
def test_calculator_webbridge_e2e(webbridge_server, webbridge):
    """真实 AI 跑 calculator 红→绿, 整条链路 over HTTP + 浏览器.

    server      : 真实 uvicorn (webbridge_server fixture), 隔离 DB
    webbridge   : 真实 Chrome (WebBridge daemon + 扩展), 操作 SPA
    判定        : CalculatorJudge 复用 asserters, 断产物存在/非空/真实 pytest 退出0
    """
    if not _claude_cli_present():
        pytest.skip("claude/codex CLI 不在 PATH — real_web_e2e 需要真实 AI CLI")

    api = StoryApiClient(webbridge_server.base_url)
    prd_content = (SCENARIO / "PRD.md").read_text(encoding="utf-8")

    # ---- drive (non-deterministic: real AI + browser) ----
    try:
        result = run_calculator_scenario(
            server=webbridge_server,
            webbridge=webbridge,
            scenario_dir=SCENARIO,
            story_key=STORY_KEY,
            prd_content=prd_content,
            stages=_STAGES,
            title="WebBridge E2E calculator",
        )
    except ScenarioError as exc:
        # 区分失败模式: 驱动失败 ≠ 判定失败. 这里明确报告是驱动问题.
        pytest.fail(f"scenario drive failed (UI/HTTP path broke, not a bad artifact): {exc}")

    # ---- judge (deterministic: pure Python over backend artifacts) ----
    CalculatorJudge(miner_db=DB).judge(
        result,
        workspace=SCENARIO,
        story_key=STORY_KEY,
        stages=_STAGES,
        api=api,
    )
