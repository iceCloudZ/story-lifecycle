"""routers/diagnostics — diagnostics domain API（设计15 阶段C 拆自 api.py）。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ....infra.db import models as db

router = APIRouter(tags=["diagnostics"])

@router.get("/api/session/health")
def health():
    return {"status": "ok", "version": "0.1.0"}


@router.get("/api/story/{story_key}/loop-trace")
def get_loop_trace(story_key: str):
    """Return adversarial loop trace for a story."""
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")

    events = db.get_story_events(story_key)

    plan_rounds = []
    code_rounds = []

    for ev in events:
        payload = db.parse_event_payload(ev)
        ev_type = ev.get("event_type", "")
        stage = ev.get("stage", "")

        # Plan loop rounds
        if ev_type == "plan" and payload.get("adversarial_loop"):
            plan_rounds.append(
                {
                    "stage": stage,
                    "loop_rounds": payload.get("loop_rounds", 0),
                    "loop_decision": payload.get("loop_decision", ""),
                    "summary": payload.get("summary", "")[:200],
                    "trajectory_score": payload.get("trajectory_score"),
                    "created_at": ev.get("created_at", ""),
                }
            )

        # Code review loop rounds
        if ev_type == "review" and payload.get("adversarial_loop"):
            code_rounds.append(
                {
                    "stage": stage,
                    "loop_rounds": payload.get("loop_rounds", 0),
                    "loop_decision": payload.get("loop_decision", ""),
                    "quality": payload.get("quality", ""),
                    "summary": payload.get("summary", "")[:200],
                    "issues_count": payload.get("issues_count", 0),
                    "trajectory_score": payload.get("trajectory_score"),
                    "created_at": ev.get("created_at", ""),
                }
            )

    return {
        "story_key": story_key,
        "plan_loop": {"rounds": plan_rounds},
        "code_loop": {"rounds": code_rounds},
    }


@router.get("/api/story/{story_key}/debug")
def debug_story(story_key: str, limit: int = 50, event_type: str = ""):
    """Read-only debug endpoint. Returns observability events and quality status.

    Query params:
        limit: Max recentEvents (default 50). Applies at DB level.
        event_type: Filter recentEvents to this type at DB level.
    """
    from ...observability.events import build_debug_response

    response = build_debug_response(
        story_key, recent_limit=limit, event_type=event_type
    )
    if "error" in response:
        raise HTTPException(404, response["error"])

    return response

