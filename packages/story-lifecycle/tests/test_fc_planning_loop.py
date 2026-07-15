"""T2.1 · 规划循环(mock LLM 生成 actions).

REFACTOR §5.4:FC 循环改为单次 invoke_structured。用 mock LLM 驱动
run_orchestrator_agent,断言产出的 _agent_actions 正确且 _plan_confirmed=False(暂停语义)。
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from story_lifecycle.infra.db import models as db
from story_lifecycle.orchestrator.engine.planner import run_orchestrator_agent


@pytest.fixture
def story(tmp_path):
    """Create a minimal story in the isolated DB."""
    return db.create_story(
        story_key="STORY-FC-1",
        title="测试规划循环",
        workspace=str(tmp_path),
        profile="minimal",
        current_stage="design",
    )


class _FakeStage:
    def __init__(self, stage, skip=False, focus=""):
        self.stage = stage
        self.skip = skip
        self.focus = focus


class _FakePlanResult:
    def __init__(self, stages):
        self.stages = stages


def _make_mock_llm(stages):
    """Return a mock LLM whose invoke_structured returns a PlanResult-like object."""
    mock_llm = MagicMock()
    mock_llm.api_key = "fake"
    mock_llm.invoke_structured.return_value = _FakePlanResult(stages)
    return mock_llm


def test_planning_generates_actions_and_pauses(story, tmp_path):
    """Mock LLM 返回 PlanResult(design/build launch + verify skip)→ actions + 暂停。"""
    stages = [
        _FakeStage("design", skip=False, focus="调研现有代码结构"),
        _FakeStage("build", skip=False, focus="实现核心功能"),
        _FakeStage("verify", skip=True, focus="无验证需求"),
    ]

    with patch(
        "story_lifecycle.orchestrator.engine.planner.get_llm",
        return_value=_make_mock_llm(stages),
    ):
        result = run_orchestrator_agent(story["story_key"])

    assert result["status"] == "planning"
    assert len(result["actions"]) == 3

    launch_actions = [a for a in result["actions"] if a.get("action") == "launch"]
    skip_actions = [a for a in result["actions"] if a.get("action") == "skip"]

    assert len(launch_actions) == 2
    assert launch_actions[0]["stage"] == "design"
    # adapter 由 profile 决定(minimal.yaml design=claude),不是模型选
    assert launch_actions[0]["adapter"] == "claude"
    assert "调研现有代码结构" in launch_actions[0]["focus"]
    assert launch_actions[1]["stage"] == "build"

    assert len(skip_actions) == 1
    assert skip_actions[0]["stage"] == "verify"

    # 确认暂停语义:_plan_confirmed=False 写入 DB context
    updated_story = db.get_story(story["story_key"])
    ctx = updated_story.get("context_json", "{}")
    ctx_dict = json.loads(ctx)
    assert ctx_dict.get("_plan_confirmed") is False
    assert ctx_dict.get("_agent_actions") == result["actions"]
    assert "plan_summary" in ctx_dict
    assert "design" in ctx_dict["plan_summary"]
