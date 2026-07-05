"""Tests for db event-log queries used by layer5 reflection reinjection."""

from story_lifecycle.infra.db import models as db


def test_get_recent_events_by_type_no_story_filter():
    """get_recent_events_by_type 跨所有 story 拿事件(flywheel playbook 是全局知识)。"""
    db.create_story("A", "a", "")
    db.create_story("B", "b", "")
    db.log_event("A", "implement", "recovery_action", {"action": "retry_new_adapter"})
    db.log_event("B", "verify", "recovery_action", {"action": "skip_stage"})
    db.log_event("A", "implement", "supervisor_decision", {"choice": "x"})

    rows = db.get_recent_events_by_type(["recovery_action"], limit=10)
    assert len(rows) == 2  # A 和 B 的 recovery_action 都拿到(无 story 过滤)
    types = {r["event_type"] for r in rows}
    assert types == {"recovery_action"}


def test_get_recent_events_by_type_respects_limit():
    db.create_story("C", "c", "")
    for i in range(5):
        db.log_event("C", "implement", "judge_verdict", {"pass": bool(i % 2)})
    rows = db.get_recent_events_by_type(["judge_verdict"], limit=2)
    assert len(rows) == 2


def test_get_recent_events_by_type_empty():
    assert db.get_recent_events_by_type(["nonexistent_type"], limit=10) == []
