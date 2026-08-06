"""deliveries — 交付物(MR/PR)CRUD（设计15 阶段B 拆分自 models.py）。"""

from __future__ import annotations

from datetime import datetime, timezone

from .connection import _db


def create_delivery_artifact(
    story_key: str,
    kind: str,
    project_id: int | None = None,
    provider: str = "",
    external_id: str = "",
    url: str = "",
    source_branch: str = "",
    target_branch: str = "",
    delivery_state: str = "not_started",
    review_state: str = "not_reviewed",
    merge_commit: str = "",
    review_summary: str = "",
    source: str = "ai",
    evidence_ref: str = "",
) -> dict:
    """Create a delivery artifact (MR/PR). Returns the created row."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _db() as conn:
        conn.execute(
            """INSERT INTO story_delivery_artifact
               (story_key, project_id, kind, provider, external_id, url,
                source_branch, target_branch, delivery_state, review_state,
                merge_commit, review_summary, source, evidence_ref,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                story_key,
                project_id,
                kind,
                provider,
                external_id,
                url,
                source_branch,
                target_branch,
                delivery_state,
                review_state,
                merge_commit,
                review_summary,
                source,
                evidence_ref,
                now,
                now,
            ),
        )
        row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        row = conn.execute(
            "SELECT * FROM story_delivery_artifact WHERE id = ?", (row_id,)
        ).fetchone()
    return dict(row) if row else {}


def get_delivery_artifact(artifact_id: int) -> dict | None:
    """Get a single delivery artifact by id."""
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM story_delivery_artifact WHERE id = ?", (artifact_id,)
        ).fetchone()
    return dict(row) if row else None


def get_story_delivery_artifacts(story_key: str) -> list[dict]:
    """Get all delivery artifacts for a story."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM story_delivery_artifact WHERE story_key = ? ORDER BY id",
            (story_key,),
        ).fetchall()
    return [dict(r) for r in rows]


def update_delivery_artifact(artifact_id: int, **kwargs) -> None:
    """Update a delivery artifact. Always bumps updated_at."""
    if not kwargs:
        return
    valid = {
        "story_key",
        "project_id",
        "kind",
        "provider",
        "external_id",
        "url",
        "source_branch",
        "target_branch",
        "delivery_state",
        "review_state",
        "merge_commit",
        "review_summary",
        "source",
        "evidence_ref",
    }
    invalid = set(kwargs.keys()) - valid
    if invalid:
        raise ValueError(f"Invalid story_delivery_artifact columns: {invalid}")
    kwargs["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [artifact_id]
    with _db() as conn:
        conn.execute(f"UPDATE story_delivery_artifact SET {sets} WHERE id = ?", values)
