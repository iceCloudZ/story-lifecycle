"""FastAPI server — REST API for story management and terminal access."""

import asyncio
import os
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..db import models as db
from ..db.models import init_db
from ..terminal import ttyd
from .graph import start_story_async, recover_orphan_stories


# -------- WebSocket broadcast --------

_ws_clients: list[WebSocket] = []


async def ws_broadcast(msg: dict):
    """Broadcast a message to all connected WebSocket clients."""
    dead = []
    for ws in _ws_clients:
        try:
            await ws.send_json(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.remove(ws)


# -------- request/response models --------


class CreateStoryRequest(BaseModel):
    key: str
    title: str = ""
    content: str = ""
    profile: str = "minimal"
    workspace: str = ""


class AdvanceRequest(BaseModel):
    description: str = ""


class SkipRequest(BaseModel):
    reason: str = ""


class CreateSubStoryRequest(BaseModel):
    sub_type: str = ""
    start_stage: str = ""
    description: str


class AbortRequest(BaseModel):
    reason: str = "User abort"


class ResumeParentRequest(BaseModel):
    strategy: str = "pause_subs"  # pause_subs | abort_subs


class ReviewFeedbackRequest(BaseModel):
    content: str


class DecideFindingRequest(BaseModel):
    action: str  # accept, reject, defer, downgrade, mark_verified
    reason: str = ""
    verification_event_id: int | None = None


# -------- app lifecycle --------


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    recovered = recover_orphan_stories()
    if recovered:
        import logging

        logging.getLogger("story-lifecycle").info(
            f"Recovered {recovered} active stories after restart"
        )
    ttyd.cleanup_orphaned_sessions()
    yield


app = FastAPI(title="Story Lifecycle Manager", version="0.1.0", lifespan=lifespan)


# -------- WebSocket endpoints --------


@app.websocket("/ws/stories")
async def ws_stories(ws: WebSocket):
    await ws.accept()
    _ws_clients.append(ws)
    try:
        await ws.send_json({"type": "stories", "data": _story_list_json()})
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if ws in _ws_clients:
            _ws_clients.remove(ws)


def _story_list_json() -> list[dict]:
    stories = db.list_active_stories()
    return [
        {
            "storyKey": s["story_key"],
            "title": s["title"],
            "currentStage": s["current_stage"],
            "status": s["status"],
            "profile": s["profile"],
            "executionCount": s["execution_count"],
            "updatedAt": s["updated_at"],
        }
        for s in stories
    ]


async def notify_story_update(story_key: str, status: str = "", stage: str = ""):
    """Call from graph nodes to push state changes to WS clients."""
    await ws_broadcast(
        {
            "type": "story_update",
            "data": {"storyKey": story_key, "status": status, "currentStage": stage},
        }
    )
    await ws_broadcast({"type": "stories", "data": _story_list_json()})


def notify_story_update_sync(story_key: str, status: str = "", stage: str = ""):
    """Thread-safe version for calling from graph worker threads."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(notify_story_update(story_key, status, stage))
    except RuntimeError:
        pass


# -------- story CRUD --------


@app.get("/api/story")
def list_stories():
    stories = db.list_active_stories()
    return JSONResponse(
        [
            {
                "storyKey": s["story_key"],
                "title": s["title"],
                "currentStage": s["current_stage"],
                "status": s["status"],
                "complexity": s["complexity"],
                "workspace": s["workspace"],
                "profile": s["profile"],
                "executionCount": s["execution_count"],
                "updatedAt": s["updated_at"],
            }
            for s in stories
        ]
    )


@app.get("/api/story/{story_key}")
def get_story(story_key: str):
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")

    subs = db.get_sub_stories(story_key)
    sub_list = (
        [
            {
                "storyKey": sub["story_key"],
                "subType": sub.get("sub_type"),
                "status": sub["status"],
                "currentStage": sub["current_stage"],
            }
            for sub in subs
        ]
        if subs
        else []
    )

    return JSONResponse(
        {
            "storyKey": s["story_key"],
            "title": s["title"],
            "currentStage": s["current_stage"],
            "status": s["status"],
            "complexity": s["complexity"],
            "workspace": s["workspace"],
            "profile": s["profile"],
            "contextJson": s["context_json"],
            "executionCount": s["execution_count"],
            "lastError": s["last_error"],
            "updatedAt": s["updated_at"],
            "parentKey": s.get("parent_key"),
            "subType": s.get("sub_type"),
            "subs": sub_list,
        }
    )


@app.post("/api/story")
def create_story(req: CreateStoryRequest):
    from .service import create_and_start_story

    workspace = req.workspace or os.getcwd()
    prd_path = None
    if req.content:
        prd_dir = Path(workspace) / "prd"
        prd_dir.mkdir(exist_ok=True)
        prd_file = prd_dir / f"{req.key}.md"
        prd_file.write_text(req.content, encoding="utf-8")
        prd_path = str(prd_file)

    story_key = create_and_start_story(
        story_key=req.key,
        title=req.title,
        profile=req.profile,
        workspace=workspace,
        prd_path=prd_path,
    )

    start_story_async(story_key)

    s = db.get_story(story_key)
    return JSONResponse(
        {
            "id": s["id"],
            "storyKey": s["story_key"],
            "title": s["title"],
            "currentStage": s["current_stage"],
            "status": s["status"],
            "workspace": s["workspace"],
        }
    )


@app.put("/api/story/{story_key}/advance")
def advance_story(story_key: str, req: AdvanceRequest = None):
    """Manually advance a story (for confirm stages or error recovery)."""
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")

    # Resume from paused
    if s["status"] == "paused":
        db.update_story(story_key, status="active")
        start_story_async(story_key)
        return {"ok": True, "status": "resumed"}

    return {"ok": True}


@app.put("/api/story/{story_key}/skip/{stage}")
def skip_stage(story_key: str, stage: str, req: SkipRequest = None):
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")

    reason = req.reason if req else "Manual skip"
    db.log_stage(story_key, stage, "skip", reason)
    db.update_story(story_key, status="active")

    # Recover: re-submit to thread pool
    start_story_async(story_key)
    return {"ok": True}


@app.put("/api/story/{story_key}/fail")
def fail_story(story_key: str, req: SkipRequest = None):
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")
    db.update_story(
        story_key, status="blocked", last_error=req.reason if req else "Manual fail"
    )
    return {"ok": True}


@app.delete("/api/story/{story_key}")
def delete_story(story_key: str):
    db.delete_story(story_key)
    ttyd.stop_ttyd(story_key)
    return {"ok": True}


@app.post("/api/story/{parent_key}/sub")
def api_create_sub_story(parent_key: str, req: CreateSubStoryRequest):
    from .service import create_sub_story as svc_create_sub

    try:
        sub_key = svc_create_sub(
            parent_key=parent_key,
            sub_type=req.sub_type or None,
            start_stage=req.start_stage or None,
            description=req.description,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    s = db.get_story(sub_key)
    return JSONResponse(
        {
            "storyKey": s["story_key"],
            "title": s["title"],
            "subType": s.get("sub_type"),
            "parentKey": parent_key,
            "currentStage": s["current_stage"],
            "status": s["status"],
        }
    )


@app.post("/api/story/{story_key}/abort")
def api_abort_story(story_key: str, req: AbortRequest = None):
    from .service import abort_story as svc_abort

    try:
        svc_abort(story_key, req.reason if req else "User abort")
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@app.put("/api/story/{parent_key}/resume")
def api_resume_parent(parent_key: str, req: ResumeParentRequest = None):
    from .service import resume_parent as svc_resume

    strategy = req.strategy if req else "pause_subs"
    try:
        svc_resume(parent_key, strategy=strategy)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


# -------- session / terminal --------


@app.get("/api/session/terminal/{story_key}")
def get_terminal(story_key: str):
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")

    url = ttyd.ensure_ttyd(story_key, s["workspace"])
    return JSONResponse(
        {
            "url": url,
            "port": ttyd._story_ports.get(story_key, 0),
            "session": ttyd.session_name(story_key),
        }
    )


@app.get("/api/session/health")
def health():
    return {"status": "ok", "version": "0.1.0"}


# -------- observability / debug --------


@app.get("/api/story/{story_key}/debug")
def debug_story(story_key: str, limit: int = 50, event_type: str = ""):
    """Read-only debug endpoint. Returns observability events and quality status.

    Query params:
        limit: Max recentEvents (default 50). Applies at DB level.
        event_type: Filter recentEvents to this type at DB level.
    """
    from .observability import build_debug_response

    response = build_debug_response(
        story_key, recent_limit=limit, event_type=event_type
    )
    if "error" in response:
        raise HTTPException(404, response["error"])

    return response


# -------- quality endpoints --------


@app.get("/api/story/{story_key}/findings")
async def get_findings(story_key: str):
    findings = db.get_open_findings(story_key)
    return {"findings": findings}


@app.get("/api/story/{story_key}/quality")
async def get_quality_status(story_key: str):
    from .quality import check_dor, check_dod

    findings = db.get_open_findings(story_key)
    patterns = db.get_active_learned_patterns(limit=10)
    verifications = db.get_recent_quality_events(
        story_key, ["verification_result"], limit=3
    )
    return {
        "findings": findings,
        "learned_patterns": patterns,
        "verifications": verifications,
        "dor": check_dor(story_key, "", record=False),
        "dod": check_dod(story_key, ""),
    }


@app.get("/api/patterns")
async def get_patterns(status: str = "active"):
    if status == "proposed":
        return {"patterns": db.get_proposed_learned_patterns()}
    return {"patterns": db.get_active_learned_patterns()}


@app.put("/api/patterns/{pattern_id}/approve")
async def approve_pattern_endpoint(pattern_id: str):
    from fastapi import HTTPException

    from .quality import approve_pattern, activate_pattern

    p = db.get_learned_pattern(pattern_id)
    if p is None:
        raise HTTPException(status_code=404, detail=f"Pattern not found: {pattern_id}")
    if p["status"] != "proposed":
        raise HTTPException(
            status_code=409,
            detail=f"Pattern {pattern_id} is '{p['status']}', must be 'proposed'",
        )

    approve_pattern(pattern_id)
    activate_pattern(pattern_id)
    return {"status": "active"}


@app.put("/api/patterns/{pattern_id}/reject")
async def reject_pattern_endpoint(pattern_id: str):
    from fastapi import HTTPException

    from .quality import reject_pattern

    p = db.get_learned_pattern(pattern_id)
    if p is None:
        raise HTTPException(status_code=404, detail=f"Pattern not found: {pattern_id}")
    if p["status"] != "proposed":
        raise HTTPException(
            status_code=409,
            detail=f"Pattern {pattern_id} is '{p['status']}', must be 'proposed'",
        )

    reject_pattern(pattern_id)
    return {"status": "rejected"}


# -------- review feedback endpoints --------


@app.post("/api/story/{story_key}/review-feedback")
def api_import_review_feedback(story_key: str, req: ReviewFeedbackRequest):
    """Import review feedback content and extract candidate findings."""
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")

    if not req.content.strip():
        raise HTTPException(400, "Review content is empty")

    from .review_feedback import import_review

    result = import_review(story_key, req.content)
    return {
        "imported": result["imported"],
        "skipped": result["skipped"],
        "mode": result["mode"],
        "warnings": result["warnings"],
    }


@app.get("/api/story/{story_key}/review-feedback")
def api_list_review_feedback(story_key: str):
    """List review feedback findings for a story."""
    findings = db.get_findings_by_story(story_key)
    review_findings = [f for f in findings if f["source"] == "review_feedback"]
    db.enrich_findings_with_evidence(review_findings)
    return {"findings": review_findings}


@app.put("/api/finding/{finding_id}/decide")
def api_decide_finding(finding_id: str, req: DecideFindingRequest):
    """Make a decision on a finding: accept/reject/defer/downgrade/mark_verified."""
    from .quality import update_finding_status

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


@app.get("/api/approvals")
def api_approvals():
    """Get approval queue: all pending (open + accepted) findings with evidence."""
    findings = db.get_all_pending_findings()
    db.enrich_findings_with_evidence(findings)
    return {"findings": findings}


# -------- static frontend (must be last — catch-all mount) --------

_WEB_DIR = Path(__file__).parent.parent / "web"
if _WEB_DIR.is_dir() and any(_WEB_DIR.iterdir()):
    app.mount("/", StaticFiles(directory=str(_WEB_DIR), html=True), name="web")
