"""跨 domain 共享 helper（设计15 阶段C 子PR-C1）。

从 api.py 抽出的、被多个 router domain 复用的 helper。api.py 与各
routers/*.py 从这里 import，避免跨 router 复制。

注：_load_tapd_config 的 CLI 副本（entry/cli/sync_cmd.py）保持独立
（entry 层不依赖 service 层），两处语义一致。
"""

from __future__ import annotations

import logging
from typing import Any

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


def _serialize_story_summary(s: dict) -> dict:
    """camelCase summary of a story for list views — REST /api/story and the
    /ws/stories push share this so the two payloads can't drift. (The WS version
    previously omitted tapdType/intakeState, leaving the Dashboard's filters
    matching nothing — see the dashboard-zero-stories bug.)"""
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
    }


def _story_list_json() -> list[dict]:
    # Same gathering + serialization as the REST /api/story endpoint, so the
    # WS-pushed list and the REST list are identical (incl. candidate stories).
    return [_serialize_story_summary(s) for s in db.list_visible_stories()]


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


__all__ = [
    "_load_tapd_config",
    "_serialize_story_summary",
    "_story_list_json",
    "_resolve_workspace_or_404",
    "_wiki_knowledge_root",
    "_get_story_documents",
    "_get_story_change_items",
]
