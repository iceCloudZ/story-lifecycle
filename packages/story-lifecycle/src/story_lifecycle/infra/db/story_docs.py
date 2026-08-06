"""story_docs — 版本化业务文档（设计15 阶段B 拆分自 models.py）。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .connection import _db
def upsert_story_doc(
    story_key: str,
    doc_type: str,
    content: str,
    change_reason: str,
    author: str = "user",
    title: str = "",
) -> int:
    """Save a new version of a doc. Returns the new version number.

    Writes the version row, updates story_doc.latest_content/current_version,
    and refreshes the FTS5 index (delete+reinsert so search sees latest).
    Idempotent across story/doc_type — each call is a new version.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _db() as conn:
        row = conn.execute(
            "SELECT current_version, title FROM story_doc WHERE story_key=? AND doc_type=?",
            (story_key, doc_type),
        ).fetchone()
        if row:
            next_v = int(row["current_version"]) + 1
            # preserve existing title if caller didn't supply one
            if not title:
                title = row["title"] or ""
        else:
            next_v = 1
        conn.execute(
            """INSERT INTO story_doc_version
               (story_key, doc_type, version, content, change_reason, author, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (story_key, doc_type, next_v, content, change_reason, author, now),
        )
        conn.execute(
            """INSERT INTO story_doc
               (story_key, doc_type, title, current_version, latest_content, local_path, updated_by, updated_at)
               VALUES (?, ?, ?, ?, ?, '', ?, ?)
               ON CONFLICT(story_key, doc_type) DO UPDATE SET
                 title = excluded.title,
                 current_version = excluded.current_version,
                 latest_content = excluded.latest_content,
                 updated_by = excluded.updated_by,
                 updated_at = excluded.updated_at""",
            (story_key, doc_type, title, next_v, content, author, now),
        )
        # FTS5: refresh latest-content index (delete old + insert new for this doc)
        conn.execute(
            "DELETE FROM story_doc_fts WHERE story_key=? AND doc_type=?",
            (story_key, doc_type),
        )
        conn.execute(
            "INSERT INTO story_doc_fts (story_key, doc_type, title, content) VALUES (?, ?, ?, ?)",
            (story_key, doc_type, title, content),
        )
    return next_v


def set_story_doc_local_path(story_key: str, doc_type: str, local_path: str) -> None:
    """Record where the local-cache .md lives (set by the API layer after sync)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _db() as conn:
        conn.execute(
            "UPDATE story_doc SET local_path=?, updated_at=? WHERE story_key=? AND doc_type=?",
            (local_path, now, story_key, doc_type),
        )


def get_story_doc(story_key: str, doc_type: str) -> dict | None:
    """Latest version of a doc: content + version + title + confirmed + updated_at."""
    with _db() as conn:
        row = conn.execute(
            "SELECT story_key, doc_type, title, current_version, latest_content, "
            "local_path, updated_by, updated_at, confirmed_by, confirmed_at "
            "FROM story_doc WHERE story_key=? AND doc_type=?",
            (story_key, doc_type),
        ).fetchone()
    return dict(row) if row else None


def confirm_story_doc(
    story_key: str, doc_type: str, confirmed_by: str = "user"
) -> bool:
    """Mark a doc as manually confirmed (人工确认)。只有 user 能调(AI 不能自我确认)。

    Returns True if a row was updated, False if doc doesn't exist.
    """
    with _db() as conn:
        cur = conn.execute(
            "UPDATE story_doc SET confirmed_by=?, confirmed_at=CURRENT_TIMESTAMP "
            "WHERE story_key=? AND doc_type=?",
            (confirmed_by, story_key, doc_type),
        )
    return cur.rowcount > 0


def get_story_doc_version(story_key: str, doc_type: str, version: int) -> dict | None:
    """Read a specific historical version (full content)."""
    with _db() as conn:
        row = conn.execute(
            "SELECT story_key, doc_type, version, content, change_reason, author, created_at "
            "FROM story_doc_version WHERE story_key=? AND doc_type=? AND version=?",
            (story_key, doc_type, version),
        ).fetchone()
    return dict(row) if row else None


def list_story_doc_versions(story_key: str, doc_type: str) -> list[dict]:
    """Version list (no full content) — newest first."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT story_key, doc_type, version, change_reason, author, created_at "
            "FROM story_doc_version WHERE story_key=? AND doc_type=? "
            "ORDER BY version DESC",
            (story_key, doc_type),
        ).fetchall()
    return [dict(r) for r in rows]


def list_story_docs(story_key: str) -> list[dict]:
    """All doc_types for a story (no full content) — for the docs tab list."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT story_key, doc_type, title, current_version, updated_by, updated_at, "
            "local_path, confirmed_by, confirmed_at "
            "FROM story_doc WHERE story_key=? ORDER BY doc_type",
            (story_key,),
        ).fetchall()
    return [dict(r) for r in rows]


def rollback_story_doc(
    story_key: str, doc_type: str, version: int, reason: str, author: str = "user"
) -> int:
    """Roll back by writing the content of `version` as a NEW version.
    History is preserved (the old versions stay). Returns the new version number.
    """
    old = get_story_doc_version(story_key, doc_type, version)
    if not old:
        raise ValueError(
            f"cannot rollback: version {version} of {doc_type} not found for {story_key}"
        )
    return upsert_story_doc(
        story_key,
        doc_type,
        old["content"],
        change_reason=reason or f"回滚到 v{version}",
        author=author,
    )


def search_docs(
    query: str,
    *,
    doc_type: str | None = None,
    story_key: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """FTS5 full-text search across all docs' latest content. Returns ranked hits
    with a snippet of the match. Query is FTS5 syntax; bare words are fine for
    CJK (unicode61 tokenizer)."""
    # escape double quotes in the query for the MATCH phrase
    q = '"' + query.replace('"', '""') + '"'
    sql = (
        "SELECT f.story_key, f.doc_type, f.title, "
        "snippet(story_doc_fts, 3, '[', ']', '...', 24) AS snippet, "
        "rank FROM story_doc_fts f WHERE story_doc_fts MATCH ?"
    )
    args: list = [q]
    if doc_type:
        sql += " AND f.doc_type = ?"
        args.append(doc_type)
    if story_key:
        sql += " AND f.story_key = ?"
        args.append(story_key)
    sql += " ORDER BY rank LIMIT ?"
    args.append(limit)
    with _db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


