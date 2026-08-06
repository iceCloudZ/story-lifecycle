"""routers/quality — quality domain API（设计15 阶段C 拆自 api.py）。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ....infra.db import models as db
from pydantic import BaseModel, Field

router = APIRouter(tags=["quality"])

class ReviewFeedbackRequest(BaseModel):
    content: str


class DecideFindingRequest(BaseModel):
    action: str  # accept, reject, defer, downgrade, mark_verified
    reason: str = ""
    verification_event_id: int | None = None


@router.post("/api/story/{story_key}/review-feedback")
def api_import_review_feedback(story_key: str, req: ReviewFeedbackRequest):
    """Import review feedback content and extract candidate findings."""
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")

    if not req.content.strip():
        raise HTTPException(400, "Review content is empty")

    from ...evaluation.review_feedback import import_review

    result = import_review(story_key, req.content)
    return {
        "imported": result["imported"],
        "skipped": result["skipped"],
        "mode": result["mode"],
        "warnings": result["warnings"],
    }


@router.get("/api/story/{story_key}/review-feedback")
def api_list_review_feedback(story_key: str):
    """List review feedback findings for a story."""
    findings = db.get_findings_by_story(story_key)
    review_findings = [f for f in findings if f["source"] == "review_feedback"]
    db.enrich_findings_with_evidence(review_findings)
    return {"findings": review_findings}


@router.put("/api/finding/{finding_id}/decide")
def api_decide_finding(finding_id: str, req: DecideFindingRequest):
    """Make a decision on a finding: accept/reject/defer/downgrade/mark_verified."""
    from ...evaluation.quality import update_finding_status

    finding = db.get_finding(finding_id)
    if not finding:
        raise HTTPException(404, f"Finding not found: {finding_id}")

    story_key = finding["story_key"]
    action = req.action

    if action == "accept":
        update_finding_status(story_key, finding_id, "accepted", reason=req.reason)
    elif action == "reject":
        update_finding_status(story_key, finding_id, "rejected", reason=req.reason)
    elif action == "defer":
        update_finding_status(story_key, finding_id, "deferred", reason=req.reason)
    elif action == "downgrade":
        sev_order = {"high": "medium", "medium": "low", "low": "low"}
        new_sev = sev_order.get(finding["severity"], "low")
        db.update_finding(finding_id, severity=new_sev)
        db.log_event(
            story_key,
            finding.get("stage", ""),
            "finding_downgraded",
            {
                "finding_id": finding_id,
                "from": finding["severity"],
                "to": new_sev,
                "reason": req.reason,
            },
        )
    elif action in ("mark_verified", "verify"):
        evidence = None
        if req.verification_event_id:
            evidence = {"verification_event_id": req.verification_event_id}
        update_finding_status(
            story_key, finding_id, "verified", reason=req.reason, evidence=evidence
        )
    else:
        raise HTTPException(
            400,
            f"Unknown action: {action}. Use: accept/reject/defer/downgrade/verify",
        )

    updated = db.get_finding(finding_id)
    return {"status": updated["status"], "severity": updated["severity"]}

