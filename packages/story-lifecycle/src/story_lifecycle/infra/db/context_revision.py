"""context_revision — context 版本号（设计15 阶段B 拆分自 models.py）。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .connection import _db
def get_context_revision(story_key: str) -> int:
    """Return the current context_revision for a story, or 0 if not found."""
    with _db() as conn:
        row = conn.execute(
            "SELECT context_revision FROM story WHERE story_key = ?", (story_key,)
        ).fetchone()
    return row["context_revision"] if row else 0


def bump_context_revision(story_key: str) -> int:
    """Increment context_revision by 1 and return the new value."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _db() as conn:
        conn.execute(
            "UPDATE story SET context_revision = context_revision + 1, updated_at = ? "
            "WHERE story_key = ?",
            (now, story_key),
        )
        row = conn.execute(
            "SELECT context_revision FROM story WHERE story_key = ?", (story_key,)
        ).fetchone()
    return row["context_revision"] if row else 0


