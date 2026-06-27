"""P3 Strategic Router Shadow Mode.

Only generates proposals at anomaly points (review revise/fail, retry
no-progress, low trajectory, provider degradation). Proposals are NOT
executed — only recorded as "shadow" decisions alongside the actual
decision, enabling counterfactual evaluation.

Design doc: idea-orchestrator-agent.md §Strategic Router
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from ..db import models as db

# ── triggers ──


class ShadowTrigger(str, Enum):
    """Conditions that activate the Strategic Router."""

    REVIEW_REVISE = "review_revise"
    REVIEW_FAIL = "review_fail"
    RETRY_NO_PROGRESS = "retry_no_progress"
    LOW_TRAJECTORY = "low_trajectory"
    PROVIDER_DEGRADATION = "provider_degradation"
    BUDGET_BURN_RATE = "budget_burn_rate"
    CONSTRAINT_CONFLICT = "constraint_conflict"
    PRODUCTION_RISK = "production_risk"


# ── data structures ──


@dataclass
class ShadowDecision:
    """A proposed decision from the Strategic Router, recorded but NOT executed."""

    shadow_id: str
    story_key: str
    stage: str
    trigger: ShadowTrigger
    proposed_action: (
        str  # e.g. "insert_stage", "switch_model", "retry_different_provider"
    )
    proposed_detail: str  # human-readable detail
    actual_action: str  # what the rule-based router actually did
    confidence: float  # 0-1
    reason: str
    budget_delta: dict[str, Any] = field(default_factory=dict)
    risk: str = ""
    created_at: str = ""

    # Counterfactual evaluation fields (filled later)
    human_label: str = ""  # "correct" / "incorrect" / "partial" / ""
    later_outcome: str = ""  # what actually happened after actual_action
    counterfactual_note: str = ""  # free-text note from human


# ── trigger detection ──


def detect_triggers(state: dict, stage_config: dict) -> list[ShadowTrigger]:
    """Detect anomaly conditions that should activate Strategic Router.

    Args:
        state: Current StoryState dict.
        stage_config: Stage configuration from profile.

    Returns:
        List of active triggers (may be empty on happy path).
    """
    triggers: list[ShadowTrigger] = []

    review_summary = state.get("review_summary", "") or ""
    last_error = state.get("last_error", "") or ""
    trajectory_score = state.get("trajectory_score")
    execution_count = state.get("execution_count", 0)

    # Review revise
    if "revise" in review_summary.lower() or (
        last_error and "revise" in last_error.lower()
    ):
        triggers.append(ShadowTrigger.REVIEW_REVISE)

    # Review fail
    if "fail" in review_summary.lower() and "revise" not in review_summary.lower():
        triggers.append(ShadowTrigger.REVIEW_FAIL)

    # Retry no progress — same error repeated
    if execution_count >= 2 and _is_repeated_error(state):
        triggers.append(ShadowTrigger.RETRY_NO_PROGRESS)

    # Low trajectory
    if trajectory_score is not None and trajectory_score < 0.3:
        triggers.append(ShadowTrigger.LOW_TRAJECTORY)

    # Provider degradation — check recent LLM traces
    if _detect_provider_degradation(state):
        triggers.append(ShadowTrigger.PROVIDER_DEGRADATION)

    # Budget burn rate
    if _detect_budget_burn(state, stage_config):
        triggers.append(ShadowTrigger.BUDGET_BURN_RATE)

    return triggers


def _is_repeated_error(state: dict) -> bool:
    """Check if the same error type occurred in the last 2+ attempts."""
    story_key = state.get("story_key", "")
    try:
        events = db.get_story_events(story_key)
        error_types: list[str] = []
        for e in reversed(events[-10:]):
            if e.get("event_type") == "node_error":
                payload = e.get("payload", {})
                if isinstance(payload, str):
                    payload = json.loads(payload)
                et = payload.get("error_type", "")
                if et:
                    error_types.append(et)
        if len(error_types) >= 2 and len(set(error_types[:2])) == 1:
            return True
    except Exception:
        pass
    return False


def _detect_provider_degradation(state: dict) -> bool:
    """Check if recent LLM traces show high failure rate."""
    story_key = state.get("story_key", "")
    try:
        conn = db.get_conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) as total, "
                "SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as failures "
                "FROM llm_trace WHERE story_key = ? "
                "AND created_at > datetime('now', '-10 minutes')",
                [story_key],
            ).fetchone()
            if row and row["total"] >= 3:
                failure_rate = row["failures"] / row["total"]
                return failure_rate > 0.5
        finally:
            conn.close()
    except Exception:
        pass
    return False


def _detect_budget_burn(state: dict, stage_config: dict) -> bool:
    """Check if budget burn rate exceeds threshold."""
    story_key = state.get("story_key", "")
    max_minutes = stage_config.get("max_minutes", 30)
    try:
        conn = db.get_conn()
        try:
            row = conn.execute(
                "SELECT SUM(duration_ms) as total_ms FROM llm_trace "
                "WHERE story_key = ? AND created_at > datetime('now', '-5 minutes')",
                [story_key],
            ).fetchone()
            if row and row["total_ms"]:
                burn_minutes = row["total_ms"] / 60000
                # If burning >50% of budget in 5 minutes
                return burn_minutes > max_minutes * 0.5
        finally:
            conn.close()
    except Exception:
        pass
    return False


# ── shadow proposal generation ──


def generate_shadow_proposal(
    state: dict,
    stage_config: dict,
    actual_action: str,
    triggers: list[ShadowTrigger],
) -> ShadowDecision | None:
    """Generate a shadow proposal based on triggers.

    This does NOT use LLM — it uses rule-based proposals only.
    The Shadow Mode is intentionally conservative: only propose
    when there's a clear mismatch between the anomaly signal
    and the actual action taken by the rule router.

    Args:
        state: Current StoryState dict.
        stage_config: Stage configuration.
        actual_action: What the rule-based router decided.
        triggers: Detected anomaly triggers.

    Returns:
        A ShadowDecision if a proposal is warranted, None otherwise.
    """
    if not triggers:
        return None

    story_key = state.get("story_key", "")
    stage = state.get("current_stage", "")
    execution_count = state.get("execution_count", 0)
    trajectory_score = state.get("trajectory_score")

    # Rule-based proposals based on trigger type
    proposed_action = ""
    proposed_detail = ""
    reason = ""
    confidence = 0.5
    budget_delta: dict[str, Any] = {}
    risk = ""

    primary_trigger = triggers[0]

    if primary_trigger == ShadowTrigger.REVIEW_REVISE:
        if actual_action == "retry" and execution_count >= 2:
            proposed_action = "switch_model"
            proposed_detail = (
                "Switch to a different LLM provider/model for fresh perspective"
            )
            reason = "Repeated retries with same provider show no progress; different model may break the loop"
            confidence = 0.65
            budget_delta = {"llm_calls": 2, "minutes": 10}
            risk = "Different model may have weaker domain knowledge"
        elif actual_action == "advance":
            proposed_action = "insert_review_stage"
            proposed_detail = "Insert an additional review stage before advancing"
            reason = "Review was revise but router still chose to advance — potential quality gap"
            confidence = 0.7
            budget_delta = {"llm_calls": 1, "minutes": 5}
            risk = "Delays story but reduces risk of carrying forward defects"

    elif primary_trigger == ShadowTrigger.REVIEW_FAIL:
        if actual_action != "fail":
            proposed_action = "fail"
            proposed_detail = "Mark story as failed and escalate to human"
            reason = "Review concluded fail but router chose differently — human should decide"
            confidence = 0.8
            budget_delta = {}
            risk = "Story stops, but prevents downstream waste"

    elif primary_trigger == ShadowTrigger.RETRY_NO_PROGRESS:
        proposed_action = "skip_stage"
        proposed_detail = "Skip current stage and let human decide on approach change"
        reason = "Multiple retries with same error indicate a systemic issue, not a transient failure"
        confidence = 0.6
        budget_delta = {"minutes": -5}
        risk = "May leave stage incomplete, but saves budget"

    elif primary_trigger == ShadowTrigger.LOW_TRAJECTORY:
        if actual_action == "advance":
            proposed_action = "wait_confirm"
            proposed_detail = (
                "Pause for human review before advancing with low trajectory score"
            )
            reason = f"Trajectory score {trajectory_score:.2f} is very low; auto-advance is risky"
            confidence = 0.75
            budget_delta = {}
            risk = "Requires human attention, but prevents wasted effort on wrong path"

    elif primary_trigger == ShadowTrigger.PROVIDER_DEGRADATION:
        proposed_action = "switch_model"
        proposed_detail = (
            "Switch to fallback provider due to current provider degradation"
        )
        reason = "Current provider showing >50% failure rate in recent calls"
        confidence = 0.7
        budget_delta = {"llm_calls": 1}
        risk = "Fallback provider may have different capabilities"

    elif primary_trigger == ShadowTrigger.BUDGET_BURN_RATE:
        proposed_action = "budget_throttle"
        proposed_detail = "Throttle LLM calls and reduce budget allocation"
        reason = "Budget burn rate exceeds 50% of allocation in 5 minutes"
        confidence = 0.6
        budget_delta = {"minutes": -10}
        risk = "May slow down story progress"

    elif primary_trigger == ShadowTrigger.CONSTRAINT_CONFLICT:
        proposed_action = "wait_confirm"
        proposed_detail = "Pause for human to resolve constraint conflict"
        reason = "Domain/engine constraint conflict detected"
        confidence = 0.7
        budget_delta = {}
        risk = "Low risk — conservative approach"

    elif primary_trigger == ShadowTrigger.PRODUCTION_RISK:
        proposed_action = "insert_review_stage"
        proposed_detail = "Insert architecture review before proceeding to production"
        reason = "Story entering production risk zone"
        confidence = 0.8
        budget_delta = {"llm_calls": 1, "minutes": 10}
        risk = "Delays delivery but prevents production incidents"

    else:
        return None

    return ShadowDecision(
        shadow_id=uuid.uuid4().hex[:12],
        story_key=story_key,
        stage=stage,
        trigger=primary_trigger,
        proposed_action=proposed_action,
        proposed_detail=proposed_detail,
        actual_action=actual_action,
        confidence=confidence,
        reason=reason,
        budget_delta=budget_delta,
        risk=risk,
        created_at=datetime.now().isoformat(),
    )


# ── persistence ──

SHADOW_DIR = Path.home() / ".story-lifecycle" / "shadow-decisions"


def save_shadow(decision: ShadowDecision) -> str:
    """Persist a shadow decision to disk and event_log.

    Returns:
        The shadow_id.
    """
    SHADOW_DIR.mkdir(parents=True, exist_ok=True)
    shadow_file = SHADOW_DIR / f"{decision.shadow_id}.json"
    data = {
        "shadow_id": decision.shadow_id,
        "story_key": decision.story_key,
        "stage": decision.stage,
        "trigger": decision.trigger.value,
        "proposed_action": decision.proposed_action,
        "proposed_detail": decision.proposed_detail,
        "actual_action": decision.actual_action,
        "confidence": decision.confidence,
        "reason": decision.reason,
        "budget_delta": decision.budget_delta,
        "risk": decision.risk,
        "created_at": decision.created_at,
        "human_label": decision.human_label,
        "later_outcome": decision.later_outcome,
        "counterfactual_note": decision.counterfactual_note,
    }
    shadow_file.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Also write to event_log for observability
    db.log_event(
        decision.story_key,
        decision.stage,
        "shadow_decision",
        data,
    )

    return decision.shadow_id


def load_shadow(shadow_id: str) -> ShadowDecision | None:
    """Load a shadow decision by ID."""
    shadow_file = SHADOW_DIR / f"{shadow_id}.json"
    if not shadow_file.exists():
        return None
    data = json.loads(shadow_file.read_text(encoding="utf-8"))
    return ShadowDecision(
        shadow_id=data["shadow_id"],
        story_key=data["story_key"],
        stage=data["stage"],
        trigger=ShadowTrigger(data["trigger"]),
        proposed_action=data["proposed_action"],
        proposed_detail=data["proposed_detail"],
        actual_action=data["actual_action"],
        confidence=data["confidence"],
        reason=data["reason"],
        budget_delta=data.get("budget_delta", {}),
        risk=data.get("risk", ""),
        created_at=data.get("created_at", ""),
        human_label=data.get("human_label", ""),
        later_outcome=data.get("later_outcome", ""),
        counterfactual_note=data.get("counterfactual_note", ""),
    )


def update_counterfactual(
    shadow_id: str,
    human_label: str = "",
    later_outcome: str = "",
    counterfactual_note: str = "",
) -> bool:
    """Update counterfactual evaluation fields on a shadow decision.

    Args:
        shadow_id: The shadow decision to update.
        human_label: "correct" / "incorrect" / "partial"
        later_outcome: What actually happened after the actual decision.
        counterfactual_note: Free-text note.

    Returns:
        True if updated, False if not found.
    """
    decision = load_shadow(shadow_id)
    if decision is None:
        return False

    if human_label:
        decision.human_label = human_label
    if later_outcome:
        decision.later_outcome = later_outcome
    if counterfactual_note:
        decision.counterfactual_note = counterfactual_note

    # Overwrite the file
    save_shadow(decision)

    # Log the counterfactual update
    db.log_event(
        decision.story_key,
        decision.stage,
        "shadow_counterfactual",
        {
            "shadow_id": shadow_id,
            "human_label": decision.human_label,
            "later_outcome": decision.later_outcome,
            "counterfactual_note": decision.counterfactual_note,
        },
    )
    return True


# ── statistics ──


@dataclass
class ShadowStats:
    """Aggregate statistics on shadow decisions."""

    total: int = 0
    proposed_correct: int = 0
    proposed_incorrect: int = 0
    proposed_partial: int = 0
    unlabeled: int = 0
    by_trigger: dict[str, int] = field(default_factory=dict)
    match_rate: float = 0.0  # % of proposed actions that were "correct"


def compute_shadow_stats(story_key: str = "") -> ShadowStats:
    """Compute shadow decision statistics, optionally filtered by story.

    Args:
        story_key: If set, filter to a specific story. Otherwise aggregate all.

    Returns:
        ShadowStats with aggregate counters.
    """
    stats = ShadowStats()
    trigger_counts: dict[str, int] = {}

    for f in SHADOW_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        if story_key and data.get("story_key") != story_key:
            continue

        stats.total += 1

        label = data.get("human_label", "")
        if label == "correct":
            stats.proposed_correct += 1
        elif label == "incorrect":
            stats.proposed_incorrect += 1
        elif label == "partial":
            stats.proposed_partial += 1
        else:
            stats.unlabeled += 1

        trigger = data.get("trigger", "unknown")
        trigger_counts[trigger] = trigger_counts.get(trigger, 0) + 1

    stats.by_trigger = trigger_counts
    labeled = stats.proposed_correct + stats.proposed_incorrect + stats.proposed_partial
    if labeled > 0:
        stats.match_rate = stats.proposed_correct / labeled

    return stats


def list_shadows(story_key: str = "", limit: int = 50) -> list[dict]:
    """List shadow decisions, most recent first.

    Args:
        story_key: If set, filter to a specific story.
        limit: Max results.

    Returns:
        List of shadow decision dicts.
    """
    results: list[dict] = []
    files = sorted(
        SHADOW_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True
    )

    for f in files:
        if len(results) >= limit:
            break
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if story_key and data.get("story_key") != story_key:
            continue
        results.append(data)

    return results


# ── main entry point ──


def run_shadow_router(
    state: dict,
    stage_config: dict,
    actual_action: str,
) -> ShadowDecision | None:
    """Run the Strategic Router in shadow mode.

    1. Detect triggers
    2. Generate proposal (if any)
    3. Persist and record

    This is the main entry point called after the rule-based router
    makes its decision. The shadow proposal does NOT affect the actual
    routing — it's only recorded for later counterfactual evaluation.

    Args:
        state: Current StoryState dict.
        stage_config: Stage configuration.
        actual_action: The action that the rule-based router took.

    Returns:
        The ShadowDecision if generated, None if no anomaly detected.
    """
    triggers = detect_triggers(state, stage_config)
    if not triggers:
        return None

    proposal = generate_shadow_proposal(state, stage_config, actual_action, triggers)
    if proposal is None:
        return None

    save_shadow(proposal)
    return proposal
