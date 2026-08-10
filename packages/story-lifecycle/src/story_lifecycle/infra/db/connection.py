"""connection — shared: get_db_path/get_conn/_db/_validate_columns + VALID_COLUMNS（设计15 阶段B 拆分自 models.py）。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path


def get_db_path() -> Path:
    from ..paths import story_home

    home = story_home()
    home.mkdir(parents=True, exist_ok=True)
    return home / "story.db"


def get_conn() -> sqlite3.Connection:
    db = get_db_path()
    # timeout:SQLite 默认 5s 忙等待。编排线程 + 外部 story tool 进程并发写时
    # (2026-08-06 real-run 1068018 approve 决策行瞬时丢失),短事务也可能撞
    # "database is locked"。提到 15s 让 busy handler 等写锁释放。
    conn = sqlite3.connect(str(db), timeout=15.0)
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


def _validate_columns(keys):
    invalid = set(keys) - VALID_COLUMNS
    if invalid:
        raise ValueError(f"Invalid story columns: {invalid}")


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
        "deleted_at",  # 软删除时间戳(NULL=未删);卡片删除走软删,可 restore 恢复
    }
)
