"""Tests for evaluator-optimizer loop: events, repair packets, plan loop, code loop."""

import json
import os
from unittest.mock import patch


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


# -- review_plan tests --


def test_review_plan_returns_structured_result(isolated_story_home):
    from story_lifecycle.orchestrator.planner import review_plan
    from unittest.mock import patch

    mock_response = {
        "quality": "revise",
        "blockers": [
            {
                "severity": "high",
                "category": "scope",
                "description": "计划缺少数据库迁移步骤",
            }
        ],
        "suggestions": ["增加 migration 脚本执行步骤"],
        "reasoning": "计划中提到新增表但未包含迁移步骤",
    }

    with patch(
        "story_lifecycle.orchestrator.planner._call_llm", return_value=mock_response
    ):
        state = _make_state(story_key="RP-REVISE")
        plan = {"adapter": "claude", "extra_instructions": "Add new table users"}
        stage_config = {"description": "实现数据库变更"}
        result = review_plan(state, plan, stage_config)

    assert result["quality"] == "revise"
    assert len(result["blockers"]) == 1
    assert result["blockers"][0]["severity"] == "high"
    assert result["blockers"][0]["category"] == "scope"
    assert len(result["suggestions"]) == 1
    assert "reasoning" in result


def test_review_plan_pass_with_no_blockers(isolated_story_home):
    from story_lifecycle.orchestrator.planner import review_plan
    from unittest.mock import patch

    mock_response = {
        "quality": "pass",
        "blockers": [],
        "suggestions": ["可以考虑添加更多单元测试"],
        "reasoning": "计划范围合理，指令具体，与知识库对齐",
    }

    with patch(
        "story_lifecycle.orchestrator.planner._call_llm", return_value=mock_response
    ):
        state = _make_state(story_key="RP-PASS")
        plan = {"adapter": "claude", "extra_instructions": "Implement auth module"}
        stage_config = {"description": "实现认证模块"}
        result = review_plan(state, plan, stage_config)

    assert result["quality"] == "pass"
    assert result["blockers"] == []


def test_review_plan_uses_reviewer_model(isolated_story_home):
    from story_lifecycle.orchestrator.planner import review_plan
    from unittest.mock import patch

    mock_response = {
        "quality": "pass",
        "blockers": [],
        "suggestions": [],
        "reasoning": "OK",
    }

    with patch(
        "story_lifecycle.orchestrator.planner._call_llm", return_value=mock_response
    ) as mock_llm:
        state = _make_state(story_key="RP-MODEL")
        plan = {"adapter": "claude"}
        stage_config = {"description": "test"}
        review_plan(state, plan, stage_config, reviewer_model="gpt-4o")

        # Verify the model parameter passed to _call_llm is the reviewer_model
        call_args = mock_llm.call_args
        assert call_args[0][2] == "gpt-4o"  # 3rd positional arg = model


# -- run_plan_loop tests --


def test_plan_loop_passes_on_first_round(isolated_story_home):
    """Plan passes review immediately on round 1."""
    from story_lifecycle.orchestrator.evaluator_loop import (
        run_plan_loop,
        AdversarialConfig,
    )
    from story_lifecycle.db import models as db
    from unittest.mock import patch

    profile = {
        "adversarial": {
            "enabled": True,
            "plan_loop": {
                "enabled": True,
                "stages": ["implement"],
                "max_rounds": 3,
                "reviewer_model": "deepseek-chat",
            },
        }
    }
    adv_config = AdversarialConfig.from_profile(profile)
    db.upsert_story("PL-PASS1", workspace=os.getcwd(), profile="minimal")

    plan_result = {
        "adapter": "claude",
        "skip": False,
        "summary": "Implement auth module",
        "extra_instructions": "Do it",
        "reasoning": "Straightforward",
        "trajectory_score": 0.9,
    }
    review_result = {
        "quality": "pass",
        "blockers": [],
        "suggestions": [],
        "reasoning": "Plan looks good",
    }

    state = _make_state(story_key="PL-PASS1", stage="implement")

    with patch(
        "story_lifecycle.orchestrator.planner.plan_stage", return_value=plan_result
    ):
        with patch(
            "story_lifecycle.orchestrator.planner.review_plan",
            return_value=review_result,
        ):
            result = run_plan_loop(state, adv_config, adapters=["claude"])

    assert result.decision == "pass"
    assert result.rounds == 1
    assert result.final_plan["adapter"] == "claude"
    assert result.reason == "all_blockers_resolved"


def test_plan_loop_revises_then_passes(isolated_story_home):
    """Round 1 returns revise, round 2 passes (uses call counter)."""
    from story_lifecycle.orchestrator.evaluator_loop import (
        run_plan_loop,
        AdversarialConfig,
    )
    from story_lifecycle.db import models as db
    from unittest.mock import patch

    profile = {
        "adversarial": {
            "enabled": True,
            "plan_loop": {
                "enabled": True,
                "stages": ["implement"],
                "max_rounds": 3,
                "reviewer_model": "deepseek-chat",
            },
        }
    }
    adv_config = AdversarialConfig.from_profile(profile)
    db.upsert_story("PL-REV1", workspace=os.getcwd(), profile="minimal")

    plan_result = {
        "adapter": "claude",
        "skip": False,
        "summary": "Implement auth",
        "extra_instructions": "Do it",
        "reasoning": "OK",
        "trajectory_score": 0.8,
    }

    # Use a counter to return different reviews on each call
    call_count = {"n": 0}

    def _mock_review_plan(state, plan, cfg, reviewer_model=""):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {
                "quality": "revise",
                "blockers": [
                    {
                        "severity": "high",
                        "category": "scope",
                        "description": "Missing migration steps",
                    }
                ],
                "suggestions": ["Add migration"],
                "reasoning": "Incomplete plan",
            }
        return {
            "quality": "pass",
            "blockers": [],
            "suggestions": [],
            "reasoning": "Plan is now complete",
        }

    state = _make_state(story_key="PL-REV1", stage="implement")

    with patch(
        "story_lifecycle.orchestrator.planner.plan_stage", return_value=plan_result
    ):
        with patch(
            "story_lifecycle.orchestrator.planner.review_plan",
            side_effect=_mock_review_plan,
        ):
            result = run_plan_loop(state, adv_config, adapters=["claude"])

    assert result.decision == "pass"
    assert result.rounds == 2
    assert call_count["n"] == 2


def test_plan_loop_stops_at_max_rounds(isolated_story_home):
    """Always revise, hits max_rounds=2. Uses different blockers per round
    to avoid triggering no-progress detection."""
    from story_lifecycle.orchestrator.evaluator_loop import (
        run_plan_loop,
        AdversarialConfig,
    )
    from story_lifecycle.db import models as db
    from unittest.mock import patch

    profile = {
        "adversarial": {
            "enabled": True,
            "plan_loop": {
                "enabled": True,
                "stages": ["implement"],
                "max_rounds": 2,
                "reviewer_model": "deepseek-chat",
            },
        }
    }
    adv_config = AdversarialConfig.from_profile(profile)
    db.upsert_story("PL-MAX1", workspace=os.getcwd(), profile="minimal")

    plan_result = {
        "adapter": "claude",
        "skip": False,
        "summary": "Implement auth",
        "extra_instructions": "Do it",
        "reasoning": "OK",
        "trajectory_score": 0.7,
    }

    call_count = {"n": 0}

    def _mock_review_plan(state, plan, cfg, reviewer_model=""):
        call_count["n"] += 1
        # Different category per round so no-progress detection doesn't trigger
        category = "scope" if call_count["n"] == 1 else "feasibility"
        return {
            "quality": "revise",
            "blockers": [
                {
                    "severity": "high",
                    "category": category,
                    "description": f"Issue in round {call_count['n']}",
                }
            ],
            "suggestions": ["Fix it"],
            "reasoning": "Not good enough",
        }

    state = _make_state(story_key="PL-MAX1", stage="implement")

    with patch(
        "story_lifecycle.orchestrator.planner.plan_stage", return_value=plan_result
    ):
        with patch(
            "story_lifecycle.orchestrator.planner.review_plan",
            side_effect=_mock_review_plan,
        ):
            result = run_plan_loop(state, adv_config, adapters=["claude"])

    assert result.decision == "max_rounds"
    assert result.rounds == 2
    assert "max_rounds_reached:2" in result.reason


def test_plan_loop_logs_events(isolated_story_home):
    """Verify started/round/completed events are written to DB."""
    from story_lifecycle.orchestrator.evaluator_loop import (
        run_plan_loop,
        AdversarialConfig,
    )
    from story_lifecycle.db import models as db
    from unittest.mock import patch

    profile = {
        "adversarial": {
            "enabled": True,
            "plan_loop": {
                "enabled": True,
                "stages": ["design"],
                "max_rounds": 3,
                "reviewer_model": "deepseek-chat",
            },
        }
    }
    adv_config = AdversarialConfig.from_profile(profile)
    db.upsert_story("PL-EV1", workspace=os.getcwd(), profile="minimal")

    plan_result = {
        "adapter": "claude",
        "skip": False,
        "summary": "Design the system",
        "extra_instructions": "Design it",
        "reasoning": "OK",
        "trajectory_score": 0.9,
    }
    review_result = {
        "quality": "pass",
        "blockers": [],
        "suggestions": [],
        "reasoning": "Good",
    }

    state = _make_state(story_key="PL-EV1", stage="design")

    with patch(
        "story_lifecycle.orchestrator.planner.plan_stage", return_value=plan_result
    ):
        with patch(
            "story_lifecycle.orchestrator.planner.review_plan",
            return_value=review_result,
        ):
            result = run_plan_loop(state, adv_config, adapters=["claude"])

    assert result.decision == "pass"

    # Check events in DB
    events = db.get_story_events("PL-EV1")

    started = [e for e in events if e.get("event_type") == "evaluator_loop_started"]
    assert len(started) == 1
    started_payload = _parse_payload(started[0])
    assert started_payload["loop_type"] == "plan"
    assert started_payload["mode"] == "in_node"
    assert started_payload["max_rounds"] == 3
    assert started_payload["reviewer_model"] == "deepseek-chat"

    rounds = [e for e in events if e.get("event_type") == "evaluator_loop_round"]
    assert len(rounds) == 1
    round_payload = _parse_payload(rounds[0])
    assert round_payload["round_id"] == 1
    assert round_payload["decision"] == "pass"
    assert round_payload["no_progress"] is False

    completed = [e for e in events if e.get("event_type") == "evaluator_loop_completed"]
    assert len(completed) == 1
    completed_payload = _parse_payload(completed[0])
    assert completed_payload["decision"] == "pass"
    assert completed_payload["rounds"] == 1
    assert completed_payload["reason"] == "all_blockers_resolved"


# -- run_code_review_loop tests --


def test_code_review_loop_pass_returns_pass(isolated_story_home):
    """Code review returns pass, result.decision == "pass"."""
    from story_lifecycle.orchestrator.evaluator_loop import (
        run_code_review_loop,
        AdversarialConfig,
    )
    from story_lifecycle.db import models as db

    profile = {
        "adversarial": {
            "enabled": True,
            "code_loop": {
                "enabled": True,
                "mode": "short_lived",
                "max_rounds": 3,
            },
        }
    }
    adv_config = AdversarialConfig.from_profile(profile)
    db.upsert_story("CR-PASS1", workspace=os.getcwd(), profile="minimal")

    state = _make_state(story_key="CR-PASS1", stage="implement")
    stage_output = {"files_changed": ["auth.py"], "summary": "Added auth"}

    review_result = {
        "quality": "pass",
        "issues": [],
        "suggestions": ["Consider adding more tests"],
        "trajectory_score": 0.9,
        "reasoning": "Looks good",
    }

    with patch(
        "story_lifecycle.orchestrator.planner.review_stage", return_value=review_result
    ):
        result = run_code_review_loop(state, adv_config, stage_output)

    assert result.decision == "pass"
    assert result.rounds == 1
    assert result.reason == "code_review_passed"
    assert result.final_review is not None
    assert result.final_review["quality"] == "pass"


def test_code_review_loop_revise_records_findings(isolated_story_home):
    """Revise quality records findings in DB and logs events."""
    from story_lifecycle.orchestrator.evaluator_loop import (
        run_code_review_loop,
        AdversarialConfig,
    )
    from story_lifecycle.db import models as db

    profile = {
        "adversarial": {
            "enabled": True,
            "code_loop": {"enabled": True, "mode": "short_lived", "max_rounds": 3},
        }
    }
    adv_config = AdversarialConfig.from_profile(profile)
    db.upsert_story("CR-REV1", workspace=os.getcwd(), profile="minimal")

    state = _make_state(story_key="CR-REV1", stage="implement")
    stage_output = {"files_changed": ["auth.py"]}

    review_result = {
        "quality": "revise",
        "issues": [
            {
                "type": "security",
                "severity": "high",
                "location": "auth.py:42",
                "description": "Missing CSRF token",
                "recommendation": "Add CSRF middleware",
            },
            {
                "type": "testing",
                "severity": "medium",
                "location": "",
                "description": "No unit tests",
                "recommendation": "Add test_auth.py",
            },
        ],
        "suggestions": [],
        "trajectory_score": 0.4,
        "reasoning": "Security issue found",
    }

    with patch(
        "story_lifecycle.orchestrator.planner.review_stage", return_value=review_result
    ):
        result = run_code_review_loop(state, adv_config, stage_output)

    assert result.decision == "revise"
    assert result.rounds == 1

    # Verify findings recorded in DB
    findings = db.get_open_findings("CR-REV1", min_severity="low")
    assert len(findings) == 2
    severities = {f["severity"] for f in findings}
    assert "high" in severities
    assert "medium" in severities
    descriptions = {f["description"] for f in findings}
    assert "Missing CSRF token" in descriptions
    assert "No unit tests" in descriptions

    # Verify events logged
    events = db.get_story_events("CR-REV1")
    started = [e for e in events if e.get("event_type") == "evaluator_loop_started"]
    assert len(started) == 1
    rounds = [e for e in events if e.get("event_type") == "evaluator_loop_round"]
    assert len(rounds) == 1
    completed = [e for e in events if e.get("event_type") == "evaluator_loop_completed"]
    assert len(completed) == 1
    completed_payload = _parse_payload(completed[0])
    assert completed_payload["decision"] == "revise"


def test_code_review_loop_revise_builds_repair_packet(isolated_story_home):
    """Revise quality builds repair packet file on disk."""
    from story_lifecycle.orchestrator.evaluator_loop import (
        run_code_review_loop,
        AdversarialConfig,
    )
    from story_lifecycle.db import models as db
    from pathlib import Path

    workspace = str(isolated_story_home / "ws")
    Path(workspace).mkdir(exist_ok=True)

    profile = {
        "adversarial": {
            "enabled": True,
            "code_loop": {"enabled": True, "mode": "short_lived", "max_rounds": 3},
        }
    }
    adv_config = AdversarialConfig.from_profile(profile)
    db.upsert_story("CR-RP1", workspace=workspace, profile="minimal")

    state = _make_state(
        story_key="CR-RP1", stage="implement", workspace=workspace, execution_count=1
    )
    stage_output = {"files_changed": ["auth.py"]}

    review_result = {
        "quality": "revise",
        "issues": [
            {
                "type": "security",
                "severity": "high",
                "location": "auth.py:42",
                "description": "Missing CSRF token",
                "recommendation": "Add CSRF middleware",
            }
        ],
        "suggestions": ["Fix security issue"],
        "trajectory_score": 0.3,
        "reasoning": "Needs fix",
    }

    with patch(
        "story_lifecycle.orchestrator.planner.review_stage", return_value=review_result
    ):
        result = run_code_review_loop(state, adv_config, stage_output)

    assert result.decision == "revise"
    assert result.final_review is not None
    repair_path = result.final_review.get("repair_packet_path")
    assert repair_path is not None
    assert "repair_implement_round2.md" in repair_path

    # Verify file content
    content = Path(repair_path).read_text(encoding="utf-8")
    assert "Missing CSRF token" in content
    assert "auth.py:42" in content


def test_code_review_loop_handles_reviewer_json_failure(isolated_story_home):
    """Reviewer raises exception -> result.decision == "fail", no findings created."""
    from story_lifecycle.orchestrator.evaluator_loop import (
        run_code_review_loop,
        AdversarialConfig,
    )
    from story_lifecycle.db import models as db

    profile = {
        "adversarial": {
            "enabled": True,
            "code_loop": {"enabled": True, "mode": "short_lived", "max_rounds": 3},
        }
    }
    adv_config = AdversarialConfig.from_profile(profile)
    db.upsert_story("CR-FAIL1", workspace=os.getcwd(), profile="minimal")

    state = _make_state(story_key="CR-FAIL1", stage="implement")
    stage_output = {"files_changed": ["main.py"]}

    with patch(
        "story_lifecycle.orchestrator.planner.review_stage",
        side_effect=RuntimeError("LLM connection refused"),
    ):
        result = run_code_review_loop(state, adv_config, stage_output)

    assert result.decision == "fail"
    assert "RuntimeError" in result.reason
    assert result.rounds == 1

    # No findings should have been created
    findings = db.get_open_findings("CR-FAIL1", min_severity="low")
    assert len(findings) == 0

    # Events should still be logged (round + completed)
    events = db.get_story_events("CR-FAIL1")
    rounds = [e for e in events if e.get("event_type") == "evaluator_loop_round"]
    assert len(rounds) == 1
    completed = [e for e in events if e.get("event_type") == "evaluator_loop_completed"]
    assert len(completed) == 1
    completed_payload = _parse_payload(completed[0])
    assert completed_payload["decision"] == "fail"


def test_code_review_loop_records_prompt_tokens_estimation(isolated_story_home):
    """Verify prompt_tokens.estimated == True in round event."""
    from story_lifecycle.orchestrator.evaluator_loop import (
        run_code_review_loop,
        AdversarialConfig,
    )
    from story_lifecycle.db import models as db

    profile = {
        "adversarial": {
            "enabled": True,
            "code_loop": {"enabled": True, "mode": "short_lived", "max_rounds": 3},
        }
    }
    adv_config = AdversarialConfig.from_profile(profile)
    db.upsert_story("CR-TOK1", workspace=os.getcwd(), profile="minimal")

    state = _make_state(story_key="CR-TOK1", stage="implement")
    stage_output = {
        "files_changed": ["a.py", "b.py"],
        "diff": "X" * 400,
        "summary": "Changes",
    }

    review_result = {
        "quality": "pass",
        "issues": [],
        "suggestions": [],
        "trajectory_score": 0.95,
        "reasoning": "All good",
    }

    with patch(
        "story_lifecycle.orchestrator.planner.review_stage", return_value=review_result
    ):
        result = run_code_review_loop(state, adv_config, stage_output)

    assert result.decision == "pass"

    # Check prompt_tokens in round event
    events = db.get_story_events("CR-TOK1")
    rounds = [e for e in events if e.get("event_type") == "evaluator_loop_round"]
    assert len(rounds) == 1
    round_payload = _parse_payload(rounds[0])
    pt = round_payload.get("prompt_tokens", {})
    assert pt.get("estimated") is True
    assert pt.get("total", 0) > 0


# -- Integration tests: adversarial loop wiring in nodes.py --


def _enabled_adversarial_profile():
    """Profile with both adversarial loops enabled."""
    return {
        "cli": "claude",
        "stages": {
            "design": {
                "description": "Design",
                "review": True,
                "expected_outputs": ["spec_path", "complexity"],
                "next_default": ["implement"],
            },
            "implement": {
                "description": "Implement",
                "review": True,
                "expected_outputs": ["files_changed", "summary"],
                "next_default": ["review"],
            },
            "review": {
                "description": "Review",
                "review": False,
                "expected_outputs": [],
                "next_default": [],
            },
        },
        "adversarial": {
            "enabled": True,
            "plan_loop": {
                "enabled": True,
                "stages": ["implement"],
                "max_rounds": 3,
                "reviewer_model": "deepseek-chat",
            },
            "code_loop": {
                "enabled": True,
                "mode": "short_lived",
                "max_rounds": 3,
            },
        },
    }


def _patch_load_profile(return_value):
    """Context manager that patches all three load_profile import paths."""
    from contextlib import ExitStack
    from unittest.mock import patch

    stack = ExitStack()
    targets = [
        "story_lifecycle.orchestrator.nodes.load_profile",
        "story_lifecycle.orchestrator.nodes.graph_nodes.load_profile",
        "story_lifecycle.orchestrator.nodes.profile_loader.load_profile",
    ]
    for t in targets:
        stack.enter_context(patch(t, return_value=return_value))
    return stack


def test_plan_stage_node_uses_loop_when_enabled(isolated_story_home):
    """When adversarial plan_loop is enabled, plan_stage_node calls run_plan_loop."""
    from story_lifecycle.orchestrator.nodes import plan_stage_node
    from story_lifecycle.db import models as db
    from unittest.mock import patch
    from story_lifecycle.orchestrator.evaluator_loop import LoopResult

    db.upsert_story(
        "INT-PLAN-ON", workspace=str(isolated_story_home), profile="minimal"
    )

    state = _make_state(
        story_key="INT-PLAN-ON",
        stage="implement",
        workspace=str(isolated_story_home),
    )

    loop_result = LoopResult(
        decision="pass",
        rounds=1,
        final_plan={
            "adapter": "claude",
            "summary": "Implement auth via adversarial loop",
            "extra_instructions": "Do it",
            "reasoning": "OK",
            "trajectory_score": 0.9,
        },
        reason="all_blockers_resolved",
    )

    with _patch_load_profile(_enabled_adversarial_profile()):
        with patch(
            "story_lifecycle.orchestrator.planner.compress_context",
            return_value=None,
        ):
            with patch(
                "story_lifecycle.orchestrator.evaluator_loop.run_plan_loop",
                return_value=loop_result,
            ) as mock_loop:
                result = plan_stage_node(state)

    assert mock_loop.called
    assert result["plan_summary"] == "Implement auth via adversarial loop"
    assert result["trajectory_score"] == 0.9


def test_plan_stage_node_skips_loop_when_disabled(isolated_story_home):
    """When adversarial is disabled, run_plan_loop should NOT be called."""
    from story_lifecycle.orchestrator.nodes import plan_stage_node
    from story_lifecycle.db import models as db
    from unittest.mock import patch

    db.upsert_story(
        "INT-PLAN-OFF", workspace=str(isolated_story_home), profile="minimal"
    )

    state = _make_state(
        story_key="INT-PLAN-OFF",
        stage="implement",
        workspace=str(isolated_story_home),
    )

    plan_result = {
        "adapter": "claude",
        "skip": False,
        "summary": "Normal plan",
        "extra_instructions": "Do it",
        "reasoning": "OK",
        "trajectory_score": 0.8,
    }

    # load_profile returns profile with NO adversarial config (default minimal)
    with (
        patch("story_lifecycle.orchestrator.nodes.load_profile", return_value={}),
        patch(
            "story_lifecycle.orchestrator.nodes.profile_loader.load_profile",
            return_value={},
        ),
        patch(
            "story_lifecycle.orchestrator.nodes.graph_nodes.load_profile",
            return_value={},
        ),
    ):
        with patch(
            "story_lifecycle.orchestrator.planner.compress_context",
            return_value=None,
        ):
            with patch(
                "story_lifecycle.orchestrator.planner.plan_stage",
                return_value=plan_result,
            ):
                with patch(
                    "story_lifecycle.orchestrator.evaluator_loop.run_plan_loop"
                ) as mock_loop:
                    result = plan_stage_node(state)

    assert not mock_loop.called
    assert result["plan_summary"] == "Normal plan"


def test_review_stage_node_uses_loop_when_enabled(isolated_story_home):
    """When adversarial code_loop is enabled, review_stage_node calls run_code_review_loop."""
    from story_lifecycle.orchestrator.nodes import review_stage_node
    from story_lifecycle.db import models as db
    from unittest.mock import patch
    from story_lifecycle.orchestrator.evaluator_loop import LoopResult

    db.upsert_story("INT-REV-ON", workspace=str(isolated_story_home), profile="minimal")

    state = _make_state(
        story_key="INT-REV-ON",
        stage="implement",
        workspace=str(isolated_story_home),
    )

    loop_result = LoopResult(
        decision="revise",
        rounds=1,
        final_review={
            "quality": "revise",
            "summary": "Security issues found",
            "issues": [
                {
                    "type": "security",
                    "severity": "high",
                    "location": "auth.py:42",
                    "description": "Missing CSRF token",
                }
            ],
            "suggestions": ["Add CSRF middleware"],
            "trajectory_score": 0.4,
            "reasoning": "Security issue",
        },
        reason="code_review_revise",
    )

    with _patch_load_profile(_enabled_adversarial_profile()):
        with patch(
            "story_lifecycle.orchestrator.evaluator_loop.run_code_review_loop",
            return_value=loop_result,
        ) as mock_loop:
            result = review_stage_node(state)

    assert mock_loop.called
    assert result["last_error"] is not None
    assert "high severity issues" in result["last_error"]
    assert result["review_summary"] == "Security issues found"


def test_review_stage_node_skips_loop_when_disabled(isolated_story_home):
    """When adversarial is disabled, run_code_review_loop should NOT be called."""
    from story_lifecycle.orchestrator.nodes import review_stage_node
    from story_lifecycle.db import models as db
    from unittest.mock import patch

    db.upsert_story(
        "INT-REV-OFF", workspace=str(isolated_story_home), profile="minimal"
    )

    state = _make_state(
        story_key="INT-REV-OFF",
        stage="implement",
        workspace=str(isolated_story_home),
    )

    review_result = {
        "quality": "pass",
        "summary": "Looks good",
        "issues": [],
        "suggestions": [],
        "trajectory_score": 0.9,
        "reasoning": "All OK",
    }

    with _patch_load_profile({}):
        with patch(
            "story_lifecycle.orchestrator.planner.review_stage",
            return_value=review_result,
        ):
            with patch(
                "story_lifecycle.orchestrator.evaluator_loop.run_code_review_loop"
            ) as mock_loop:
                result = review_stage_node(state)

    assert not mock_loop.called
    assert result["review_summary"] == "Looks good"


def test_repair_packet_injected_on_retry(isolated_story_home):
    """When repair_packet_path is in context, _render_prompt includes repair content."""
    from story_lifecycle.orchestrator.nodes import _render_prompt
    from pathlib import Path

    workspace = str(isolated_story_home)
    story_key = "INT-REPAIR"

    # Create a repair packet file on disk
    repair_dir = Path(workspace) / ".story" / "context" / story_key
    repair_dir.mkdir(parents=True, exist_ok=True)
    repair_file = repair_dir / "repair_implement_round1.md"
    repair_file.write_text(
        "# Repair Packet: implement Round 1\nMissing CSRF token",
        encoding="utf-8",
    )

    state = _make_state(
        story_key=story_key,
        stage="implement",
        workspace=workspace,
    )
    state["context"]["repair_packet_path"] = str(repair_file.relative_to(workspace))

    prompt, meta = _render_prompt("implement", state)
    assert "Missing CSRF token" in prompt
    assert "Repair Packet" in prompt


# ── Code loop no_progress → wait_confirm integration ──


def test_code_review_loop_no_progress_returns_wait_confirm(isolated_story_home):
    """When current round repeats all previous high findings, return wait_confirm."""
    from story_lifecycle.orchestrator.evaluator_loop import (
        run_code_review_loop,
        AdversarialConfig,
    )
    from story_lifecycle.db import models as db

    profile = {
        "adversarial": {
            "enabled": True,
            "code_loop": {"enabled": True, "mode": "short_lived", "max_rounds": 3},
        }
    }
    adv_config = AdversarialConfig.from_profile(profile)
    db.upsert_story("CR-NP1", workspace=os.getcwd(), profile="minimal")

    # Seed a previous high finding in the SAME stage
    db.create_finding(
        story_key="CR-NP1",
        stage="implement",
        source="code_review",
        severity="high",
        category="null-safety",
        description="NPE in handler",
        location="src/handler.py:42",
    )

    state = _make_state(story_key="CR-NP1", stage="implement", execution_count=1)
    stage_output = {"files_changed": ["src/handler.py"]}

    review_return = {
        "quality": "revise",
        "issues": [
            {
                "type": "null-safety",
                "severity": "high",
                "description": "NPE in handler",
                "location": "src/handler.py:42",
            }
        ],
        "trajectory_score": 0.3,
        "suggestions": [],
        "context_updates": {},
        "reasoning": "same issue",
    }

    with patch(
        "story_lifecycle.orchestrator.planner.review_stage", return_value=review_return
    ):
        result = run_code_review_loop(state, adv_config, stage_output)

    assert result.decision == "wait_confirm"
    assert "no_progress" in result.reason

    completed = _get_events_by_type("CR-NP1", "evaluator_loop_completed")
    assert len(completed) == 1
    assert _parse_payload(completed[0])["decision"] == "wait_confirm"


def test_code_review_loop_cross_stage_findings_not_repeated(isolated_story_home):
    """High finding in a different stage should NOT trigger no_progress."""
    from story_lifecycle.orchestrator.evaluator_loop import (
        run_code_review_loop,
        AdversarialConfig,
    )
    from story_lifecycle.db import models as db

    profile = {
        "adversarial": {
            "enabled": True,
            "code_loop": {"enabled": True, "mode": "short_lived", "max_rounds": 3},
        }
    }
    adv_config = AdversarialConfig.from_profile(profile)
    db.upsert_story("CR-ISO1", workspace=os.getcwd(), profile="minimal")

    # Seed a high finding in DESIGN stage (different from current stage)
    db.create_finding(
        story_key="CR-ISO1",
        stage="design",
        source="code_review",
        severity="high",
        category="null-safety",
        description="NPE in handler",
        location="src/handler.py:42",
    )

    state = _make_state(story_key="CR-ISO1", stage="implement", execution_count=1)
    stage_output = {"files_changed": ["src/handler.py"]}

    # Reviewer raises the SAME finding in implement stage
    review_return = {
        "quality": "revise",
        "issues": [
            {
                "type": "null-safety",
                "severity": "high",
                "description": "NPE in handler",
                "location": "src/handler.py:42",
            }
        ],
        "trajectory_score": 0.3,
        "suggestions": [],
        "context_updates": {},
        "reasoning": "same issue found",
    }

    with patch(
        "story_lifecycle.orchestrator.planner.review_stage", return_value=review_return
    ):
        result = run_code_review_loop(state, adv_config, stage_output)

    # Should NOT be wait_confirm because the previous finding is in a different stage
    assert result.decision == "revise"

    # Verify round event shows no no_progress
    rounds = _get_events_by_type("CR-ISO1", "evaluator_loop_round")
    assert len(rounds) == 1
    round_payload = _parse_payload(rounds[0])
    assert round_payload.get("no_progress") is False
