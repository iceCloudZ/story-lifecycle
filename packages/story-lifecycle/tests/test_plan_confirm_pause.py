"""T2.2 · plan_confirm 暂停语义.

证明 _plan_confirmed=False 时 actions 不执行,confirm 后 continue_orchestrator_agent
才执行 actions。
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from story_lifecycle.infra.db import models as db
from story_lifecycle.orchestrator.engine.planner import (
    continue_orchestrator_agent,
    run_orchestrator_agent,
)


@pytest.fixture
def story(tmp_path):
    """Create a story using the headless-smoke profile so continue_orchestrator_agent
    takes the headless path (easier to mock than PTY)."""
    return db.create_story(
        story_key="STORY-CONFIRM-1",
        title="测试 confirm 暂停语义",
        workspace=str(tmp_path),
        profile="headless-smoke",
        current_stage="design",
    )


def _make_mock_llm():
    """Mock LLM whose invoke_structured returns a single-design-stage PlanResult."""
    mock_llm = MagicMock()
    mock_llm.api_key = "fake"

    class _FakeStage:
        def __init__(self, stage, skip=False, focus="", task_actions=None, grill=False):
            self.stage = stage
            self.skip = skip
            self.focus = focus
            self.task_actions = task_actions or []
            self.grill = grill

    class _FakePlanResult:
        def __init__(self, stages):
            self.stages = stages

    mock_llm.invoke_structured.return_value = _FakePlanResult([
        _FakeStage("design", skip=False, focus="分析 bug 并设计方案"),
    ])
    return mock_llm


def test_planning_pauses_with_plan_confirmed_false(story):
    """After run_orchestrator_agent, plan is paused and no CLI was launched."""
    with patch(
        "story_lifecycle.orchestrator.engine.planner.get_llm",
        return_value=_make_mock_llm(),
    ), patch("subprocess.Popen") as mock_popen:
        result = run_orchestrator_agent(story["story_key"])

    assert result["status"] == "planning"
    assert len(result["actions"]) == 1

    updated = db.get_story(story["story_key"])
    ctx = json.loads(updated.get("context_json", "{}"))
    assert ctx.get("_plan_confirmed") is False
    assert len(ctx.get("_agent_actions", [])) == 1

    # No CLI launch during planning
    mock_popen.assert_not_called()


def test_continue_after_confirm_executes_actions(story, tmp_path):
    """Simulate confirm (_plan_confirmed=True) and assert continue launches CLI.

    STEP 1.4:完成信号是成果物落地(headless-smoke design → story/spec.md),不是 done.json。
    成果物由 Popen side_effect 在 spawn 时落地(而非预置):预置会被 orphan-artifacts
    claim 当成已完成跳过 —— spawn 时落地保持本测试原意("confirm → CLI 启动")。
    """
    # Pre-plan: run planning first to get actions into context
    with patch(
        "story_lifecycle.orchestrator.engine.planner.get_llm",
        return_value=_make_mock_llm(),
    ):
        run_orchestrator_agent(story["story_key"])

    # Simulate user confirm
    updated = db.get_story(story["story_key"])
    ctx = json.loads(updated.get("context_json", "{}"))
    ctx["_plan_confirmed"] = True
    db.update_story(
        story["story_key"],
        context_json=json.dumps(ctx, ensure_ascii=False),
    )

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = MagicMock()
    mock_proc.stderr = MagicMock()

    # 在 spawn 时落地成果物文件(headless-smoke design → story/spec.md),
    # 让 poll loop 立即检测到成果物落地而推进(不预置 → 防 orphan-claim 跳过)。
    spec_path = tmp_path / "story" / "spec.md"
    spec_path.parent.mkdir(parents=True, exist_ok=True)

    def _popen_side_effect(*args, **kwargs):
        spec_path.write_text("# 设计方案\n", encoding="utf-8")
        return mock_proc

    with patch("subprocess.Popen", side_effect=_popen_side_effect) as mock_popen, patch(
        "story_lifecycle.orchestrator.engine.claude_stream.supervise_headless_stdout"
    ), patch(
        "story_lifecycle.orchestrator.engine.planner._kill_headless"
    ):
        continue_orchestrator_agent(story["story_key"], headless=True)

    # CLI was launched -> actions executed
    mock_popen.assert_called_once()

    # _plan_confirmed should remain True after continue starts executing
    updated = db.get_story(story["story_key"])
    ctx = json.loads(updated.get("context_json", "{}"))
    assert ctx.get("_plan_confirmed") is True
    # design recorded as completed after its artifact landed
    assert "design" in ctx.get("_completed_stages", [])
