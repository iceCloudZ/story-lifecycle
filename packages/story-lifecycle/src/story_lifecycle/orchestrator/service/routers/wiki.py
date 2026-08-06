"""routers/wiki — wiki domain API（设计15 阶段C 拆自 api.py）。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ....infra.db import models as db
from pydantic import BaseModel
from .._shared import _resolve_workspace_or_404, _wiki_knowledge_root

router = APIRouter(tags=["wiki"])


class WikiSaveRequest(BaseModel):
    title: str
    content: str
    source: str = "human"  # human | story:<key> | probe:<name>
    summary: str = ""
    evidence_refs: list[dict] = []
    related: list[str] = []
    source_refs: list[str] = []
    slug: str | None = None


class WikiReviewRequest(BaseModel):
    decision: str  # approve | reject
    reviewer: str = ""
    reason: str = ""


@router.get("/api/workspace-entities/{slug}/wiki")
def api_list_wiki(slug: str, review_state: str = ""):
    """列出 wiki 条目(review_state=draft|merged|'')。"""
    from ....knowledge.wiki_pipeline import list_wiki_entries

    _, kroot = _wiki_knowledge_root(slug)
    return {"wiki": list_wiki_entries(kroot, review_state)}


@router.post("/api/workspace-entities/{slug}/wiki")
def api_save_wiki(slug: str, req: WikiSaveRequest):
    """保存 wiki 条目。source=human → 直接生效;AI/probe → draft(§4.3)。"""
    from ....knowledge.wiki_pipeline import save_wiki_entry

    _, kroot = _wiki_knowledge_root(slug)
    try:
        entry = save_wiki_entry(
            kroot,
            title=req.title,
            content=req.content,
            source=req.source,
            summary=req.summary,
            evidence_refs=req.evidence_refs,
            related=req.related,
            source_refs=req.source_refs,
            slug=req.slug,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return entry


@router.post("/api/workspace-entities/{slug}/wiki/{wiki_id}/review")
def api_review_wiki(slug: str, wiki_id: str, req: WikiReviewRequest):
    """人工确认:approve → merged(写 verified_at);reject → 回 draft + reason。"""
    from ....knowledge.wiki_pipeline import review_wiki

    _, kroot = _wiki_knowledge_root(slug)
    try:
        return review_wiki(
            kroot, wiki_id, req.decision, reviewer=req.reviewer, reason=req.reason
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Wiki 条目不存在: {wiki_id}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/api/workspace-entities/{slug}/wiki/{wiki_id}")
def api_delete_wiki(slug: str, wiki_id: str):
    from ....knowledge.wiki_pipeline import delete_wiki

    _, kroot = _wiki_knowledge_root(slug)
    return {"ok": delete_wiki(kroot, wiki_id)}


@router.post("/api/workspace-entities/{slug}/wiki/generate")
def api_generate_wiki_drafts(slug: str):
    """跑 probe 生成/刷新 wiki draft(gen_wiki step 的 API 形态)。"""
    from ...workspace.workspace_registry import _knowledge_root_for
    from ....knowledge.wiki_pipeline import generate_wiki_drafts

    ws = _resolve_workspace_or_404(slug)
    kroot = _knowledge_root_for(ws)
    if not kroot:
        raise HTTPException(status_code=400, detail="Workspace 无知识根目录")
    ws_dict = {
        "id": ws["id"],
        "name": ws["name"],
        "knowledge_root": kroot,
        "repos": db.list_projects_by_workspace(ws["id"]),
    }
    drafts = generate_wiki_drafts(ws_dict)
    return {"created": len(drafts), "drafts": [d["id"] for d in drafts]}
