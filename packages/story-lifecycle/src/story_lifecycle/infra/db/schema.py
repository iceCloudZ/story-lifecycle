"""schema — shared: init_db + _create_*_tables（按表族）（设计15 阶段B 拆分自 models.py）。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, timedelta

from .connection import _db


def init_db():
    """Create tables if not exist. Idempotent — safe to call on every startup.

    设计 14 (F1)：按表族 Extract Function —— 每个 _create_*_tables 管一族
    表的 CREATE + 索引 + 迁移。纯机械拆分，不改任何 SQL。
    """
    with _db() as conn:
        _create_story_tables(conn)
        _create_session_tables(conn)
        _create_finding_tables(conn)
        _create_runtime_fact_tables(conn)
        _create_doc_tables(conn)
        _create_change_item_tables(conn)
        _create_delivery_tables(conn)
        _create_trace_tables(conn)
        _create_decision_tables(conn)


def _create_story_tables(conn):
    """story 族：story / stage_log / gate_result / event_log + story 列迁移。"""
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
        conn.execute("ALTER TABLE story ADD COLUMN intake_state TEXT DEFAULT 'ready'")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE story ADD COLUMN context_revision INTEGER DEFAULT 0")
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
    # deleted_at:软删除时间戳。卡片「删除」置位(可 restore 恢复),物理删除仍由
    # delete_story() 负责(一次性脚本用,不暴露到卡片)。列表三档查询都过滤 NULL。
    try:
        conn.execute("ALTER TABLE story ADD COLUMN deleted_at TIMESTAMP")
    except sqlite3.OperationalError:
        pass


def _create_session_tables(conn):
    """session 族：story_session + 执行轨迹列迁移。"""
    # story_session: agent 会话恢复回填(每阶段一个会话)。
    # 全自动/半自动循环复用 session 省 token:同阶段重试/崩溃 resume 续上,跨阶段独立。
    # claude session_id 由后端 uuid5 主动给(--session-id);kimi 由 CLI 分配,
    # 后端从启动 banner 捕获后回填。UNIQUE(story_key,stage,adapter) 保证每阶段每 adapter
    # 一条;upsert 用 INSERT...ON CONFLICT。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS story_session (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            story_key   TEXT NOT NULL,
            stage       TEXT NOT NULL,
            adapter     TEXT NOT NULL,
            session_id  TEXT,
            status      TEXT NOT NULL DEFAULT 'active',
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            UNIQUE (story_key, stage, adapter)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ss_story_stage ON story_session(story_key, stage)"
    )
    # STEP 1.7a: story_session 扩展执行轨迹(DESIGN §4.10)。
    # attempt/outcome/failure_reason 记每阶段会话结果;artifacts_prod 存本阶段产出
    # 的成果物 JSON 清单;pty_log_ref 指 .story/runs/<key>/pty_<stage>/ 日志目录。
    # 幂等迁移(列已存在则跳过)。
    for _col, _type in (
        ("attempt", "INTEGER"),
        ("outcome", "TEXT"),
        ("failure_reason", "TEXT"),
        ("artifacts_prod", "TEXT"),
        ("pty_log_ref", "TEXT"),
        # 设计12 改动3:stage 完成摘要(judge_stage_completion 的 summary,TerminalTab 展示)。
        ("completion_summary", "TEXT"),
    ):
        try:
            conn.execute(f"ALTER TABLE story_session ADD COLUMN {_col} {_type}")
        except Exception:
            pass  # column already exists


def _create_finding_tables(conn):
    """finding 族：finding / learned_pattern（质量飞轮）。"""
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


def _create_runtime_fact_tables(conn):
    """runtime_fact 族：project / story_project / project_runtime_fact /
    workspace（+ 历史占位 worktree 清理）。"""
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sp_story ON story_project(story_key)")
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

    # -------- Workspace entity (11-workspace-entity-design.md) --------
    # 业务项目实体:聚合多个 Repo(project 表)、wiki 知识、测试旅程、外部集成。
    # 显式 opt-in —— 不建 Workspace 时行为与今天完全一致(开源零配置路径)。
    conn.execute("""
        CREATE TABLE IF NOT EXISTS workspace (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,           -- 业务项目名,如 "HappyCash 授信域"
            slug TEXT NOT NULL UNIQUE,           -- kebab-case,用于目录/URL
            knowledge_root TEXT,                 -- 知识根目录(默认 <主工作区>/.story/knowledge)
            integrations_json TEXT NOT NULL DEFAULT '{}',  -- §6 集成元数据
            init_state TEXT NOT NULL DEFAULT '{}',         -- §3 初始化管线状态(每步 done/pending/failed)
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ws_slug ON workspace(slug)")
    # 1.2 project 表挂 workspace_id:NULL = 未归属任何 Workspace 的散仓库(向后兼容)。
    try:
        conn.execute(
            "ALTER TABLE project ADD COLUMN workspace_id INTEGER "
            "REFERENCES workspace(id)"
        )
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_project_workspace ON project(workspace_id)"
    )

    # Migration: legacy _pending_<story>_<proj> placeholders → NULL.
    # SQLite UNIQUE 豁免 NULL,未建 worktree 的绑定不再需要占位字符串。
    # 幂等:首次执行后无匹配行。覆盖诊断文档里手动绕过留下的 _pending_ 行。
    conn.execute(
        "UPDATE story_project SET worktree_path = NULL, "
        "updated_at = strftime('%Y-%m-%d %H:%M:%S', 'now') "
        "WHERE worktree_path LIKE '_pending_%'"
    )


def _create_doc_tables(conn):
    """doc 族：story_document / story_doc / story_doc_version / story_doc_fts。"""
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sd_story ON story_document(story_key)")
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
    # Migration: 人工确认字段(成果物 gate 用)。AI 不能自我确认,只有 user 点确认才写。
    for _col in ("confirmed_by", "confirmed_at"):
        try:
            conn.execute(f"ALTER TABLE story_doc ADD COLUMN {_col} TEXT DEFAULT NULL")
        except Exception:
            pass  # column already exists
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


def _create_change_item_tables(conn):
    """change_item 族：story_change_item（DDL / 配置变更）。"""
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


def _create_delivery_tables(conn):
    """delivery 族：story_delivery_artifact（MR/PR 交付证据）。"""
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


def _create_trace_tables(conn):
    """trace 族：llm_trace / llm_call（LLM 调用审计）。"""
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


def _create_decision_tables(conn):
    """decision 族：orchestrator_decision（编排决策审计）。"""
    # STEP 2.1: orchestrator_decision 决策审计表(DESIGN §4.9)。
    # 记编排 LLM 的每次决策(边界纯判定 / 卡住诊断),含 reject 上限防护用的
    # story_key+stage+trigger+decision+reason。无状态编排的前提(§4.6)+审计载体。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orchestrator_decision (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            story_key       TEXT NOT NULL,
            stage           TEXT NOT NULL,
            trigger         TEXT NOT NULL,
            context_ref     TEXT NOT NULL DEFAULT '',
            decision        TEXT NOT NULL,
            reason          TEXT NOT NULL DEFAULT '',
            action_taken    TEXT NOT NULL DEFAULT '',
            action_payload  TEXT,
            llm_model       TEXT NOT NULL DEFAULT '',
            decided_at      TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_od_story_stage "
        "ON orchestrator_decision(story_key, stage)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_od_story_stage_trigger "
        "ON orchestrator_decision(story_key, stage, trigger)"
    )


def _backfill_llm_trace_story_keys(conn) -> None:
    """Best-effort backfill: attribute untraced llm_client rows to nearby stories.

    Old code logged token usage in llm_client without story_key, while planner.py
    logged a separate row with story_key but zero usage. This function pairs the
    two by timestamp proximity (within 5 minutes). It is idempotent and only
    touches rows whose story_key is still empty.
    """
    from datetime import datetime

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
