"""routers/documents — documents domain API（设计15 阶段C 拆自 api.py）。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from ....infra.db import models as db
from pydantic import BaseModel

log = logging.getLogger("story-lifecycle.api.documents")

router = APIRouter(tags=["documents"])


class SaveDocRequest(BaseModel):
    content: str
    change_reason: str  # required — every save must say why
    author: str = "user"
    title: str = ""


class RollbackDocRequest(BaseModel):
    reason: str
    author: str = "user"


@router.get("/api/story/{story_key}/docs")
def api_list_story_docs(story_key: str):
    if not db.get_story(story_key):
        raise HTTPException(status_code=404, detail=f"story not found: {story_key}")
    return {"docs": db.list_story_docs(story_key)}


@router.get("/api/story/{story_key}/docs/{doc_type}")
def api_get_story_doc(story_key: str, doc_type: str):
    if not db.get_story(story_key):
        raise HTTPException(status_code=404, detail=f"story not found: {story_key}")
    doc = db.get_story_doc(story_key, doc_type)
    if not doc:
        raise HTTPException(status_code=404, detail=f"doc not found: {doc_type}")
    return doc


@router.put("/api/story/{story_key}/docs/{doc_type}")
def api_save_story_doc(story_key: str, doc_type: str, req: SaveDocRequest):
    if not db.get_story(story_key):
        raise HTTPException(status_code=404, detail=f"story not found: {story_key}")
    if not req.content.strip():
        raise HTTPException(status_code=400, detail="content must not be empty")
    if not req.change_reason.strip():
        raise HTTPException(status_code=400, detail="change_reason is required")
    version = db.upsert_story_doc(
        story_key, doc_type, req.content, req.change_reason, req.author, req.title
    )
    # sync to local .md cache (best-effort; workspace/title from story row)
    try:
        story = db.get_story(story_key) or {}
        workspace = story.get("workspace") or ""
        title = story.get("title") or req.title or ""
        if workspace:
            from ....infra.doc_sync import sync_doc_to_local

            sync_doc_to_local(
                story_key, doc_type, req.content, version, workspace, title
            )
            # for prd, also update context_json.prd_path so the execution layer
            # and existing /plan endpoints keep working
            if doc_type == "prd":
                from ....infra.story_paths import story_doc_path

                prd_path = story_doc_path(workspace, story_key, doc_type, title)
                db.update_context(story_key, "prd_path", str(prd_path))
                db.bump_context_revision(story_key)
    except Exception as exc:  # local sync failure is non-fatal (DB is truth)
        log.warning("doc local-sync failed for %s/%s: %s", story_key, doc_type, exc)
    return db.get_story_doc(story_key, doc_type)


@router.get("/api/story/{story_key}/docs/{doc_type}/versions")
def api_list_doc_versions(story_key: str, doc_type: str):
    if not db.get_story(story_key):
        raise HTTPException(status_code=404, detail=f"story not found: {story_key}")
    return {"versions": db.list_story_doc_versions(story_key, doc_type)}


@router.get("/api/story/{story_key}/docs/{doc_type}/versions/{version}")
def api_get_doc_version(story_key: str, doc_type: str, version: int):
    if not db.get_story(story_key):
        raise HTTPException(status_code=404, detail=f"story not found: {story_key}")
    row = db.get_story_doc_version(story_key, doc_type, version)
    if not row:
        raise HTTPException(status_code=404, detail=f"version {version} not found")
    return row


@router.get("/api/story/{story_key}/docs/{doc_type}/diff")
def api_diff_doc_versions(story_key: str, doc_type: str, a: int, b: int):
    """Unified diff between version a and version b (b is the 'new' side)."""
    if not db.get_story(story_key):
        raise HTTPException(status_code=404, detail=f"story not found: {story_key}")
    va = db.get_story_doc_version(story_key, doc_type, a)
    vb = db.get_story_doc_version(story_key, doc_type, b)
    if not va or not vb:
        missing = "a" if not va else "b"
        raise HTTPException(status_code=404, detail=f"version {missing} not found")
    import difflib

    diff = "".join(
        difflib.unified_diff(
            (va["content"] or "").splitlines(keepends=True),
            (vb["content"] or "").splitlines(keepends=True),
            fromfile=f"v{a}",
            tofile=f"v{b}",
        )
    )
    return {"diff": diff, "a": a, "b": b}


@router.post("/api/story/{story_key}/docs/{doc_type}/rollback/{version}")
def api_rollback_doc(
    story_key: str, doc_type: str, version: int, req: RollbackDocRequest
):
    if not db.get_story(story_key):
        raise HTTPException(status_code=404, detail=f"story not found: {story_key}")
    if not req.reason.strip():
        raise HTTPException(status_code=400, detail="reason is required")
    try:
        new_v = db.rollback_story_doc(
            story_key, doc_type, version, req.reason, req.author
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    # sync rolled-back content to local cache
    try:
        story = db.get_story(story_key) or {}
        workspace = story.get("workspace") or ""
        title = story.get("title") or ""
        if workspace:
            from ....infra.doc_sync import sync_doc_to_local

            doc = db.get_story_doc(story_key, doc_type) or {}
            sync_doc_to_local(
                story_key,
                doc_type,
                doc.get("latest_content", ""),
                new_v,
                workspace,
                title,
            )
    except Exception as exc:
        log.warning("doc rollback local-sync failed: %s", exc)
    return db.get_story_doc(story_key, doc_type)


@router.post("/api/story/{story_key}/docs/{doc_type}/sync")
def api_sync_doc_from_local(story_key: str, doc_type: str):
    """把本地 .md 文件的改动拉回 DB(生成新版本)。

    半自动 agent 可能直接改本地 .md 绕过 DB(应为 doc_sync 契约禁止,但 agent 跑在
    外部终端不可控),导致 docs tab 显示旧版本。本端点:重算 .md 路径 → 读内容 → 跟
    DB 当前 latest_content 比 hash → 变了就 upsert_story_doc 生成 v+1。

    文件不存在/内容未变 → 200 + {synced:false}(前端据此提示「无需同步」)。
    """
    story = db.get_story(story_key)
    if not story:
        raise HTTPException(404, detail=f"story not found: {story_key}")
    workspace = story.get("workspace") or ""
    title = story.get("title") or ""
    if not workspace:
        raise HTTPException(400, detail="story has no workspace; cannot locate .md")

    from ....infra.doc_sync import _sha256
    from ....infra.story_paths import story_doc_path

    md_path = story_doc_path(workspace, story_key, doc_type, title)
    if not md_path.exists():
        raise HTTPException(404, detail=f"local .md not found: {md_path}")
    file_body = md_path.read_text(encoding="utf-8", errors="replace")

    doc = db.get_story_doc(story_key, doc_type) or {}
    db_body = doc.get("latest_content") or ""
    if _sha256(file_body) == _sha256(db_body):
        # 内容未变 —— 文件跟 DB 一致,无需新版本。
        return {
            "synced": False,
            "version": doc.get("current_version"),
            "reason": "unchanged",
        }

    new_v = db.upsert_story_doc(
        story_key,
        doc_type,
        file_body,
        change_reason="从本地文件同步(agent 直接改了 .md)",
        author="sync",
        title=title,
    )
    # 同步成功后也刷新 local_path + .meta(让 .meta 的 version/hash 跟上新 DB 版本)。
    try:
        from ....infra.doc_sync import sync_doc_to_local

        sync_doc_to_local(story_key, doc_type, file_body, new_v, workspace, title)
    except Exception as exc:
        log.warning("doc sync local-cache refresh failed: %s", exc)
    db.log_event(
        story_key,
        doc_type,
        "doc_synced_from_local",
        {"doc_type": doc_type, "version": new_v},
    )
    return {"synced": True, "version": new_v}


@router.get("/api/docs/search")
def api_search_docs(q: str, type: str = "", story: str = ""):
    if not q.strip():
        return {"results": []}
    hits = db.search_docs(q, doc_type=(type or None), story_key=(story or None))
    return {"query": q, "results": hits}


@router.put("/api/story/{story_key}/docs/{doc_type}/confirm")
def api_confirm_doc(story_key: str, doc_type: str):
    """人工确认文档(成果物 gate 用)。只有 user 手动调用 —— AI 不能自我确认。

    确认后该 doc 的 confirmed_by/confirmed_at 被写入,成果物 gate 视为 confirmed。
    """
    if not db.confirm_story_doc(story_key, doc_type):
        raise HTTPException(404, f"doc not found: {doc_type}")
    return {"ok": True}
