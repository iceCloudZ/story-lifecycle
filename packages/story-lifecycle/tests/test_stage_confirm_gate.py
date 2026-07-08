"""PLAN-stage-confirm-gate · stage 间确认闸 + resume 跳过 + 认领游离 done.

覆盖 docs/PLAN-stage-confirm-gate.md 的核心行为(决策 2/3/4):
- ``confirm=True`` 的非末段 stage done 后 → story ``paused`` + 写 ``_stage_gate``,
  释放 driver claim(不自动推进);不 spawn 下一 stage。
- ``confirm=False`` → 一气跑完(不 paused)。
- resume:``_completed_stages`` 已记录的 stage 不重 spawn(start_idx 跳过)。
- 认领游离 done:``_completed_stages`` 为空但某 stage done file 已存在 →
  认领进 ``_completed_stages`` 并记 ``completed`` 事件,不重跑。

headless 路径好 mock(Popen / done file),用 headless-smoke profile + monkeypatch
``resolve_profile`` 把目标 stage 的 ``confirm`` 顶成 True。
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from story_lifecycle.infra.db import models as db
from story_lifecycle.infra.paths import stage_done_file
from story_lifecycle.orchestrator.engine.planner import continue_orchestrator_agent
from story_lifecycle.orchestrator.engine.profile_loader import (
    ResolvedProfile,
    StageConfig,
    resolve_profile,
)


@pytest.fixture
def story(tmp_path):
    """headless-smoke profile → continue_orchestrator_agent 走 headless 路径。"""
    return db.create_story(
        story_key="STORY-GATE-1",
        title="测试 stage 确认闸",
        workspace=str(tmp_path),
        profile="headless-smoke",
        current_stage="design",
    )


def _set_actions(story, actions):
    """直接写 _agent_actions + _plan_confirmed 进 context(跳过 LLM 规划)。"""
    updated = db.get_story(story["story_key"])
    ctx = json.loads(updated.get("context_json", "{}"))
    ctx["_agent_actions"] = actions
    ctx["_plan_confirmed"] = True
    db.update_story(
        story["story_key"],
        context_json=json.dumps(ctx, ensure_ascii=False),
    )


def _profile_with(confirm_map):
    """构造 ResolvedProfile:headless,design/implement 两 stage,confirm 按 map。"""
    stages = {}
    stages["design"] = StageConfig(
        order=1,
        description="design",
        execution_mode="headless",
        confirm=confirm_map.get("design", False),
    )
    stages["implement"] = StageConfig(
        order=2,
        description="implement",
        execution_mode="headless",
        confirm=confirm_map.get("implement", False),
    )
    return ResolvedProfile(
        name="headless-smoke",
        cli="claude",
        execution_mode="headless",
        stages=stages,
    )


def _mock_proc():
    p = MagicMock()
    p.poll.return_value = None
    p.stdin = MagicMock()
    p.stdout = MagicMock()
    p.stderr = MagicMock()
    return p


# ---- confirm=True 触发 paused + _stage_gate ----


def test_confirm_true_pauses_after_stage_done(story, tmp_path):
    """design.confirm=True 且后面还有 implement → done 后 paused + _stage_gate。"""
    _set_actions(
        story,
        [
            {"action": "launch", "stage": "design", "adapter": "claude", "focus": "设计"},
            {"action": "launch", "stage": "implement", "adapter": "claude", "focus": "实现"},
        ],
    )

    design_done = stage_done_file(tmp_path, story["story_key"], "design")
    design_done.parent.mkdir(parents=True, exist_ok=True)

    def _popen_side_effect(*a, **kw):
        design_done.write_text(json.dumps({"summary": "design done"}), encoding="utf-8")
        return _mock_proc()

    with patch("subprocess.Popen", side_effect=_popen_side_effect), patch(
        "story_lifecycle.orchestrator.engine.claude_stream.supervise_headless_stdout"
    ), patch("story_lifecycle.orchestrator.engine.planner._kill_headless"), patch(
        "story_lifecycle.orchestrator.engine.profile_loader.resolve_profile",
        return_value=_profile_with({"design": True}),
    ):
        continue_orchestrator_agent(story["story_key"], headless=True)

    updated = db.get_story(story["story_key"])
    assert updated["status"] == "paused"
    ctx = json.loads(updated.get("context_json", "{}"))
    gate = ctx.get("_stage_gate")
    assert gate is not None
    assert gate["completed_stage"] == "design"
    assert gate["next_stage"] == "implement"
    assert gate["awaiting_confirm"] is True
    # design 记进 _completed_stages
    assert "design" in ctx.get("_completed_stages", [])
    # implement 尚未完成
    assert "implement" not in ctx.get("_completed_stages", [])
    # stage_gate_reached 事件已落
    gate_events = db.get_recent_quality_events(
        story["story_key"], ["stage_gate_reached"], limit=5
    )
    assert len(gate_events) == 1


def test_confirm_false_does_not_pause(story, tmp_path):
    """confirm=False → 两个 stage 一气跑完,status=completed,无 _stage_gate。"""
    _set_actions(
        story,
        [
            {"action": "launch", "stage": "design", "adapter": "claude", "focus": "设计"},
            {"action": "launch", "stage": "implement", "adapter": "claude", "focus": "实现"},
        ],
    )

    done_design = stage_done_file(tmp_path, story["story_key"], "design")
    done_impl = stage_done_file(tmp_path, story["story_key"], "implement")
    done_design.parent.mkdir(parents=True, exist_ok=True)
    spawn_count = [0]

    def _popen_side_effect(*a, **kw):
        spawn_count[0] += 1
        if spawn_count[0] == 1:
            done_design.write_text(json.dumps({"summary": "d"}), encoding="utf-8")
        else:
            done_impl.write_text(json.dumps({"summary": "i"}), encoding="utf-8")
        return _mock_proc()

    with patch("subprocess.Popen", side_effect=_popen_side_effect), patch(
        "story_lifecycle.orchestrator.engine.claude_stream.supervise_headless_stdout"
    ), patch("story_lifecycle.orchestrator.engine.planner._kill_headless"), patch(
        "story_lifecycle.orchestrator.engine.profile_loader.resolve_profile",
        return_value=_profile_with({}),  # 全部 confirm=False
    ):
        continue_orchestrator_agent(story["story_key"], headless=True)

    updated = db.get_story(story["story_key"])
    assert updated["status"] == "completed"
    ctx = json.loads(updated.get("context_json", "{}"))
    assert ctx.get("_stage_gate") is None
    assert spawn_count[0] == 2  # 两个 stage 都 spawn 了


# ---- resume 跳过已完成 stage ----


def test_resume_skips_completed_stage(story, tmp_path):
    """_completed_stages=['design'] → resume 不重 spawn design,直接跑 implement。"""
    _set_actions(
        story,
        [
            {"action": "launch", "stage": "design", "adapter": "claude", "focus": "设计"},
            {"action": "launch", "stage": "implement", "adapter": "claude", "focus": "实现"},
        ],
    )
    # 模拟之前 design 已完成(gate paused 后 resume 的状态)
    updated = db.get_story(story["story_key"])
    ctx = json.loads(updated.get("context_json", "{}"))
    ctx["_completed_stages"] = ["design"]
    db.update_story(
        story["story_key"],
        context_json=json.dumps(ctx, ensure_ascii=False),
    )

    impl_done = stage_done_file(tmp_path, story["story_key"], "implement")
    impl_done.parent.mkdir(parents=True, exist_ok=True)

    def _popen_side_effect(*a, **kw):
        # kw 里没有 stage;靠 done 文件路径推断哪个 stage 被跑
        impl_done.write_text(json.dumps({"summary": "impl done"}), encoding="utf-8")
        return _mock_proc()

    with patch("subprocess.Popen", side_effect=_popen_side_effect) as mock_popen, patch(
        "story_lifecycle.orchestrator.engine.claude_stream.supervise_headless_stdout"
    ), patch("story_lifecycle.orchestrator.engine.planner._kill_headless"), patch(
        "story_lifecycle.orchestrator.engine.profile_loader.resolve_profile",
        return_value=_profile_with({}),
    ):
        continue_orchestrator_agent(story["story_key"], headless=True)

    # resume 后只 spawn 一次(implement),design 被跳过不重 spawn
    assert mock_popen.call_count == 1
    updated = db.get_story(story["story_key"])
    assert updated["status"] == "completed"
    ctx = json.loads(updated.get("context_json", "{}"))
    assert ctx.get("_completed_stages") == ["design", "implement"]


# ---- 认领游离 done ----


def test_orphan_done_claimed_not_rerun(story, tmp_path):
    """_completed_stages 空 + design done file 已存在 → 认领,不重跑 design。"""
    _set_actions(
        story,
        [
            {"action": "launch", "stage": "design", "adapter": "claude", "focus": "设计"},
            {"action": "launch", "stage": "implement", "adapter": "claude", "focus": "实现"},
        ],
    )
    # 手动产出 design done file(从未走过自动链路)
    design_done = stage_done_file(tmp_path, story["story_key"], "design")
    design_done.parent.mkdir(parents=True, exist_ok=True)
    design_done.write_text(json.dumps({"summary": "manual design"}), encoding="utf-8")

    impl_done = stage_done_file(tmp_path, story["story_key"], "implement")
    spawn_count = [0]

    def _popen_side_effect(*a, **kw):
        spawn_count[0] += 1
        impl_done.write_text(json.dumps({"summary": "impl done"}), encoding="utf-8")
        return _mock_proc()

    with patch("subprocess.Popen", side_effect=_popen_side_effect) as mock_popen, patch(
        "story_lifecycle.orchestrator.engine.claude_stream.supervise_headless_stdout"
    ), patch("story_lifecycle.orchestrator.engine.planner._kill_headless"), patch(
        "story_lifecycle.orchestrator.engine.profile_loader.resolve_profile",
        return_value=_profile_with({}),
    ):
        continue_orchestrator_agent(story["story_key"], headless=True)

    # design 被认领 → 只 spawn implement 一次
    assert mock_popen.call_count == 1
    updated = db.get_story(story["story_key"])
    ctx = json.loads(updated.get("context_json", "{}"))
    assert "design" in ctx.get("_completed_stages", [])
    assert "implement" in ctx.get("_completed_stages", [])
    # 认领时也记了 design 的 completed 事件
    completed = db.get_recent_quality_events(
        story["story_key"], ["completed"], limit=10
    )
    completed_stages = {e["stage"] for e in completed}
    assert "design" in completed_stages
    assert "implement" in completed_stages


# ---- minimal.yaml 默认开启确认闸 ----


def test_minimal_profile_design_build_confirm_true():
    """minimal.yaml:design/build confirm=true(人主导默认 profile 开确认闸),
    verify confirm=false(最后阶段走自己的 quality gate)。CI/自动化 profile 不在此断言。
    """
    rp = resolve_profile("minimal")
    assert rp.stages["design"].confirm is True
    assert rp.stages["build"].confirm is True
    assert rp.stages["verify"].confirm is False
