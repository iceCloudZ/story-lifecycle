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


# -- AdversarialConfig tests --


def test_adversarial_config_defaults_when_disabled():
    from story_lifecycle.orchestrator.evaluator_loop import AdversarialConfig

    cfg = AdversarialConfig.from_profile({})
    assert cfg.enabled is False
    assert cfg.plan_loop.enabled is False
    assert cfg.code_loop.enabled is False


def test_adversarial_config_parses_yaml():
    from story_lifecycle.orchestrator.evaluator_loop import AdversarialConfig

    profile = {
        "adversarial": {
            "enabled": True,
            "plan_loop": {
                "enabled": True,
                "stages": ["design", "implement"],
                "max_rounds": 3,
                "reviewer_model": "deepseek-chat",
                "pass_condition": "no_open_blocker_or_major",
            },
            "code_loop": {
                "enabled": True,
                "mode": "short_lived",
                "max_rounds": 3,
                "reviewer_model": "deepseek-chat",
                "pass_condition": "no_open_blocker",
                "fallback": "repair_packet",
            },
        }
    }
    cfg = AdversarialConfig.from_profile(profile)
    assert cfg.enabled is True
    assert cfg.plan_loop.enabled is True
    assert cfg.plan_loop.stages == ["design", "implement"]
    assert cfg.plan_loop.max_rounds == 3
    assert cfg.plan_loop.reviewer_model == "deepseek-chat"
    assert cfg.code_loop.enabled is True
    assert cfg.code_loop.mode == "short_lived"
    assert cfg.code_loop.max_rounds == 3


def test_adversarial_config_falls_back_to_env_model():
    from story_lifecycle.orchestrator.evaluator_loop import AdversarialConfig

    cfg = AdversarialConfig.from_profile(
        {
            "adversarial": {
                "enabled": True,
                "plan_loop": {"enabled": True, "stages": ["design"]},
                "code_loop": {"enabled": True, "mode": "short_lived"},
            }
        }
    )
    assert cfg.plan_loop.reviewer_model == ""  # empty means fallback to env
    assert cfg.code_loop.reviewer_model == ""


# -- LoopResult tests --


def test_loop_result_fields():
    from story_lifecycle.orchestrator.evaluator_loop import LoopResult

    result = LoopResult(
        decision="pass",
        rounds=2,
        final_plan={"adapter": "claude"},
        final_review=None,
        reason="all_blockers_resolved",
        remaining_findings=[],
    )
    assert result.decision == "pass"
    assert result.rounds == 2
    assert result.final_plan["adapter"] == "claude"


# -- build_repair_packet tests --


def test_build_repair_packet_contains_findings_and_plan(isolated_story_home):
    from story_lifecycle.orchestrator.evaluator_loop import build_repair_packet

    packet = build_repair_packet(
        story_key="LOOP-RP1",
        stage="implement",
        workspace=os.getcwd(),
        plan_summary="Implement auth module",
        stage_output_summary="Added auth.py, login.py",
        findings=[
            {
                "severity": "high",
                "category": "security",
                "description": "Missing CSRF token",
                "recommendation": "Add CSRF middleware",
                "location": "auth.py:42",
            },
            {
                "severity": "medium",
                "category": "testing",
                "description": "No unit tests",
                "recommendation": "Add test_auth.py",
                "location": "",
            },
        ],
        verification={"status": "unavailable", "commands": []},
        round_num=1,
    )
    assert "Missing CSRF token" in packet
    assert "auth.py:42" in packet
    assert "No unit tests" in packet
    assert "Implement auth module" in packet
    assert "unavailable" in packet
    assert "无关的重构" in packet or "unrelated refactoring" in packet.lower()


def test_build_repair_packet_no_full_diff():
    from story_lifecycle.orchestrator.evaluator_loop import build_repair_packet

    packet = build_repair_packet(
        story_key="LOOP-RP2",
        stage="implement",
        workspace=os.getcwd(),
        plan_summary="Fix bug",
        stage_output_summary="Changed main.py",
        findings=[],
        verification={"status": "passed", "commands": ["pytest"]},
        round_num=1,
    )
    # Should not contain raw diff markers
    assert "--- a/" not in packet
    assert "+++ b/" not in packet


def test_build_repair_packet_respects_hard_budget():
    from story_lifecycle.orchestrator.evaluator_loop import build_repair_packet

    # Create findings with very long descriptions
    long_findings = [
        {
            "severity": "high",
            "category": f"cat-{i}",
            "description": "X" * 5000,
            "recommendation": "Fix it",
            "location": f"file_{i}.py",
        }
        for i in range(20)
    ]
    packet = build_repair_packet(
        story_key="LOOP-RP3",
        stage="implement",
        workspace=os.getcwd(),
        plan_summary="P" * 5000,
        stage_output_summary="S" * 5000,
        findings=long_findings,
        verification={"status": "failed", "commands": []},
        round_num=1,
    )
    # hard_budget is ~80000 chars (20000 tokens * ~4 chars/token)
    assert len(packet) < 100000


def test_build_repair_packet_writes_file(isolated_story_home):
    from story_lifecycle.orchestrator.evaluator_loop import build_repair_packet
    from pathlib import Path

    workspace = str(isolated_story_home / "ws")
    Path(workspace).mkdir(exist_ok=True)

    path = build_repair_packet(
        story_key="LOOP-RP4",
        stage="implement",
        workspace=workspace,
        plan_summary="Plan summary here",
        stage_output_summary="Output summary",
        findings=[],
        verification={"status": "passed", "commands": []},
        round_num=2,
        write_file=True,
    )
    assert path is not None
    assert "repair_implement_round2.md" in str(path)
    content = Path(path).read_text(encoding="utf-8")
    assert "Plan summary here" in content


# -- detect_no_progress tests --


def test_detect_no_progress_true_on_repeated_blockers():
    from story_lifecycle.orchestrator.evaluator_loop import detect_no_progress

    prev = [
        {
            "severity": "high",
            "category": "security",
            "location": "auth.py:42",
            "description": "Missing CSRF token",
        },
    ]
    curr = [
        {
            "severity": "high",
            "category": "security",
            "location": "auth.py:42",
            "description": "CSRF token still missing",
        },
    ]
    assert detect_no_progress(prev, curr) is True


def test_detect_no_progress_false_on_new_finding():
    from story_lifecycle.orchestrator.evaluator_loop import detect_no_progress

    prev = [
        {
            "severity": "high",
            "category": "security",
            "location": "auth.py:42",
            "description": "Missing CSRF token",
        },
    ]
    curr = [
        {
            "severity": "high",
            "category": "testing",
            "location": "test_api.py:10",
            "description": "Missing integration test",
        },
    ]
    assert detect_no_progress(prev, curr) is False


def test_detect_no_progress_false_on_resolved_with_new():
    from story_lifecycle.orchestrator.evaluator_loop import detect_no_progress

    prev = [
        {
            "severity": "high",
            "category": "security",
            "location": "auth.py:42",
            "description": "Missing CSRF token",
        },
    ]
    curr = []
    assert detect_no_progress(prev, curr) is False


def test_detect_no_progress_true_on_exact_repeat():
    from story_lifecycle.orchestrator.evaluator_loop import detect_no_progress

    finding = {
        "severity": "high",
        "category": "null-safety",
        "location": "main.py:15",
        "description": "NPE risk",
    }
    assert detect_no_progress([finding], [finding]) is True


def test_detect_no_progress_ignores_low_severity():
    from story_lifecycle.orchestrator.evaluator_loop import detect_no_progress

    prev = [
        {
            "severity": "low",
            "category": "style",
            "location": "a.py:1",
            "description": "Missing docstring",
        },
    ]
    curr = [
        {
            "severity": "low",
            "category": "style",
            "location": "a.py:1",
            "description": "Still missing docstring",
        },
    ]
    assert detect_no_progress(prev, curr) is False
