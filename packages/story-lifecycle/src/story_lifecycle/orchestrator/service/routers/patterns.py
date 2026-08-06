"""routers/patterns — patterns domain API（设计15 阶段C 拆自 api.py）。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ....infra.db import models as db

router = APIRouter(tags=["patterns"])

@router.get("/api/approvals")
def api_approvals():
    """Get approval queue: all pending (open + accepted) findings with evidence."""
    findings = db.get_all_pending_findings()
    db.enrich_findings_with_evidence(findings)
    return {"findings": findings}

