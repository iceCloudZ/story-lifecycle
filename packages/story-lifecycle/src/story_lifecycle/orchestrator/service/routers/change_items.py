"""routers/change_items — change_items domain API（设计15 阶段C 拆自 api.py）。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ....infra.db import models as db
from pydantic import BaseModel, Field

router = APIRouter(tags=["change_items"])

class AddChangeItemRequest(BaseModel):
    kind: str
    ref: str = ""
    summary: str = ""
    evidence_ref: str = ""
    environment: str = ""
    project_id: int | None = None


@router.post("/api/story/{story_key}/context/change-items")
def api_add_change_item(story_key: str, req: AddChangeItemRequest):
    """Add a change item (ddl/nacos) — agent backfill."""
    if not db.get_story(story_key):
        raise HTTPException(status_code=404, detail=f"story not found: {story_key}")
    ci = db.create_change_item(
        story_key,
        req.kind,
        project_id=req.project_id,
        ref=req.ref,
        summary=req.summary,
        evidence_ref=req.evidence_ref,
        environment=req.environment,
        source="agent",
    )
    db.bump_context_revision(story_key)
    return ci

