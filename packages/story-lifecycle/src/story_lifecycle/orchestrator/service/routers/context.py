"""routers/context — context domain API（设计15 阶段C 拆自 api.py）。"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from ....infra.db import models as db
from pydantic import BaseModel
from .._shared import _get_story_change_items, _get_story_documents

log = logging.getLogger("story-lifecycle.api.context")

router = APIRouter(tags=["context"])


class PutContextRequest(BaseModel):
    revision: int
    projects: list[dict] | None = None
    documents: list[dict] | None = None
    change_items: list[dict] | None = None


class AddDocumentRequest(BaseModel):
    kind: str
    ref: str = ""
    summary: str = ""
    evidence_ref: str = ""
    project_id: int | None = None


class SetBranchRequest(BaseModel):
    project_id: int
    branch: str
    worktree_path: str | None = None
    base_branch: str | None = None
    worktree_state: str | None = None


@router.get("/api/story/{story_key}/context")
def api_get_context(story_key: str):
    """Get full ContextBundle for a story."""
    try:
        from ...context.resolver import ContextResolver

        resolver = ContextResolver()
        bundle = resolver.resolve(story_key)
        errors = resolver.validate(bundle)
        return {
            "story": bundle.story,
            "projects": bundle.projects,
            "story_projects": bundle.story_projects,
            "documents": bundle.documents,
            "change_items": bundle.change_items,
            "delivery_artifacts": bundle.delivery_artifacts,
            "runtime_facts": bundle.runtime_facts,
            "profile": bundle.profile,
            "revision": bundle.revision,
            "validation_errors": errors,
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.put("/api/story/{story_key}/context")
def api_put_context(story_key: str, req: PutContextRequest):
    """Update story context. Fails on revision conflict (409)."""
    current_rev = db.get_context_revision(story_key)
    if req.revision != current_rev:
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "reasonCode": "context_revision_conflict",
                "current_revision": current_rev,
            },
        )
    # Apply updates
    new_rev = db.bump_context_revision(story_key)
    return {"ok": True, "revision": new_rev}


@router.post("/api/story/{story_key}/context/refresh")
def api_refresh_context(story_key: str):
    """Trigger auto-discovery for a single story. Does NOT start AI."""
    from ...context.auto_discovery import Scanner, Decider, Handler

    sps = db.get_story_projects(story_key)
    scanner = Scanner()
    decider = Decider()
    handler = Handler()

    results = []
    for sp in sps:
        project = db.get_project(sp["project_id"])
        if not project:
            continue
        scan_result = scanner.scan(story_key, sp, project)
        current_docs = _get_story_documents(story_key)
        current_cis = _get_story_change_items(story_key)
        mutation = decider.merge(current_docs, current_cis, scan_result)
        if mutation.new_documents or mutation.new_change_items:
            new_rev = handler.apply(story_key, mutation)
            results.append(
                {
                    "project_id": sp["project_id"],
                    "new_documents": len(mutation.new_documents),
                    "new_change_items": len(mutation.new_change_items),
                    "new_revision": new_rev,
                }
            )
        else:
            results.append(
                {
                    "project_id": sp["project_id"],
                    "new_documents": 0,
                    "new_change_items": 0,
                }
            )
    return {"results": results}


@router.get("/api/story/{story_key}/context/snapshot")
def api_get_snapshot(story_key: str):
    """Get the latest context snapshot content."""
    from ...context.snapshot import generate_snapshot

    result = generate_snapshot(story_key)
    snapshot_path = Path(result["snapshot_path"])
    if snapshot_path.exists():
        content = snapshot_path.read_text(encoding="utf-8")
        return {
            "path": str(snapshot_path),
            "revision": result["revision"],
            "content": content,
        }
    return {"path": str(snapshot_path), "revision": result["revision"], "content": ""}


@router.get("/api/analysis/prompts")
def api_export_prompt_analysis(
    status: str = "completed",
    stage: str = "",
    profile: str = "",
    since: str = "",
    limit: int = 50,
):
    """Export (prompt + outcome + llm_calls + events) tuples for offline
    prompt-quality analysis by an external AI.

    Returns one row per (story, stage) — the unit at which a prompt is
    assembled and a result is produced. Lets an external analyzer correlate
    prompt patterns with stage failures / retries / long durations, then feed
    findings back into template changes (offline, not real-time judge).

    Query params:
      - status: completed/failed/aborted/active/paused/all (default completed)
      - stage: design/build/verify/all (default all)
      - profile: single-pass/minimal/.../all (default all)
      - since: ISO datetime lower bound (default 30 days ago)
      - limit: 1-200 (default 50)
    """
    from ..observability.prompt_export import export_prompt_analysis

    # Clamp limit into a sane range.
    limit = max(1, min(200, int(limit or 50)))
    try:
        return export_prompt_analysis(
            status=status,
            stage=stage,
            profile=profile,
            since=since,
            limit=limit,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/story/{story_key}/prompts")
def api_get_prompts(story_key: str):
    """返回该 story 所有 stage 的 prompt 内容(复盘用)。

    提示词已落盘在 .story/context/<key>/prompt_<stage>.md(每次 stage launch
    时写),原先无查看入口——本端点遍历该目录,把每个 stage 的完整 prompt 拉出来。
    """
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")
    from ....infra.story_paths import safe_story_path

    workspace = s.get("workspace", "")
    context_dir = safe_story_path(workspace, ".story", "context", story_key)
    prompts = []
    if context_dir.exists():
        for f in sorted(context_dir.glob("prompt_*.md")):
            stage = f.stem.replace("prompt_", "")
            prompts.append(
                {
                    "stage": stage,
                    "path": str(f),
                    "content": f.read_text(encoding="utf-8"),
                }
            )
    return {"story_key": story_key, "prompts": prompts}


@router.get("/api/story/{story_key}/context/pack")
def api_get_context_pack(story_key: str, skill: str = ""):
    """Render a neutral mixed-density context pack for AI injection."""
    try:
        from ...context.pack import generate_pack

        return generate_pack(story_key, skill=skill)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/api/story/{story_key}/context/release-prompt")
def api_get_release_prompt(story_key: str):
    """Render a pre-release checklist prompt for a code AI."""
    if not db.get_story(story_key):
        raise HTTPException(status_code=404, detail=f"story not found: {story_key}")
    try:
        from ...context.release_prompt import generate_release_prompt

        return generate_release_prompt(story_key)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/api/story/{story_key}/context/post-release-prompt")
def api_get_post_release_prompt(story_key: str):
    """Render a post-release auto-verification prompt for a code AI."""
    if not db.get_story(story_key):
        raise HTTPException(status_code=404, detail=f"story not found: {story_key}")
    try:
        from ...context.release_prompt import generate_post_release_prompt

        return generate_post_release_prompt(story_key)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/api/story/{story_key}/context/documents")
def api_add_document(story_key: str, req: AddDocumentRequest):
    """Add a document (prd/spec/plan) — agent backfill."""
    story = db.get_story(story_key)
    if not story:
        raise HTTPException(status_code=404, detail=f"story not found: {story_key}")
    doc = db.create_document(
        story_key,
        req.kind,
        project_id=req.project_id,
        ref=req.ref,
        summary=req.summary,
        evidence_ref=req.evidence_ref,
        source="agent",
    )
    # Dual-write: also version the body into story_doc so the docs UI sees
    # agent-backfilled docs. Best-effort; if req.ref points to a real file we
    # read it, otherwise we skip versioning (legacy ref-only row remains).
    try:
        from ....infra.doc_sync import register_doc_dual_write

        register_doc_dual_write(
            story_key,
            req.kind,
            req.ref,
            change_reason=f"Agent 回填: {req.summary or req.kind}",
            author="agent",
            workspace=story.get("workspace") or "",
            summary=req.summary,
            source="agent",
            verification_state="unverified",
        )
    except Exception as exc:  # noqa: BLE001 — versioning is best-effort
        log.debug("[%s] doc backfill dual-write skipped: %s", story_key, exc)
    db.bump_context_revision(story_key)
    return doc


@router.put("/api/story/{story_key}/context/branch")
def api_set_branch(story_key: str, req: SetBranchRequest):
    """Create or update a story-project branch binding — agent backfill.

    worktree_path semantics: omitted (None) → untouched; explicit "" → clear
    the binding's worktree_path to NULL (releases a main checkout); a real
    path → set it (conflict with an active occupant → 409).
    worktree_state (e.g. 'available') lets agent-driven flows that prepare the
    branch themselves mark the binding ready without the worktree handler."""
    if not db.get_story(story_key):
        raise HTTPException(status_code=404, detail=f"story not found: {story_key}")
    try:
        existing = db.get_story_project(story_key, req.project_id)
        fields: dict = {"branch": req.branch}
        if req.worktree_path is None:
            pass  # omitted → don't touch worktree_path
        elif req.worktree_path == "":
            fields["worktree_path"] = None  # explicit clear → release the path
        else:
            fields["worktree_path"] = req.worktree_path
        if req.base_branch is not None:
            fields["base_branch"] = req.base_branch
        if req.worktree_state:
            fields["worktree_state"] = req.worktree_state
        if existing:
            db.update_story_project(story_key, req.project_id, **fields)
        else:
            fields.setdefault("base_branch", "main")
            db.bind_story_project(story_key, req.project_id, **fields)
        db.bump_context_revision(story_key)
        return db.get_story_project(story_key, req.project_id)
    except db.WorktreePathConflict as e:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"worktree_path {e.worktree_path} 已被 story "
                    f"{e.occupant.get('story_key')} 占用 "
                    f"(state={e.occupant.get('worktree_state')})。"
                    f"用 worktree_path='' 清空旧绑定,或 POST /worktrees/prepare 建独立 worktree。"
                ),
                "occupant_story_key": e.occupant.get("story_key"),
                "occupant_state": e.occupant.get("worktree_state"),
                "worktree_path": e.worktree_path,
            },
        )
