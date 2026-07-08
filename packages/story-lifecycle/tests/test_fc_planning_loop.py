"""T2.1 · FC 规划循环(mock LLM 生成 actions).

用 mock LLM 驱动 run_orchestrator_agent 完整 FC 规划循环,
断言产出的 _agent_actions 正确且 _plan_confirmed=False(暂停语义)。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from story_lifecycle.infra.db import models as db
from story_lifecycle.orchestrator.engine.planner import run_orchestrator_agent


@pytest.fixture
def story(tmp_path):
    """Create a minimal story in the isolated DB."""
    return db.create_story(
        story_key="STORY-FC-1",
        title="测试 FC 规划循环",
        workspace=str(tmp_path),
        profile="minimal",
        current_stage="design",
    )


def _make_mock_llm(tool_calls_sequence: list[list[dict]]):
    """Return a mock LLM whose invoke_with_tools yields each round's tool_calls."""
    mock_llm = MagicMock()

    def _invoke_with_tools(*args, **kwargs):
        calls = tool_calls_sequence.pop(0) if tool_calls_sequence else []
        return {
            "message": {"role": "assistant", "content": "planning"},
            "tool_calls": calls,
        }

    mock_llm.invoke_with_tools = _invoke_with_tools
    return mock_llm


def test_fc_planning_loop_generates_actions_and_pauses(story, tmp_path):
    """Mock LLM returns plan_step/skip_stage tool calls; agent writes actions and pauses."""
    tool_calls_sequence = [
        [
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "plan_step",
                    "arguments": {
                        "stage": "design",
                        "adapter": "claude",
                        "focus": "调研现有代码结构",
                    },
                },
            },
            {
                "id": "call-2",
                "type": "function",
                "function": {
                    "name": "plan_step",
                    "arguments": {
                        "stage": "build",
                        "adapter": "claude",
                        "focus": "实现核心功能",
                    },
                },
            },
            {
                "id": "call-3",
                "type": "function",
                "function": {
                    "name": "skip_stage",
                    "arguments": {
                        "stage": "verify",
                        "reason": "无验证需求",
                    },
                },
            },
        ],
        [],  # second round: no more tool calls -> agent finishes planning
    ]

    with patch(
        "story_lifecycle.orchestrator.engine.planner.get_llm",
        return_value=_make_mock_llm(tool_calls_sequence),
    ):
        result = run_orchestrator_agent(story["story_key"])

    assert result["status"] == "planning"
    assert len(result["actions"]) == 3

    launch_actions = [a for a in result["actions"] if a.get("action") == "launch"]
    skip_actions = [a for a in result["actions"] if a.get("action") == "skip"]

    assert len(launch_actions) == 2
    assert launch_actions[0]["stage"] == "design"
    assert launch_actions[0]["adapter"] == "claude"
    assert "调研现有代码结构" in launch_actions[0]["focus"]
    assert launch_actions[1]["stage"] == "build"

    assert len(skip_actions) == 1
    assert skip_actions[0]["stage"] == "verify"

    # Confirm pause semantics: _plan_confirmed=False written to DB context
    updated_story = db.get_story(story["story_key"])
    ctx = updated_story.get("context_json", "{}")
    import json

    ctx_dict = json.loads(ctx)
    assert ctx_dict.get("_plan_confirmed") is False
    assert ctx_dict.get("_agent_actions") == result["actions"]
    assert "plan_summary" in ctx_dict
    assert "design" in ctx_dict["plan_summary"]
