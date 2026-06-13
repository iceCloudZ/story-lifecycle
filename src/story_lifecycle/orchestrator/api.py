"""FastAPI server — REST API for story management and terminal access."""

import asyncio
import os
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..adapters import get_adapter
from ..db import models as db
from ..db.models import init_db
from ..terminal.pty import get_pty, ensure_agent_pty, kill_pty
from .graph import (
    start_story_async,
    recover_orphan_stories,
    resume_ready_interactive_stories,
)
from .nodes.profile_loader import resolve_profile


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
    autostart: bool = True


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
    watcher = asyncio.create_task(_watch_interactive_done_files())
    try:
        yield
    finally:
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass


async def _watch_interactive_done_files():
    while True:
        resume_ready_interactive_stories()
        await asyncio.sleep(1)


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


# -------- PTY WebSocket --------


@app.websocket("/ws/pty/{story_id}")
async def pty_ws(ws: WebSocket, story_id: str):
    """Bidirectional PTY stream: read output → push to xterm.js, recv input → write to PTY."""
    await ws.accept()

    pty = get_pty(story_id)
    if not pty:
        await ws.send_json({"type": "error", "message": "No PTY for this story"})
        await ws.close(code=4044)
        return

    async def read_and_send():
        while pty.alive:
            try:
                data = await asyncio.wait_for(pty._queue.get(), timeout=0.5)
                await ws.send_bytes(data)
            except asyncio.TimeoutError:
                continue
            except Exception:
                break
        # PTY exited
        try:
            await ws.send_json({"type": "exit"})
        except Exception:
            pass

    async def recv_and_write():
        while True:
            try:
                msg = await ws.receive()
            except Exception:
                break
            if "bytes" in msg and msg["bytes"]:
                pty.write(msg["bytes"])
            elif "text" in msg and msg["text"]:
                data = msg["text"]
                if data.startswith('{"type":"resize"'):
                    import json as _json

                    try:
                        r = _json.loads(data)
                        pty.resize(r.get("cols", 120), r.get("rows", 30))
                    except Exception:
                        pass
                    continue
                pty.write(data.encode("utf-8"))
            else:
                break

    try:
        await asyncio.gather(read_and_send(), recv_and_write())
    except Exception:
        pass


@app.post("/api/pty/{story_id}/spawn")
def api_spawn_pty(story_id: str):
    """Start or reuse the story's interactive agent PTY."""
    s = db.get_story(story_id)
    if not s:
        raise HTTPException(404, "Story not found")

    return _ensure_story_agent_pty(s)


def _ensure_story_agent_pty(story: dict) -> dict:
    workspace = story.get("workspace", "")
    if not workspace or not Path(workspace).exists():
        raise HTTPException(400, "Invalid workspace")

    profile = resolve_profile(story.get("profile", "minimal"))
    stage_cfg = profile.stage(story.get("current_stage", "design"))
    adapter_name = stage_cfg.cli or profile.cli or "claude"
    model = stage_cfg.model or profile.model or "sonnet"
    existing = get_pty(story["story_key"])
    reused = bool(existing and existing.alive and existing.purpose == "agent")

    adapter = get_adapter(adapter_name)
    ensure_agent_pty(
        story["story_key"],
        adapter.interactive_launch_cmd(model),
        workspace,
        "",
    )
    return {
        "ok": True,
        "reused": reused,
        "purpose": "agent",
        "adapter": adapter_name,
        "model": model,
    }


@app.delete("/api/pty/{story_id}")
def api_kill_pty(story_id: str):
    """Kill PTY for a story."""
    kill_pty(story_id)
    return {"ok": True}


# -------- story CRUD --------


@app.get("/api/story")
def list_stories(
    status: str = "",
    overdue: bool = False,
    show_all: bool = False,
    tapd_type: str = "",
    show_completed: bool = False,
):
    """List stories with optional filters.

    Query params:
        status: Filter by status (active, paused, completed, failed)
        overdue: Only show stories past their deadline
        show_all: Include completed/failed stories
        tapd_type: Filter by type (story/bug/subtask)
        show_completed: Show completed TAPD stories (default hides resolved/rejected/closed)
    """
    if show_all:
        stories = (
            db.list_active_stories()
            + db.list_completed_stories(limit=100)
            + db.list_candidate_stories()
        )
    else:
        stories = db.list_active_stories() + db.list_candidate_stories()

    if status:
        stories = [s for s in stories if s["status"] == status]

    if tapd_type:
        stories = [s for s in stories if s.get("tapd_type") == tapd_type]

    if not show_completed:
        COMPLETED_STATES = {"resolved", "rejected", "closed"}
        stories = [s for s in stories if s.get("tapd_status") not in COMPLETED_STATES]

    if overdue:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        stories = [s for s in stories if s.get("deadline") and s["deadline"][:10] < now]

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
                "deadline": s.get("deadline"),
                "priority": s.get("priority"),
                "owner": s.get("owner"),
                "tapdStatus": s.get("tapd_status"),
                "tapdUrl": s.get("tapd_url"),
                "tapdType": s.get("tapd_type"),
                "intakeState": s.get("intake_state"),
                "sourceType": s.get("source_type"),
                "sourceId": s.get("source_id"),
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
            "deadline": s.get("deadline"),
            "priority": s.get("priority"),
            "owner": s.get("owner"),
            "branchesJson": s.get("branches_json", "[]"),
            "tapdStatus": s.get("tapd_status"),
            "tapdUrl": s.get("tapd_url"),
            "sourceType": s.get("source_type"),
            "sourceId": s.get("source_id"),
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

    if req.autostart:
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
    kill_pty(story_key)
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


# -------- Per-Story WebSocket --------


_per_story_ws: dict[str, list[WebSocket]] = {}


@app.websocket("/ws/story/{story_key}")
async def ws_story(ws: WebSocket, story_key: str):
    """Per-story WebSocket — granular real-time events for a single story."""
    await ws.accept()
    _per_story_ws.setdefault(story_key, []).append(ws)
    try:
        # Send initial state
        s = db.get_story(story_key)
        if s:
            await ws.send_json(
                {
                    "type": "story_state",
                    "data": {
                        "storyKey": s["story_key"],
                        "status": s["status"],
                        "currentStage": s["current_stage"],
                        "lastError": s.get("last_error"),
                        "executionCount": s["execution_count"],
                    },
                }
            )
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        clients = _per_story_ws.get(story_key, [])
        if ws in clients:
            clients.remove(ws)


async def notify_per_story(story_key: str, msg: dict):
    """Send a message to all WebSocket clients subscribed to a specific story."""
    clients = _per_story_ws.get(story_key, [])
    dead = []
    for ws in clients:
        try:
            await ws.send_json(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.remove(ws)


# -------- session / terminal --------


@app.get("/api/session/terminal/{story_key}")
def get_terminal(story_key: str):
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")

    info = _ensure_story_agent_pty(s)
    info["url"] = f"/ws/pty/{story_key}"
    return JSONResponse(info)


@app.get("/api/session/health")
def health():
    return {"status": "ok", "version": "0.1.0"}


# -------- Timeline API (Task 3.1) --------


@app.get("/api/story/{story_key}/timeline")
def get_story_timeline(story_key: str):
    """Return the complete stage timeline for a story.

    Aggregates from stage_log + event_log to produce per-stage
    status, duration, plan/review summaries, gate decisions,
    loop rounds, and trajectory score.
    """
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")

    stage_logs = db.get_stage_logs(story_key, limit=200)
    events = db.get_story_events(story_key)

    # Group events by stage
    events_by_stage: dict[str, list[dict]] = {}
    for ev in events:
        stage = ev.get("stage", "")
        events_by_stage.setdefault(stage, []).append(ev)

    # Build per-stage timeline entries
    stages_seen: dict[str, dict] = {}
    for sl in stage_logs:
        stage = sl.get("stage", "")
        if stage not in stages_seen:
            stages_seen[stage] = {
                "stage": stage,
                "status": "",
                "started_at": None,
                "completed_at": None,
                "duration_ms": None,
                "plan_summary": "",
                "review_summary": "",
                "gate_decisions": [],
                "loop_rounds": 0,
                "trajectory_score": None,
                "events": [],
            }
        entry = stages_seen[stage]
        action = sl.get("action", "")
        if action == "complete":
            entry["status"] = "completed"
            entry["completed_at"] = sl.get("created_at")
        elif action == "retry":
            entry["status"] = "retrying"
        elif action == "skip":
            entry["status"] = "skipped"
            entry["completed_at"] = sl.get("created_at")
        elif action == "fail":
            entry["status"] = "failed"
            entry["completed_at"] = sl.get("created_at")
        elif action == "pause":
            entry["status"] = "paused"
        if not entry["started_at"]:
            entry["started_at"] = sl.get("created_at")

    # Fill from events
    for stage, stage_events in events_by_stage.items():
        if stage not in stages_seen:
            stages_seen[stage] = {
                "stage": stage,
                "status": "active",
                "started_at": None,
                "completed_at": None,
                "duration_ms": None,
                "plan_summary": "",
                "review_summary": "",
                "gate_decisions": [],
                "loop_rounds": 0,
                "trajectory_score": None,
                "events": [],
            }
        entry = stages_seen[stage]
        for ev in stage_events:
            ev_type = ev.get("event_type", "")
            import json as _json

            payload = ev.get("payload")
            if isinstance(payload, str):
                try:
                    payload = _json.loads(payload)
                except Exception:
                    payload = {}
            if not isinstance(payload, dict):
                payload = {}

            if ev_type == "plan":
                if payload.get("summary"):
                    entry["plan_summary"] = payload["summary"]
                if payload.get("trajectory_score") is not None:
                    entry["trajectory_score"] = payload["trajectory_score"]
                if payload.get("loop_rounds"):
                    entry["loop_rounds"] = max(
                        entry["loop_rounds"], payload["loop_rounds"]
                    )
            elif ev_type == "review":
                if payload.get("summary"):
                    entry["review_summary"] = payload["summary"]
            elif ev_type == "gate_decision":
                entry["gate_decisions"].append(payload)

            # Key events summary
            if ev_type in (
                "plan",
                "review",
                "gate_decision",
                "route_decision",
                "node_error",
                "validation_failure",
            ):
                entry["events"].append(
                    {
                        "event_type": ev_type,
                        "created_at": ev.get("created_at"),
                        "summary": payload.get("summary", payload.get("reason", ""))[
                            :100
                        ],
                    }
                )

    # Compute duration for completed stages
    for entry in stages_seen.values():
        if entry["started_at"] and entry["completed_at"]:
            try:
                from datetime import datetime

                start = datetime.fromisoformat(entry["started_at"])
                end = datetime.fromisoformat(entry["completed_at"])
                entry["duration_ms"] = int((end - start).total_seconds() * 1000)
            except Exception:
                pass

    # Order stages by their first appearance in stage_logs
    stage_order = []
    for sl in reversed(stage_logs):
        stage = sl.get("stage", "")
        if stage and stage not in stage_order:
            stage_order.append(stage)
    stage_order.reverse()

    # Add any stages only in events
    for stage in stages_seen:
        if stage and stage not in stage_order:
            stage_order.append(stage)

    result_stages = [stages_seen[s] for s in stage_order if s in stages_seen]

    # Mark current stage
    for entry in result_stages:
        if entry["stage"] == s["current_stage"] and not entry["status"]:
            entry["status"] = s["status"]

    return {"story_key": story_key, "stages": result_stages}


# -------- Gate History API (Task 3.2) --------


@app.get("/api/story/{story_key}/gate-history")
def get_gate_history(story_key: str):
    """Return the gate decision chain for a story."""
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")

    events = db.get_story_events(story_key)
    decisions = []
    for ev in events:
        if ev.get("event_type") != "gate_decision":
            continue
        import json as _json

        payload = ev.get("payload")
        if isinstance(payload, str):
            try:
                payload = _json.loads(payload)
            except Exception:
                continue
        if not isinstance(payload, dict):
            continue
        decisions.append(
            {
                "decision_id": payload.get("decision_id", ""),
                "stage": ev.get("stage", ""),
                "decision": payload.get("decision", ""),
                "reason_code": payload.get("reason_code", ""),
                "human_message": payload.get("human_message", ""),
                "evidence": payload.get("evidence", {}),
                "allowed_actions": payload.get("allowed_actions", []),
                "created_at": ev.get("created_at", ""),
            }
        )

    # Also include gate_result table entries
    gate_results = db.get_gate_results(story_key, limit=100)
    for gr in gate_results:
        detail = gr.get("detail", "")
        import json as _json2

        try:
            detail_data = _json2.loads(detail) if detail else {}
        except Exception:
            detail_data = {}
        decisions.append(
            {
                "decision_id": detail_data.get("decision_id", ""),
                "stage": gr.get("stage", ""),
                "decision": gr.get("result", ""),
                "reason_code": detail_data.get("reason_code", ""),
                "human_message": "",
                "evidence": {},
                "allowed_actions": [],
                "created_at": gr.get("created_at", ""),
            }
        )

    return {"decisions": decisions}


# -------- Loop Trace API (Task 3.3) --------


@app.get("/api/story/{story_key}/loop-trace")
def get_loop_trace(story_key: str):
    """Return adversarial loop trace for a story."""
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")

    events = db.get_story_events(story_key)

    plan_rounds = []
    code_rounds = []

    for ev in events:
        import json as _json

        payload = ev.get("payload")
        if isinstance(payload, str):
            try:
                payload = _json.loads(payload)
            except Exception:
                continue
        if not isinstance(payload, dict):
            continue

        ev_type = ev.get("event_type", "")
        stage = ev.get("stage", "")

        # Plan loop rounds
        if ev_type == "plan" and payload.get("adversarial_loop"):
            plan_rounds.append(
                {
                    "stage": stage,
                    "loop_rounds": payload.get("loop_rounds", 0),
                    "loop_decision": payload.get("loop_decision", ""),
                    "summary": payload.get("summary", "")[:200],
                    "trajectory_score": payload.get("trajectory_score"),
                    "created_at": ev.get("created_at", ""),
                }
            )

        # Code review loop rounds
        if ev_type == "review" and payload.get("adversarial_loop"):
            code_rounds.append(
                {
                    "stage": stage,
                    "loop_rounds": payload.get("loop_rounds", 0),
                    "loop_decision": payload.get("loop_decision", ""),
                    "quality": payload.get("quality", ""),
                    "summary": payload.get("summary", "")[:200],
                    "issues_count": payload.get("issues_count", 0),
                    "trajectory_score": payload.get("trajectory_score"),
                    "created_at": ev.get("created_at", ""),
                }
            )

    return {
        "story_key": story_key,
        "plan_loop": {"rounds": plan_rounds},
        "code_loop": {"rounds": code_rounds},
    }


# -------- Findings API enhancement (Task 3.4) --------


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
async def get_findings(
    story_key: str,
    status: str = "",
    min_severity: str = "",
):
    """Return quality findings for a story with optional filters.

    Query params:
        status: Filter by finding status (open, accepted, fixed, verified, etc.)
        min_severity: Minimum severity threshold (high, medium, low)
    """
    findings = db.get_open_findings(story_key)

    # If status filter is specified, get all findings not just open
    if status and status != "open":
        findings = db.get_findings_by_story(story_key)
        findings = [f for f in findings if f.get("status") == status]

    # Severity filter
    severity_order = {"high": 3, "medium": 2, "low": 1}
    if min_severity:
        min_level = severity_order.get(min_severity, 0)
        findings = [
            f
            for f in findings
            if severity_order.get(f.get("severity", "low"), 0) >= min_level
        ]

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


# -------- Dependency Graph API (Task 3.5) --------


@app.get("/api/story/{story_key}/dependency-graph")
def get_dependency_graph(story_key: str):
    """Return sub-story DAG for a parent story."""
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")

    nodes = []
    edges = []

    # Add parent node
    nodes.append(
        {
            "key": story_key,
            "status": s["status"],
            "stage": s["current_stage"],
            "title": s.get("title", ""),
        }
    )

    # Add sub-story nodes
    subs = db.get_sub_stories(story_key) or []
    for sub in subs:
        sub_key = sub["story_key"]
        nodes.append(
            {
                "key": sub_key,
                "status": sub["status"],
                "stage": sub["current_stage"],
                "title": sub.get("title", ""),
                "sub_type": sub.get("sub_type", ""),
            }
        )
        # Edge from parent to sub
        edges.append({"from": story_key, "to": sub_key})

    # Check for deeper sub-stories (2 levels)
    for sub in subs:
        sub_key = sub["story_key"]
        deeper_subs = db.get_sub_stories(sub_key) or []
        for ds in deeper_subs:
            ds_key = ds["story_key"]
            # Avoid duplicate nodes
            if not any(n["key"] == ds_key for n in nodes):
                nodes.append(
                    {
                        "key": ds_key,
                        "status": ds["status"],
                        "stage": ds["current_stage"],
                        "title": ds.get("title", ""),
                        "sub_type": ds.get("sub_type", ""),
                    }
                )
            edges.append({"from": sub_key, "to": ds_key})

    return {"nodes": nodes, "edges": edges}


# -------- Patterns API enhancement (Task 3.7) --------


@app.post("/api/patterns/{pattern_id}/approve")
async def approve_pattern_endpoint_post(pattern_id: str):
    """Approve and activate a proposed pattern."""
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


@app.post("/api/patterns/{pattern_id}/reject")
async def reject_pattern_endpoint_post(pattern_id: str):
    """Reject a proposed pattern."""
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


# -------- observability / debug --------


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


# -------- TAPD Sync API --------


class SyncRequest(BaseModel):
    workspace: str = ""
    autostart: bool = True
    dry_run: bool = False
    status_only: bool = False
    fetch_all: bool = False


@app.post("/api/sync/tapd")
def api_sync_tapd(req: SyncRequest):
    """Trigger TAPD sync."""
    from ..sources.tapd_source import TapdSource

    config = _load_tapd_config()
    if not config:
        raise HTTPException(
            400, "TAPD not configured. Add 'tapd' section to config.yaml."
        )

    source = TapdSource(config)
    try:
        items = source.fetch_pending(fetch_all=req.fetch_all)
    except Exception as e:
        raise HTTPException(502, f"TAPD fetch failed: {e}")

    from .sync_service import sync_tapd

    result = sync_tapd(
        items,
        workspace=req.workspace or ".",
        dry_run=req.dry_run,
        status_only=req.status_only,
    )
    return result


@app.get("/api/sync/tapd/status")
def api_sync_status():
    """Get TAPD config status."""
    config = _load_tapd_config()
    return {
        "configured": bool(config),
        "workspace_id": config.get("workspace_id", ""),
    }


def _load_tapd_config() -> dict:
    import os
    from pathlib import Path
    import yaml

    home = os.environ.get("STORY_HOME", str(Path.home() / ".story-lifecycle"))
    config_file = Path(home) / "config.yaml"
    if not config_file.exists():
        return {}
    with open(config_file, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("tapd", {})


# -------- Context endpoints --------


@app.get("/api/story/{story_key}/context")
def api_get_context(story_key: str):
    """Get full ContextBundle for a story."""
    try:
        from .context.resolver import ContextResolver

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


class PutContextRequest(BaseModel):
    revision: int
    projects: list[dict] | None = None
    documents: list[dict] | None = None
    change_items: list[dict] | None = None


@app.put("/api/story/{story_key}/context")
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


@app.post("/api/story/{story_key}/context/refresh")
def api_refresh_context(story_key: str):
    """Trigger auto-discovery for a single story. Does NOT start AI."""
    from .context.auto_discovery import Scanner, Decider, Handler

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


@app.get("/api/story/{story_key}/context/snapshot")
def api_get_snapshot(story_key: str):
    """Get the latest context snapshot content."""
    from .context.snapshot import generate_snapshot

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


# -------- Project registry endpoints --------


class CreateProjectRequest(BaseModel):
    name: str
    repo_path: str
    default_branch: str = "main"
    remote_url: str = ""


@app.get("/api/projects")
def api_list_projects():
    """List all registered projects with fresh availability."""
    from .project_registry import check_project_availability

    projects = db.list_projects()
    # 刷新每个项目的 availability（轻量 git rev-parse）
    for p in projects:
        check_project_availability(p["id"])
    return {"projects": db.list_projects()}


@app.post("/api/projects")
def api_create_project(req: CreateProjectRequest):
    """Register a new project."""
    from .project_registry import register_project

    proj = register_project(
        name=req.name,
        repo_path=req.repo_path,
        default_branch=req.default_branch,
        remote_url=req.remote_url,
    )
    return proj


class UpdateProjectRequest(BaseModel):
    name: str | None = None
    repo_path: str | None = None
    default_branch: str | None = None
    remote_url: str | None = None


@app.put("/api/projects/{project_id}")
def api_update_project(project_id: int, req: UpdateProjectRequest):
    """Update a project."""
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="no fields to update")
    from .project_registry import update_project

    update_project(project_id, **updates)
    return db.get_project(project_id)


# -------- Worktree endpoints --------


class WorktreePrepareRequest(BaseModel):
    worktree_root: str = ""


@app.post("/api/story/{story_key}/worktrees/prepare")
def api_prepare_worktrees(
    story_key: str, req: WorktreePrepareRequest = WorktreePrepareRequest()
):
    """Prepare worktrees for all project bindings of a story."""
    from .worktree.handler import prepare_worktrees

    results = prepare_worktrees(story_key, worktree_root=req.worktree_root)
    return {"results": results}


@app.get("/api/story/{story_key}/worktrees/cleanup-preview")
def api_cleanup_preview(story_key: str):
    """Preview worktree cleanup for a story."""
    from .worktree.resolver import resolve_story_worktree
    from .delivery import can_cleanup_worktree

    worktree_states = resolve_story_worktree(story_key)
    can_clean, reason = can_cleanup_worktree(story_key)
    return {
        "worktrees": worktree_states,
        "can_cleanup": can_clean,
        "reason": reason,
    }


class CleanupRequest(BaseModel):
    project_id: int
    delivery_state: str = ""
    force: bool = False


@app.post("/api/story/{story_key}/worktrees/cleanup")
def api_cleanup_worktree(story_key: str, req: CleanupRequest):
    """Remove a worktree. Requires user confirmation."""
    from .worktree.handler import cleanup_worktree

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


# -------- Delivery artifact endpoints --------


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


@app.get("/api/story/{story_key}/delivery-artifacts")
def api_list_delivery_artifacts(story_key: str):
    """List all delivery artifacts for a story."""
    from .delivery import list_delivery_artifacts

    return {"artifacts": list_delivery_artifacts(story_key)}


@app.post("/api/story/{story_key}/delivery-artifacts")
def api_create_delivery_artifact(story_key: str, req: CreateDeliveryRequest):
    """Register a delivery artifact."""
    from .delivery import register_delivery

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


class UpdateDeliveryRequest(BaseModel):
    delivery_state: str | None = None
    source: str = "user"


@app.put("/api/story/{story_key}/delivery-artifacts/{artifact_id}")
def api_update_delivery(story_key: str, artifact_id: int, req: UpdateDeliveryRequest):
    """Update delivery artifact state."""
    from .delivery import update_delivery_state

    if req.delivery_state:
        try:
            return update_delivery_state(artifact_id, req.delivery_state, req.source)
        except PermissionError as e:
            raise HTTPException(status_code=403, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    return db.get_delivery_artifact(artifact_id)


# -------- Lifecycle endpoints --------


class StartStoryRequest(BaseModel):
    project_ids: list[int] = []


@app.post("/api/story/{story_key}/start")
def api_start_story(story_key: str, req: StartStoryRequest | None = None):
    """Start a story. Binds user-selected projects for candidate stories."""
    story = db.get_story(story_key)
    if not story:
        raise HTTPException(status_code=404, detail="story not found")

    intake_state = story.get("intake_state", "ready")
    req = req or StartStoryRequest()

    # Bind user-selected projects for candidate stories
    if intake_state == "candidate":
        sps = db.get_story_projects(story_key)
        if not sps and not req.project_ids:
            return JSONResponse(
                status_code=409,
                content={
                    "ok": False,
                    "reasonCode": "project_not_selected",
                    "message": "请选择要关联的项目",
                },
            )
        if req.project_ids:
            all_projects = {p["id"]: p for p in db.list_projects()}
            existing_pids = {sp["project_id"] for sp in sps}
            for pid in req.project_ids:
                if pid in existing_pids:
                    continue  # 跳过已绑定的项目
                proj = all_projects.get(pid)
                if proj:
                    db.bind_story_project(
                        story_key=story_key,
                        project_id=proj["id"],
                        branch=f"codex/{story_key}-{proj['name']}",
                        base_branch=proj.get("default_branch", "main"),
                        worktree_state="unprepared",
                        source="user",
                    )

        # Promote candidate to ready + activate
        db.update_story(story_key, intake_state="ready", status="active")

    start_story_async(story_key)
    return {"ok": True, "story_key": story_key}


@app.get("/api/story/{story_key}/tapd-writeback-suggestion")
def api_tapd_writeback_suggestion(story_key: str):
    """Generate TAPD writeback suggestion (read-only, P0)."""
    story = db.get_story(story_key)
    if not story:
        raise HTTPException(status_code=404, detail="story not found")

    suggestion = {
        "story_key": story_key,
        "current_status": story.get("tapd_status", ""),
        "local_status": story.get("status", ""),
        "suggested_action": "review_and_confirm",
        "note": "P0: TAPD writeback is read-only. User must manually update TAPD.",
    }
    return suggestion


# -------- helpers --------


def _get_story_documents(story_key: str) -> list[dict]:
    with db._db() as conn:
        rows = conn.execute(
            "SELECT * FROM story_document WHERE story_key = ? ORDER BY id",
            (story_key,),
        ).fetchall()
    return [dict(r) for r in rows]


def _get_story_change_items(story_key: str) -> list[dict]:
    with db._db() as conn:
        rows = conn.execute(
            "SELECT * FROM story_change_item WHERE story_key = ? ORDER BY id",
            (story_key,),
        ).fetchall()
    return [dict(r) for r in rows]


# -------- static frontend (must be last — catch-all mount) --------

_WEB_DIR = Path(__file__).parent.parent / "web"
if _WEB_DIR.is_dir() and any(_WEB_DIR.iterdir()):
    app.mount("/", StaticFiles(directory=str(_WEB_DIR), html=True), name="web")
