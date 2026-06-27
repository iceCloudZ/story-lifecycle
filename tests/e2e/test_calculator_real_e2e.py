"""Real E2E: calculator 红→绿，真实 AI 跑完整 story 飞轮。

@real_e2e 默认跳过（见 conftest.py）；`pytest -m real_e2e` 手动/nightly 跑，
需 claude/codex CLI + key。
"""
import pytest
from pathlib import Path

from testing.harness import run_real_story
from testing import workspace as ws
from testing import asserters

_ROOT = Path(__file__).resolve().parents[2]
SCENARIO = _ROOT / "packages" / "testing" / "src" / "testing" / "scenarios" / "calculator"
DB = _ROOT / "packages" / "story-miner" / "data" / "transcripts.db"
STORY_KEY = "E2E-CALC"


@pytest.mark.real_e2e
def test_calculator_real_e2e():
    """真实 AI 实现 Calculator：design→implement→verify，17 测试全过，飞轮联动。"""
    ws.reset_workspace(SCENARIO, STORY_KEY)
    result = run_real_story(
        workspace=str(SCENARIO),
        story_key=STORY_KEY,
        prd_path=str(SCENARIO / "PRD.md"),
        stages=["design", "implement", "verify"],
        adapter="claude",
    )
    asserters.assert_design(result, SCENARIO, STORY_KEY)
    asserters.assert_implement(result, SCENARIO, STORY_KEY)
    asserters.assert_verify(result, SCENARIO, STORY_KEY)
    asserters.assert_done_retrospect(SCENARIO, STORY_KEY)
    # 跑真实 miner ingest+link（作用域限 calculator 工作区），让 transcript 绑回 story
    asserters.run_miner_loopback(SCENARIO)
    asserters.assert_miner_linked(DB, STORY_KEY)
