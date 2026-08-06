"""change_items — DDL/配置变更（设计15 阶段B 拆分自 models.py）。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .connection import _db
def create_change_item(
    story_key: str,
    kind: str,
    project_id: int | None = None,
    ref: str = "",
    summary: str = "",
    lifecycle_state: str = "proposed",
    verification_state: str = "unverified",
    environment: str = "",
    source: str = "ai",
    evidence_ref: str = "",
) -> dict:
    """Create a change item (DDL / Nacos). Returns the created row."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _db() as conn:
        conn.execute(
            """INSERT INTO story_change_item
               (story_key, project_id, kind, ref, summary, lifecycle_state,
                verification_state, environment, source, evidence_ref,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                story_key,
                project_id,
                kind,
                ref,
                summary,
                lifecycle_state,
                verification_state,
                environment,
                source,
                evidence_ref,
                now,
                now,
            ),
        )
        row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        row = conn.execute(
            "SELECT * FROM story_change_item WHERE id = ?", (row_id,)
        ).fetchone()
    return dict(row) if row else {}


def get_change_item(item_id: int) -> dict | None:
    """Get a single change item by id."""
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM story_change_item WHERE id = ?", (item_id,)
        ).fetchone()
    return dict(row) if row else None


def get_story_change_items(story_key: str) -> list[dict]:
    """Get all change items for a story."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM story_change_item WHERE story_key = ? ORDER BY id",
            (story_key,),
        ).fetchall()
    return [dict(r) for r in rows]


def update_change_item(item_id: int, **kwargs) -> None:
    """Update a change item. Always bumps updated_at."""
    if not kwargs:
        return
    valid = {
        "story_key",
        "project_id",
        "kind",
        "ref",
        "summary",
        "lifecycle_state",
        "verification_state",
        "environment",
        "source",
        "evidence_ref",
    }
    invalid = set(kwargs.keys()) - valid
    if invalid:
        raise ValueError(f"Invalid story_change_item columns: {invalid}")
    kwargs["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [item_id]
    with _db() as conn:
        conn.execute(f"UPDATE story_change_item SET {sets} WHERE id = ?", values)


