"""Engine Flywheel — engine trace and strategy lifecycle tracking.

The Engine Flywheel manages the lifecycle of engine execution knowledge:
- EngineTrace: records of how the orchestration engine performed
- StrategyRecord: records of which strategies worked/didn't work
- EvalEvidence: structured evidence from evaluation outcomes

Design doc: idea-dual-flywheel-domain-and-engine.md §Engine Flywheel
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ...db import models as db

# ── data structures ──


@dataclass
class EngineTrace:
    """Record of a single engine execution decision and its outcome.

    Captures the full context of a routing/strategy decision for
    later analysis and strategy improvement.
    """

    trace_id: str
    story_key: str
    stage: str
    decision_type: str  # "route", "gate", "strategy", "retry"
    decision: str  # e.g. "retry", "advance", "fail"
    context_snapshot: dict[str, Any] = field(default_factory=dict)
    outcome: str = ""  # Filled later: "success", "failure", "partial"
    outcome_detail: str = ""
    duration_ms: int = 0
    created_at: str = ""


@dataclass
class EvalEvidence:
    """Structured evidence from an evaluation outcome.

    Links engine decisions to measurable outcomes, enabling
    strategy confidence recalculation.
    """

    evidence_id: str
    trace_id: str
    story_key: str
    metric: str  # e.g. "trajectory_score", "review_pass_rate", "time_to_complete"
    value: float = 0.0
    baseline: float = 0.0  # Expected baseline for comparison
    improvement: float = 0.0  # (value - baseline) / baseline
    created_at: str = ""


@dataclass
class StrategyRecord:
    """Record of a strategy's cumulative performance.

    Aggregates eval evidence to determine which strategies
    should be preferred in similar future scenarios.
    """

    strategy_id: str
    strategy_name: str  # e.g. "retry_with_different_provider"
    applies_when: str  # Condition description
    total_applications: int = 0
    success_count: int = 0
    avg_improvement: float = 0.0
    confidence: float = 0.0
    last_applied: str = ""
    created_at: str = ""


# ── persistence ──

ENGINE_DIR = Path.home() / ".story-lifecycle" / "flywheel" / "engine"


def _now_iso() -> str:
    return datetime.now().isoformat()


def record_engine_trace(
    story_key: str,
    stage: str,
    decision_type: str,
    decision: str,
    context_snapshot: dict[str, Any] | None = None,
    duration_ms: int = 0,
) -> EngineTrace:
    """Record an engine execution trace.

    Args:
        story_key: Story being executed.
        stage: Current stage.
        decision_type: Type of decision (route/gate/strategy/retry).
        decision: The decision made.
        context_snapshot: Key state at decision time.
        duration_ms: How long the decision took.

    Returns:
        The recorded EngineTrace.
    """
    trace = EngineTrace(
        trace_id=uuid.uuid4().hex[:12],
        story_key=story_key,
        stage=stage,
        decision_type=decision_type,
        decision=decision,
        context_snapshot=context_snapshot or {},
        duration_ms=duration_ms,
        created_at=_now_iso(),
    )

    ENGINE_DIR.mkdir(parents=True, exist_ok=True)
    trace_file = ENGINE_DIR / f"{trace.trace_id}.json"
    data = {
        "trace_id": trace.trace_id,
        "story_key": trace.story_key,
        "stage": trace.stage,
        "decision_type": trace.decision_type,
        "decision": trace.decision,
        "context_snapshot": trace.context_snapshot,
        "outcome": trace.outcome,
        "outcome_detail": trace.outcome_detail,
        "duration_ms": trace.duration_ms,
        "created_at": trace.created_at,
    }
    trace_file.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Log event
    db.log_event(
        story_key,
        stage,
        "engine_trace",
        {
            "trace_id": trace.trace_id,
            "decision_type": decision_type,
            "decision": decision,
            "duration_ms": duration_ms,
        },
    )

    return trace


def update_trace_outcome(
    trace_id: str,
    outcome: str,
    outcome_detail: str = "",
) -> bool:
    """Update the outcome of an engine trace.

    Args:
        trace_id: The trace to update.
        outcome: "success", "failure", or "partial".
        outcome_detail: Free-text detail.

    Returns:
        True if updated, False if not found.
    """
    trace_file = ENGINE_DIR / f"{trace_id}.json"
    if not trace_file.exists():
        return False

    try:
        data = json.loads(trace_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False

    data["outcome"] = outcome
    data["outcome_detail"] = outcome_detail

    trace_file.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return True


def record_eval_evidence(
    trace_id: str,
    story_key: str,
    metric: str,
    value: float,
    baseline: float = 0.0,
) -> EvalEvidence:
    """Record evaluation evidence for an engine trace.

    Args:
        trace_id: The engine trace being evaluated.
        story_key: Story being evaluated.
        metric: Metric name.
        value: Observed value.
        baseline: Expected baseline.

    Returns:
        The recorded EvalEvidence.
    """
    improvement = (value - baseline) / baseline if baseline != 0 else 0.0

    evidence = EvalEvidence(
        evidence_id=uuid.uuid4().hex[:12],
        trace_id=trace_id,
        story_key=story_key,
        metric=metric,
        value=value,
        baseline=baseline,
        improvement=improvement,
        created_at=_now_iso(),
    )

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    evidence_file = EVIDENCE_DIR / f"{evidence.evidence_id}.json"
    data = {
        "evidence_id": evidence.evidence_id,
        "trace_id": evidence.trace_id,
        "story_key": evidence.story_key,
        "metric": evidence.metric,
        "value": evidence.value,
        "baseline": evidence.baseline,
        "improvement": evidence.improvement,
        "created_at": evidence.created_at,
    }
    evidence_file.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return evidence


EVIDENCE_DIR = ENGINE_DIR / "evidence"


# ── strategy records ──

STRATEGY_DIR = ENGINE_DIR / "strategies"


def get_or_create_strategy(strategy_name: str, applies_when: str) -> StrategyRecord:
    """Get an existing strategy record or create a new one."""
    STRATEGY_DIR.mkdir(parents=True, exist_ok=True)

    # Find existing
    for f in STRATEGY_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("strategy_name") == strategy_name:
            return StrategyRecord(
                strategy_id=data["strategy_id"],
                strategy_name=data["strategy_name"],
                applies_when=data.get("applies_when", ""),
                total_applications=data.get("total_applications", 0),
                success_count=data.get("success_count", 0),
                avg_improvement=data.get("avg_improvement", 0.0),
                confidence=data.get("confidence", 0.0),
                last_applied=data.get("last_applied", ""),
                created_at=data.get("created_at", ""),
            )

    # Create new
    record = StrategyRecord(
        strategy_id=uuid.uuid4().hex[:12],
        strategy_name=strategy_name,
        applies_when=applies_when,
        created_at=_now_iso(),
    )
    _save_strategy_record(record)
    return record


def update_strategy_performance(
    strategy_name: str,
    success: bool,
    improvement: float = 0.0,
) -> StrategyRecord | None:
    """Update a strategy's performance record.

    Args:
        strategy_name: Name of the strategy.
        success: Whether the application was successful.
        improvement: Observed improvement score.

    Returns:
        Updated StrategyRecord, or None if not found.
    """
    STRATEGY_DIR.mkdir(parents=True, exist_ok=True)

    for f in STRATEGY_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("strategy_name") == strategy_name:
            record = StrategyRecord(
                strategy_id=data["strategy_id"],
                strategy_name=data["strategy_name"],
                applies_when=data.get("applies_when", ""),
                total_applications=data.get("total_applications", 0) + 1,
                success_count=data.get("success_count", 0) + (1 if success else 0),
                avg_improvement=data.get("avg_improvement", 0.0),
                confidence=data.get("confidence", 0.0),
                last_applied=_now_iso(),
                created_at=data.get("created_at", ""),
            )

            # Recalculate average improvement
            old_avg = data.get("avg_improvement", 0.0)
            old_count = data.get("total_applications", 0)
            if old_count > 0:
                record.avg_improvement = (
                    old_avg * old_count + improvement
                ) / record.total_applications
            else:
                record.avg_improvement = improvement

            # Recalculate confidence
            if record.total_applications >= 3:
                record.confidence = record.success_count / record.total_applications

            _save_strategy_record(record)
            return record

    return None


def _save_strategy_record(record: StrategyRecord) -> None:
    """Persist a strategy record."""
    STRATEGY_DIR.mkdir(parents=True, exist_ok=True)
    f = STRATEGY_DIR / f"{record.strategy_id}.json"
    data = {
        "strategy_id": record.strategy_id,
        "strategy_name": record.strategy_name,
        "applies_when": record.applies_when,
        "total_applications": record.total_applications,
        "success_count": record.success_count,
        "avg_improvement": record.avg_improvement,
        "confidence": record.confidence,
        "last_applied": record.last_applied,
        "created_at": record.created_at,
    }
    f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ── query helpers ──


def list_engine_traces(
    story_key: str = "",
    decision_type: str = "",
    limit: int = 50,
) -> list[dict]:
    """List engine traces with optional filters."""
    ENGINE_DIR.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    for f in sorted(
        ENGINE_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True
    ):
        if len(results) >= limit:
            break
        if f.parent.name != "engine":
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if story_key and data.get("story_key") != story_key:
            continue
        if decision_type and data.get("decision_type") != decision_type:
            continue
        results.append(data)
    return results


def list_strategies(min_confidence: float = 0.0, limit: int = 50) -> list[dict]:
    """List strategy records with optional filters."""
    STRATEGY_DIR.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    for f in sorted(
        STRATEGY_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True
    ):
        if len(results) >= limit:
            break
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("confidence", 0) < min_confidence:
            continue
        results.append(data)
    return results
