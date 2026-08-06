"""routers/worktrees — worktrees domain API（设计15 阶段C 拆自 api.py）。"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from pydantic import BaseModel

router = APIRouter(tags=["worktrees"])


class WorktreePrepareRequest(BaseModel):
    worktree_root: str = ""


class CleanupRequest(BaseModel):
    project_id: int
    delivery_state: str = ""
    force: bool = False


@router.post("/api/story/{story_key}/worktrees/prepare")
def api_prepare_worktrees(
    story_key: str, req: WorktreePrepareRequest = WorktreePrepareRequest()
):
    """Prepare worktrees for all project bindings of a story."""
    from ...workspace.worktree.handler import prepare_worktrees

    results = prepare_worktrees(story_key, worktree_root=req.worktree_root)
    return {"results": results}


@router.get("/api/story/{story_key}/worktrees/cleanup-preview")
def api_cleanup_preview(story_key: str):
    """Preview worktree cleanup for a story."""
    from ...workspace.worktree.resolver import resolve_story_worktree
    from ..delivery import can_cleanup_worktree

    worktree_states = resolve_story_worktree(story_key)
    can_clean, reason = can_cleanup_worktree(story_key)
    return {
        "worktrees": worktree_states,
        "can_cleanup": can_clean,
        "reason": reason,
    }


@router.post("/api/story/{story_key}/worktrees/cleanup")
def api_cleanup_worktree(story_key: str, req: CleanupRequest):
    """Remove a worktree. Requires user confirmation."""
    from ...workspace.worktree.handler import cleanup_worktree

    result = cleanup_worktree(
        story_key,
        req.project_id,
        delivery_state=req.delivery_state,
        force=req.force,
    )
    if result["action"] == "reject":
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "reasonCode": result.get("reject_reason", "unknown"),
                "message": result["reason"],
            },
        )
    return {"ok": True, "worktree_path": result["worktree_path"]}
