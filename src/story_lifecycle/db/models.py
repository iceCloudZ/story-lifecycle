"""SQLite data models — single-file, zero-ORM."""

import sqlite3
import json
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone

VALID_COLUMNS = frozenset(
    {
        "title",
        "workspace",
        "profile",
        "current_stage",
        "status",
        "complexity",
        "context_json",
        "execution_count",
        "last_error",
        "updated_at",
        "parent_key",
        "subtask_index",
        "sub_type",
        "source_type",
        "source_id",
    }
)


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


@contextmanager
def _db():
    """Context manager that auto-commits and closes the DB connection."""
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create tables if not exist. Idempotent — safe to call on every startup."""
    with _db() as conn:
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

            CREATE TABLE IF NOT EXISTS llm_trace (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                story_key TEXT,
                stage TEXT,
                operation TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                total_tokens INTEGER,
                duration_ms INTEGER,
                success INTEGER NOT NULL DEFAULT 1,
                error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        # Finding table for quality flywheel
        conn.execute("""
            CREATE TABLE IF NOT EXISTS finding (
                id TEXT PRIMARY KEY,
                story_key TEXT NOT NULL,
                stage TEXT,
                source TEXT NOT NULL,
                severity TEXT NOT NULL,
                category TEXT NOT NULL,
                location TEXT,
                description TEXT NOT NULL,
                recommendation TEXT,
                root_cause TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                verification_event_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_finding_story_status ON finding(story_key, status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_finding_severity ON finding(severity, status)"
        )
        # Learned pattern table for quality flywheel
        conn.execute("""
            CREATE TABLE IF NOT EXISTS learned_pattern (
                id TEXT PRIMARY KEY,
                pattern TEXT NOT NULL,
                applies_to TEXT NOT NULL,
                rule TEXT NOT NULL,
                source_findings TEXT,
                confidence TEXT NOT NULL DEFAULT 'medium',
                status TEXT NOT NULL DEFAULT 'proposed',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pattern_status ON learned_pattern(status)"
        )
        # Idempotent column migration
        try:
            conn.execute("ALTER TABLE story ADD COLUMN parent_key TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE story ADD COLUMN subtask_index INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE story ADD COLUMN sub_type TEXT")
        except sqlite3.OperationalError:
            pass
        for col in ("source_type", "source_id"):
            try:
                conn.execute(f"ALTER TABLE story ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_story_source ON story(source_type, source_id)"
            )
        except sqlite3.OperationalError:
            pass


# -------- CRUD helpers --------


def _validate_columns(keys):
    invalid = set(keys) - VALID_COLUMNS
    if invalid:
        raise ValueError(f"Invalid story columns: {invalid}")


def create_story(
    story_key: str,
    title: str,
    workspace: str,
    profile: str = "minimal",
    current_stage: str = "design",
    parent_key: str | None = None,
    subtask_index: int = 0,
) -> dict:
    with _db() as conn:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            """INSERT INTO story (story_key, title, workspace, profile, current_stage, status, created_at, updated_at, parent_key, subtask_index)
               VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)""",
            (
                story_key,
                title,
                str(workspace),
                profile,
                current_stage,
                now,
                now,
                parent_key,
                subtask_index,
            ),
        )
        row = conn.execute(
            "SELECT * FROM story WHERE story_key = ?", (story_key,)
        ).fetchone()
    return dict(row) if row else {}


def get_story(story_key: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM story WHERE story_key = ?", (story_key,)
        ).fetchone()
    return dict(row) if row else None


def find_by_source_id(source_type: str, source_id: str) -> dict | None:
    """Find a story by its external source type and ID (e.g. tapd, 1001234)."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM story WHERE source_type = ? AND source_id = ?",
            (source_type, source_id),
        ).fetchall()
    return dict(rows[0]) if rows else None


def list_active_stories() -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            """SELECT * FROM story WHERE status IN ('active', 'paused', 'blocked', 'waiting_subtasks')
               ORDER BY updated_at DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


def get_sub_stories(parent_key: str) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM story WHERE parent_key = ? ORDER BY subtask_index",
            (parent_key,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_pending_parents() -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM story WHERE status = 'waiting_subtasks'"
        ).fetchall()
    return [dict(r) for r in rows]


def update_story(story_key: str, **kwargs):
    """Update story fields. Always bumps updated_at."""
    if not kwargs:
        return
    _validate_columns(kwargs.keys())
    kwargs["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [story_key]
    with _db() as conn:
        conn.execute(f"UPDATE story SET {sets} WHERE story_key = ?", values)


def update_context(story_key: str, field: str, value: str):
    """Merge a single field into context_json."""
    with _db() as conn:
        row = conn.execute(
            "SELECT context_json FROM story WHERE story_key = ?", (story_key,)
        ).fetchone()
        if not row:
            return
        ctx = json.loads(row["context_json"] or "{}")
        ctx[field] = value
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE story SET context_json = ?, updated_at = ? WHERE story_key = ?",
            (json.dumps(ctx, ensure_ascii=False), now, story_key),
        )


def log_stage(story_key: str, stage: str, action: str, detail: str = ""):
    with _db() as conn:
        row = conn.execute(
            "SELECT id FROM story WHERE story_key = ?", (story_key,)
        ).fetchone()
        if not row:
            return
        conn.execute(
            "INSERT INTO stage_log (story_id, stage, action, detail) VALUES (?, ?, ?, ?)",
            (row["id"], stage, action, detail),
        )


def delete_story(story_key: str):
    with _db() as conn:
        row = conn.execute(
            "SELECT id FROM story WHERE story_key = ?", (story_key,)
        ).fetchone()
        if row:
            conn.execute("DELETE FROM stage_log WHERE story_id = ?", (row["id"],))
            conn.execute("DELETE FROM gate_result WHERE story_id = ?", (row["id"],))
            conn.execute("DELETE FROM story WHERE id = ?", (row["id"],))


# -------- Event log --------


def log_event(story_key: str, stage: str, event_type: str, payload: dict | None = None):
    """Record an event to event_log. Structured replacement for log_stage."""
    with _db() as conn:
        conn.execute(
            "INSERT INTO event_log (story_key, stage, event_type, payload) VALUES (?, ?, ?, ?)",
            (
                story_key,
                stage,
                event_type,
                json.dumps(payload or {}, ensure_ascii=False),
            ),
        )


def log_llm_trace(
    *,
    story_key: str = "",
    stage: str = "",
    operation: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    duration_ms: int = 0,
    success: bool = True,
    error: str = "",
):
    """Record an LLM call trace with token usage."""
    with _db() as conn:
        conn.execute(
            """INSERT INTO llm_trace (story_key, stage, operation, model,
               prompt_tokens, completion_tokens, total_tokens,
               duration_ms, success, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                story_key,
                stage,
                operation,
                model,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                duration_ms,
                1 if success else 0,
                error,
            ),
        )


def get_story_events(story_key: str) -> list[dict]:
    """Return all events for a story, ordered by id."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM event_log WHERE story_key = ? ORDER BY id",
            (story_key,),
        ).fetchall()
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
    _validate_columns(kwargs.keys())
    with _db() as conn:
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
            parent_key = kwargs.pop("parent_key", None)
            subtask_index = kwargs.pop("subtask_index", 0)
            conn.execute(
                """INSERT INTO story (story_key, title, workspace, profile, current_stage, status, created_at, updated_at, parent_key, subtask_index)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    story_key,
                    title,
                    str(workspace),
                    profile,
                    current_stage,
                    status,
                    now,
                    now,
                    parent_key,
                    subtask_index,
                ),
            )


# -------- Finding helpers --------


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
) -> str:
    import uuid

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    fid = f"finding-{uuid.uuid4().hex[:12]}"
    with _db() as conn:
        conn.execute(
            "INSERT INTO finding (id, story_key, stage, source, severity, category, location, description, recommendation, root_cause, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
    severity_order = {"high": 3, "medium": 2, "low": 1}
    min_level = severity_order.get(min_severity, 2)
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM finding WHERE story_key = ? AND status = 'open'",
            (story_key,),
        ).fetchall()
    return [dict(r) for r in rows if severity_order.get(r["severity"], 0) >= min_level]


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


def get_recent_quality_events(
    story_key: str, event_types: list[str], limit: int = 50
) -> list[dict]:
    """Get recent events of specified types from event_log."""
    placeholders = ",".join("?" * len(event_types))
    with _db() as conn:
        rows = conn.execute(
            f"SELECT * FROM event_log WHERE story_key = ? AND event_type IN ({placeholders}) ORDER BY id DESC LIMIT ?",
            [story_key] + event_types + [limit],
        ).fetchall()
    return [dict(r) for r in rows]


# -------- Learned pattern helpers --------


def create_learned_pattern(
    pattern, applies_to, rule, source_findings=None, confidence="medium"
) -> str:
    import uuid

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
