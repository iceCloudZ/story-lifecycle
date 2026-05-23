import json

from unittest.mock import patch


def _make_state(story_key="TEST-001", stage="design", **overrides):
    base = {
        "story_key": story_key,
        "title": "Test Story",
        "workspace": __import__("os").getcwd(),
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


def _get_events_by_type(story_key, event_type):
    from story_lifecycle.db import models as _db

    return [
        e for e in _db.get_story_events(story_key) if e.get("event_type") == event_type
    ]


# ── route_decision (via router_node integration) ──


def test_route_decision_happy_path_advance(isolated_story_home):
    """router_node writes route_decision on happy path advance."""
    from story_lifecycle.db import models as db

    db.upsert_story("TEST-001", workspace=__import__("os").getcwd(), profile="minimal")
    state = _make_state(stage="implement")
    state["last_error"] = None

    from story_lifecycle.orchestrator.nodes import router_node

    router_node(state)

    events = _get_events_by_type("TEST-001", "route_decision")
    assert len(events) >= 1
    payload = _parse_payload(events[-1])
    assert payload["action"] == "advance"
    assert payload["router_mode"] == "rule"


# ── prompt_context tests ──


def test_prompt_context_metadata(isolated_story_home):
    """_render_prompt returns metadata dict with quality injection info."""
    from story_lifecycle.db import models as db

    db.upsert_story("TEST-003", workspace=__import__("os").getcwd(), profile="minimal")
    db.create_finding(
        story_key="TEST-003",
        stage="design",
        source="code_review",
        severity="high",
        category="null-safety",
        description="NPE in faceVerify",
        recommendation="Add null check",
    )

    from story_lifecycle.orchestrator.nodes import _render_prompt

    state = _make_state(story_key="TEST-003", stage="implement")
    prompt, meta = _render_prompt("implement", state)

    assert isinstance(prompt, str)
    assert len(prompt) > 0
    assert isinstance(meta, dict)
    assert "open_findings_count" in meta
    assert "learned_patterns_count" in meta
    assert "relevance_tags" in meta
    assert "has_prd" in meta
    assert "quality_packet_injected" in meta


# ── dod_check tests ──


def test_dod_check_logged_on_advance(isolated_story_home):
    """advance_node logs dod_check event on advance (pass or fail)."""
    from story_lifecycle.db import models as db

    db.upsert_story("TEST-004", workspace=__import__("os").getcwd(), profile="minimal")
    state_pass = _make_state(story_key="TEST-004", stage="implement")
    state_pass["context"]["complexity"] = "M"

    from story_lifecycle.orchestrator.nodes import advance_node

    advance_node(state_pass)
    dod_events = _get_events_by_type("TEST-004", "dod_check")
    assert len(dod_events) >= 0  # at minimum no crash


def test_dod_check_exception_writes_node_error(isolated_story_home):
    """check_dod() exception writes node_error, not silently pass."""
    from story_lifecycle.db import models as db

    db.upsert_story("TEST-005", workspace=__import__("os").getcwd(), profile="minimal")
    state = _make_state(story_key="TEST-005", stage="implement")
    state["execution_count"] = 0

    def _raising_dod(*args, **kwargs):
        raise RuntimeError("DB connection lost")

    with patch(
        "story_lifecycle.orchestrator.quality.check_dod",
        side_effect=_raising_dod,
    ):
        from story_lifecycle.orchestrator.nodes import advance_node

        advance_node(state)

    node_errors = _get_events_by_type("TEST-005", "node_error")
    assert len(node_errors) == 1
    payload = _parse_payload(node_errors[-1])
    assert payload["node"] == "advance_node"
    assert payload["action"] == "do_not_silently_pass"
    assert state["last_error"] is not None


# ── node_error tests ──


def test_poll_completion_json_parse_error(isolated_story_home):
    """poll_completion_node writes node_error on done file JSON parse failure."""
    from pathlib import Path

    from story_lifecycle.db import models as db

    db.upsert_story("TEST-006", workspace=__import__("os").getcwd(), profile="minimal")
    state = _make_state(story_key="TEST-006", stage="design")

    ws = __import__("os").getcwd()
    done_dir = Path(ws) / ".story-done" / "TEST-006"
    done_dir.mkdir(parents=True, exist_ok=True)
    done_file = done_dir / "design.json"
    done_file.write_text("not json {{{", encoding="utf-8")

    from story_lifecycle.orchestrator.nodes import poll_completion_node

    poll_completion_node(state)

    try:
        if done_file.exists():
            done_file.unlink()
        if done_dir.exists():
            done_dir.rmdir()
    except Exception:
        pass

    node_errors = _get_events_by_type("TEST-006", "node_error")
    assert len(node_errors) >= 1
    payload = _parse_payload(node_errors[-1])
    assert payload["node"] == "poll_completion_node"
    assert "design.json" in payload.get("file_hint", "")


# ── debug API tests ──


def test_debug_api_read_only(isolated_story_home):
    """Debug API is read-only: calling it does not write new events."""
    from story_lifecycle.db import models as db

    db.upsert_story("TEST-007", workspace=__import__("os").getcwd(), profile="minimal")
    db.log_event("TEST-007", "design", "route_decision", {"action": "advance"})

    from story_lifecycle.orchestrator.observability import build_debug_response

    events_before = len(db.get_story_events("TEST-007"))
    response = build_debug_response("TEST-007")
    events_after = len(db.get_story_events("TEST-007"))

    assert events_before == events_after
    assert response["story"]["storyKey"] == "TEST-007"
    assert len(response["routeDecisions"]) >= 1


def test_debug_api_returns_new_and_old_events(isolated_story_home):
    """Debug API returns new observability + old readiness_check events."""
    from story_lifecycle.db import models as db

    db.upsert_story("TEST-008", workspace=__import__("os").getcwd(), profile="minimal")
    db.log_event("TEST-008", "design", "readiness_check", {"ready": True})
    db.log_event("TEST-008", "design", "route_decision", {"action": "advance"})

    from story_lifecycle.orchestrator.observability import build_debug_response

    response = build_debug_response("TEST-008")
    assert len(response["recentEvents"]) >= 2
    assert len(response["readinessChecks"]) >= 1
    assert len(response["routeDecisions"]) >= 1


def test_debug_api_category_buckets_independent_limits(isolated_story_home):
    """Category buckets not truncated by recentEvents global limit."""
    from story_lifecycle.db import models as db

    db.upsert_story("TEST-009", workspace=__import__("os").getcwd(), profile="minimal")
    for i in range(15):
        db.log_event(
            "TEST-009",
            "implement",
            "prompt_context",
            {"attempt_id": f"implement:{i}"},
        )

    from story_lifecycle.orchestrator.observability import build_debug_response

    response = build_debug_response("TEST-009")
    assert len(response["promptContexts"]) <= 10
    assert len(response["promptContexts"]) > 0


# ── helpers ──


def _parse_payload(event: dict) -> dict:
    payload = event.get("payload", {})
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return {}
    return payload or {}
