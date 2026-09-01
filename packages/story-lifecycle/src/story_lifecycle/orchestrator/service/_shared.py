"""跨 domain 共享 helper（设计15 阶段C 子PR-C1）。

从 api.py 抽出的、被多个 router domain 复用的 helper。api.py 与各
routers/*.py 从这里 import，避免跨 router 复制。

注：_load_tapd_config 的 CLI 副本（entry/cli/sync_cmd.py）保持独立
（entry 层不依赖 service 层），两处语义一致。
"""

from __future__ import annotations

import logging

from fastapi import HTTPException

from ...infra.db import models as db

log = logging.getLogger("story-lifecycle.api-shared")


def _load_tapd_config() -> dict:
    import yaml

    from ...infra.paths import story_home

    config_file = story_home() / "config.yaml"
    if not config_file.exists():
        return {}
    with open(config_file, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("tapd", {})


def _pending_confirm_gates(s: dict) -> list[dict]:
    """story 当前挂着的确认门列表（管家 MCP 工具面数据源，DESIGN-story-butler §3.2）。

    把 context_json 里四种确认闸归一成结构化列表，每项带：
    - ``kind``: plan_confirm / story_state / upgrade / stage
    - ``targetState``: confirm_token 的拼接材料（token = f"{story_key}:{targetState}"）
    - ``targetIsTerminal``: 终态/发布类判定。集合来源是 routers/lifecycle.UPGRADE_STATES
      （("上线","结项")，428 升级门同一集合）—— 服务端判，butler 不本地硬编码。

    只读解析，无副作用。ctx 解析失败 → 空列表（宁可少报不可误报）。
    """
    import json as _json

    try:
        ctx = _json.loads((s or {}).get("context_json") or "{}")
    except (ValueError, TypeError):
        return []
    if not isinstance(ctx, dict):
        return []
    gates: list[dict] = []

    # 1) plan 确认门：有规划未确认 → 确认后 lifecycle 推进到「开发」（/plan/confirm 语义）
    if ctx.get("_agent_actions") and not ctx.get("_plan_confirmed"):
        from ...sourcing.lifecycle_state import LifecycleState

        gates.append(
            {
                "kind": "plan_confirm",
                "targetState": LifecycleState.DEV.value,
                "targetIsTerminal": False,
            }
        )

    # 2) lifecycle ui_button 确认门（judge 判 target 后停门等确认）
    g = ctx.get("_story_state_gate")
    if isinstance(g, dict) and g.get("awaiting_confirm"):
        from .routers.lifecycle import UPGRADE_STATES

        to = str(g.get("to") or "")
        gates.append(
            {
                "kind": "story_state",
                "fromState": str(g.get("from") or ""),
                "targetState": to,
                "finalTarget": str(g.get("final_target") or ""),
                "label": str(g.get("label") or ""),
                "targetIsTerminal": to in UPGRADE_STATES,
            }
        )

    # 3) 428 挂起的升级门（上线/结项，只能 UI 点确认）—— 恒为终态类
    up = ctx.get("_upgrade_gate")
    if isinstance(up, dict) and up.get("target"):
        gates.append(
            {
                "kind": "upgrade",
                "fromState": str(up.get("prev") or ""),
                "targetState": str(up.get("target")),
                "targetIsTerminal": True,
            }
        )

    # 4) stage 间确认闸（profile stage confirm=True，确认后进 next_stage）
    sg = ctx.get("_stage_gate")
    if isinstance(sg, dict) and sg.get("awaiting_confirm"):
        gates.append(
            {
                "kind": "stage",
                "completedStage": str(sg.get("completed_stage") or ""),
                "targetState": str(sg.get("next_stage") or ""),
                "targetIsTerminal": False,
            }
        )
    return gates


def _serialize_story_summary(s: dict, patrol: dict | None = None) -> dict:
    """camelCase summary of a story for list views — REST /api/story and the
    /ws/stories push share this so the two payloads can't drift. (The WS version
    previously omitted tapdType/intakeState, leaving the Dashboard's filters
    matching nothing — see the dashboard-zero-stories bug.)

    patrol: 该 story 的巡检摘要（itemsCount/latestRunAt/latestResult，来自
    db.get_patrol_summaries() 的批量聚合），无巡检数据时为 None。调用方一次
    拉全量 map 传入，避免这里逐 story 查库（列表 243 story 会 N+1）。"""
    return {
        "storyKey": s["story_key"],
        "title": s["title"],
        "currentStage": s["current_stage"],
        "status": s["status"],
        "complexity": s.get("complexity"),
        "workspace": s.get("workspace"),
        "profile": s["profile"],
        "executionCount": s["execution_count"],
        "createdAt": s.get("created_at"),
        "updatedAt": s["updated_at"],
        "deadline": s.get("deadline"),
        "priority": s.get("priority"),
        "owner": s.get("owner"),
        "tapdStatus": s.get("tapd_status"),
        "tapdUrl": s.get("tapd_url"),
        "tapdType": s.get("tapd_type"),
        "intakeState": s.get("intake_state"),
        "sourceType": s.get("source_type"),
        "sourceId": s.get("source_id"),
        "parentKey": s.get("parent_key"),
        "lifecycleState": s.get("lifecycle_state"),
        "releaseTrain": s.get("release_train"),
        "isTest": bool(s.get("is_test")),
        "patrolSummary": patrol,
        # 管家 story_list 的「是否停确认门」列（DESIGN-story-butler §3.2）。
        # 布尔来自 _pending_confirm_gates 的轻量 ctx 解析，列表页免 N+1 拉详情。
        "awaitingConfirm": bool(_pending_confirm_gates(s)),
    }


def _story_list_json() -> list[dict]:
    # Same gathering + serialization as the REST /api/story endpoint, so the
    # WS-pushed list and the REST list are identical (incl. candidate stories).
    patrol_map = db.get_patrol_summaries()
    return [
        _serialize_story_summary(s, patrol_map.get(s["story_key"]))
        for s in db.list_visible_stories()
    ]


def _resolve_workspace_or_404(ident: str | int) -> dict:
    from ..workspace.workspace_registry import get_workspace

    ws = get_workspace(ident)
    if not ws:
        raise HTTPException(status_code=404, detail=f"Workspace not found: {ident}")
    return ws


def _wiki_knowledge_root(slug: str) -> tuple[dict, str]:
    """解析 workspace + 知识根;无知识根 → 400(还没跑 gen_wiki)。"""
    from ..workspace.workspace_registry import _knowledge_root_for

    ws = _resolve_workspace_or_404(slug)
    kroot = _knowledge_root_for(ws)
    if not kroot:
        raise HTTPException(
            status_code=400,
            detail="Workspace 无知识根目录,先跑 story workspace init --step gen_wiki",
        )
    return ws, kroot


def _get_story_documents(story_key: str) -> list[dict]:
    with db._db() as conn:
        rows = conn.execute(
            "SELECT * FROM story_document WHERE story_key = ? ORDER BY id",
            (story_key,),
        ).fetchall()
    return [dict(r) for r in rows]


def _get_story_change_items(story_key: str) -> list[dict]:
    with db._db() as conn:
        rows = conn.execute(
            "SELECT * FROM story_change_item WHERE story_key = ? ORDER BY id",
            (story_key,),
        ).fetchall()
    return [dict(r) for r in rows]


def _workspace_root_for_project(repo_path: str):
    """Infer the story workspace root for a registered project path.

    In a monorepo, a sub-project like ``D:/hc-all/frontends/hc-admin`` should
    resolve to ``D:/hc-all`` when the monorepo root carries ``.story``/``.agents``
    markers. For standalone projects, the project directory itself is the root.
    The walk is bounded by the git top-level (when present) and a small max depth
    so unrelated ancestor directories (e.g. the user's home directory) that happen
    to have markers are not picked.
    """
    import subprocess
    from pathlib import Path

    path = Path(repo_path).resolve()

    # Find the git top-level to bound the ancestor walk.
    git_root = None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            git_root = Path(result.stdout.strip()).resolve()
    except Exception:
        pass

    max_depth = 5
    candidates = [path]
    for i, parent in enumerate(path.parents):
        if git_root is not None and parent == git_root:
            candidates.append(parent)
            break
        if i >= max_depth:
            break
        candidates.append(parent)

    for candidate in candidates:
        if (
            (candidate / ".story").exists()
            or (candidate / ".agents").exists()
            or (candidate / "AGENTS.md").exists()
        ):
            return candidate
    return git_root if git_root is not None else path


__all__ = [
    "_load_tapd_config",
    "_serialize_story_summary",
    "_story_list_json",
    "_pending_confirm_gates",
    "_resolve_workspace_or_404",
    "_wiki_knowledge_root",
    "_get_story_documents",
    "_get_story_change_items",
    "_workspace_root_for_project",
]
