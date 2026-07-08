"""Tests for Agent API endpoints — /confirm, /regenerate, /answer, /wait."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from story_lifecycle.orchestrator.service.api import app
from story_lifecycle.infra.db import models as db


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient with isolated DB."""
    db_path = tmp_path / "story.db"
    monkeypatch.setattr(db, "get_db_path", lambda: db_path)
    db.init_db()
    return TestClient(app)


def _create_story(client, key="TEST-001", **overrides):
    """Create a story via API or direct DB insert."""
    defaults = {
        "story_key": key,
        "title": "Test Story",
        "profile": "minimal",
        "workspace": "",
        "status": "planning",
        "intake_state": "ready",
    }
    defaults.update(overrides)
    db.upsert_story(**defaults)
    return defaults


class TestPlanConfirm:
    def test_confirms_and_returns_ok(self, client, tmp_path, monkeypatch):
        _create_story(client)
        ctx = {
            "_agent_actions": [{"action": "skip", "stage": "design", "reason": "test"}],
            "_plan_confirmed": False,
        }
        db.update_story("TEST-001", context_json=json.dumps(ctx))

        with patch("story_lifecycle.orchestrator.engine.graph.start_story_async"):
            resp = client.post("/api/story/TEST-001/plan/confirm")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True

    def test_404_for_missing_story(self, client):
        resp = client.post("/api/story/NONEXISTENT/plan/confirm")
        assert resp.status_code == 404


class TestPlanRegenerate:
    def test_regenerate_returns_ok(self, client, tmp_path, monkeypatch):
        _create_story(client)

        mock_llm = MagicMock()
        mock_llm.invoke_with_tools = lambda *a, **kw: {
            "message": {"role": "assistant", "content": "done"},
            "tool_calls": [],
            "content": "done",
        }

        with patch(
            "story_lifecycle.orchestrator.engine.planner.get_llm", return_value=mock_llm
        ):
            resp = client.post("/api/story/TEST-001/plan/regenerate")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True

    def test_404_for_missing_story(self, client):
        resp = client.post("/api/story/NONEXISTENT/plan/regenerate")
        assert resp.status_code == 404


class TestAnswer:
    def test_answer_writes_file(self, client, tmp_path, monkeypatch):
        ws = str(tmp_path / "workspace")
        _create_story(client, workspace=ws)

        # Create wait file
        wait_dir = Path(ws) / ".story-wait"
        wait_dir.mkdir(parents=True, exist_ok=True)
        wait_file = wait_dir / "TEST-001-design.json"
        wait_file.write_text(
            json.dumps({"question": "Which approach?", "options": ["A", "B"]}),
            encoding="utf-8",
        )

        resp = client.post(
            "/api/story/TEST-001/answer",
            json={"answer": "A"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["answer"] == "A"

        # Verify answer file was written
        answer_file = wait_dir / "TEST-001-design.answer.json"
        assert answer_file.exists()
        answer = json.loads(answer_file.read_text(encoding="utf-8"))
        assert answer["answer"] == "A"

    def test_404_when_no_wait_file(self, client, tmp_path, monkeypatch):
        _create_story(client, workspace=str(tmp_path / "ws"))
        resp = client.post(
            "/api/story/TEST-001/answer",
            json={"answer": "test"},
        )
        assert resp.status_code == 404


class TestWait:
    def test_returns_question_when_waiting(self, client, tmp_path, monkeypatch):
        ws = str(tmp_path / "workspace")
        _create_story(client, workspace=ws, current_stage="design")

        wait_dir = Path(ws) / ".story-wait"
        wait_dir.mkdir(parents=True, exist_ok=True)
        wait_file = wait_dir / "TEST-001-design.json"
        wait_file.write_text(
            json.dumps({"question": "Which approach?"}),
            encoding="utf-8",
        )

        resp = client.get("/api/story/TEST-001/wait")
        assert resp.status_code == 200
        data = resp.json()
        assert data["waiting"] is True
        assert data["question"]["question"] == "Which approach?"

    def test_returns_not_waiting_when_no_file(self, client, tmp_path, monkeypatch):
        _create_story(client, workspace=str(tmp_path / "ws"))
        resp = client.get("/api/story/TEST-001/wait")
        assert resp.status_code == 200
        data = resp.json()
        assert data["waiting"] is False


class TestGetPlan:
    def test_returns_plan_with_actions(self, client, tmp_path, monkeypatch):
        _create_story(client)
        ctx = {
            "_agent_actions": [
                {
                    "action": "launch",
                    "adapter": "claude",
                    "stage": "design",
                    "focus": "test",
                }
            ],
            "_plan_confirmed": False,
        }
        db.update_story("TEST-001", context_json=json.dumps(ctx))

        resp = client.get("/api/story/TEST-001/plan")
        assert resp.status_code == 200
        data = resp.json()
        assert data["mode"] == "agent"
        assert len(data["actions"]) == 1
        assert data["confirmed"] is False

    def test_returns_stages_view_with_done_flags(self, client, tmp_path):
        """PLAN-stage-confirm-gate:/plan 回 stages 进度条数据(done 标记)。"""
        _create_story(client)
        ctx = {
            "_agent_actions": [
                {"action": "launch", "stage": "design", "adapter": "claude", "focus": "f1"},
                {"action": "launch", "stage": "build", "adapter": "claude", "focus": "f2"},
            ],
            "_completed_stages": ["design"],
            "_plan_confirmed": True,
        }
        db.update_story("TEST-001", context_json=json.dumps(ctx))

        resp = client.get("/api/story/TEST-001/plan")
        assert resp.status_code == 200
        data = resp.json()
        stages = data["stages"]
        assert len(stages) == 2
        assert stages[0] == {
            "name": "design",
            "focus": "f1",
            "adapter": "claude",
            "done": True,
        }
        assert stages[1]["name"] == "build"
        assert stages[1]["done"] is False

    def test_returns_stage_gate_when_paused(self, client, tmp_path):
        """paused + _stage_gate → /plan 回 stage_gate(前端显示确认闸卡片)。"""
        _create_story(client, status="paused")
        ctx = {
            "_agent_actions": [{"action": "launch", "stage": "design", "adapter": "claude"}],
            "_completed_stages": ["design"],
            "_stage_gate": {
                "completed_stage": "design",
                "next_stage": "build",
                "awaiting_confirm": True,
            },
        }
        db.update_story("TEST-001", context_json=json.dumps(ctx))

        resp = client.get("/api/story/TEST-001/plan")
        assert resp.status_code == 200
        gate = resp.json()["stage_gate"]
        assert gate["completed_stage"] == "design"
        assert gate["next_stage"] == "build"
        assert gate["awaiting_confirm"] is True

    def test_advance_clears_stage_gate_on_resume(self, client, tmp_path):
        """PLAN-stage-confirm-gate:/advance(paused→resume)清除 _stage_gate 标记。"""
        _create_story(client, status="paused")
        ctx = {
            "_agent_actions": [{"action": "launch", "stage": "design", "adapter": "claude"}],
            "_completed_stages": ["design"],
            "_stage_gate": {
                "completed_stage": "design",
                "next_stage": "build",
                "awaiting_confirm": True,
            },
        }
        db.update_story("TEST-001", context_json=json.dumps(ctx))

        with patch("story_lifecycle.orchestrator.engine.graph.start_story_async"):
            resp = client.put("/api/story/TEST-001/advance")

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "status": "resumed"}
        # _stage_gate 已清掉
        updated = db.get_story("TEST-001")
        ctx_after = json.loads(updated["context_json"])
        assert "_stage_gate" not in ctx_after
