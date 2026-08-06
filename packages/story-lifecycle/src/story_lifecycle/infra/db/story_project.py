"""story_project — story↔project 绑定 + worktree 占用（设计15 阶段B 拆分自 models.py）。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .connection import _db
class WorktreePathConflict(Exception):
    """worktree_path 已被一个活跃绑定占用,无法登记。"""

    def __init__(self, worktree_path: str, occupant: dict):
        self.worktree_path = worktree_path
        self.occupant = occupant
        super().__init__(
            f"worktree_path {worktree_path} 已被 story {occupant.get('story_key')} "
            f"占用 (state={occupant.get('worktree_state')})"
        )


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


_DISPLACEABLE_STATES = {"unprepared", "missing"}


