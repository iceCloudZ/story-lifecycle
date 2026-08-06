"""documents — story_document CRUD（设计15 阶段B 拆分自 models.py）。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .connection import _db


def _normalize_doc_ref(ref: str) -> str:
    """Normalize a document ref so equivalent paths compare equal."""
    if not ref:
        return ref
    # Use POSIX separators; preserve http(s) refs untouched.
    if ref.startswith("http://") or ref.startswith("https://"):
        return ref
    return Path(ref).as_posix()


def create_document(
    story_key: str,
    kind: str,
    project_id: int | None = None,
    ref: str = "",
    summary: str = "",
    source: str = "ai",
    evidence_ref: str = "",
    verification_state: str = "unverified",
) -> dict:
    """Create a story document (PRD / design). Returns the created row.

    Idempotent: if the same (story_key, kind, ref) already exists, the existing
    row is returned and no insert happens.
    """
    ref = _normalize_doc_ref(ref)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _db() as conn:
        existing = conn.execute(
            "SELECT * FROM story_document WHERE story_key = ? AND kind = ? AND ref = ?",
            (story_key, kind, ref),
        ).fetchone()
        if existing:
            return dict(existing)

        conn.execute(
            """INSERT INTO story_document
               (story_key, project_id, kind, ref, summary, source,
                evidence_ref, verification_state, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                story_key,
                project_id,
                kind,
                ref,
                summary,
                source,
                evidence_ref,
                verification_state,
                now,
                now,
            ),
        )
        row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        row = conn.execute(
            "SELECT * FROM story_document WHERE id = ?", (row_id,)
        ).fetchone()
    return dict(row) if row else {}


def get_document(doc_id: int) -> dict | None:
    """Get a single document by id."""
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM story_document WHERE id = ?", (doc_id,)
        ).fetchone()
    return dict(row) if row else None


def get_story_documents(story_key: str) -> list[dict]:
    """Get all documents for a story."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM story_document WHERE story_key = ? ORDER BY id",
            (story_key,),
        ).fetchall()
    return [dict(r) for r in rows]


def update_document(doc_id: int, **kwargs) -> None:
    """Update a document. Always bumps updated_at."""
    if not kwargs:
        return
    valid = {
        "story_key",
        "project_id",
        "kind",
        "ref",
        "summary",
        "source",
        "evidence_ref",
        "verification_state",
    }
    invalid = set(kwargs.keys()) - valid
    if invalid:
        raise ValueError(f"Invalid story_document columns: {invalid}")
    if "ref" in kwargs:
        kwargs["ref"] = _normalize_doc_ref(kwargs["ref"])
    kwargs["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [doc_id]
    with _db() as conn:
        conn.execute(f"UPDATE story_document SET {sets} WHERE id = ?", values)


def delete_document(doc_id: int) -> None:
    """Delete a document by id."""
    with _db() as conn:
        conn.execute("DELETE FROM story_document WHERE id = ?", (doc_id,))
