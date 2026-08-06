"""runtime_facts — 运行时环境事实（设计15 阶段B 拆分自 models.py）。"""

from __future__ import annotations

from datetime import datetime, timezone

from .connection import _db


def upsert_runtime_facts(
    project_id: int,
    runtime_type: str,
    runtime_version: str = "",
    dependency_ref: str = "",
    check_command: str = "",
    availability: str = "unknown",
    evidence_ref: str = "",
) -> dict:
    """Insert or update runtime facts for a project.
    One row per (project_id, runtime_type) combination.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _db() as conn:
        existing = conn.execute(
            "SELECT id FROM project_runtime_fact WHERE project_id = ? AND runtime_type = ?",
            (project_id, runtime_type),
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE project_runtime_fact
                   SET runtime_version = ?, dependency_ref = ?, check_command = ?,
                       availability = ?, evidence_ref = ?, updated_at = ?
                   WHERE project_id = ? AND runtime_type = ?""",
                (
                    runtime_version,
                    dependency_ref,
                    check_command,
                    availability,
                    evidence_ref,
                    now,
                    project_id,
                    runtime_type,
                ),
            )
        else:
            conn.execute(
                """INSERT INTO project_runtime_fact
                   (project_id, runtime_type, runtime_version, dependency_ref,
                    check_command, availability, evidence_ref, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    project_id,
                    runtime_type,
                    runtime_version,
                    dependency_ref,
                    check_command,
                    availability,
                    evidence_ref,
                    now,
                ),
            )
        row = conn.execute(
            "SELECT * FROM project_runtime_fact WHERE project_id = ? AND runtime_type = ?",
            (project_id, runtime_type),
        ).fetchone()
    return dict(row) if row else {}


def get_runtime_facts(project_id: int) -> list[dict]:
    """Get all runtime facts for a project."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM project_runtime_fact WHERE project_id = ? ORDER BY id",
            (project_id,),
        ).fetchall()
    return [dict(r) for r in rows]
