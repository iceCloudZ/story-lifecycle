"""Real WebBridge E2E for the consult tool — 经 UI 创建真实 story(PRD 埋了
强制 consult 场景),跑 headless stage,断言 consult 链路落了事件 / 文件 /
decorrelation / advisory 非空。

@real_web_e2e 默认跳过(见 conftest.py);``pytest -m real_web_e2e`` 才跑。
前置:
- story-lifecycle LLM 已配置(本机 deepseek)
- claude / kimi CLI 在 PATH(claude 是 caller,kimi 是被 spawn 的 reviewer)
- WebBridge daemon 127.0.0.1:10086 在跑且浏览器扩展已连接

flake 降级:WebBridge 前置不满足或连续 2 次 gate 时序 flake 时,改用 in-process
通道 testing.harness.run_real_story() 跑同一场景 + 同一组断言。
``test_consult_inprocess_fallback`` 是降级路径的入口,也是 real_e2e marker
(不需要浏览器,只需真 LLM + claude CLI)。

判定权在 ConsultJudge —— 纯 Python,读 DB 事件 + .story/consult/ 文件,
不交给 AI。
"""

from pathlib import Path

import pytest

from testing.web import (
    ConsultJudge,
    ScenarioError,
    StoryApiClient,
    WebBridgeClient,
    WebBridgeError,
    run_consult_scenario,
)

_ROOT = Path(__file__).resolve().parents[2]
SCENARIO = _ROOT / "packages" / "testing" / "src" / "testing" / "scenarios" / "consult_demo"
DB = _ROOT / "packages" / "story-miner" / "data" / "transcripts.db"
STORY_KEY_WEB = "E2E-WEB-CONSULT"
STORY_KEY_INPROC = "E2E-INPROC-CONSULT"

_STAGES = ["design", "implement", "verify"]


def _claude_cli_present() -> bool:
    from shutil import which

    return which("claude") is not None


@pytest.fixture
def webbridge(monkeypatch):
    """A WebBridgeClient bound to one tab-group session; skip if daemon/extension down."""
    try:
        wb = WebBridgeClient(session=f"e2e-{STORY_KEY_WEB}")
    except WebBridgeError as exc:
        pytest.skip(f"WebBridge unavailable: {exc}")
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
def test_consult_webbridge_e2e(webbridge_server, webbridge):
    """真实 AI(caller=claude headless)跑 consult_demo 场景,经 UI 创建 +
    真实 HTTP/WS/SSE 推进。ConsultJudge 断后端 consult 事件 + .story/consult/ 文件。

    server      : 真实 uvicorn(webbridge_server fixture),隔离 DB
    webbridge   : 真实 Chrome(WebBridge daemon + 扩展),操作 SPA
    judge       : ConsultJudge —— DB 事件 + .story/consult/<rid>*.json 文件,
                  reviewer adapter ≠ caller,advisory 非空
    """
    if not _claude_cli_present():
        pytest.skip("claude CLI 不在 PATH — real_web_e2e 需要真实 AI CLI")

    api = StoryApiClient(webbridge_server.base_url)
    prd_content = (SCENARIO / "PRD.md").read_text(encoding="utf-8")

    # ---- drive (non-deterministic: real AI + browser) ----
    try:
        result = run_consult_scenario(
            server=webbridge_server,
            webbridge=webbridge,
            scenario_dir=SCENARIO,
            story_key=STORY_KEY_WEB,
            prd_content=prd_content,
            stages=_STAGES,
            title="WebBridge E2E consult",
        )
    except ScenarioError as exc:
        pytest.fail(
            f"scenario drive failed (UI/HTTP path broke, not a bad artifact): {exc}"
        )

    # ---- judge (deterministic: pure Python over backend artifacts) ----
    ConsultJudge(caller_adapter="claude", miner_db=DB).judge(
        result,
        workspace=SCENARIO,
        story_key=STORY_KEY_WEB,
        stages=_STAGES,
        api=api,
    )


# ─── flake 降级:in-process 通道(经 StoryApiClient 直驱,不依赖 harness) ─


@pytest.mark.real_e2e
def test_consult_inprocess_fallback(webbridge_server, tmp_path):
    """flake 降级路径:WebBridge 前置不满足 / 连续 2 次 gate 时序 flake 时启用。

    用 ``webbridge_server`` fixture 起真实 uvicorn(隔离 DB)+ ``StoryApiClient``
    经 HTTP 直接驱动 consult_demo story(不经浏览器,不经 harness.run_real_story
    —— 后者内部用错了 module 路径,有 pre-existing bug,见 DESIGN 附录 B.3)。

    跑完用 **同一组 ConsultJudge 断言** 判定:DB consult_* 事件 + .story/consult/
    文件 + decorrelation + advisory 非空。

    这条路径不需要浏览器,只需要:
    - 真 LLM 已配置
    - claude CLI 在 PATH(caller)
    - kimi CLI 在 PATH(reviewer,被 spawn)
    """

    from testing.web.api_client import StoryApiClient
    from testing.web.scenario import (
        ScenarioResult,
        _confirm_plan,
        _drive_to_completion,
        _run_plan,
        _seed,
    )
    from testing.web.webbridge import WebBridgeClient, WebBridgeError
    from testing.workspace import reset_workspace

    if not _claude_cli_present():
        pytest.skip("claude CLI 不在 PATH — consult in-process fallback 需要真实 AI CLI")

    # workspace = scenario dir(同 calculator 模式)。reset 到 red baseline。
    workspace = Path(SCENARIO).resolve()
    reset_workspace(workspace, STORY_KEY_INPROC, red_files=("greeter.py",))

    api = StoryApiClient(webbridge_server.base_url)
    prd_content = (SCENARIO / "PRD.md").read_text(encoding="utf-8")
    result = ScenarioResult(
        story_key=STORY_KEY_INPROC,
        workspace=str(workspace),
        server=webbridge_server,
    )

    # use_browser_for_gates=False:gate 全走 API,不经浏览器。
    # webbridge 参数仍要传(签名要求),但 use_browser=False 时不会被调用做点击。
    # 这里用一个 noop-like client:真实 daemon 可能没连,但 use_browser=False
    # 时 _drive_to_completion 只在 use_browser=True 才用 webbridge.click_text。
    # 若真实 WebBridge 不可用,降级用 None(签名上仍要传,我们传 None 让它在
    # use_browser=False 分支不被实际调用)。
    try:
        wb = WebBridgeClient(session=f"inproc-{STORY_KEY_INPROC}")
    except WebBridgeError:
        wb = None  # type: ignore[assignment]

    try:
        _seed(api, workspace, STORY_KEY_INPROC, "In-process consult E2E",
              "minimal", prd_content, result)
        _run_plan(api, STORY_KEY_INPROC, result)
        _confirm_plan(api, wb, STORY_KEY_INPROC, False, False, result)
        _drive_to_completion(api, wb, STORY_KEY_INPROC, False, False,
                             1800.0, 3.0, result)
        result.final_status = api.story_status(STORY_KEY_INPROC)
    except Exception as exc:
        pytest.fail(f"in-process drive failed: {exc}")

    ConsultJudge(caller_adapter="claude", miner_db=DB).judge(
        result,
        workspace=workspace,
        story_key=STORY_KEY_INPROC,
        stages=_STAGES,
        api=api,
    )
