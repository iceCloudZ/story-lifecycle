"""workspaces — 业务项目实体 CRUD（设计15 阶段B 拆分自 models.py）。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .connection import _db
def create_workspace(
    name: str,
    slug: str,
    knowledge_root: str | None = None,
    integrations_json: dict | None = None,
) -> dict:
    """Create a workspace entity. name/slug UNIQUE — 冲突直接抛 sqlite3.IntegrityError。"""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _db() as conn:
        cur = conn.execute(
            """INSERT INTO workspace (name, slug, knowledge_root, integrations_json,
               init_state, created_at, updated_at)
               VALUES (?, ?, ?, ?, '{}', ?, ?)
               RETURNING id""",
            (
                name,
                slug,
                knowledge_root,
                json.dumps(integrations_json or {}, ensure_ascii=False),
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM workspace WHERE id = ?", (cur.fetchone()[0],)
        ).fetchone()
    return dict(row) if row else {}


def get_workspace(workspace_id: int) -> dict | None:
    """Get a single workspace by id."""
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM workspace WHERE id = ?", (workspace_id,)
        ).fetchone()
    return dict(row) if row else None


def get_workspace_by_slug(slug: str) -> dict | None:
    """Get a workspace by its unique slug."""
    with _db() as conn:
        row = conn.execute("SELECT * FROM workspace WHERE slug = ?", (slug,)).fetchone()
    return dict(row) if row else None


def get_workspace_by_name(name: str) -> dict | None:
    """Get a workspace by its unique name."""
    with _db() as conn:
        row = conn.execute("SELECT * FROM workspace WHERE name = ?", (name,)).fetchone()
    return dict(row) if row else None


def list_workspaces() -> list[dict]:
    """Return all workspaces ordered by name."""
    with _db() as conn:
        rows = conn.execute("SELECT * FROM workspace ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def update_workspace(workspace_id: int, **kwargs) -> None:
    """Update workspace fields. Always bumps updated_at."""
    if not kwargs:
        return
    valid = {
        "name",
        "slug",
        "knowledge_root",
        "integrations_json",
        "init_state",
    }
    invalid = set(kwargs.keys()) - valid
    if invalid:
        raise ValueError(f"Invalid workspace columns: {invalid}")
    kwargs["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [workspace_id]
    with _db() as conn:
        conn.execute(f"UPDATE workspace SET {sets} WHERE id = ?", values)


def delete_workspace(workspace_id: int) -> None:
    """Delete a workspace. 下属 Repo 保留(workspace_id 置 NULL,回到散仓库形态)。"""
    with _db() as conn:
        conn.execute(
            "UPDATE project SET workspace_id = NULL WHERE workspace_id = ?",
            (workspace_id,),
        )
        conn.execute("DELETE FROM workspace WHERE id = ?", (workspace_id,))


def update_workspace_init_state(
    workspace_id: int,
    step: str,
    status: str,
    reason: str = "",
) -> None:
    """Merge one step's status into workspace.init_state.

    init_state 形如 {"register_repos": "done", "gen_wiki": {"status": "failed",
    "reason": "..."}} —— 值可以是纯状态字符串(done/pending),失败时升级为
    {status, reason} 对象(§3:每步失败标记 failed + 原因)。
    """
    with _db() as conn:
        row = conn.execute(
            "SELECT init_state FROM workspace WHERE id = ?", (workspace_id,)
        ).fetchone()
        if not row:
            return
        state = json.loads(row["init_state"] or "{}")
        if status == "failed" and reason:
            state[step] = {"status": "failed", "reason": reason}
        else:
            state[step] = status
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE workspace SET init_state = ?, updated_at = ? WHERE id = ?",
            (json.dumps(state, ensure_ascii=False), now, workspace_id),
        )


def list_projects_by_workspace(workspace_id: int) -> list[dict]:
    """Return all repos (project rows) belonging to a workspace."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM project WHERE workspace_id = ? ORDER BY name",
            (workspace_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_stories_by_workspace(workspace_id: int) -> list[dict]:
    """Return stories bound to any repo of the workspace (经 Repo → story_project 反查)。

    story 仍然绑定到 Repo 层(story_project),不直接绑 Workspace(§1.3 不变的关系)。
    """
    with _db() as conn:
        rows = conn.execute(
            """SELECT DISTINCT s.* FROM story s
               JOIN story_project sp ON sp.story_key = s.story_key
               JOIN project p ON p.id = sp.project_id
               WHERE p.workspace_id = ?
               ORDER BY s.updated_at DESC""",
            (workspace_id,),
        ).fetchall()
    return [dict(r) for r in rows]


WORKSPACE_INIT_STEPS = (
    "register_repos",
    "detect_runtime",
    "gen_wiki",
    "register_integrations",
    "init_scenarios",
    "detect_test_env",
)


