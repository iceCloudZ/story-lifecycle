"""projects — git 仓库 CRUD（设计15 阶段B 拆分自 models.py）。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .connection import _db
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
        "workspace_id",
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


