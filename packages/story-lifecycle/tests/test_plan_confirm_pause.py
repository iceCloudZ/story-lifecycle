"""T2.2 · plan_confirm 暂停语义.

证明 _plan_confirmed=False 时 actions 不执行,confirm 后 continue_orchestrator_agent
才执行 actions。
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from story_lifecycle.infra.db import models as db
from story_lifecycle.infra.paths import stage_done_file
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
    """Mock LLM that returns one plan_step tool call then stops."""
    mock_llm = MagicMock()
    rounds = [
        [
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "plan_step",
                    "arguments": {
                        "stage": "design",
                        "adapter": "claude",
                        "focus": "分析 bug 并设计方案",
                    },
                },
            }
        ],
        [],
    ]

    def _invoke_with_tools(*args, **kwargs):
        calls = rounds.pop(0) if rounds else []
        return {
            "message": {"role": "assistant", "content": "planning"},
            "tool_calls": calls,
        }

    mock_llm.invoke_with_tools = _invoke_with_tools
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
    """Simulate confirm (_plan_confirmed=True) and assert continue launches CLI."""
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

    # Prepare done file so the poll loop completes immediately
    done_path = stage_done_file(tmp_path, story["story_key"], "design")
    done_path.parent.mkdir(parents=True, exist_ok=True)
    done_path.write_text(
        json.dumps({"summary": "design done", "tests_passed": True}),
        encoding="utf-8",
    )

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = MagicMock()
    mock_proc.stderr = MagicMock()

    with patch("subprocess.Popen", return_value=mock_proc) as mock_popen, patch(
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
