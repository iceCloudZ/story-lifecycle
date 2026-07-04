"""Tests for run_story error writeback (0d-D) + recovery wiring (层3).

``run_story`` 的 except 必须把 story 标 ``failed`` + 写 ``last_error``,
否则崩溃的 story 永远卡在 running —— 这是全自动流水线断点 D(manual §5「0d」)。
层3 recovery:except 还要调 ``decide_recovery`` 落 ``recovery_action`` 事件
(审计 + 层5 反思数据源),不卡 active。
"""

import json

from story_lifecycle.infra.db import models as db
from story_lifecycle.orchestrator.engine import graph


def test_run_story_marks_failed_on_raise(monkeypatch):
    """planner 抛错 → run_story 吞异常 + 标 story failed + 写 last_error。"""
    db.create_story("FAIL-1", title="boom", workspace="")  # workspace="" 跳过 acquire

    def boom(story_key):
        raise RuntimeError("planner exploded: simulated crash")

    monkeypatch.setattr(graph.planner, "continue_orchestrator_agent", boom)

    graph.run_story("FAIL-1", epoch=0)  # 不应向外抛

    story = db.get_story("FAIL-1")
    assert story["status"] == "failed"
    assert "planner exploded" in (story["last_error"] or "")


def test_run_story_no_error_does_not_mark_failed(monkeypatch):
    """正常完成不标 failed(不误伤)。"""
    db.create_story("OK-1", title="fine", workspace="")
    seen = {"n": 0}

    def ok(story_key):
        seen["n"] += 1

    monkeypatch.setattr(graph.planner, "continue_orchestrator_agent", ok)
    graph.run_story("OK-1", epoch=0)

    story = db.get_story("OK-1")
    assert story["status"] != "failed"
    assert seen["n"] == 1


def test_run_story_logs_recovery_action_on_raise(monkeypatch):
    """planner 抛错 → run_story 调 decide_recovery 落 recovery_action 事件(层3)。"""
    db.create_story("REC-1", title="boom", workspace="")
    db.update_story("REC-1", priority="P2")

    def boom(story_key):
        raise TimeoutError("done file never appeared")

    monkeypatch.setattr(graph.planner, "continue_orchestrator_agent", boom)

    graph.run_story("REC-1", epoch=0)

    events = db.get_recent_quality_events("REC-1", ["recovery_action"])
    assert len(events) == 1
    payload = json.loads(events[0]["payload"])
    # P2 + 瞬时错 + attempt 1 → retry_new_adapter
    assert payload["action"] == "retry_new_adapter"
    assert payload.get("new_adapter")  # 带新 adapter
    assert isinstance(payload.get("reason"), str) and payload["reason"]
    # story 不卡 active(标 failed)
    assert db.get_story("REC-1")["status"] == "failed"

