"""设计 13 Step 4：DecisionHandler 测试（handlers.py）。

approve/reject/escalate 三分支，每个独立测。不需要真跑编排线程。
"""

import json

import pytest

from story_lifecycle.infra.db import models as db
from story_lifecycle.orchestrator.handlers import (
    AutomaticDecisionHandler,
    InteractiveDecisionHandler,
    make_decision_handler,
)


@pytest.fixture
def tmp_story(tmp_path, isolated_story_home):
    """创建一个临时 story（minimal profile, design stage, 有 _agent_actions）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    db.create_story("S-HAND-1", "提现门槛优化", str(ws), profile="minimal")
    db.update_story(
        "S-HAND-1",
        context_json=json.dumps(
            {
                "_plan_confirmed": True,
                "_agent_actions": [
                    {"action": "launch", "stage": "design", "adapter": "claude", "focus": "设计"},
                    {"action": "launch", "stage": "build", "adapter": "claude", "focus": "实现"},
                ],
            },
            ensure_ascii=False,
        ),
    )
    return "S-HAND-1"


def _reload_ctx(story_key):
    story = db.get_story(story_key)
    return json.loads(story.get("context_json") or "{}")


class TestInteractiveDecisionHandler:
    def test_approve_writes_completed_stages(self, tmp_story):
        handler = InteractiveDecisionHandler()
        ctx = _reload_ctx(tmp_story)
        actions = ctx["_agent_actions"]
        decision = {"quality": "approve", "lifecycle_target": None, "summary": "ok"}
        handler.handle_approve(tmp_story, "design", decision, ctx, actions)
        ctx = _reload_ctx(tmp_story)
        assert "design" in ctx["_completed_stages"]

    def test_approve_writes_summary(self, tmp_story):
        handler = InteractiveDecisionHandler()
        # 先建 session 行（set_session_completion_summary 只 UPDATE 不 INSERT）
        db.upsert_session(tmp_story, "design", "claude", session_id="sess-x")
        ctx = _reload_ctx(tmp_story)
        actions = ctx["_agent_actions"]
        decision = {"quality": "approve", "summary": "本轮完成了调研"}
        handler.handle_approve(tmp_story, "design", decision, ctx, actions)
        session = db.get_session(tmp_story, "design", "claude")
        assert "本轮完成了调研" in (session.get("completion_summary") or "")

    def test_reject_pauses_story(self, tmp_story):
        handler = InteractiveDecisionHandler()
        ctx = _reload_ctx(tmp_story)
        actions = ctx["_agent_actions"]
        decision = {"quality": "reject", "reason": "spec 不完整"}
        handler.handle_reject(tmp_story, "design", decision, ctx, actions)
        story = db.get_story(tmp_story)
        assert story["status"] == "paused"

    def test_escalate_pauses_story(self, tmp_story):
        handler = InteractiveDecisionHandler()
        ctx = _reload_ctx(tmp_story)
        decision = {"quality": "escalate", "reason": "超限"}
        handler.handle_escalate(tmp_story, "design", decision, ctx)
        story = db.get_story(tmp_story)
        assert story["status"] == "paused"

    def test_reject_does_not_insert_retry(self, tmp_story):
        """半自动 reject 不插 retry action（转人）"""
        handler = InteractiveDecisionHandler()
        ctx = _reload_ctx(tmp_story)
        actions = ctx["_agent_actions"]
        decision = {"quality": "reject", "reason": "spec 不完整"}
        handler.handle_reject(tmp_story, "design", decision, ctx, actions)
        ctx = _reload_ctx(tmp_story)
        design_actions = [a for a in ctx["_agent_actions"] if a.get("stage") == "design"]
        assert len(design_actions) == 1  # 原始 1 个,没有 retry

    def test_approve_all_stages_done_completes_story(self, tmp_story):
        """两个 stage 都 approve → story completed"""
        handler = InteractiveDecisionHandler()
        ctx = _reload_ctx(tmp_story)
        actions = ctx["_agent_actions"]
        handler.handle_approve(
            tmp_story, "design", {"quality": "approve", "lifecycle_target": None}, ctx, actions
        )
        ctx = _reload_ctx(tmp_story)
        handler.handle_approve(
            tmp_story, "build", {"quality": "approve", "lifecycle_target": None}, ctx, actions
        )
        story = db.get_story(tmp_story)
        assert story["status"] == "completed"
        assert set(ctx["_completed_stages"]) == {"design", "build"}


class TestAutomaticDecisionHandler:
    def test_reject_inserts_retry_action(self, tmp_story):
        """全自动 reject 插 retry action（半自动不插）"""
        handler = AutomaticDecisionHandler()
        ctx = _reload_ctx(tmp_story)
        actions = ctx["_agent_actions"]
        decision = {"quality": "reject", "reason": "spec 不完整"}
        handler.handle_reject(tmp_story, "design", decision, ctx, actions)
        ctx = _reload_ctx(tmp_story)
        # actions 里应该多了一个 design retry
        design_actions = [a for a in ctx["_agent_actions"] if a.get("stage") == "design"]
        assert len(design_actions) == 2  # 原始 + retry
        retry = design_actions[-1]
        assert "reject" in retry.get("focus", "")

    def test_reject_retry_after_stage(self, tmp_story):
        """retry 插在当前 stage 之后（design retry 在 build 前）"""
        handler = AutomaticDecisionHandler()
        ctx = _reload_ctx(tmp_story)
        actions = ctx["_agent_actions"]
        handler.handle_reject(
            tmp_story, "design", {"quality": "reject", "reason": "x"}, ctx, actions
        )
        ctx = _reload_ctx(tmp_story)
        stages = [a.get("stage") for a in ctx["_agent_actions"]]
        assert stages == ["design", "design", "build"]  # retry 紧跟 design

    def test_approve_advances_to_next_stage(self, tmp_story, monkeypatch):
        """approve 后 current_stage 推进到 build（无 confirm 闸时）"""
        # minimal design confirm=true → 会 paused 等确认；改用 confirm=false 的
        # profile 验证自动推进（全自动模式不等人点 spawn）。
        from story_lifecycle.orchestrator.engine.profile_loader import (
            ResolvedProfile,
            StageConfig,
        )

        profile = ResolvedProfile(
            name="auto-test",
            cli="claude",
            execution_mode="interactive_pty",
            stages={
                "design": StageConfig(order=1, description="d", confirm=False),
                "build": StageConfig(order=2, description="b", confirm=False),
            },
        )
        import story_lifecycle.orchestrator.engine.profile_loader as pl

        monkeypatch.setattr(pl, "resolve_profile", lambda name: profile)
        handler = AutomaticDecisionHandler()
        ctx = _reload_ctx(tmp_story)
        actions = ctx["_agent_actions"]
        decision = {"quality": "approve", "lifecycle_target": None, "summary": "ok"}
        handler.handle_approve(tmp_story, "design", decision, ctx, actions)
        story = db.get_story(tmp_story)
        assert story["current_stage"] == "build"

    def test_escalate_pauses_story(self, tmp_story):
        handler = AutomaticDecisionHandler()
        ctx = _reload_ctx(tmp_story)
        handler.handle_escalate(
            tmp_story, "design", {"quality": "escalate", "reason": "超限"}, ctx
        )
        story = db.get_story(tmp_story)
        assert story["status"] == "paused"


class TestFactory:
    def test_minimal_profile_returns_interactive(self, tmp_story):
        """minimal → InteractiveDecisionHandler"""
        story = db.get_story(tmp_story)
        handler = make_decision_handler(story, _reload_ctx(tmp_story))
        assert isinstance(handler, InteractiveDecisionHandler)

    def test_single_pass_returns_automatic(self, tmp_story):
        """single-pass → AutomaticDecisionHandler"""
        db.update_story(tmp_story, profile="single-pass")
        story = db.get_story(tmp_story)
        handler = make_decision_handler(story, _reload_ctx(tmp_story))
        assert isinstance(handler, AutomaticDecisionHandler)
