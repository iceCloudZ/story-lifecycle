"""Observability helpers for the Story Lifecycle graph.

Event writers (log_*) record structured events to event_log.
Debug query helpers (build_debug_response) provide read-only diagnostics.
"""

from __future__ import annotations

import json
from pathlib import Path

from ...infra.db import models as db


# -------- attempt_id --------


def _attempt_id(stage: str, execution_count: int) -> str:
    return f"{stage}:{execution_count}"


# -------- event writers --------


def log_node_error(
    story_key: str,
    stage: str,
    node: str,
    error_type: str,
    error: str,
    execution_count: int = 0,
    recoverable: bool = True,
    action: str = "",
    file_hint: str = "",
) -> None:
    db.log_event(
        story_key,
        stage,
        "node_error",
        {
            "node": node,
            "error_type": error_type,
            "error": error,
            "attempt_id": _attempt_id(stage, execution_count),
            "execution_count": execution_count,
            "recoverable": recoverable,
            "action": action,
            "file_hint": file_hint,
        },
    )


def log_route_decision(
    state: dict,
    action: str,
    reason: str,
    router_mode: str,
    extra: dict | None = None,
) -> None:
    stage = state.get("current_stage", "")
    execution_count = state.get("execution_count", 0)

    payload = {
        "action": action,
        "reason": reason,
        "attempt_id": _attempt_id(stage, execution_count),
        "last_error": state.get("last_error"),
        "execution_count": execution_count,
        "trajectory_score": state.get("trajectory_score"),
        "review_summary": state.get("review_summary"),
        "router_mode": router_mode,
        **(extra or {}),
    }
    db.log_event(state.get("story_key", ""), stage, "route_decision", payload)


def log_prompt_context(state: dict, metadata: dict) -> None:
    stage = state.get("current_stage", "")
    execution_count = state.get("execution_count", 0)

    db.log_event(
        state.get("story_key", ""),
        stage,
        "prompt_context",
        {
            "quality_packet_injected": metadata.get("quality_packet_injected", False),
            "quality_checklist_injected": metadata.get(
                "quality_checklist_injected", False
            ),
            "attempt_id": _attempt_id(stage, execution_count),
            "execution_count": execution_count,
            "open_findings_count": metadata.get("open_findings_count", 0),
            "learned_patterns_count": metadata.get("learned_patterns_count", 0),
            "relevance_tags": metadata.get("relevance_tags", []),
            "has_prd": metadata.get("has_prd", False),
            "has_plan_file": metadata.get("has_plan_file", False),
            "prompt_sha256": metadata.get("prompt_sha256", ""),
            "quality_context_sha256": metadata.get("quality_context_sha256", ""),
        },
    )


def log_dod_check(state: dict, dod: dict) -> None:
    stage = state.get("current_stage", "")
    execution_count = state.get("execution_count", 0)

    db.log_event(
        state.get("story_key", ""),
        stage,
        "dod_check",
        {
            "passed": dod.get("passed", False),
            "attempt_id": _attempt_id(stage, execution_count),
            "execution_count": execution_count,
            "blocking": dod.get("blocking", []),
            "warnings": dod.get("warnings", []),
            "open_high_count": dod.get("open_high_count", 0),
            "verification_present": dod.get("verification_present", False),
        },
    )


# -------- debug query helpers (read-only) --------

OBSERVABILITY_EVENT_TYPES = frozenset(
    {
        "route_decision",
        "node_error",
        "prompt_context",
        "dod_check",
        "gate_decision",
    }
)

RELATED_EVENT_TYPES = frozenset(
    {
        "router",
        "review",
        "execute",
        "complete",
        "fail",
        "retry",
        "skip",
        "verification_result",
        "code_review_finding",
        "finding_status_changed",
        "readiness_check",
        "story_intake",
    }
)

ALL_DEBUG_EVENT_TYPES = OBSERVABILITY_EVENT_TYPES | RELATED_EVENT_TYPES


def _load_events_by_type(
    story_key: str, event_types: list[str], limit: int = 20
) -> list[dict]:
    """Load events of given types for a story. Read-only."""
    if not event_types:
        return []
    placeholders = ",".join("?" * len(event_types))
    conn = db.get_conn()
    try:
        rows = conn.execute(
            f"SELECT * FROM event_log WHERE story_key = ? AND event_type IN ({placeholders}) "
            "ORDER BY id DESC LIMIT ?",
            [story_key] + event_types + [limit],
        ).fetchall()
    finally:
        conn.close()
    return [_serialize_event(dict(r)) for r in rows]


def _load_recent_events(
    story_key: str, limit: int = 50, event_type: str = ""
) -> list[dict]:
    """Load recent related events for timeline view. Read-only.

    Args:
        story_key: The story to query.
        limit: Max events to return.
        event_type: If set, query only this event type at the DB level.
    """
    if event_type:
        types = [event_type]
    else:
        types = list(ALL_DEBUG_EVENT_TYPES)
    placeholders = ",".join("?" * len(types))
    conn = db.get_conn()
    try:
        rows = conn.execute(
            f"SELECT * FROM event_log WHERE story_key = ? AND event_type IN ({placeholders}) "
            "ORDER BY id DESC LIMIT ?",
            [story_key] + types + [limit],
        ).fetchall()
    finally:
        conn.close()
    return [_serialize_event(dict(r)) for r in rows]


def _serialize_event(e: dict) -> dict:
    """Serialize an event row for JSON output."""
    payload = e.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            pass
    return {
        "id": e.get("id"),
        "eventType": e.get("event_type"),
        "stage": e.get("stage"),
        "payload": payload if isinstance(payload, dict) else {},
        "createdAt": e.get("created_at"),
    }


def build_debug_response(
    story_key: str, recent_limit: int = 50, event_type: str = ""
) -> dict:
    """Build the debug response for a story. Pure read-only.

    Args:
        story_key: The story to query.
        recent_limit: Max recent events for the timeline bucket.
        event_type: If set, filter recentEvents to this type at the DB level.
    """
    from ..evaluation.quality import check_dor, check_dod

    s = db.get_story(story_key)
    if not s:
        return {"error": "Story not found"}

    workspace = s.get("workspace", "") or str(Path.cwd())
    story_context = Path(workspace) / ".story" / "context" / story_key
    done_dir = Path(workspace) / ".story" / "done" / story_key
    story_home = Path.home() / ".story-lifecycle"

    route_decisions = _load_events_by_type(story_key, ["route_decision"], limit=20)
    node_errors = _load_events_by_type(story_key, ["node_error"], limit=20)
    prompt_contexts = _load_events_by_type(story_key, ["prompt_context"], limit=10)
    dod_checks = _load_events_by_type(story_key, ["dod_check"], limit=20)
    verification_results = _load_events_by_type(
        story_key, ["verification_result"], limit=5
    )
    readiness_checks = _load_events_by_type(story_key, ["readiness_check"], limit=5)
    recent_events = _load_recent_events(
        story_key, limit=recent_limit, event_type=event_type
    )
    open_findings = db.get_open_findings(story_key)

    # Read-only: check_dor with record=False, check_dod is already pure query
    dor_result = check_dor(story_key, "", record=False)
    dod_result = check_dod(story_key, "")

    return {
        "story": {
            "storyKey": s.get("story_key"),
            "title": s.get("title"),
            "stage": s.get("current_stage"),
            "status": s.get("status"),
            "lastError": s.get("last_error"),
            "executionCount": s.get("execution_count"),
        },
        "recentEvents": recent_events,
        "routeDecisions": route_decisions,
        "nodeErrors": node_errors,
        "promptContexts": prompt_contexts,
        "dodChecks": dod_checks,
        "verificationResults": verification_results,
        "readinessChecks": readiness_checks,
        "openFindings": open_findings,
        "quality": {
            "dor": dor_result,
            "dod": dod_result,
        },
        "fileHints": {
            "storyContextDir": str(story_context.relative_to(workspace)),
            "doneDir": str(done_dir.relative_to(workspace)),
            "graphErrorLog": str(story_home / "graph_error.log"),
            "plannerErrorLog": str(story_home / "planner_error.log"),
        },
    }
