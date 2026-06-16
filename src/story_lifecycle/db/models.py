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
        "deadline",
        "priority",
        "owner",
        "branches_json",
        "tapd_status",
        "tapd_url",
        "tapd_type",
        "intake_state",
        "context_revision",
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
                evidence TEXT,
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
        # Migration: add evidence column to existing finding table
        try:
            conn.execute("ALTER TABLE finding ADD COLUMN evidence TEXT DEFAULT '[]'")
        except Exception:
            pass  # column already exists
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
        for col, default in [
            ("deadline", "TEXT"),
            ("priority", "TEXT"),
            ("owner", "TEXT"),
            ("branches_json", "TEXT DEFAULT '[]'"),
            ("tapd_status", "TEXT"),
            ("tapd_url", "TEXT"),
            ("tapd_type", "TEXT DEFAULT 'story'"),
        ]:
            try:
                conn.execute(f"ALTER TABLE story ADD COLUMN {col} {default}")
            except sqlite3.OperationalError:
                pass
        # Story context & TAPD lifecycle columns
        try:
            conn.execute(
                "ALTER TABLE story ADD COLUMN intake_state TEXT DEFAULT 'ready'"
            )
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute(
                "ALTER TABLE story ADD COLUMN context_revision INTEGER DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass

        # -------- Story Context & TAPD Lifecycle tables --------

        # 1. project — a git repository that stories are implemented in
        conn.execute("""
            CREATE TABLE IF NOT EXISTS project (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                repo_path TEXT NOT NULL UNIQUE,
                default_branch TEXT DEFAULT 'main',
                remote_url TEXT,
                availability TEXT NOT NULL DEFAULT 'unknown',
                availability_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_project_name ON project(name)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_project_availability ON project(availability)"
        )

        # 2. story_project — n:m binding of story to project with workspace details
        conn.execute("""
            CREATE TABLE IF NOT EXISTS story_project (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                story_key TEXT NOT NULL,
                project_id INTEGER NOT NULL,
                branch TEXT,
                base_branch TEXT DEFAULT 'main',
                base_commit TEXT,
                worktree_path TEXT UNIQUE,
                workspace_type TEXT,
                worktree_state TEXT NOT NULL DEFAULT 'unprepared',
                summary TEXT,
                source TEXT NOT NULL DEFAULT 'user',
                evidence_ref TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (story_key) REFERENCES story(story_key) ON DELETE CASCADE,
                FOREIGN KEY (project_id) REFERENCES project(id) ON DELETE CASCADE,
                UNIQUE(story_key, project_id)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sp_story ON story_project(story_key)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sp_project ON story_project(project_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sp_state ON story_project(worktree_state)"
        )

        # 3. project_runtime_fact — detected runtime environment facts
        conn.execute("""
            CREATE TABLE IF NOT EXISTS project_runtime_fact (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                runtime_type TEXT NOT NULL DEFAULT 'unknown',
                runtime_version TEXT,
                dependency_ref TEXT,
                check_command TEXT,
                availability TEXT NOT NULL DEFAULT 'unknown',
                evidence_ref TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES project(id) ON DELETE CASCADE
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_prf_project ON project_runtime_fact(project_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_prf_runtime ON project_runtime_fact(runtime_type)"
        )

        # 4. story_document — PRD / design docs associated with a story
        conn.execute("""
            CREATE TABLE IF NOT EXISTS story_document (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                story_key TEXT NOT NULL,
                project_id INTEGER,
                kind TEXT NOT NULL,
                ref TEXT,
                summary TEXT,
                source TEXT NOT NULL DEFAULT 'ai',
                evidence_ref TEXT,
                verification_state TEXT NOT NULL DEFAULT 'unverified',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (story_key) REFERENCES story(story_key) ON DELETE CASCADE,
                FOREIGN KEY (project_id) REFERENCES project(id) ON DELETE SET NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sd_story ON story_document(story_key)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sd_kind ON story_document(kind)")

        # 5. story_change_item — DDL / Nacos configuration changes
        conn.execute("""
            CREATE TABLE IF NOT EXISTS story_change_item (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                story_key TEXT NOT NULL,
                project_id INTEGER,
                kind TEXT NOT NULL,
                ref TEXT,
                summary TEXT,
                lifecycle_state TEXT NOT NULL DEFAULT 'proposed',
                verification_state TEXT NOT NULL DEFAULT 'unverified',
                environment TEXT,
                source TEXT NOT NULL DEFAULT 'ai',
                evidence_ref TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (story_key) REFERENCES story(story_key) ON DELETE CASCADE,
                FOREIGN KEY (project_id) REFERENCES project(id) ON DELETE SET NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sci_story ON story_change_item(story_key)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sci_lifecycle ON story_change_item(lifecycle_state)"
        )

        # 6. story_delivery_artifact — MR/PR and merge evidence
        conn.execute("""
            CREATE TABLE IF NOT EXISTS story_delivery_artifact (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                story_key TEXT NOT NULL,
                project_id INTEGER,
                kind TEXT NOT NULL,
                provider TEXT,
                external_id TEXT,
                url TEXT,
                source_branch TEXT,
                target_branch TEXT,
                delivery_state TEXT NOT NULL DEFAULT 'not_started',
                review_state TEXT NOT NULL DEFAULT 'not_reviewed',
                merge_commit TEXT,
                review_summary TEXT,
                source TEXT NOT NULL DEFAULT 'ai',
                evidence_ref TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (story_key) REFERENCES story(story_key) ON DELETE CASCADE,
                FOREIGN KEY (project_id) REFERENCES project(id) ON DELETE SET NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sda_story ON story_delivery_artifact(story_key)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sda_delivery ON story_delivery_artifact(delivery_state)"
        )


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
            """INSERT INTO story (story_key, title, workspace, profile, current_stage, status, created_at, updated_at, parent_key, subtask_index, intake_state)
               VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, 'ready')""",
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
            """SELECT * FROM story WHERE status IN ('active', 'paused', 'blocked', 'waiting_subtasks', 'planning')
               AND intake_state = 'ready'
               ORDER BY updated_at DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


def list_candidate_stories() -> list[dict]:
    """Return candidate stories that need project binding before activation."""
    with _db() as conn:
        rows = conn.execute(
            """SELECT * FROM story WHERE intake_state = 'candidate'
               ORDER BY updated_at DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


def list_completed_stories(limit: int = 20) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM story WHERE status IN ('completed', 'failed', 'aborted') ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


COMPLETED_STATES = frozenset({"resolved", "rejected", "closed"})
"""TAPD lifecycle states treated as 'done' and hidden from list views by default."""


def list_visible_stories(
    show_all: bool = False,
    status: str = "",
    item_type: str = "",
    show_completed: bool = False,
    overdue: bool = False,
) -> list[dict]:
    """Gather + filter stories for list views.

    Shared by the REST ``/api/story`` endpoint and the ``story list`` CLI so the
    two can't drift apart — the CLI previously omitted candidate stories that the
    API included, and COMPLETED_STATES was hardcoded in both places.

    show_all: include completed/failed/aborted stories.
    status: filter by lifecycle status (active/paused/blocked/planning/...).
    item_type: filter by tapd_type (story/bug/subtask).
    show_completed: keep resolved/rejected/closed TAPD stories (hidden by default).
    overdue: only stories past their deadline.
    """
    stories = list_active_stories() + list_candidate_stories()
    if show_all:
        stories = stories + list_completed_stories(limit=100)

    if status:
        stories = [s for s in stories if s["status"] == status]
    if item_type:
        stories = [s for s in stories if s.get("tapd_type") == item_type]
    if not show_completed:
        stories = [s for s in stories if s.get("tapd_status") not in COMPLETED_STATES]
    if overdue:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        stories = [s for s in stories if s.get("deadline") and s["deadline"][:10] < now]
    return stories


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


def get_stage_logs(story_key: str, limit: int = 50) -> list[dict]:
    """Return recent stage_log rows for a story, newest first."""
    with _db() as conn:
        rows = conn.execute(
            """SELECT sl.* FROM stage_log sl
               JOIN story s ON s.id = sl.story_id
               WHERE s.story_key = ?
               ORDER BY sl.id DESC LIMIT ?""",
            (story_key, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_gate_results(story_key: str, limit: int = 20) -> list[dict]:
    """Return recent gate_result rows for a story, newest first."""
    with _db() as conn:
        rows = conn.execute(
            """SELECT gr.* FROM gate_result gr
               JOIN story s ON s.id = gr.story_id
               WHERE s.story_key = ?
               ORDER BY gr.id DESC LIMIT ?""",
            (story_key, limit),
        ).fetchall()
    return [dict(r) for r in rows]


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


def record_gate_result(
    story_key: str, stage: str, gate_name: str, result: str, detail: str = ""
):
    """Record a compact gate result in the existing gate_result table."""
    with _db() as conn:
        row = conn.execute(
            "SELECT id FROM story WHERE story_key = ?", (story_key,)
        ).fetchone()
        if not row:
            return
        conn.execute(
            "INSERT INTO gate_result (story_id, stage, gate_name, result, detail) "
            "VALUES (?, ?, ?, ?, ?)",
            (row["id"], stage, gate_name, result, detail),
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


def parse_event_payload(event: dict) -> dict:
    """Decode an event's payload (stored as JSON str or dict) into a dict.

    Centralized so failure semantics don't drift across the many endpoints
    that read event payloads (stats / loop-trace / timeline / gate-history).
    Returns {} on missing or unparseable payload, so callers can uniformly
    do dict operations — a failed parse simply yields no matches.
    """
    payload = event.get("payload")
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        try:
            decoded = json.loads(payload)
            return decoded if isinstance(decoded, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def is_adversarial_loop_event(event: dict) -> bool:
    """True if this event records one adversarial plan↔review / code↔review round."""
    payload = parse_event_payload(event)
    return bool(payload.get("adversarial_loop")) and event.get("event_type") in (
        "plan",
        "review",
    )


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
                """INSERT INTO story (story_key, title, workspace, profile, current_stage, status, intake_state, created_at, updated_at, parent_key, subtask_index)
                   VALUES (?, ?, ?, ?, ?, ?, 'ready', ?, ?, ?, ?)""",
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


def upsert_story_from_source(
    source_type: str,
    source_id: str,
    title: str = "",
    workspace: str = "",
    profile: str = "minimal",
    current_stage: str = "design",
    status: str = "idle",
    intake_state: str = "candidate",
    deadline: str = "",
    priority: str = "",
    owner: str = "",
    tapd_status: str = "",
    tapd_url: str = "",
    tapd_type: str = "story",
    parent_key: str = "",
) -> tuple[dict, bool]:
    """Insert or update a story from an external source.

    For new stories, defaults to intake_state="candidate" and status="idle"
    to reflect the intake lifecycle. Existing stories are updated in place.

    Returns (story_dict, was_created).
    """
    existing = find_by_source_id(source_type, source_id)
    if existing:
        updates = {}
        if title:
            updates["title"] = title
        if deadline:
            updates["deadline"] = deadline
        if priority:
            updates["priority"] = priority
        if owner:
            updates["owner"] = owner
        if tapd_status:
            updates["tapd_status"] = tapd_status
        if tapd_url:
            updates["tapd_url"] = tapd_url
        if tapd_type:
            updates["tapd_type"] = tapd_type
        if parent_key:
            updates["parent_key"] = parent_key
        # intake_state is a local lifecycle field, NOT TAPD-authoritative.
        # Never overwrite it on update — a user may have promoted the story to ready.
        if updates:
            update_story(existing["story_key"], **updates)
        return get_story(existing["story_key"]), False
    else:
        key = f"{source_type}-{source_id}"
        create_story(
            story_key=key,
            title=title,
            workspace=workspace or str(Path.cwd()),
            profile=profile,
            current_stage=current_stage,
        )
        update_story(
            key,
            source_type=source_type,
            source_id=source_id,
            status=status,
            intake_state=intake_state,
            deadline=deadline,
            priority=priority,
            owner=owner,
            tapd_status=tapd_status,
            tapd_url=tapd_url,
            tapd_type=tapd_type,
            parent_key=parent_key,
        )
        return get_story(key), True


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
    evidence=None,
) -> str:
    import uuid

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


SEVERITY_ORDER = {"high": 3, "medium": 2, "low": 1}
"""Severity ranking shared by findings filtering (db + api). Single source so
the /findings endpoint and get_open_findings can't drift apart again."""


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


# -------- Context revision helpers --------


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


# -------- Project CRUD --------


def create_project(
    name: str,
    repo_path: str,
    default_branch: str = "main",
    remote_url: str = "",
    availability: str = "unknown",
    availability_reason: str = "",
) -> dict:
    """Create a project record. Returns the created row as dict."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _db() as conn:
        conn.execute(
            """INSERT INTO project (name, repo_path, default_branch, remote_url,
               availability, availability_reason, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                name,
                repo_path,
                default_branch,
                remote_url,
                availability,
                availability_reason,
                now,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM project WHERE name = ?", (name,)).fetchone()
    return dict(row) if row else {}


def get_project(project_id: int) -> dict | None:
    """Get a single project by id."""
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM project WHERE id = ?", (project_id,)
        ).fetchone()
    return dict(row) if row else None


def get_project_by_name(name: str) -> dict | None:
    """Get a project by its unique name."""
    with _db() as conn:
        row = conn.execute("SELECT * FROM project WHERE name = ?", (name,)).fetchone()
    return dict(row) if row else None


def list_projects() -> list[dict]:
    """Return all projects ordered by name."""
    with _db() as conn:
        rows = conn.execute("SELECT * FROM project ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def update_project(project_id: int, **kwargs) -> None:
    """Update project fields. Always bumps updated_at."""
    if not kwargs:
        return
    valid = {
        "name",
        "repo_path",
        "default_branch",
        "remote_url",
        "availability",
        "availability_reason",
    }
    invalid = set(kwargs.keys()) - valid
    if invalid:
        raise ValueError(f"Invalid project columns: {invalid}")
    kwargs["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [project_id]
    with _db() as conn:
        conn.execute(f"UPDATE project SET {sets} WHERE id = ?", values)


def delete_project(project_id: int) -> None:
    """Delete a project and all related rows (CASCADE handles children)."""
    with _db() as conn:
        conn.execute("DELETE FROM project WHERE id = ?", (project_id,))


# -------- Story-Project binding CRUD --------


def bind_story_project(
    story_key: str,
    project_id: int,
    branch: str = "",
    base_branch: str = "main",
    base_commit: str = "",
    worktree_path: str = "",
    workspace_type: str = "",
    worktree_state: str = "unprepared",
    summary: str = "",
    source: str = "user",
    evidence_ref: str = "",
) -> dict:
    """Bind a story to a project. Returns the created row."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    # worktree_path 有 UNIQUE 约束，未创建 worktree 时用 branch 占位避免空串冲突
    if not worktree_path:
        worktree_path = f"_pending_{story_key}_{project_id}"
    with _db() as conn:
        conn.execute(
            """INSERT INTO story_project (story_key, project_id, branch, base_branch,
               base_commit, worktree_path, workspace_type, worktree_state,
               summary, source, evidence_ref, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                story_key,
                project_id,
                branch,
                base_branch,
                base_commit,
                worktree_path,
                workspace_type,
                worktree_state,
                summary,
                source,
                evidence_ref,
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM story_project WHERE story_key = ? AND project_id = ?",
            (story_key, project_id),
        ).fetchone()
    return dict(row) if row else {}


def get_story_project(story_key: str, project_id: int) -> dict | None:
    """Get a specific story-project binding."""
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM story_project WHERE story_key = ? AND project_id = ?",
            (story_key, project_id),
        ).fetchone()
    return dict(row) if row else None


def get_story_projects(story_key: str) -> list[dict]:
    """Get all project bindings for a story."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM story_project WHERE story_key = ? ORDER BY id",
            (story_key,),
        ).fetchall()
    return [dict(r) for r in rows]


def update_story_project(story_key: str, project_id: int, **kwargs) -> None:
    """Update a story-project binding. Always bumps updated_at."""
    if not kwargs:
        return
    valid = {
        "branch",
        "base_branch",
        "base_commit",
        "worktree_path",
        "workspace_type",
        "worktree_state",
        "summary",
        "source",
        "evidence_ref",
    }
    invalid = set(kwargs.keys()) - valid
    if invalid:
        raise ValueError(f"Invalid story_project columns: {invalid}")
    kwargs["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [story_key, project_id]
    with _db() as conn:
        conn.execute(
            f"UPDATE story_project SET {sets} WHERE story_key = ? AND project_id = ?",
            values,
        )


def unbind_story_project(story_key: str, project_id: int) -> None:
    """Remove a story-project binding."""
    with _db() as conn:
        conn.execute(
            "DELETE FROM story_project WHERE story_key = ? AND project_id = ?",
            (story_key, project_id),
        )


# -------- Project runtime fact CRUD --------


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


# -------- Story document CRUD --------


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
    """Create a story document (PRD / design). Returns the created row."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _db() as conn:
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
    kwargs["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [doc_id]
    with _db() as conn:
        conn.execute(f"UPDATE story_document SET {sets} WHERE id = ?", values)


def delete_document(doc_id: int) -> None:
    """Delete a document by id."""
    with _db() as conn:
        conn.execute("DELETE FROM story_document WHERE id = ?", (doc_id,))


# -------- Story change item CRUD --------


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


# -------- Story delivery artifact CRUD --------


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
