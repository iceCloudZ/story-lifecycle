"""T2.3 · .done 握手轮询(超时/成功).

测 continue_orchestrator_agent 中对 `.story/done/<key>/<stage>.json` 的轮询:
- 成功路径:timeout 内发现 done 文件 → stage 完成
- 超时路径:timeout 内无 done 文件 → stage 失败且不无限挂起

约束:不修改 pty.py;mock 时间加速轮询;mock CLI 不启动真实进程。
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from story_lifecycle.infra.db import models as db
from story_lifecycle.infra.paths import stage_done_file
from story_lifecycle.orchestrator.engine.planner import continue_orchestrator_agent, run_orchestrator_agent


@pytest.fixture
def story(tmp_path):
    return db.create_story(
        story_key="STORY-DONE-1",
        title="测试 done 握手轮询",
        workspace=str(tmp_path),
        profile="headless-smoke",
        current_stage="design",
    )


def _make_mock_llm():
    """Mock LLM that plans a single design stage (REFACTOR §5.4: invoke_structured)。"""
    mock_llm = MagicMock()
    mock_llm.api_key = "fake"

    # invoke_structured 返回 PlanResult-like 对象(有 .stages 属性)
    class FakeStage:
        def __init__(self, stage, skip=False, focus="", task_actions=None, grill=False):
            self.stage = stage
            self.skip = skip
            self.focus = focus
            self.task_actions = task_actions or []
            self.grill = grill

    class FakePlanResult:
        def __init__(self, stages):
            self.stages = stages

    mock_llm.invoke_structured.return_value = FakePlanResult([
        FakeStage("design", skip=False, focus="设计方案"),
    ])

    # 保留 invoke_with_tools mock(向后兼容,某些测试可能还调)
    def _invoke_with_tools(*args, **kwargs):
        return {
            "message": {"role": "assistant", "content": "planning"},
            "tool_calls": [],
        }

    mock_llm.invoke_with_tools = _invoke_with_tools
    return mock_llm


def _setup_planning(story):
    """Run planning phase and confirm the plan."""
    with patch(
        "story_lifecycle.orchestrator.engine.planner.get_llm",
        return_value=_make_mock_llm(),
    ):
        run_orchestrator_agent(story["story_key"])

    updated = db.get_story(story["story_key"])
    ctx = json.loads(updated.get("context_json", "{}"))
    ctx["_plan_confirmed"] = True
    db.update_story(
        story["story_key"],
        context_json=json.dumps(ctx, ensure_ascii=False),
    )


def test_done_handshake_success(story, tmp_path):
    """Done file appears within timeout -> stage completes."""
    _setup_planning(story)

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

    with patch("subprocess.Popen", return_value=mock_proc), patch(
        "story_lifecycle.orchestrator.engine.claude_stream.supervise_headless_stdout"
    ), patch("story_lifecycle.orchestrator.engine.planner._kill_headless"):
        continue_orchestrator_agent(story["story_key"], headless=True)

    updated = db.get_story(story["story_key"])
    assert updated["status"] == "completed"

    events = db.get_recent_quality_events(story["story_key"], ["completed"], limit=1)
    assert len(events) == 1


def test_done_handshake_timeout(story, tmp_path, monkeypatch):
    """No done file within timeout -> stage fails and loop does not hang."""
    _setup_planning(story)

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = MagicMock()
    mock_proc.stderr = MagicMock()

    # Accelerate the poll loop: make time.sleep a no-op.
    monkeypatch.setattr(time, "sleep", lambda s: None)

    with patch("subprocess.Popen", return_value=mock_proc), patch(
        "story_lifecycle.orchestrator.engine.claude_stream.supervise_headless_stdout"
    ), patch("story_lifecycle.orchestrator.engine.planner._kill_headless"):
        continue_orchestrator_agent(story["story_key"], headless=True)

    updated = db.get_story(story["story_key"])
    assert updated["status"] == "failed"
    assert "timed out" in updated.get("last_error", "").lower()
