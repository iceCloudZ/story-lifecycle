"""Real WebBridge E2E: hc-config (real Java subproject in hc-all workspace) red→green.

@real_web_e2e 默认跳过（见 conftest.py）；`pytest -m real_web_e2e` 才跑。
需要：story-lifecycle LLM 已配置 + claude/codex CLI 在 PATH + WebBridge daemon
在跑且浏览器扩展已连接 + D:\\hc-all 可写。

工作区模型
----------
``D:\\hc-all`` 是一个工作区**容器**（不是 git 仓库），下面是多个独立的 Java
子项目（hc-order / hc-user / **hc-config** / ...），每个子项目自己是 git 仓库
+ Maven 多模块项目。story-lifecycle 把 hc-all 当作 workspace（产物落
hc-all/.story/），AI 在各子项目里改代码。

本场景的微示例落在 hc-config 子项目（它的 business 模块测试套件干净、0 个
既有测试文件，注入判定测试不会和现有坏测试冲突）。判定测试用
MavenTestRunner(mvnw -pl hc-config-business -am test -Dtest=WebBridgeDemoUtilTest)
跑 JUnit 5。

注入的判定测试和 AI 写的 .java 都在 cleanup 时删除，hc-all / hc-config 的
真实工作树不留残渣（hc-all 不是 git 仓库，靠删文件而非 git restore 清理）。

这是"真代码库跑真需求 + 预置测试绿"的最小验证。后续把 PRD 换成你的真实业务
需求、把判定测试换成业务验收、把 subproject 换成目标子项目即可。
"""
import os as _os
from pathlib import Path

import pytest

from testing.web import (
    HcAllJavaJudge,
    InjectedSpecPrep,
    ScenarioError,
    StoryApiClient,
    WebBridgeClient,
    WebBridgeError,
    run_scenario,
)

_ROOT = Path(__file__).resolve().parents[2]
SCENARIO = _ROOT / "packages" / "testing" / "src" / "testing" / "scenarios" / "hc_config"

# hc-all 工作区容器（可被环境变量覆盖，便于 CI/其他主机）。
HC_ALL_WS = Path(_os.environ.get("HC_ALL_WORKSPACE", r"D:\hc-all"))

SUBPROJECT = "hc-config"
MODULE = "hc-config-business"
IMPL_PKG = "com/ys/hc/config/utils"
CLASS = "WebBridgeDemoUtil"

STORY_KEY = "E2E-WEB-HC-CONFIG"
_STAGES = ["design", "implement", "verify"]

# InjectedSpecPrep: scenario src → workspace dst (相对 hc-all 根)。
_INJECT = {
    "WebBridgeDemoUtilTest.java": (
        f"{SUBPROJECT}/{MODULE}/src/test/java/{IMPL_PKG}/{CLASS}Test.java"
    ),
}
# AI 应产出的实现文件（红 = 不存在）。cleanup 时一并删除。
_RED_FILES = [
    f"{SUBPROJECT}/{MODULE}/src/main/java/{IMPL_PKG}/{CLASS}.java",
]


def _claude_cli_present() -> bool:
    from shutil import which

    return which("claude") is not None or which("codex") is not None


def _hc_all_ready() -> tuple[bool, str]:
    """hc-all + hc-config 可写、是 Maven 子项目。skip（不 fail）否则。"""
    ws = HC_ALL_WS
    if not ws.is_dir():
        return False, f"workspace not found: {ws}"
    sub = ws / SUBPROJECT
    if not sub.is_dir():
        return False, f"subproject {SUBPROJECT} not found under {ws}"
    if not (sub / "pom.xml").exists():
        return False, f"{sub} is not a Maven project (no pom.xml)"
    if not (sub / "mvnw.cmd").exists() and not (sub / "mvnw").exists():
        return False, f"no mvnw wrapper in {sub}"
    return True, ""


@pytest.fixture
def webbridge(monkeypatch):
    try:
        wb = WebBridgeClient(session=f"e2e-{STORY_KEY}")
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
def test_hc_config_webbridge_e2e(real_webbridge_server, webbridge):
    """真实 AI 在 hc-all/hc-config (Java) 里实现 WebBridgeDemoUtil, mvn test 绿.

    全 UI 路径（ui_seed=True）：创建 story 走 IntakeStartModal（点"新建并开始"
    → 填表 → 选 hc-all 工作区 → 勾 hc-config 项目 → 填 PRD → "准备 PRD并进入
    规划"），plan 确认/stage 推进/clarify 答题全部走 SPA，无任何 API 操作回退。
    连真实 DB（real_webbridge_server），因 IntakeStartModal 工作区下拉需已注册。
    """
    if not _claude_cli_present():
        pytest.skip("claude/codex CLI 不在 PATH — real_web_e2e 需要真实 AI CLI")
    ok, why = _hc_all_ready()
    if not ok:
        pytest.skip(f"hc-all/hc-config not usable: {why}")

    api = StoryApiClient(real_webbridge_server.base_url)
    prd_content = (SCENARIO / "PRD.md").read_text(encoding="utf-8")
    prep = InjectedSpecPrep(inject=_INJECT, red_files=_RED_FILES)

    try:
        result = run_scenario(
            server=real_webbridge_server,
            webbridge=webbridge,
            workspace=HC_ALL_WS,  # hc-all 容器作为 workspace
            scenario_dir=SCENARIO,
            story_key=STORY_KEY,
            prd_content=prd_content,
            prep=prep,
            stages=_STAGES,
            title="WebBridge E2E hc-config",
            gate_timeout=2400.0,  # mvn + real AI 需要充足时间
            # 全 UI 模式：创建 + 所有运行时交互都走 SPA，失败即报错不回退 API
            ui_seed=True,
            ui_workspace_path=str(HC_ALL_WS),  # 工作区下拉选 D:\hc-all
            ui_project_name=SUBPROJECT,  # 受影响项目勾 hc-config
        )
    except ScenarioError as exc:
        pytest.fail(
            f"scenario drive failed (UI path broke, not a bad artifact): {exc}"
        )

    HcAllJavaJudge(
        subproject=SUBPROJECT,
        module=MODULE,
        impl_rel_package=IMPL_PKG,
        class_name=CLASS,
    ).judge(
        result,
        workspace=HC_ALL_WS,
        story_key=STORY_KEY,
        stages=_STAGES,
        api=api,
    )
