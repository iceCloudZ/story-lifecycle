"""learned_patterns — playbook 模式 CRUD（设计15 阶段B 拆分自 models.py）。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from .connection import _db


def create_learned_pattern(
    pattern, applies_to, rule, source_findings=None, confidence="medium"
) -> str:

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    pid = f"pattern-{uuid.uuid4().hex[:12]}"
    with _db() as conn:
        conn.execute(
            "INSERT INTO learned_pattern (id, pattern, applies_to, rule, source_findings, confidence, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                pid,
                pattern,
                json.dumps(applies_to),
                rule,
                json.dumps(source_findings or []),
                confidence,
                "proposed",
                now,
                now,
            ),
        )
    return pid


def get_learned_pattern(pattern_id: str) -> dict | None:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM learned_pattern WHERE id = ?", (pattern_id,)
        ).fetchall()
    if not rows:
        return None
    r = dict(rows[0])
    r["applies_to"] = json.loads(r["applies_to"])
    r["source_findings"] = json.loads(r["source_findings"])
    return r


def update_learned_pattern(pattern_id: str, **kwargs) -> None:
    for json_field in ("applies_to", "source_findings"):
        if json_field in kwargs and isinstance(kwargs[json_field], list):
            kwargs[json_field] = json.dumps(kwargs[json_field])
    kwargs["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [pattern_id]
    with _db() as conn:
        conn.execute(f"UPDATE learned_pattern SET {sets} WHERE id = ?", vals)


def get_active_learned_patterns(limit: int = 20) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM learned_pattern WHERE status = 'active' ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        d["applies_to"] = json.loads(d["applies_to"])
        d["source_findings"] = json.loads(d["source_findings"])
        results.append(d)
    return results


def get_proposed_learned_patterns(limit: int = 20) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM learned_pattern WHERE status = 'proposed' ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        d["applies_to"] = json.loads(d["applies_to"])
        d["source_findings"] = json.loads(d["source_findings"])
        results.append(d)
    return results


def find_relevant_patterns(tags: list[str], limit: int = 5) -> list[dict]:
    """Find active patterns whose applies_to overlaps with given tags."""
    active = get_active_learned_patterns()
    scored = []
    for p in active:
        applies = p.get("applies_to", [])
        overlap = len(set(applies) & set(tags))
        if overlap > 0:
            scored.append((overlap, p))
    scored.sort(key=lambda x: -x[0])
    return [p for _, p in scored[:limit]]
