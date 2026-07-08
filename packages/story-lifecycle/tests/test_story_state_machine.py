"""STORY-STATE-MODEL · Story 业务状态机 + 两层闸优先级。

Story 状态(开发/测试/上线)是独立第一公民。driver 跑完一个状态的所有 stages 后,
按该状态 confirm 规则转移:ui_button→paused+`_story_state_gate`;config(auto)→
自动推进 lifecycle_state;none→推进;终态→completed。Story 状态闸优先于阶段间闸。

用 headless 路径 mock(Popen / done file)。构造带 story_states 的 ResolvedProfile。
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
    return db.create_story(
        story_key="STORY-SSM-1",
        title="测试 Story 状态机",
        workspace=str(tmp_path),
        profile="headless-smoke",
        current_stage="design",
    )


def _set_actions(story, actions):
    updated = db.get_story(story["story_key"])
    ctx = json.loads(updated.get("context_json", "{}"))
    ctx["_agent_actions"] = actions
    ctx["_plan_confirmed"] = True
    db.update_story(
        story["story_key"],
        context_json=json.dumps(ctx, ensure_ascii=False),
    )


def _profile_with_story_states(story_states, confirm_map=None):
    """构造 ResolvedProfile:headless,design/build 两 stage,带 story_states。

    confirm_map: per-stage confirm (阶段间闸),默认全 False(让 Story 状态闸不被阶段闸抢)。
    """
    confirm_map = confirm_map or {}
    stages = {
        "design": StageConfig(
            order=1, description="design", execution_mode="headless",
            confirm=confirm_map.get("design", False),
        ),
        "build": StageConfig(
            order=2, description="build", execution_mode="headless",
            confirm=confirm_map.get("build", False),
        ),
    }
    return ResolvedProfile(
        name="headless-smoke",
        cli="claude",
        execution_mode="headless",
        stages=stages,
        story_states=story_states,
    )


def _mock_proc():
    p = MagicMock()
    p.poll.return_value = None
    p.stdin = MagicMock()
    p.stdout = MagicMock()
    p.stderr = MagicMock()
    return p


# ---- Story 状态闸:ui_button → paused + _story_state_gate ----


def test_story_state_gate_pauses_when_state_stages_done(story, tmp_path):
    """开发=[design,build] 全 done,confirm=ui_button → paused + _story_state_gate。"""
    _set_actions(
        story,
        [
            {"action": "launch", "stage": "design", "adapter": "claude", "focus": "设计"},
            {"action": "launch", "stage": "build", "adapter": "claude", "focus": "实现"},
        ],
    )
    states = {
        "开发": {"stages": ["design", "build"], "next": "测试",
                 "confirm": {"type": "ui_button", "label": "进入测试"}},
        "测试": {"stages": [], "next": None, "confirm": {"type": "none"}},
    }
    design_done = stage_done_file(tmp_path, story["story_key"], "design")
    build_done = stage_done_file(tmp_path, story["story_key"], "build")
    design_done.parent.mkdir(parents=True, exist_ok=True)

    call = {"n": 0}

    def _popen_side_effect(*a, **kw):
        call["n"] += 1
        if call["n"] == 1:
            design_done.write_text(json.dumps({"summary": "design done"}), encoding="utf-8")
        else:
            build_done.write_text(json.dumps({"summary": "build done"}), encoding="utf-8")
        return _mock_proc()

    with patch("subprocess.Popen", side_effect=_popen_side_effect), patch(
        "story_lifecycle.orchestrator.engine.claude_stream.supervise_headless_stdout"
    ), patch("story_lifecycle.orchestrator.engine.planner._kill_headless"), patch(
        "story_lifecycle.orchestrator.engine.profile_loader.resolve_profile",
        return_value=_profile_with_story_states(states),
    ):
        continue_orchestrator_agent(story["story_key"], headless=True)

    updated = db.get_story(story["story_key"])
    assert updated["status"] == "paused"
    ctx = json.loads(updated.get("context_json", "{}"))
    gate = ctx.get("_story_state_gate")
    assert gate is not None
    assert gate["from"] == "开发"
    assert gate["to"] == "测试"
    assert gate["awaiting_confirm"] is True
    # 两个 stage 都记完成
    assert "design" in ctx.get("_completed_stages", [])
    assert "build" in ctx.get("_completed_stages", [])
    # lifecycle_state 仍是开发(未推进,等人确认)
    assert updated["lifecycle_state"] == "开发"


# ---- Story 状态闸:config auto → 自动推进不停顿 ----


def test_story_state_auto_advances_on_config(story, tmp_path):
    """开发全 done,confirm=config 且 STORY_AUTO_ADVANCE_DEV=true → 自动推进 lifecycle_state。"""
    _set_actions(
        story,
        [
            {"action": "launch", "stage": "design", "adapter": "claude", "focus": "设计"},
            {"action": "launch", "stage": "build", "adapter": "claude", "focus": "实现"},
        ],
    )
    states = {
        "开发": {"stages": ["design", "build"], "next": "测试",
                 "confirm": {"type": "config", "key": "auto_advance_dev"}},
        "测试": {"stages": [], "next": None, "confirm": {"type": "none"}},
    }
    design_done = stage_done_file(tmp_path, story["story_key"], "design")
    build_done = stage_done_file(tmp_path, story["story_key"], "build")
    design_done.parent.mkdir(parents=True, exist_ok=True)

    call = {"n": 0}

    def _popen_side_effect(*a, **kw):
        call["n"] += 1
        if call["n"] == 1:
            design_done.write_text(json.dumps({"summary": "design done"}), encoding="utf-8")
        else:
            build_done.write_text(json.dumps({"summary": "build done"}), encoding="utf-8")
        return _mock_proc()

    with patch.dict("os.environ", {"STORY_AUTO_ADVANCE_DEV": "true"}), patch(
        "subprocess.Popen", side_effect=_popen_side_effect
    ), patch(
        "story_lifecycle.orchestrator.engine.claude_stream.supervise_headless_stdout"
    ), patch("story_lifecycle.orchestrator.engine.planner._kill_headless"), patch(
        "story_lifecycle.orchestrator.engine.profile_loader.resolve_profile",
        return_value=_profile_with_story_states(states),
    ):
        continue_orchestrator_agent(story["story_key"], headless=True)

    updated = db.get_story(story["story_key"])
    # config auto → 直接推进到测试;测试 stages 空 → 终态 completed
    assert updated["lifecycle_state"] == "测试"
    assert updated["status"] == "completed"
    ctx = json.loads(updated.get("context_json", "{}"))
    assert ctx.get("_lifecycle_state") == "测试"
    # 无 _story_state_gate(自动推进不停顿)
    assert ctx.get("_story_state_gate") is None


# ---- 终态:所有 Story 状态跑完 → completed ----


def test_terminal_state_completes_story(story, tmp_path):
    """开发=[design] done,next=null(终态) → story completed,无 paused。"""
    _set_actions(
        story,
        [{"action": "launch", "stage": "design", "adapter": "claude", "focus": "设计"}],
    )
    states = {
        "开发": {"stages": ["design"], "next": None, "confirm": {"type": "none"}},
    }
    design_done = stage_done_file(tmp_path, story["story_key"], "design")
    design_done.parent.mkdir(parents=True, exist_ok=True)

    def _popen_side_effect(*a, **kw):
        design_done.write_text(json.dumps({"summary": "design done"}), encoding="utf-8")
        return _mock_proc()

    with patch("subprocess.Popen", side_effect=_popen_side_effect), patch(
        "story_lifecycle.orchestrator.engine.claude_stream.supervise_headless_stdout"
    ), patch("story_lifecycle.orchestrator.engine.planner._kill_headless"), patch(
        "story_lifecycle.orchestrator.engine.profile_loader.resolve_profile",
        return_value=_profile_with_story_states(states),
    ):
        continue_orchestrator_agent(story["story_key"], headless=True)

    updated = db.get_story(story["story_key"])
    assert updated["status"] == "completed"
    assert updated["lifecycle_state"] == "开发"


# ---- 两层闸优先级:Story 状态闸优先于阶段间闸 ----


def test_story_state_gate_takes_priority_over_stage_gate(story, tmp_path):
    """开发=[design,build],design.confirm=True(阶段闸) 且 build 也 done(状态闸触发)。
    build done 时两个闸都满足 → Story 状态闸优先(_story_state_gate,不是 _stage_gate)。"""
    _set_actions(
        story,
        [
            {"action": "launch", "stage": "design", "adapter": "claude", "focus": "设计"},
            {"action": "launch", "stage": "build", "adapter": "claude", "focus": "实现"},
        ],
    )
    states = {
        "开发": {"stages": ["design", "build"], "next": "测试",
                 "confirm": {"type": "ui_button"}},
        "测试": {"stages": [], "next": None, "confirm": {"type": "none"}},
    }
    design_done = stage_done_file(tmp_path, story["story_key"], "design")
    build_done = stage_done_file(tmp_path, story["story_key"], "build")
    design_done.parent.mkdir(parents=True, exist_ok=True)

    call = {"n": 0}

    def _popen_side_effect(*a, **kw):
        call["n"] += 1
        if call["n"] == 1:
            design_done.write_text(json.dumps({"summary": "design done"}), encoding="utf-8")
        else:
            build_done.write_text(json.dumps({"summary": "build done"}), encoding="utf-8")
        return _mock_proc()

    # build.confirm=True(阶段闸),但 build 是开发状态最后 stage → Story 状态闸优先
    with patch("subprocess.Popen", side_effect=_popen_side_effect), patch(
        "story_lifecycle.orchestrator.engine.claude_stream.supervise_headless_stdout"
    ), patch("story_lifecycle.orchestrator.engine.planner._kill_headless"), patch(
        "story_lifecycle.orchestrator.engine.profile_loader.resolve_profile",
        return_value=_profile_with_story_states(states, {"build": True}),
    ):
        continue_orchestrator_agent(story["story_key"], headless=True)

    updated = db.get_story(story["story_key"])
    ctx = json.loads(updated.get("context_json", "{}"))
    # Story 状态闸触发(build 是开发最后 stage),不是阶段闸
    assert ctx.get("_story_state_gate") is not None
    assert ctx.get("_stage_gate") is None
    assert updated["status"] == "paused"


# ---- 向后兼容:无 story_states → 退化扁平行为 ----


def test_no_story_states_falls_back_to_flat(story, tmp_path):
    """无 story_states 配置 → driver 走扁平阶段行为(阶段闸仍工作,无 Story 状态闸)。"""
    _set_actions(
        story,
        [
            {"action": "launch", "stage": "design", "adapter": "claude", "focus": "设计"},
            {"action": "launch", "stage": "build", "adapter": "claude", "focus": "实现"},
        ],
    )
    design_done = stage_done_file(tmp_path, story["story_key"], "design")
    design_done.parent.mkdir(parents=True, exist_ok=True)

    def _popen_side_effect(*a, **kw):
        design_done.write_text(json.dumps({"summary": "design done"}), encoding="utf-8")
        return _mock_proc()

    # design.confirm=True(阶段闸),无 story_states → 走阶段闸
    with patch("subprocess.Popen", side_effect=_popen_side_effect), patch(
        "story_lifecycle.orchestrator.engine.claude_stream.supervise_headless_stdout"
    ), patch("story_lifecycle.orchestrator.engine.planner._kill_headless"), patch(
        "story_lifecycle.orchestrator.engine.profile_loader.resolve_profile",
        return_value=_profile_with_story_states({}, {"design": True}),
    ):
        continue_orchestrator_agent(story["story_key"], headless=True)

    updated = db.get_story(story["story_key"])
    ctx = json.loads(updated.get("context_json", "{}"))
    # 无 story_states → 阶段闸工作,无 Story 状态闸
    assert ctx.get("_stage_gate") is not None
    assert ctx.get("_story_state_gate") is None
    assert updated["status"] == "paused"


# ---- profile_loader 解析 story_states 段 ----


def test_minimal_profile_has_story_states():
    """minimal.yaml 应解析出 story_states(开发/测试/上线 三态)。"""
    rp = resolve_profile("minimal")
    assert rp.story_states, "minimal profile 应含 story_states 段"
    assert "开发" in rp.story_states
    assert "测试" in rp.story_states
    assert "上线" in rp.story_states
    dev = rp.story_states["开发"]
    assert dev["stages"] == ["design", "build"]
    assert dev["next"] == "测试"


def test_profile_without_story_states_defaults_empty():
    """realtest 等无 story_states → 解析成空 dict(向后兼容)。"""
    rp = resolve_profile("realtest")
    assert rp.story_states == {}


def test_resolved_profile_roundtrip_story_states():
    """to_dict / from_dict 透传 story_states(StoryState 持久化用)。"""
    states = {"开发": {"stages": ["design"], "next": "测试", "confirm": {"type": "none"}}}
    rp = ResolvedProfile(name="x", stages={}, story_states=states)
    d = rp.to_dict()
    assert d["story_states"] == states
    rp2 = ResolvedProfile.from_dict(d)
    assert rp2.story_states == states


# ---- /lifecycle/advance 端点 ----


def test_lifecycle_advance_endpoint(story, tmp_path):
    """POST /lifecycle/advance 推进 lifecycle_state,清 _story_state_gate。"""
    from starlette.testclient import TestClient

    from story_lifecycle.orchestrator.service.api import app

    # 构造 paused + _story_state_gate 状态
    ctx = json.loads(story.get("context_json", "{}"))
    ctx["_story_state_gate"] = {
        "from": "开发",
        "to": "测试",
        "awaiting_confirm": True,
        "label": "进入测试",
    }
    ctx["_lifecycle_state"] = "开发"
    db.update_story(
        story["story_key"],
        status="paused",
        lifecycle_state="开发",
        context_json=json.dumps(ctx, ensure_ascii=False),
    )
    client = TestClient(app)
    r = client.post(f"/api/story/{story['story_key']}/lifecycle/advance")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["lifecycle_state"] == "测试"
    updated = db.get_story(story["story_key"])
    assert updated["lifecycle_state"] == "测试"
    new_ctx = json.loads(updated.get("context_json", "{}"))
    assert new_ctx.get("_story_state_gate") is None
    assert new_ctx.get("_lifecycle_state") == "测试"


def test_lifecycle_advance_rejects_without_gate(story):
    """无 pending _story_state_gate → 409(防误调)。"""
    from starlette.testclient import TestClient

    from story_lifecycle.orchestrator.service.api import app

    client = TestClient(app)
    r = client.post(f"/api/story/{story['story_key']}/lifecycle/advance")
    assert r.status_code == 409
