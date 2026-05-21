"""FastAPI server — REST API for story management and terminal access."""

import os
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..db import models as db
from ..db.models import init_db
from ..terminal import ttyd
from .graph import start_story_async, recover_orphan_stories


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


# -------- app lifecycle --------

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    recovered = recover_orphan_stories()
    if recovered:
        import logging
        logging.getLogger("story-lifecycle").info(
            f"Recovered {recovered} active stories after restart")
    ttyd.cleanup_orphaned_sessions()
    yield


app = FastAPI(title="Story Lifecycle Manager", version="0.1.0", lifespan=lifespan)


# -------- story CRUD --------

@app.get("/api/story")
def list_stories():
    stories = db.list_active_stories()
    return JSONResponse([{
        "storyKey": s["story_key"],
        "title": s["title"],
        "currentStage": s["current_stage"],
        "status": s["status"],
        "complexity": s["complexity"],
        "workspace": s["workspace"],
        "profile": s["profile"],
        "executionCount": s["execution_count"],
        "updatedAt": s["updated_at"],
    } for s in stories])


@app.get("/api/story/{story_key}")
def get_story(story_key: str):
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")
    return JSONResponse({
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
    })


@app.post("/api/story")
def create_story(req: CreateStoryRequest):
    workspace = req.workspace or os.getcwd()  # Specified or CWD

    # Save PRD content if provided
    if req.content:
        prd_dir = Path(workspace) / "prd"
        prd_dir.mkdir(exist_ok=True)
        prd_file = prd_dir / f"{req.key}.md"
        prd_file.write_text(req.content, encoding="utf-8")

    s = db.create_story(
        story_key=req.key,
        title=req.title,
        workspace=workspace,
        profile=req.profile,
        current_stage="design",  # minimal profile starts at design
    )

    if req.content:
        db.update_context(req.key, "prd_path", str(Path(workspace) / "prd" / f"{req.key}.md"))

    # Fire and forget — start execution in background
    start_story_async(req.key)

    return JSONResponse({
        "id": s["id"],
        "storyKey": s["story_key"],
        "title": s["title"],
        "currentStage": s["current_stage"],
        "status": s["status"],
        "workspace": s["workspace"],
    })


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
    db.update_story(story_key, status="blocked",
                    last_error=req.reason if req else "Manual fail")
    return {"ok": True}


@app.delete("/api/story/{story_key}")
def delete_story(story_key: str):
    db.delete_story(story_key)
    ttyd.stop_ttyd(story_key)
    return {"ok": True}


# -------- session / terminal --------

@app.get("/api/session/terminal/{story_key}")
def get_terminal(story_key: str):
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")

    url = ttyd.ensure_ttyd(story_key, s["workspace"])
    return JSONResponse({
        "url": url,
        "port": ttyd._story_ports.get(story_key, 0),
        "session": ttyd.session_name(story_key),
    })


@app.get("/api/session/health")
def health():
    return {"status": "ok", "version": "0.1.0"}
