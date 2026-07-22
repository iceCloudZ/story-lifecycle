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
        "driver_claim",
        "lifecycle_state",
        "release_train",  # 班车归属(v3.2/v3.3/后台快线/NULL)
        "is_test",  # 测试/demo story 标记(0=正常,1=测试),看板与列表默认过滤
    }
)

# Default pricing in CNY per 1M tokens. Override via STORY_TOKEN_PRICING_JSON env.
MODEL_PRICING_CNY: dict[str, dict[str, float]] = {
    "default": {"input": 5.0, "output": 5.0},
    "deepseek-v3": {"input": 2.0, "output": 8.0},
    "deepseek-chat": {"input": 1.0, "output": 5.0},
    "deepseek-reasoner": {"input": 4.0, "output": 16.0},
    "deepseek-v4-pro": {"input": 2.0, "output": 8.0},
    "kimi-k2.5": {"input": 10.0, "output": 30.0},
    "kimi-k2": {"input": 10.0, "output": 30.0},
    "kimi-for-coding": {"input": 10.0, "output": 30.0},
    "moonshot-v1-8k": {"input": 6.0, "output": 6.0},
    "moonshot-v1-32k": {"input": 12.0, "output": 12.0},
    "moonshot-v1-128k": {"input": 24.0, "output": 24.0},
    "qwen-max": {"input": 20.0, "output": 60.0},
    "qwen-plus": {"input": 8.0, "output": 20.0},
    "qwen-turbo": {"input": 2.0, "output": 6.0},
    "qwen-coder-plus": {"input": 8.0, "output": 20.0},
    "gpt-4o": {"input": 35.0, "output": 105.0},
    "gpt-4o-mini": {"input": 1.5, "output": 6.0},
    "claude-3-5-sonnet": {"input": 21.0, "output": 105.0},
    "claude-3-5-sonnet-20241022": {"input": 21.0, "output": 105.0},
}


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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_lt_story ON llm_trace(story_key)")
        _backfill_llm_trace_story_keys(conn)
        # llm_call: prompt/response/reasoning 正文明细，外键挂到 llm_trace(id)。
        # 主表 llm_trace 保持轻（只指标），审计时 JOIN 本表取正文。ON DELETE CASCADE
        # 生效（get_conn() 每连接开 PRAGMA foreign_keys=ON）。
        conn.execute("""
            CREATE TABLE IF NOT EXISTS llm_call (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id INTEGER NOT NULL REFERENCES llm_trace(id) ON DELETE CASCADE,
                prompt_text TEXT,
                response_text TEXT,
                reasoning_text TEXT,
                tool_calls_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_lc_trace ON llm_call(trace_id)")
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
        # driver_claim: cross-process driver mutual-exclusion token (optimistic
        # CAS). NULL = free; non-NULL = held by a driver (token = pid:epoch:ts).
        # See graph.start_story_async / claim_story_driver. Idempotent migration.
        try:
            conn.execute("ALTER TABLE story ADD COLUMN driver_claim TEXT")
        except sqlite3.OperationalError:
            pass
        # STORY-STATE-MODEL: lifecycle_state = Story 业务状态(待启动/开发/测试/上线/结项),
        # 独立第一公民,不从阶段派生(区别于引擎 status)。新 story 初值「待启动」—
        # 确认规划(/plan/confirm)后才进「开发」。幂等迁移:老库已建的列不变(老数据
        # 逐条人工确认),仅新库/新行取 DEFAULT '待启动'。
        try:
            conn.execute(
                "ALTER TABLE story ADD COLUMN lifecycle_state TEXT DEFAULT '待启动'"
            )
        except sqlite3.OperationalError:
            pass
        # 班车看板:release_train = Story 归属班车(v3.2/v3.3/后台快线/...),人手动拖。
        # 字符串字段,不建表;NULL 表示待分配。同步时不覆盖(跟 intake_state 同理)。
        try:
            conn.execute("ALTER TABLE story ADD COLUMN release_train TEXT")
        except sqlite3.OperationalError:
            pass
        # is_test:测试/demo story 标记(0=正常,1=测试)。看板与列表默认过滤 is_test=0,
        # 避免本地跑测试/seed 造的数据污染真实看板。同步默认 0(真实数据)。
        try:
            conn.execute("ALTER TABLE story ADD COLUMN is_test INTEGER DEFAULT 0")
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

        # Migration: normalize path separators, dedupe, then enforce uniqueness.
        conn.execute(
            "UPDATE story_document SET ref = REPLACE(ref, '\\', '/') WHERE ref LIKE '%\\%'"
        )
        conn.execute("""
            DELETE FROM story_document
            WHERE id NOT IN (
                SELECT MIN(id)
                FROM story_document
                GROUP BY story_key, kind, ref
            )
        """)
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_sd_story_kind_ref"
            " ON story_document(story_key, kind, ref)"
        )

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

        # Migration: legacy _pending_<story>_<proj> placeholders → NULL.
        # SQLite UNIQUE 豁免 NULL,未建 worktree 的绑定不再需要占位字符串。
        # 幂等:首次执行后无匹配行。覆盖诊断文档里手动绕过留下的 _pending_ 行。
        conn.execute(
            "UPDATE story_project SET worktree_path = NULL, "
            "updated_at = strftime('%Y-%m-%d %H:%M:%S', 'now') "
            "WHERE worktree_path LIKE '_pending_%'"
        )

        # 5. story_doc / story_doc_version / story_doc_fts
        # Versioned business docs (PRD/spec/plan/research/test_report/...). DB is
        # the single source of truth (full content + history + change reason);
        # a local .md file mirrors the latest version as a read-only cache so
        # code agents read files (not DB) and execution doesn't depend on DB.
        # doc_type is an open string (no whitelist) — custom types allowed.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS story_doc (
                story_key       TEXT NOT NULL,
                doc_type        TEXT NOT NULL,
                title           TEXT NOT NULL DEFAULT '',
                current_version INTEGER NOT NULL DEFAULT 1,
                latest_content  TEXT NOT NULL DEFAULT '',
                local_path      TEXT NOT NULL DEFAULT '',
                updated_by      TEXT NOT NULL DEFAULT '',
                updated_at      TEXT NOT NULL,
                PRIMARY KEY (story_key, doc_type),
                FOREIGN KEY (story_key) REFERENCES story(story_key) ON DELETE CASCADE
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sd2_story ON story_doc(story_key)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS story_doc_version (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                story_key       TEXT NOT NULL,
                doc_type        TEXT NOT NULL,
                version         INTEGER NOT NULL,
                content         TEXT NOT NULL,
                change_reason   TEXT NOT NULL DEFAULT '',
                author          TEXT NOT NULL DEFAULT '',
                created_at      TEXT NOT NULL,
                UNIQUE (story_key, doc_type, version),
                FOREIGN KEY (story_key) REFERENCES story(story_key) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sdv_story_doc ON story_doc_version(story_key, doc_type, version)"
        )
        # FTS5 full-text index over the latest version of every doc (rebuilt from
        # story_doc on each upsert). unicode61 tokenizer handles CJK adequately.
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS story_doc_fts USING fts5(
                story_key UNINDEXED,
                doc_type  UNINDEXED,
                title,
                content,
                tokenize = 'unicode61'
            )
            """
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
            "SELECT * FROM story WHERE status IN ('completed', 'failed', 'aborted', 'archived') ORDER BY updated_at DESC LIMIT ?",
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
    show_test: bool = False,
) -> list[dict]:
    """Gather + filter stories for list views.

    Shared by the REST ``/api/story`` endpoint and the ``story list`` CLI so the
    two can't drift apart — the CLI previously omitted candidate stories that the
    API included, and COMPLETED_STATES was hardcoded in both places.

    show_all: include failed/aborted/archived stories (completed shows by default).
    status: filter by lifecycle status (active/paused/blocked/planning/...).
    item_type: filter by tapd_type (story/bug/subtask).
    show_completed: keep resolved/rejected/closed TAPD stories (hidden by default).
    overdue: only stories past their deadline.
    show_test: keep is_test=1 stories (hidden by default to keep worklist clean).
    """
    stories = list_active_stories() + list_candidate_stories()
    # completed (successfully finished) shows by default so done work isn't buried;
    # failed/aborted/archived only appear with show_all to keep the worklist focused.
    completed_pool = list_completed_stories(limit=100)
    stories = stories + [s for s in completed_pool if s.get("status") == "completed"]
    if show_all:
        stories = stories + [
            s for s in completed_pool if s.get("status") != "completed"
        ]

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
    if not show_test:
        # is_test:0/1/NULL 都按「非测试」处理(not None / not 0 = keep)。
        # 老行迁移后 DEFAULT 0,新建真实数据 0,仅测试/demo 造的置 1。
        stories = [s for s in stories if not s.get("is_test")]
    return stories


def get_sub_stories(parent_key: str) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM story WHERE parent_key = ? ORDER BY subtask_index",
            (parent_key,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_stories_by_parent(parent_key: str, item_type: str = "") -> list[dict]:
    """List stories linked to a parent, optionally filtered by tapd_type."""
    with _db() as conn:
        if item_type:
            rows = conn.execute(
                "SELECT * FROM story WHERE parent_key = ? AND tapd_type = ? ORDER BY updated_at DESC",
                (parent_key, item_type),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM story WHERE parent_key = ? ORDER BY updated_at DESC",
                (parent_key,),
            ).fetchall()
    return [dict(r) for r in rows]


def list_unlinked_bugs() -> list[dict]:
    """List bugs that are not linked to any story."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM story WHERE tapd_type = 'bug' AND (parent_key IS NULL OR parent_key = '') ORDER BY updated_at DESC LIMIT 200",
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


def claim_story_driver(story_key: str, token: str) -> bool:
    """Optimistic CAS claim for cross-process driver mutual exclusion.

    ``NULL`` driver_claim = free; the first caller's conditional UPDATE wins and
    returns True; concurrent callers (other processes) see rowcount 0 and return
    False. SQLite serializes the UPDATE so exactly one caller wins. The in-process
    ``_running_stories`` dict can't see other processes (each python process has
    its own), so without this a second driver double-drives the same story
    (real-run 2026-07-06: event_log events appeared ×2).

    Dead-PID recovery: if the existing claim's PID is no longer alive (process
    crashed / was emergency-stopped / machine rebooted), the claim is stale and
    a new caller is allowed to seize it. Without this, a crashed driver's claim
    locks the story forever (real-run 2026-07-20: emergency_stop killed driver
    but driver_claim stayed → next confirm failed CAS → story stuck).
    Token format ``<pid>:<epoch>`` (see graph.start_story_async).
    """
    with _db() as conn:
        # fast path: free claim
        cur = conn.execute(
            "UPDATE story SET driver_claim = ? WHERE story_key = ? "
            "AND driver_claim IS NULL",
            (token, story_key),
        )
        if cur.rowcount == 1:
            return True
        # slow path: existing claim — is its PID dead? If so, seize.
        row = conn.execute(
            "SELECT driver_claim FROM story WHERE story_key = ?",
            (story_key,),
        ).fetchone()
        existing = (row or [None])[0]
        if not existing:
            return False  # row vanished (story deleted between calls)
        if not _driver_pid_alive(existing):
            cur = conn.execute(
                "UPDATE story SET driver_claim = ? "
                "WHERE story_key = ? AND driver_claim = ?",
                (token, story_key, existing),
            )
            return cur.rowcount == 1
        return False


def _driver_pid_alive(token: str) -> bool:
    """Check if the PID encoded in a driver_claim token is still running.

    Token format: ``<pid>:<epoch>``. Returns False if the PID is gone, the
    token is malformed, or the check is unsupported on this platform.

    Platform notes:
      - POSIX: ``os.kill(pid, 0)`` raises ``ProcessLookupError`` when the PID
        doesn't exist (and ``PermissionError`` when it exists but is not ours
        — treat as alive).
      - Windows: ``os.kill`` with signal 0 throws ``OSError: WinError 87``
        regardless of liveness, so use ``OpenProcess`` via ctypes — succeeds
        for live PIDs, fails (returns NULL) for dead ones.
    """
    import os

    try:
        pid = int(str(token).split(":", 1)[0])
    except (ValueError, AttributeError):
        return True  # malformed → don't seize (safer than guessing)
    if pid <= 0:
        return True  # sentinel / missing → don't seize

    if os.name == "nt":
        # Windows: OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION=0x1000, FALSE, pid).
        # Returns NULL (0) if the process doesn't exist; nonzero handle if alive.
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.OpenProcess.argtypes = (
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            )
            SYNCHRONIZE = 0x00100000
            handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if not handle:
                return False  # dead — GetLastError() usually ERROR_INVALID_PARAMETER
            kernel32.CloseHandle(handle)
            return True
        except Exception:
            # ctypes failure → can't tell, don't seize (safe default)
            return True

    # POSIX: use signal-0 probe.
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # PID exists but not ours — still alive
    except OSError:
        return True  # unsupported / unknown — don't seize


def release_story_driver(story_key: str, token: str) -> None:
    """Release the driver claim — only if it is still ours (token matches).

    Guards against releasing a claim a newer driver force-claimed after a crash.
    """
    with _db() as conn:
        conn.execute(
            "UPDATE story SET driver_claim = NULL "
            "WHERE story_key = ? AND driver_claim = ?",
            (story_key, token),
        )


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
) -> int:
    """Record an LLM call trace with token usage. Returns the new row id."""
    with _db() as conn:
        cur = conn.execute(
            """INSERT INTO llm_trace (story_key, stage, operation, model,
               prompt_tokens, completion_tokens, total_tokens,
               duration_ms, success, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               RETURNING id""",
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
        return cur.fetchone()[0]


def log_llm_call(
    trace_id: int,
    *,
    prompt_text: str = "",
    response_text: str = "",
    reasoning_text: str = "",
    tool_calls_json: str = "",
) -> int:
    """Record the prompt/response/reasoning body of an LLM call.

    Linked to ``llm_trace`` via ``trace_id`` (ON DELETE CASCADE). Returns the
    new row id. Callers should keep ``log_llm_trace`` + ``log_llm_call`` paired.
    """
    with _db() as conn:
        cur = conn.execute(
            """INSERT INTO llm_call
               (trace_id, prompt_text, response_text, reasoning_text, tool_calls_json)
               VALUES (?, ?, ?, ?, ?)
               RETURNING id""",
            (trace_id, prompt_text, response_text, reasoning_text, tool_calls_json),
        )
        return cur.fetchone()[0]


def get_story_llm_calls(story_key: str) -> list[dict]:
    """Return prompt/response/reasoning bodies for a story, ordered by call id.

    JOIN llm_call ↔ llm_trace on trace_id, filter by story_key. Use this for
    auditing what was asked/answered/thought across an orchestration run.
    """
    with _db() as conn:
        rows = conn.execute(
            """SELECT lc.id, lc.trace_id, lc.prompt_text, lc.response_text,
                      lc.reasoning_text, lc.tool_calls_json, lc.created_at,
                      lt.stage, lt.operation, lt.model, lt.prompt_tokens,
                      lt.completion_tokens, lt.total_tokens, lt.duration_ms,
                      lt.success, lt.error
               FROM llm_call lc
               JOIN llm_trace lt ON lt.id = lc.trace_id
               WHERE lt.story_key = ?
               ORDER BY lc.id""",
            (story_key,),
        ).fetchall()
    return [dict(r) for r in rows]


def _backfill_llm_trace_story_keys(conn) -> None:
    """Best-effort backfill: attribute untraced llm_client rows to nearby stories.

    Old code logged token usage in llm_client without story_key, while planner.py
    logged a separate row with story_key but zero usage. This function pairs the
    two by timestamp proximity (within 5 minutes). It is idempotent and only
    touches rows whose story_key is still empty.
    """
    from datetime import datetime, timedelta, timezone

    traced = conn.execute(
        "SELECT id, story_key, model, created_at FROM llm_trace WHERE story_key != ''"
    ).fetchall()
    if not traced:
        return

    untraced = conn.execute(
        "SELECT id, created_at FROM llm_trace WHERE story_key = ''"
    ).fetchall()
    if not untraced:
        return

    def _parse(dt_str: str) -> datetime:
        # SQLite timestamps are UTC; ensure timezone-aware comparison.
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    threshold = timedelta(minutes=5)
    for u in untraced:
        u_dt = _parse(u["created_at"])
        best = None
        best_diff = threshold
        for t in traced:
            t_dt = _parse(t["created_at"])
            diff = abs(u_dt - t_dt)
            if diff < best_diff:
                best_diff = diff
                best = t
        if best:
            conn.execute(
                "UPDATE llm_trace SET story_key = ?, model = ? WHERE id = ?",
                (best["story_key"], best["model"], u["id"]),
            )


def _pricing_for_model(model: str) -> dict[str, float]:
    """Return CNY pricing per 1M tokens for a model name.

    Falls back to longest prefix match, then default. Env var
    STORY_TOKEN_PRICING_JSON can override or extend the table.
    """
    import os

    pricing = dict(MODEL_PRICING_CNY)
    env_json = os.environ.get("STORY_TOKEN_PRICING_JSON", "")
    if env_json:
        try:
            pricing.update(json.loads(env_json))
        except Exception:
            pass

    normalized = (model or "").lower().strip()
    if normalized in pricing:
        return pricing[normalized]

    # Longest prefix match
    best = None
    best_len = 0
    for key in pricing:
        if key == "default":
            continue
        if normalized.startswith(key.lower()) and len(key) > best_len:
            best = pricing[key]
            best_len = len(key)

    return best if best else pricing["default"]


def get_story_token_usage(story_key: str) -> dict:
    """Aggregate LLM token usage and estimated cost for a story.

    Returns:
        {
            "prompt_tokens": int,
            "completion_tokens": int,
            "total_tokens": int,
            "calls": int,
            "cost_cny": float,
            "by_stage": dict[str, int],
            "by_model": dict[str, int],
        }
    """
    with _db() as conn:
        rows = conn.execute(
            """SELECT stage, model, prompt_tokens, completion_tokens, total_tokens
               FROM llm_trace
               WHERE story_key = ?""",
            (story_key,),
        ).fetchall()

    total_prompt = 0
    total_completion = 0
    total_tokens = 0
    calls = 0
    by_stage: dict[str, int] = {}
    by_model: dict[str, int] = {}
    cost_cny = 0.0

    for r in rows:
        model = r["model"] or ""
        stage = r["stage"] or "unknown"
        prompt = r["prompt_tokens"] or 0
        completion = r["completion_tokens"] or 0
        total = r["total_tokens"] or 0

        total_prompt += prompt
        total_completion += completion
        total_tokens += total
        calls += 1

        by_stage[stage] = by_stage.get(stage, 0) + total
        by_model[model] = by_model.get(model, 0) + total

        p = _pricing_for_model(model)
        cost_cny += (prompt * p["input"] + completion * p["output"]) / 1_000_000

    return {
        "prompt_tokens": total_prompt,
        "completion_tokens": total_completion,
        "total_tokens": total_tokens,
        "calls": calls,
        "cost_cny": round(cost_cny, 4),
        "by_stage": by_stage,
        "by_model": by_model,
    }


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
    if not existing:
        # story_key may already exist without source_id linked (hand-created,
        # historical, or duplicate items in one sync batch). Fall back to a key
        # lookup so upsert stays idempotent instead of crashing on UNIQUE
        # story_key in create_story().
        key = f"{source_type}-{source_id}"
        existing_by_key = get_story(key)
        if existing_by_key:
            update_story(key, source_type=source_type, source_id=source_id)
            existing = existing_by_key
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


def get_recent_events_by_type(event_types: list[str], limit: int = 100) -> list[dict]:
    """跨所有 story 取近期事件(无 story_key 过滤)。

    供层5 reflection 的全局 playbook 用(飞轮知识是跨 story 的)。
    """
    placeholders = ",".join("?" * len(event_types))
    with _db() as conn:
        rows = conn.execute(
            f"SELECT * FROM event_log WHERE event_type IN ({placeholders}) ORDER BY id DESC LIMIT ?",
            list(event_types) + [limit],
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
    """Get-or-create a project by repo_path（idempotent）. Returns the row as dict.

    repo_path 有 UNIQUE 约束——已存在则更新 name/default_branch/remote_url（保留
    availability，它由 check_project_availability 管），不再 INSERT 撞约束 500。
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _db() as conn:
        existing = conn.execute(
            "SELECT * FROM project WHERE repo_path = ?", (repo_path,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE project SET name=?, default_branch=?, remote_url=?, updated_at=? "
                "WHERE repo_path=?",
                (name, default_branch, remote_url, now, repo_path),
            )
        else:
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
        row = conn.execute(
            "SELECT * FROM project WHERE repo_path = ?", (repo_path,)
        ).fetchone()
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


class WorktreePathConflict(Exception):
    """worktree_path 已被一个活跃绑定占用,无法登记。"""

    def __init__(self, worktree_path: str, occupant: dict):
        self.worktree_path = worktree_path
        self.occupant = occupant
        super().__init__(
            f"worktree_path {worktree_path} 已被 story {occupant.get('story_key')} "
            f"占用 (state={occupant.get('worktree_state')})"
        )


# 可被自动迁移(释放路径)的占用者状态:肯定没有活跃 worktree
_DISPLACEABLE_STATES = {"unprepared", "missing"}


def _find_worktree_occupant(worktree_path: str) -> dict | None:
    """查 worktree_path 的当前占用者。新开只读连接(调用方写事务已因异常退出)。"""
    with _db() as conn:
        row = conn.execute(
            "SELECT story_key, project_id, worktree_state, branch "
            "FROM story_project WHERE worktree_path = ?",
            (worktree_path,),
        ).fetchone()
    return dict(row) if row else None


def _resolve_worktree_conflict(worktree_path: str) -> None:
    """写操作撞 worktree_path UNIQUE 时调用。
    占用者陈旧(unprepared/missing)→ 置 NULL 释放路径,调用方重试即成功;
    占用者活跃 → 抛 WorktreePathConflict;未命中(非 worktree_path 冲突)→ 直接返回。"""
    occupant = _find_worktree_occupant(worktree_path)
    if not occupant:
        return
    if occupant.get("worktree_state") in _DISPLACEABLE_STATES:
        with _db() as conn:
            conn.execute(
                "UPDATE story_project SET worktree_path = NULL, updated_at = ? "
                "WHERE story_key = ? AND project_id = ?",
                (
                    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    occupant["story_key"],
                    occupant["project_id"],
                ),
            )
        return
    raise WorktreePathConflict(worktree_path, occupant)


def bind_story_project(
    story_key: str,
    project_id: int,
    branch: str = "",
    base_branch: str = "main",
    base_commit: str = "",
    worktree_path: str | None = None,
    workspace_type: str = "",
    worktree_state: str = "unprepared",
    summary: str = "",
    source: str = "user",
    evidence_ref: str = "",
) -> dict:
    """Bind a story to a project. Returns the created row."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    # worktree_path 列为 TEXT UNIQUE,但 SQLite 的 UNIQUE 对 NULL 豁免(多 NULL 互不冲突)。
    # 未创建 worktree 时存 NULL,而非占位字符串——避免假路径污染 prepare/scan。
    if not worktree_path:
        worktree_path = None
    _insert = """INSERT INTO story_project (story_key, project_id, branch, base_branch,
       base_commit, worktree_path, workspace_type, worktree_state,
       summary, source, evidence_ref, created_at, updated_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
    _select = "SELECT * FROM story_project WHERE story_key = ? AND project_id = ?"
    _vals = (
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
    )
    try:
        with _db() as conn:
            conn.execute(_insert, _vals)
            row = conn.execute(_select, (story_key, project_id)).fetchone()
        return dict(row) if row else {}
    except sqlite3.IntegrityError:
        if not worktree_path:
            raise  # 非 worktree_path 维度冲突(如 (story_key, project_id) 重复),原样抛
        # 陈旧占用者 → 已置 NULL,重试必成功;活跃占用者 → 抛 WorktreePathConflict
        _resolve_worktree_conflict(worktree_path)
        with _db() as conn:
            conn.execute(_insert, _vals)
            row = conn.execute(_select, (story_key, project_id)).fetchone()
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
    _update_sql = (
        f"UPDATE story_project SET {sets} WHERE story_key = ? AND project_id = ?"
    )
    try:
        with _db() as conn:
            conn.execute(_update_sql, values)
    except sqlite3.IntegrityError:
        wp = kwargs.get("worktree_path")
        if not wp:
            raise  # 非 worktree_path 维度冲突(worktree_path 设 NULL 不会撞 UNIQUE)
        # 陈旧占用者 → 已置 NULL,重试必成功;活跃占用者 → 抛 WorktreePathConflict
        _resolve_worktree_conflict(wp)
        with _db() as conn:
            conn.execute(_update_sql, values)


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


# ---------------------------------------------------------------------------
# story_doc / story_doc_version — versioned business docs
# DB is the single source of truth; a local .md file mirrors the latest
# version as a read-only cache (synced by the API layer on save). doc_type is
# an open string — prd / spec / plan / research / test_report / custom.
# ---------------------------------------------------------------------------


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
    """Latest version of a doc: content + version + title + updated_at."""
    with _db() as conn:
        row = conn.execute(
            "SELECT story_key, doc_type, title, current_version, latest_content, "
            "local_path, updated_by, updated_at FROM story_doc "
            "WHERE story_key=? AND doc_type=?",
            (story_key, doc_type),
        ).fetchone()
    return dict(row) if row else None


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
            "SELECT story_key, doc_type, title, current_version, updated_by, updated_at, local_path "
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
