"""Tests for evaluator-optimizer loop: events, repair packets, plan loop, code loop."""

import json
import os


def _make_state(story_key="LOOP-001", stage="implement", **overrides):
    base = {
        "story_key": story_key,
        "title": "Loop Test Story",
        "workspace": os.getcwd(),
        "profile": "minimal",
        "current_stage": stage,
        "status": "active",
        "context": {},
        "execution_count": 0,
        "last_error": None,
        "trajectory_score": None,
        "review_summary": None,
    }
    base.update(overrides)
    return base


def _get_events_by_type(story_key, event_types):
    from story_lifecycle.db import models as _db

    return [
        e for e in _db.get_story_events(story_key) if e.get("event_type") == event_types
    ]


def _parse_payload(event):
    payload = event.get("payload", {})
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return {}
    return payload or {}


# ── loop_events tests ──


def test_log_loop_started_writes_event(isolated_story_home):
    from story_lifecycle.db import models as db
    from story_lifecycle.orchestrator.loop_events import log_loop_started

    db.upsert_story("LOOP-EV1", workspace=os.getcwd(), profile="minimal")
    log_loop_started(
        story_key="LOOP-EV1",
        stage="implement",
        loop_id="implement:20260524-abc",
        loop_type="code",
        mode="short_lived",
        max_rounds=3,
        optimizer_model="claude-sonnet",
        reviewer_model="deepseek-chat",
        attempt_id="implement:1",
    )
    events = _get_events_by_type("LOOP-EV1", "evaluator_loop_started")
    assert len(events) == 1
    p = _parse_payload(events[0])
    assert p["loop_id"] == "implement:20260524-abc"
    assert p["loop_type"] == "code"
    assert p["mode"] == "short_lived"
    assert p["max_rounds"] == 3
    assert p["reviewer_model"] == "deepseek-chat"
    assert p["attempt_id"] == "implement:1"


def test_log_loop_round_writes_event(isolated_story_home):
    from story_lifecycle.db import models as db
    from story_lifecycle.orchestrator.loop_events import log_loop_round

    db.upsert_story("LOOP-EV2", workspace=os.getcwd(), profile="minimal")
    log_loop_round(
        story_key="LOOP-EV2",
        stage="implement",
        loop_id="implement:20260524-abc",
        round_id=1,
        loop_type="code",
        mode="short_lived",
        decision="revise",
        score=0.78,
        findings={
            "open_before": [],
            "new": ["F-001", "F-002"],
            "resolved": [],
            "repeated": [],
        },
        verification={"status": "unavailable", "commands": []},
    )
    events = _get_events_by_type("LOOP-EV2", "evaluator_loop_round")
    assert len(events) == 1
    p = _parse_payload(events[0])
    assert p["loop_id"] == "implement:20260524-abc"
    assert p["round_id"] == 1
    assert p["decision"] == "revise"
    assert p["score"] == 0.78
    assert p["findings"]["new"] == ["F-001", "F-002"]
    assert p["verification"]["status"] == "unavailable"
    assert p["no_progress"] is False


def test_log_loop_completed_writes_event(isolated_story_home):
    from story_lifecycle.db import models as db
    from story_lifecycle.orchestrator.loop_events import log_loop_completed

    db.upsert_story("LOOP-EV3", workspace=os.getcwd(), profile="minimal")
    log_loop_completed(
        story_key="LOOP-EV3",
        stage="implement",
        loop_id="plan:20260524-xyz",
        loop_type="plan",
        decision="pass",
        rounds=2,
        reason="all_blockers_resolved",
        remaining_findings=[],
    )
    events = _get_events_by_type("LOOP-EV3", "evaluator_loop_completed")
    assert len(events) == 1
    p = _parse_payload(events[0])
    assert p["loop_id"] == "plan:20260524-xyz"
    assert p["decision"] == "pass"
    assert p["rounds"] == 2
    assert p["reason"] == "all_blockers_resolved"
    assert p["remaining_findings"] == []


def test_log_loop_fallback_writes_event(isolated_story_home):
    from story_lifecycle.db import models as db
    from story_lifecycle.orchestrator.loop_events import log_loop_fallback

    db.upsert_story("LOOP-EV4", workspace=os.getcwd(), profile="minimal")
    log_loop_fallback(
        story_key="LOOP-EV4",
        stage="implement",
        loop_id="code:20260524-fb",
        from_mode="persistent",
        to_mode="short_lived",
        reason="session_dead",
        repair_packet_path=".story-context/LOOP-EV4/repair_implement_round2.md",
    )
    events = _get_events_by_type("LOOP-EV4", "evaluator_loop_fallback")
    assert len(events) == 1
    p = _parse_payload(events[0])
    assert p["from_mode"] == "persistent"
    assert p["to_mode"] == "short_lived"
    assert p["reason"] == "session_dead"
    assert "repair_implement_round2" in p["repair_packet_path"]
