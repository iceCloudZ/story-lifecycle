"""Tests for run_story error writeback (0d-D).

``run_story`` 的 except 必须把 story 标 ``failed`` + 写 ``last_error``,
否则崩溃的 story 永远卡在 running —— 这是全自动流水线断点 D(manual §5「0d」)。
"""

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
