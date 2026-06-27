"""Event writers for evaluator-optimizer loop observability.

All events go to the existing event_log table via db.log_event().
"""

from __future__ import annotations

from ..db import models as db


def log_loop_started(
    *,
    story_key: str,
    stage: str,
    loop_id: str,
    loop_type: str,
    mode: str,
    max_rounds: int,
    optimizer_model: str,
    reviewer_model: str,
    attempt_id: str,
) -> None:
    db.log_event(
        story_key,
        stage,
        "evaluator_loop_started",
        {
            "loop_id": loop_id,
            "loop_type": loop_type,
            "stage": stage,
            "mode": mode,
            "max_rounds": max_rounds,
            "optimizer_model": optimizer_model,
            "reviewer_model": reviewer_model,
            "attempt_id": attempt_id,
        },
    )


def log_loop_round(
    *,
    story_key: str,
    stage: str,
    loop_id: str,
    round_id: int,
    loop_type: str,
    mode: str,
    decision: str,
    score: float = 0.0,
    findings: dict | None = None,
    verification: dict | None = None,
    prompt_tokens: dict | None = None,
    timing_ms: dict | None = None,
    diff: dict | None = None,
    no_progress: bool = False,
) -> None:
    db.log_event(
        story_key,
        stage,
        "evaluator_loop_round",
        {
            "loop_id": loop_id,
            "round_id": round_id,
            "loop_type": loop_type,
            "mode": mode,
            "decision": decision,
            "score": score,
            "findings": findings or {},
            "verification": verification or {},
            "prompt_tokens": prompt_tokens or {},
            "timing_ms": timing_ms or {},
            "diff": diff or {},
            "no_progress": no_progress,
        },
    )


def log_loop_completed(
    *,
    story_key: str,
    stage: str,
    loop_id: str,
    loop_type: str,
    decision: str,
    rounds: int,
    reason: str,
    remaining_findings: list | None = None,
) -> None:
    db.log_event(
        story_key,
        stage,
        "evaluator_loop_completed",
        {
            "loop_id": loop_id,
            "loop_type": loop_type,
            "decision": decision,
            "rounds": rounds,
            "reason": reason,
            "remaining_findings": remaining_findings or [],
        },
    )
