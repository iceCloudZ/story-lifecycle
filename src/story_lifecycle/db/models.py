"""SQLite data models — single-file, zero-ORM."""

import sqlite3
import json
from pathlib import Path
from datetime import datetime, timezone


def get_db_path() -> Path:
    import os

    home = os.environ.get("STORY_HOME", str(Path.home() / ".story-lifecycle"))
    Path(home).mkdir(parents=True, exist_ok=True)
    return Path(home) / "story.db"


def get_conn() -> sqlite3.Connection:
    db = get_db_path()
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create tables if not exist. Idempotent — safe to call on every startup."""
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS story (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            story_key TEXT NOT NULL UNIQUE,
            title TEXT,
            workspace TEXT NOT NULL,
            profile TEXT NOT NULL DEFAULT 'minimal',
            current_stage TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            complexity TEXT,
            context_json TEXT DEFAULT '{}',
            execution_count INTEGER DEFAULT 0,
            last_error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS stage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            story_id INTEGER REFERENCES story(id),
            stage TEXT NOT NULL,
            action TEXT NOT NULL,
            detail TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS gate_result (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            story_id INTEGER REFERENCES story(id),
            stage TEXT NOT NULL,
            gate_name TEXT NOT NULL,
            result TEXT NOT NULL,
            detail TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS event_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            story_key TEXT NOT NULL,
            stage TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    conn.close()


# -------- CRUD helpers --------


def create_story(
    story_key: str,
    title: str,
    workspace: str,
    profile: str = "minimal",
    current_stage: str = "design",
) -> dict:
    conn = get_conn()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """INSERT INTO story (story_key, title, workspace, profile, current_stage, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 'active', ?, ?)""",
        (story_key, title, str(workspace), profile, current_stage, now, now),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM story WHERE story_key = ?", (story_key,)
    ).fetchone()
    conn.close()
    return dict(row) if row else {}


def get_story(story_key: str) -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM story WHERE story_key = ?", (story_key,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def list_active_stories() -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM story WHERE status IN ('active', 'paused', 'blocked')
           ORDER BY updated_at DESC"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_story(story_key: str, **kwargs):
    """Update story fields. Always bumps updated_at."""
    if not kwargs:
        return
    conn = get_conn()
    kwargs["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [story_key]
    conn.execute(f"UPDATE story SET {sets} WHERE story_key = ?", values)
    conn.commit()
    conn.close()


def update_context(story_key: str, field: str, value: str):
    """Merge a single field into context_json."""
    conn = get_conn()
    row = conn.execute(
        "SELECT context_json FROM story WHERE story_key = ?", (story_key,)
    ).fetchone()
    if not row:
        conn.close()
        return
    ctx = json.loads(row["context_json"] or "{}")
    ctx[field] = value
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE story SET context_json = ?, updated_at = ? WHERE story_key = ?",
        (json.dumps(ctx, ensure_ascii=False), now, story_key),
    )
    conn.commit()
    conn.close()


def log_stage(story_key: str, stage: str, action: str, detail: str = ""):
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM story WHERE story_key = ?", (story_key,)
    ).fetchone()
    if not row:
        conn.close()
        return
    conn.execute(
        "INSERT INTO stage_log (story_id, stage, action, detail) VALUES (?, ?, ?, ?)",
        (row["id"], stage, action, detail),
    )
    conn.commit()
    conn.close()


def delete_story(story_key: str):
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM story WHERE story_key = ?", (story_key,)
    ).fetchone()
    if row:
        conn.execute("DELETE FROM stage_log WHERE story_id = ?", (row["id"],))
        conn.execute("DELETE FROM gate_result WHERE story_id = ?", (row["id"],))
        conn.execute("DELETE FROM story WHERE id = ?", (row["id"],))
    conn.commit()
    conn.close()


# -------- Event log --------


def log_event(
    story_key: str, stage: str, event_type: str, payload: dict | None = None
):
    """Record an event to event_log. Structured replacement for log_stage."""
    conn = get_conn()
    conn.execute(
        "INSERT INTO event_log (story_key, stage, event_type, payload) VALUES (?, ?, ?, ?)",
        (story_key, stage, event_type, json.dumps(payload or {}, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()


def get_story_events(story_key: str) -> list[dict]:
    """Return all events for a story, ordered by id."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM event_log WHERE story_key = ? ORDER BY id",
        (story_key,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def upsert_story(
    story_key: str,
    title: str = "",
    workspace: str = "",
    profile: str = "minimal",
    current_stage: str = "design",
    status: str = "active",
    **kwargs,
):
    """Insert or update a story. Used by service layer."""
    conn = get_conn()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    existing = conn.execute(
        "SELECT id FROM story WHERE story_key = ?", (story_key,)
    ).fetchone()
    if existing:
        kwargs["updated_at"] = now
        if title:
            kwargs["title"] = title
        if status:
            kwargs["status"] = status
        if current_stage:
            kwargs["current_stage"] = current_stage
        if kwargs:
            sets = ", ".join(f"{k} = ?" for k in kwargs)
            values = list(kwargs.values()) + [story_key]
            conn.execute(f"UPDATE story SET {sets} WHERE story_key = ?", values)
    else:
        conn.execute(
            """INSERT INTO story (story_key, title, workspace, profile, current_stage, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                story_key,
                title,
                str(workspace),
                profile,
                current_stage,
                status,
                now,
                now,
            ),
        )
    conn.commit()
    conn.close()
