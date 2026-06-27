"""P4 Runtime Blackboard — cross-story shared runtime state.

The Blackboard aggregates event_log data into time-bounded snapshots
that other components (Router, Planner) can read as low-priority
evidence. It is intentionally read-only in the main orchestration
flow — the Router should degrade gracefully when the Blackboard
is unavailable or stale.

Design doc: idea-orchestrator-agent.md §Runtime Blackboard
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..db import models as db

# ── data structures ──


@dataclass
class ProviderHealth:
    """Health snapshot for a single LLM provider/model."""

    provider: str
    model: str
    total_calls: int = 0
    failure_count: int = 0
    failure_rate: float = 0.0
    avg_latency_ms: float = 0.0
    last_success_at: str = ""
    last_failure_at: str = ""


@dataclass
class FailureSignature:
    """A recurring failure pattern detected across stories."""

    signature: str  # e.g. "TimeoutError:anthropic/claude-3"
    occurrence_count: int = 0
    affected_stories: list[str] = field(default_factory=list)
    first_seen: str = ""
    last_seen: str = ""


@dataclass
class WorkspacePressure:
    """Resource pressure indicators for a workspace."""

    workspace: str
    active_stories: int = 0
    pending_llm_calls: int = 0
    estimated_minutes_remaining: float = 0.0
    lock_conflicts: int = 0


@dataclass
class BlackboardSnapshot:
    """A time-bounded aggregate snapshot of runtime state."""

    updated_at: str = ""
    staleness_ms: int = 0
    ttl_seconds: int = 300  # 5 min default TTL

    provider_health: list[ProviderHealth] = field(default_factory=list)
    failure_signatures: list[FailureSignature] = field(default_factory=list)
    workspace_pressure: list[WorkspacePressure] = field(default_factory=list)

    # Summary flags for quick consumption by Router/Planner
    any_provider_degraded: bool = False
    any_workspace_overloaded: bool = False
    top_failure_signature: str = ""


# ── Blackboard aggregator ──

SNAPSHOT_DIR = Path.home() / ".story-lifecycle" / "blackboard"
SNAPSHOT_FILE = SNAPSHOT_DIR / "snapshot.json"

_lock = threading.Lock()
_cached_snapshot: BlackboardSnapshot | None = None
_last_refresh: float = 0.0
REFRESH_INTERVAL_S = 30  # refresh at most every 30 seconds


def _now_iso() -> str:
    return datetime.now().isoformat()


def refresh_snapshot(force: bool = False) -> BlackboardSnapshot:
    """Recompute the blackboard snapshot from event_log.

    This is the main aggregation function. It reads from DB and
    computes provider health, failure signatures, and workspace pressure.

    Args:
        force: If True, bypass the refresh interval throttle.

    Returns:
        A fresh BlackboardSnapshot.
    """
    global _cached_snapshot, _last_refresh

    now = time.monotonic()
    if not force and _cached_snapshot and (now - _last_refresh) < REFRESH_INTERVAL_S:
        # Update staleness but don't recompute
        elapsed_ms = int((now - _last_refresh) * 1000)
        _cached_snapshot.staleness_ms = elapsed_ms
        return _cached_snapshot

    snapshot = BlackboardSnapshot(updated_at=_now_iso())

    # 1. Provider health — aggregate llm_trace
    snapshot.provider_health = _compute_provider_health()
    snapshot.any_provider_degraded = any(
        ph.failure_rate > 0.5 for ph in snapshot.provider_health
    )

    # 2. Failure signatures — aggregate recent node_error events
    snapshot.failure_signatures = _compute_failure_signatures()
    if snapshot.failure_signatures:
        snapshot.top_failure_signature = snapshot.failure_signatures[0].signature

    # 3. Workspace pressure — count active stories and pending calls
    snapshot.workspace_pressure = _compute_workspace_pressure()
    snapshot.any_workspace_overloaded = any(
        wp.active_stories >= 3 for wp in snapshot.workspace_pressure
    )

    # Persist
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    _save_snapshot(snapshot)

    with _lock:
        _cached_snapshot = snapshot
        _last_refresh = now

    return snapshot


def get_snapshot() -> BlackboardSnapshot:
    """Get the current blackboard snapshot (cached or refreshed).

    Returns:
        The latest BlackboardSnapshot, refreshing if stale.
    """
    global _cached_snapshot, _last_refresh

    with _lock:
        cached = _cached_snapshot

    if cached is None:
        # Try loading from disk first
        loaded = _load_snapshot()
        if loaded is not None:
            with _lock:
                _cached_snapshot = loaded
                _last_refresh = time.monotonic()
            return loaded
        return refresh_snapshot(force=True)

    # Check TTL
    elapsed_ms = cached.staleness_ms + int((time.monotonic() - _last_refresh) * 1000)
    if elapsed_ms > cached.ttl_seconds * 1000:
        return refresh_snapshot(force=True)

    return cached


def is_stale(snapshot: BlackboardSnapshot | None = None) -> bool:
    """Check if the snapshot is stale (exceeded TTL)."""
    if snapshot is None:
        snapshot = get_snapshot()
    return snapshot.staleness_ms > snapshot.ttl_seconds * 1000


# ── aggregation helpers ──


def _compute_provider_health() -> list[ProviderHealth]:
    """Aggregate LLM trace data into per-provider health snapshots."""
    results: list[ProviderHealth] = []

    try:
        conn = db.get_conn()
        try:
            rows = conn.execute(
                "SELECT model, "
                "COUNT(*) as total_calls, "
                "SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as failures, "
                "AVG(duration_ms) as avg_latency, "
                "MAX(CASE WHEN success = 1 THEN created_at END) as last_success, "
                "MAX(CASE WHEN success = 0 THEN created_at END) as last_failure "
                "FROM llm_trace "
                "WHERE created_at > datetime('now', '-30 minutes') "
                "GROUP BY model"
            ).fetchall()

            for row in rows:
                model_name = row["model"] or "unknown"
                total = row["total_calls"] or 0
                failures = row["failures"] or 0
                results.append(
                    ProviderHealth(
                        provider=_extract_provider(model_name),
                        model=model_name,
                        total_calls=total,
                        failure_count=failures,
                        failure_rate=failures / total if total > 0 else 0.0,
                        avg_latency_ms=row["avg_latency"] or 0.0,
                        last_success_at=row["last_success"] or "",
                        last_failure_at=row["last_failure"] or "",
                    )
                )
        finally:
            conn.close()
    except Exception:
        pass

    # Sort by failure rate descending
    results.sort(key=lambda p: p.failure_rate, reverse=True)
    return results


def _extract_provider(model_name: str) -> str:
    """Extract provider from model name (e.g. 'deepseek/deepseek-chat' -> 'deepseek')."""
    if "/" in model_name:
        return model_name.split("/")[0]
    if "-" in model_name:
        return model_name.split("-")[0]
    return model_name


def _compute_failure_signatures() -> list[FailureSignature]:
    """Detect recurring failure patterns from node_error events."""
    signatures: dict[str, FailureSignature] = {}

    try:
        conn = db.get_conn()
        try:
            rows = conn.execute(
                "SELECT story_key, payload FROM event_log "
                "WHERE event_type = 'node_error' "
                "AND created_at > datetime('now', '-30 minutes') "
                "ORDER BY id DESC LIMIT 100"
            ).fetchall()

            for row in rows:
                payload = row["payload"]
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except (json.JSONDecodeError, TypeError):
                        continue
                if not isinstance(payload, dict):
                    continue

                error_type = payload.get("error_type", "Unknown")
                node = payload.get("node", "")
                sig = f"{error_type}:{node}"

                if sig not in signatures:
                    signatures[sig] = FailureSignature(
                        signature=sig,
                        first_seen=_now_iso(),
                    )
                signatures[sig].occurrence_count += 1
                story_key = row["story_key"]
                if story_key and story_key not in signatures[sig].affected_stories:
                    signatures[sig].affected_stories.append(story_key)
                signatures[sig].last_seen = _now_iso()
        finally:
            conn.close()
    except Exception:
        pass

    # Sort by occurrence count descending
    result = sorted(signatures.values(), key=lambda s: s.occurrence_count, reverse=True)
    return result[:10]  # Top 10


def _compute_workspace_pressure() -> list[WorkspacePressure]:
    """Compute workspace resource pressure."""
    results: list[WorkspacePressure] = []

    try:
        conn = db.get_conn()
        try:
            rows = conn.execute(
                "SELECT workspace, COUNT(*) as active_stories "
                "FROM story WHERE status IN ('running', 'active') "
                "GROUP BY workspace"
            ).fetchall()

            for row in rows:
                workspace = row["workspace"] or ""
                results.append(
                    WorkspacePressure(
                        workspace=workspace,
                        active_stories=row["active_stories"] or 0,
                    )
                )
        finally:
            conn.close()
    except Exception:
        pass

    # Sort by active stories descending
    results.sort(key=lambda w: w.active_stories, reverse=True)
    return results


# ── persistence ──


def _save_snapshot(snapshot: BlackboardSnapshot) -> None:
    """Persist snapshot to disk."""
    data = {
        "updated_at": snapshot.updated_at,
        "staleness_ms": snapshot.staleness_ms,
        "ttl_seconds": snapshot.ttl_seconds,
        "provider_health": [
            {
                "provider": ph.provider,
                "model": ph.model,
                "total_calls": ph.total_calls,
                "failure_count": ph.failure_count,
                "failure_rate": ph.failure_rate,
                "avg_latency_ms": ph.avg_latency_ms,
                "last_success_at": ph.last_success_at,
                "last_failure_at": ph.last_failure_at,
            }
            for ph in snapshot.provider_health
        ],
        "failure_signatures": [
            {
                "signature": fs.signature,
                "occurrence_count": fs.occurrence_count,
                "affected_stories": fs.affected_stories,
                "first_seen": fs.first_seen,
                "last_seen": fs.last_seen,
            }
            for fs in snapshot.failure_signatures
        ],
        "workspace_pressure": [
            {
                "workspace": wp.workspace,
                "active_stories": wp.active_stories,
                "pending_llm_calls": wp.pending_llm_calls,
                "estimated_minutes_remaining": wp.estimated_minutes_remaining,
                "lock_conflicts": wp.lock_conflicts,
            }
            for wp in snapshot.workspace_pressure
        ],
        "any_provider_degraded": snapshot.any_provider_degraded,
        "any_workspace_overloaded": snapshot.any_workspace_overloaded,
        "top_failure_signature": snapshot.top_failure_signature,
    }
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _load_snapshot() -> BlackboardSnapshot | None:
    """Load snapshot from disk."""
    if not SNAPSHOT_FILE.exists():
        return None
    try:
        data = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    snapshot = BlackboardSnapshot(
        updated_at=data.get("updated_at", ""),
        staleness_ms=data.get("staleness_ms", 0),
        ttl_seconds=data.get("ttl_seconds", 300),
        any_provider_degraded=data.get("any_provider_degraded", False),
        any_workspace_overloaded=data.get("any_workspace_overloaded", False),
        top_failure_signature=data.get("top_failure_signature", ""),
    )

    for ph_data in data.get("provider_health", []):
        snapshot.provider_health.append(ProviderHealth(**ph_data))

    for fs_data in data.get("failure_signatures", []):
        snapshot.failure_signatures.append(FailureSignature(**fs_data))

    for wp_data in data.get("workspace_pressure", []):
        snapshot.workspace_pressure.append(WorkspacePressure(**wp_data))

    return snapshot


# ── Router consumption helper ──


def get_router_evidence() -> dict[str, Any]:
    """Get a compact evidence dict for the Router to consume.

    This is the primary interface for the Router/Planner to read
    Blackboard state. Returns a flat dict with key signals.

    Returns:
        Dict with provider_health_summary, top_failures, pressure_flags.
        Empty dict if Blackboard is unavailable.
    """
    try:
        snapshot = get_snapshot()
    except Exception:
        # Graceful degradation — blackboard failure should not break routing
        return {}

    return {
        "provider_degraded": snapshot.any_provider_degraded,
        "workspace_overloaded": snapshot.any_workspace_overloaded,
        "top_failure_signature": snapshot.top_failure_signature,
        "degraded_providers": [
            {
                "provider": ph.provider,
                "model": ph.model,
                "failure_rate": ph.failure_rate,
            }
            for ph in snapshot.provider_health
            if ph.failure_rate > 0.3
        ],
        "staleness_ms": snapshot.staleness_ms,
        "is_stale": is_stale(snapshot),
    }
