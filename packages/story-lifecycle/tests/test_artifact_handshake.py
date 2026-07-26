"""成果物落地轮询(超时/成功)。

测 continue_orchestrator_agent 中对 stage.artifacts 落地的轮询:
- 成功路径:timeout 内成果物落地(spec.md 出现)→ stage 完成
- 超时路径:timeout 内成果物没落地 → stage 失败且不无限挂起

约束:不修改 pty.py;mock 时间加速轮询;mock CLI 不启动真实进程。
headless-smoke profile 的 design stage 声明 artifacts: [story/spec.md]。
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from story_lifecycle.infra.db import models as db
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
    """成果物(spec.md)落地 → stage 完成(STEP 1.4:替 done.json 自报)。"""
    _setup_planning(story)

    # STEP 1.4:完成信号是 stage.artifacts 落地(headless-smoke design → story/spec.md),
    # 不是 done.json。code agent(或测试)落地成果物文件,planner 据此推进。
    spec_path = tmp_path / "story" / "spec.md"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text("# 设计方案\n", encoding="utf-8")

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
    """成果物没落地 within timeout → stage 失败且不无限挂起。"""
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
