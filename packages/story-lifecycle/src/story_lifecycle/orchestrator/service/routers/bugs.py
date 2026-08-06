"""routers/bugs — bugs domain API（设计15 阶段C 拆自 api.py）。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from ....infra.db import models as db
from .._shared import _load_tapd_config, _serialize_story_summary
from pydantic import BaseModel

router = APIRouter(tags=["bugs"])


class BatchFixPromptRequest(BaseModel):
    bug_keys: list[str]


@router.get("/api/story/{story_key}/bugs")
def api_get_related_bugs(story_key: str):
    """List local bug stories linked to this story via parent_key."""
    if not db.get_story(story_key):
        raise HTTPException(status_code=404, detail=f"story not found: {story_key}")
    bugs = db.list_stories_by_parent(story_key, item_type="bug")
    return JSONResponse([_serialize_story_summary(b) for b in bugs])


@router.post("/api/story/{story_key}/sync-related-bugs")
def api_sync_related_bugs(story_key: str):
    """Sync bugs linked to this story (via TAPD get_related_bugs), setting parent_key."""
    story = db.get_story(story_key)
    if not story:
        raise HTTPException(status_code=404, detail=f"story not found: {story_key}")
    if story.get("source_type") not in ("tapd", None):
        return {"synced": 0, "reason": "not a tapd source"}
    # source_id 优先；旧 story 可能只把 TAPD id 编进 story_key（tapd-{id}），从中提取
    tapd_id = story.get("source_id") or (
        story_key[5:] if story_key.startswith("tapd-") else ""
    )
    if not tapd_id:
        return {"synced": 0, "reason": "no tapd id (not a tapd story)"}
    config = _load_tapd_config()
    if not config.get("workspace_id"):
        raise HTTPException(status_code=503, detail="TAPD not configured")
    from ....sourcing.sources.tapd_api import TapdApi

    api = TapdApi(workspace_id=config["workspace_id"])
    related = api.get_related_bugs(tapd_id) or []
    synced = 0
    for r in related:
        bug_id = r.get("bug_id")
        if not bug_id:
            continue
        flat = (api.get_bug_detail(bug_id) or {}).get("Bug", {})
        db.upsert_story_from_source(
            source_type="tapd",
            source_id=f"bug_{bug_id}",
            title=flat.get("title", ""),
            tapd_type="bug",
            tapd_status=flat.get("status", ""),
            owner=flat.get("current_owner", ""),
            tapd_url=f"https://www.tapd.cn/{config['workspace_id']}/bugtrace/bugs/view?bug_id={bug_id}",
            parent_key=story_key,
        )
        synced += 1
    return {"synced": synced, "story_key": story_key}


@router.post("/api/story/{story_key}/bugs/{bug_key}/link")
def api_link_bug_to_story(story_key: str, bug_key: str):
    """Manually bind an unassociated bug to a story."""
    story = db.get_story(story_key)
    if not story:
        raise HTTPException(404, "Story not found")
    bug = db.get_story(bug_key)
    if not bug:
        raise HTTPException(404, "Bug not found")
    if bug.get("tapd_type") != "bug":
        raise HTTPException(400, "Target is not a bug")
    db.update_story(bug_key, parent_key=story_key)
    db.log_stage(
        bug_key, bug.get("current_stage", ""), "link", f"Manually linked to {story_key}"
    )
    return {"ok": True, "parentKey": story_key}


@router.get("/api/story/{story_key}/available-bugs")
def api_list_available_bugs(story_key: str):
    """List bugs that are not linked to any story (for drag-and-drop binding)."""
    story = db.get_story(story_key)
    if not story:
        raise HTTPException(404, "Story not found")
    bugs = db.list_unlinked_bugs()
    return JSONResponse([_serialize_story_summary(b) for b in bugs])


@router.post("/api/story/{story_key}/bugs/{bug_key}/fix-prompt")
def api_get_bugfix_prompt(story_key: str, bug_key: str):
    """Render a bug-fix prompt for a code AI based on the parent story context."""
    if not db.get_story(story_key):
        raise HTTPException(404, "Story not found")
    if not db.get_story(bug_key):
        raise HTTPException(404, "Bug not found")
    try:
        from ...context.release_prompt import generate_bugfix_prompt

        return generate_bugfix_prompt(story_key, bug_key)
    except ValueError as e:
        raise HTTPException(404, detail=str(e))


@router.post("/api/story/{story_key}/bugs/fix-prompt")
def api_get_batch_bugfix_prompt(story_key: str, req: BatchFixPromptRequest):
    """Render a combined bug-fix prompt for multiple bugs under a story."""
    if not db.get_story(story_key):
        raise HTTPException(404, "Story not found")
    if not req.bug_keys:
        raise HTTPException(400, "bug_keys is empty")
    try:
        from ...context.release_prompt import generate_batch_bugfix_prompt

        return generate_batch_bugfix_prompt(story_key, req.bug_keys)
    except ValueError as e:
        raise HTTPException(404, detail=str(e))


@router.post("/api/story/{bug_key}/resolve")
def api_resolve_bug(bug_key: str):
    """Mark a bug resolved: update TAPD + local status. Warns if no bugfix-report."""
    story = db.get_story(bug_key)
    if not story:
        raise HTTPException(status_code=404, detail=f"story not found: {bug_key}")
    if story.get("tapd_type") != "bug":
        raise HTTPException(status_code=400, detail="not a bug")
    has_evidence = any(
        d.get("kind") == "bugfix-report" for d in db.get_story_documents(bug_key)
    )
    config = _load_tapd_config()
    if config.get("workspace_id") and story.get("source_id"):
        from ....sourcing.sources.tapd_api import TapdApi

        api = TapdApi(workspace_id=config["workspace_id"])
        bug_id = story["source_id"].removeprefix("bug_")
        api.update_bug(bug_id, {"status": "resolved"})
    db.update_story(bug_key, status="completed", tapd_status="resolved")
    return {"ok": True, "has_bugfix_report": has_evidence}
