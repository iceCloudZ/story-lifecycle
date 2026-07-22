"""Tests for Agent planner — run_orchestrator_agent and continue_orchestrator_agent."""

import json
from unittest.mock import patch, MagicMock

import pytest

from story_lifecycle.orchestrator.engine.planner import (
    run_orchestrator_agent,
    continue_orchestrator_agent,
    _build_agent_system_prompt,
    _build_agent_user_message,
)


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Set up isolated DB for testing."""
    from story_lifecycle.infra.db import models as db

    db_path = tmp_path / "story.db"
    monkeypatch.setattr(db, "get_db_path", lambda: db_path)
    db.init_db()
    return db


def _make_story(isolated_db, monkeypatch, tmp_path, **overrides):
    """Create a test story in DB."""
    from story_lifecycle.infra.db import models as db

    defaults = {
        "story_key": "TEST-001",
        "title": "Test Story",
        "profile": "minimal",
        "workspace": str(tmp_path / "workspace"),
        "status": "planning",
        "intake_state": "ready",
    }
    defaults.update(overrides)
    db.upsert_story(**defaults)
    return defaults


class TestBuildPrompts:
    def test_system_prompt_contains_stages(self):
        prompt = _build_agent_system_prompt(
            profile_stages={"design": {"description": "设计方案", "cli": "codex"}},
            story_title="Auth",
            story_key="AUTH-001",
        )
        assert "design" in prompt
        assert "codex" in prompt
        assert "AUTH-001" in prompt

    def test_user_message_contains_title(self):
        msg = _build_agent_user_message(
            story_key="AUTH-001",
            title="实现登录",
            content="需要 JWT",
            workspace="/tmp/ws",
        )
        assert "实现登录" in msg
        assert "JWT" in msg


class TestRunOrchestratorAgent:
    def test_structured_plan_produces_launch_actions(self, isolated_db, monkeypatch, tmp_path):
        """REFACTOR §5.4.1:invoke_structured 返回 PlanResult → launch actions(adapter 由 profile 决定)。"""
        _make_story(isolated_db, monkeypatch, tmp_path)
        mock_llm = MagicMock()
        mock_llm.api_key = "fake"

        # mock invoke_structured 返回一个有 .stages 属性的 PlanResult-like 对象
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
            FakeStage("design", skip=False, focus="需求澄清"),
        ])
        actions_received = []

        with patch("story_lifecycle.orchestrator.engine.planner.get_llm", return_value=mock_llm):
            result = run_orchestrator_agent(
                "TEST-001", on_action=lambda e: actions_received.append(e)
            )

        assert result["status"] == "planning"
        assert len(result["actions"]) == 1
        assert result["actions"][0]["action"] == "launch"
        assert result["actions"][0]["stage"] == "design"
        assert result["actions"][0]["focus"] == "需求澄清"
        # adapter 由 profile 决定(minimal.yaml design=claude),不是模型选
        assert result["actions"][0]["adapter"] is not None

    def test_writes_actions_to_db(self, isolated_db, monkeypatch, tmp_path):
        from story_lifecycle.infra.db import models as db

        _make_story(isolated_db, monkeypatch, tmp_path)
        mock_llm = MagicMock()
        mock_llm.api_key = "fake"
        mock_llm.invoke_structured.return_value = MagicMock(stages=[])

        with patch(
            "story_lifecycle.orchestrator.engine.planner.get_llm", return_value=mock_llm
        ):
            run_orchestrator_agent("TEST-001")

        story = db.get_story("TEST-001")
        ctx = json.loads(story["context_json"])
        assert "_agent_actions" in ctx
        assert ctx["_plan_confirmed"] is False
        # planning 移出 status:规划期间引擎在跑规划 LLM,DB status=active
        # (lifecycle_state 才驱动「待启动」tab)。
        assert story["status"] == "active"

    def test_skip_stage_via_structured_plan(self, isolated_db, monkeypatch, tmp_path):
        """模型在 PlanResult 里标 skip=True → 产出 skip action。"""
        _make_story(isolated_db, monkeypatch, tmp_path)
        mock_llm = MagicMock()
        mock_llm.api_key = "fake"

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
            FakeStage("design", skip=False, focus="需求澄清"),
            FakeStage("test", skip=True, focus="not needed"),
        ])

        with patch("story_lifecycle.orchestrator.engine.planner.get_llm", return_value=mock_llm):
            result = run_orchestrator_agent("TEST-001")

        skip_actions = [a for a in result["actions"] if a["action"] == "skip"]
        assert len(skip_actions) == 1
        assert skip_actions[0]["stage"] == "test"
        assert "not needed" in skip_actions[0]["reason"]

    def test_llm_exception_falls_back_to_default_actions(self, isolated_db, monkeypatch, tmp_path):
        """LLM 调用失败 → fallback 全跑 profile 默认阶段。"""
        _make_story(isolated_db, monkeypatch, tmp_path)
        mock_llm = MagicMock()
        mock_llm.api_key = "fake"
        mock_llm.invoke_structured.side_effect = RuntimeError("network down")

        with patch("story_lifecycle.orchestrator.engine.planner.get_llm", return_value=mock_llm):
            result = run_orchestrator_agent("TEST-001")

        # fallback:全跑 profile 默认阶段(minimal.yaml 有 design/build/verify)
        assert len(result["actions"]) >= 1
        assert all(a["action"] == "launch" for a in result["actions"])

    def test_story_not_found_raises(self, isolated_db):
        with pytest.raises(ValueError, match="Story not found"):
            run_orchestrator_agent("NONEXISTENT-001")


class TestContinueOrchestratorAgent:
    def test_skip_action_records_event(self, isolated_db, monkeypatch, tmp_path):
        from story_lifecycle.infra.db import models as db

        _make_story(isolated_db, monkeypatch, tmp_path)
        db.update_story(
            "TEST-001",
            context_json=json.dumps(
                {
                    "_agent_actions": [
                        {"action": "skip", "stage": "test", "reason": "not needed"}
                    ],
                    "_plan_confirmed": True,
                }
            ),
        )

        with patch("story_lifecycle.infra.terminal.pty.ensure_agent_pty"):
            continue_orchestrator_agent("TEST-001")

        story = db.get_story("TEST-001")
        assert story["status"] == "completed"

    def test_no_actions_marks_failed(self, isolated_db, monkeypatch, tmp_path):
        from story_lifecycle.infra.db import models as db

        _make_story(isolated_db, monkeypatch, tmp_path)
        db.update_story(
            "TEST-001",
            context_json=json.dumps({"_agent_actions": [], "_plan_confirmed": True}),
        )

        continue_orchestrator_agent("TEST-001")

        story = db.get_story("TEST-001")
        assert story["status"] == "failed"

    def test_story_not_found_raises(self, isolated_db):
        with pytest.raises(ValueError, match="Story not found"):
            continue_orchestrator_agent("NONEXISTENT-001")
