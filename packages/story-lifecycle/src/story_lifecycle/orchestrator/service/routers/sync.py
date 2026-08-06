"""routers/sync — sync domain API（设计15 阶段C 拆自 api.py）。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ....infra.db import models as db
from .._shared import (
    _load_tapd_config,
)

router = APIRouter(tags=["sync"])


class SyncRequest(BaseModel):
    workspace: str = ""
    autostart: bool = True
    dry_run: bool = False
    status_only: bool = False
    fetch_all: bool = False
    item_type: str = ""  # "bug" | "story" | "requirement" | ""


def _sync_related_bugs_from_stories(source, item_type_filter: str = "") -> dict:
    """For every local TAPD story, fetch related bugs and upsert them with parent_key."""
    result = {"synced": 0, "failed": 0}
    # Only run when syncing stories or everything; pure bug-only sync already has its own path.
    if item_type_filter == "bug":
        return result

    # show_test=True:测试 story 的关联 bug 同步不能因 is_test 过滤而漏。
    stories = db.list_visible_stories(show_all=True, item_type="story", show_test=True)
    stories = [
        s for s in stories if s.get("source_type") == "tapd" and s.get("source_id")
    ]

    # Collect related bugs first to avoid concurrent SQLite writes on the same key.
    bug_map: dict[str, tuple[dict, str]] = {}
    failed_stories = 0
    for story in stories:
        try:
            related = source._api.get_related_bugs(story["source_id"]) or []
            for r in related:
                bug_id = (r.get("Bug") or r).get("id")
                if not bug_id or bug_id in bug_map:
                    continue
                detail = source._api.get_bug_detail(bug_id)
                flat = (detail.get("Bug", {}) if detail else {}) or {}
                if flat:
                    bug_map[bug_id] = (flat, story["story_key"])
        except Exception:
            failed_stories += 1

    for bug_id, (flat, parent_key) in bug_map.items():
        try:
            db.upsert_story_from_source(
                source_type="tapd",
                source_id=f"bug_{bug_id}",
                title=flat.get("title", ""),
                tapd_type="bug",
                tapd_status=flat.get("status", ""),
                owner=flat.get("current_owner", ""),
                tapd_url=f"https://www.tapd.cn/{source._api.workspace_id}/bugtrace/bugs/view?bug_id={bug_id}",
                parent_key=parent_key,
            )
            result["synced"] += 1
        except Exception:
            result["failed"] += 1

    if failed_stories:
        result["failed"] += failed_stories
    return result


@router.post("/api/sync/tapd")
def api_sync_tapd(req: SyncRequest):
    """Trigger TAPD sync."""
    from ....sourcing.sources.tapd_source import TapdSource

    config = _load_tapd_config()
    if not config:
        raise HTTPException(
            400, "TAPD not configured. Add 'tapd' section to config.yaml."
        )

    source = TapdSource(config)
    try:
        items = source.fetch_pending(
            fetch_all=req.fetch_all, item_type=req.item_type or None
        )
    except Exception as e:
        raise HTTPException(502, f"TAPD fetch failed: {e}")

    from ..sync_service import sync_tapd

    # Require an explicit absolute workspace. The previous `or "."` fallback
    # silently stored the server's CWD as the story workspace, which caused
    # evidence artifacts to land inside the tool's own package directory.
    workspace = req.workspace.strip()
    if not workspace:
        raise HTTPException(
            status_code=400, detail="workspace required (select a project first)"
        )
    if not Path(workspace).is_absolute():
        raise HTTPException(
            status_code=400, detail="workspace must be an absolute path"
        )

    result = sync_tapd(
        items,
        workspace=workspace,
        dry_run=req.dry_run,
        status_only=req.status_only,
    )

    # Also pull bugs linked to stories via TAPD get_related_bugs, which catches
    # associations that the bug's own story_id field misses.
    if not req.dry_run:
        related = _sync_related_bugs_from_stories(
            source, item_type_filter=req.item_type
        )
        result["related_bugs_synced"] = related["synced"]
        result["related_bugs_failed"] = related["failed"]

    return result


@router.get("/api/sync/tapd/status")
def api_sync_status():
    """Get TAPD config status."""
    config = _load_tapd_config()
    return {
        "configured": bool(config),
        "workspace_id": config.get("workspace_id", ""),
    }


@router.get("/api/profiles")
def api_list_profiles():
    """List available profiles for the create-story picker."""
    from ...engine.profile_loader import list_profiles

    return {"profiles": list_profiles()}
