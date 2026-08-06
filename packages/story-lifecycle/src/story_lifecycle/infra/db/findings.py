"""findings — quality finding CRUD（设计15 阶段B 拆分自 models.py）。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from .connection import _db


def create_finding(
    story_key,
    stage,
    source,
    severity,
    category,
    description,
    location=None,
    recommendation=None,
    root_cause=None,
    evidence=None,
) -> str:

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    fid = f"finding-{uuid.uuid4().hex[:12]}"
    evidence_json = json.dumps(evidence) if evidence else "[]"
    with _db() as conn:
        conn.execute(
            "INSERT INTO finding (id, story_key, stage, source, severity, category, location, description, recommendation, root_cause, evidence, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                fid,
                story_key,
                stage,
                source,
                severity,
                category,
                location,
                description,
                recommendation,
                root_cause,
                evidence_json,
                "open",
                now,
                now,
            ),
        )
    return fid


def get_finding(finding_id: str) -> dict | None:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM finding WHERE id = ?", (finding_id,)
        ).fetchall()
    return dict(rows[0]) if rows else None


def update_finding(finding_id: str, **kwargs) -> None:
    kwargs["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [finding_id]
    with _db() as conn:
        conn.execute(f"UPDATE finding SET {sets} WHERE id = ?", vals)


def get_open_findings(story_key: str, min_severity: str = "medium") -> list[dict]:
    min_level = SEVERITY_ORDER.get(min_severity, 2)
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM finding WHERE story_key = ? AND status = 'open'",
            (story_key,),
        ).fetchall()
    return [dict(r) for r in rows if SEVERITY_ORDER.get(r["severity"], 0) >= min_level]


def get_findings_by_status(statuses: list[str]) -> list[dict]:
    """Get findings matching any of the given statuses."""
    placeholders = ",".join("?" * len(statuses))
    with _db() as conn:
        rows = conn.execute(
            f"SELECT * FROM finding WHERE status IN ({placeholders}) ORDER BY created_at DESC",
            statuses,
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_pending_findings() -> list[dict]:
    """Get all open + accepted findings across stories (for approval queue)."""
    return get_findings_by_status(["open", "accepted"])


def get_findings_by_story(story_key: str) -> list[dict]:
    """Get all findings for a story regardless of status."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM finding WHERE story_key = ? ORDER BY created_at",
            (story_key,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_finding_evidence(finding_id: str) -> list[str]:
    """Get evidence list for a finding from the evidence column."""
    with _db() as conn:
        row = conn.execute(
            "SELECT evidence FROM finding WHERE id = ?",
            (finding_id,),
        ).fetchone()
    if row and row["evidence"]:
        try:
            return json.loads(row["evidence"])
        except (json.JSONDecodeError, TypeError):
            pass
    return []


def enrich_findings_with_evidence(findings: list[dict]) -> list[dict]:
    """Attach evidence from the evidence column to each finding."""
    for f in findings:
        raw = f.get("evidence")
        if isinstance(raw, str):
            try:
                f["evidence"] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                f["evidence"] = []
        elif raw is None:
            f["evidence"] = []
    return findings


SEVERITY_ORDER = {"high": 3, "medium": 2, "low": 1}
