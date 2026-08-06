"""routers/deliveries — deliveries domain API（设计15 阶段C 拆自 api.py）。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ....infra.db import models as db
from pydantic import BaseModel, Field

router = APIRouter(tags=["deliveries"])

class CreateDeliveryRequest(BaseModel):
    kind: str
    project_id: int | None = None
    provider: str = ""
    external_id: str = ""
    url: str = ""
    source_branch: str = ""
    target_branch: str = ""
    delivery_state: str = "not_started"
    merge_commit: str = ""
    review_summary: str = ""
    source: str = "user"
    evidence_ref: str = ""


class UpdateDeliveryRequest(BaseModel):
    delivery_state: str | None = None
    source: str = "user"


@router.get("/api/story/{story_key}/delivery-artifacts")
def api_list_delivery_artifacts(story_key: str):
    """List all delivery artifacts for a story."""
    from ..delivery import list_delivery_artifacts

    return {"artifacts": list_delivery_artifacts(story_key)}


@router.post("/api/story/{story_key}/delivery-artifacts")
def api_create_delivery_artifact(story_key: str, req: CreateDeliveryRequest):
    """Register a delivery artifact."""
    from ..delivery import register_delivery

    try:
        artifact = register_delivery(
            story_key=story_key,
            kind=req.kind,
            project_id=req.project_id,
            provider=req.provider,
            external_id=req.external_id,
            url=req.url,
            source_branch=req.source_branch,
            target_branch=req.target_branch,
            delivery_state=req.delivery_state,
            merge_commit=req.merge_commit,
            review_summary=req.review_summary,
            source=req.source,
            evidence_ref=req.evidence_ref,
        )
        return artifact
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/api/story/{story_key}/delivery-artifacts/{artifact_id}")
def api_update_delivery(story_key: str, artifact_id: int, req: UpdateDeliveryRequest):
    """Update delivery artifact state."""
    from ..delivery import update_delivery_state

    if req.delivery_state:
        try:
            return update_delivery_state(artifact_id, req.delivery_state, req.source)
        except PermissionError as e:
            raise HTTPException(status_code=403, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    return db.get_delivery_artifact(artifact_id)

