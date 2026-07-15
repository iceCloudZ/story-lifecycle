"""FastAPI server — REST API for story management and terminal access."""

import asyncio
import logging
import tempfile
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import (
    Body,
    FastAPI,
    File,
    Form,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ...knowledge.adapters import get_adapter
from ...sourcing.workspace_diff import get_story_workspace_diff
from ...infra.db import models as db
from ...infra.db.models import init_db
from ...infra.terminal.pty import (
    cleanup_all,
    get_pty,
    ensure_agent_pty,
    kill_pty,
    list_pty_sessions,
    spawn_pty,
)
from ..engine.graph import (
    start_story_async,
    recover_orphan_stories,
    resume_ready_interactive_stories,
    force_stop_story,
)
from ..engine.profile_loader import resolve_profile
from ..engine import planner


log = logging.getLogger("story-lifecycle.api")


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


class SetReleaseTrainRequest(BaseModel):
    train: str | None = None


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


class CreateGateResultRequest(BaseModel):
    stage: str
    gate_name: str
    result: str
    summary: str = ""
    evidence_ref: str = ""
    evidence: dict = Field(default_factory=dict)


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
        # Clean PTY teardown on shutdown: ask each agent to `/exit` first so
        # claude flushes its transcript (a complete transcript is what makes
        # --resume pick up full history), force-killing any that don't exit in
        # time. Runs in a worker thread because cleanup_all blocks (polls
        # pty.alive up to _CLEAN_EXIT_TIMEOUT per PTY). Best-effort — if uvicorn
        # hard-cuts shutdown the atexit backstop still fires. See handoff §12.
        try:
            await asyncio.to_thread(cleanup_all)
        except Exception:
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


def _serialize_story_summary(s: dict) -> dict:
    """camelCase summary of a story for list views — REST /api/story and the
    /ws/stories push share this so the two payloads can't drift. (The WS version
    previously omitted tapdType/intakeState, leaving the Dashboard's filters
    matching nothing — see the dashboard-zero-stories bug.)"""
    return {
        "storyKey": s["story_key"],
        "title": s["title"],
        "currentStage": s["current_stage"],
        "status": s["status"],
        "complexity": s.get("complexity"),
        "workspace": s.get("workspace"),
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
        "parentKey": s.get("parent_key"),
        "lifecycleState": s.get("lifecycle_state"),
        "releaseTrain": s.get("release_train"),
        "isTest": bool(s.get("is_test")),
    }


def _story_list_json() -> list[dict]:
    # Same gathering + serialization as the REST /api/story endpoint, so the
    # WS-pushed list and the REST list are identical (incl. candidate stories).
    return [_serialize_story_summary(s) for s in db.list_visible_stories()]


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


# ---- PTY WebSocket (supports both old single and new multi-session paths) ----


async def _pty_ws_handler(ws: WebSocket, story_id: str, session_id: str = ""):
    """Shared PTY WebSocket handler.

    Close-code semantics (product decision: distinguish terminal death from
    transient errors so the UI stops reconnecting to dead sessions):

    - 4404: session does not exist. The frontend should NOT auto-reconnect.
    - 1000: PTY existed but the underlying process has already exited. The
            frontend should show "process exited" and stop reconnecting.
    - 1011: internal server error during streaming. The frontend may retry
            with exponential backoff.
    """
    await ws.accept()

    pty = get_pty(story_id, session_id)
    if not pty:
        await ws.send_json(
            {
                "type": "error",
                "code": "session_not_found",
                "message": "No PTY session for this story",
            }
        )
        await ws.close(code=4404)
        return

    if not pty.alive:
        await ws.send_json(
            {
                "type": "exit",
                "reason": "process_ended",
                "message": "PTY process has already exited",
            }
        )
        await ws.close(code=1000)
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
        try:
            await ws.send_json({"type": "exit", "reason": "process_ended"})
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


@app.websocket("/ws/pty/{story_id}/{session_id}")
async def pty_ws_multi(ws: WebSocket, story_id: str, session_id: str):
    """Multi-session PTY WebSocket."""
    await _pty_ws_handler(ws, story_id, session_id)


@app.websocket("/ws/pty/{story_id}")
async def pty_ws(ws: WebSocket, story_id: str):
    """Legacy single-PTY WebSocket."""
    await _pty_ws_handler(ws, story_id, "")


# ---- Multi-Session Management API ----


class SpawnSessionRequest(BaseModel):
    adapter: str = "claude"
    model: str = ""


@app.get("/api/story/{story_key}/sessions")
def api_list_sessions(story_key: str):
    """List all PTY sessions for a story."""
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")
    return {"sessions": list_pty_sessions(story_key)}


@app.post("/api/story/{story_key}/sessions/spawn")
def api_spawn_session(story_key: str, req: SpawnSessionRequest = None):
    """Spawn a new PTY session for a story."""
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")

    workspace = s.get("workspace", "")
    if not workspace or not Path(workspace).exists():
        raise HTTPException(400, "Invalid workspace")

    req = req or SpawnSessionRequest()
    adapter = get_adapter(req.adapter or "claude")
    model = req.model or "sonnet"
    # 启动(NEW)或续上(RESUME)—— 同 _ensure_story_agent_pty 路径(claude "query" seed /
    # claude --resume <uuid>)。前端「启动终端」走这个端点。
    command, is_resume = _build_stage_launch_cmd(s, adapter, model)
    session_id, _ = spawn_pty(
        story_key, command, workspace, purpose=req.adapter or "claude"
    )
    return {"session_id": session_id, "ok": True, "resumed": is_resume}


@app.post("/api/pty/{story_id}/spawn")
def api_spawn_pty(story_id: str):
    """Start or reuse the story's interactive agent PTY (legacy, single-session)."""
    s = db.get_story(story_id)
    if not s:
        raise HTTPException(404, "Story not found")
    return _ensure_story_agent_pty(s)


def _build_interactive_stage_prompt(story: dict, stage: str) -> str:
    """构建交互终端 spawn 时注入的 stage prompt(复用自主路径 ``_build_cli_prompt``)。

    让点「启动终端」后 claude 直接拿到 design/build 任务上下文(需求 PRD 路径 + 设计维度
    协议 + done 握手 + 项目仓库),人只管 steer(Esc 打断 + 打字纠偏),不用手打需求。
    之前 ``_ensure_story_agent_pty`` 传空 prompt → 起空白 ❯ = 这个 bug 的修。
    """
    from ...knowledge.context_providers import get_transcript_context
    from ...infra.paths import stage_done_file_rel
    from ..engine import planner
    import json as _json

    story_key = story["story_key"]
    workspace = story.get("workspace", "")
    try:
        ctx = _json.loads(story.get("context_json") or "{}")
    except (_json.JSONDecodeError, TypeError):
        ctx = {}
    profile_stages = {}
    try:
        rp = resolve_profile(story.get("profile", "minimal"))
        profile_stages = {n: c for n, c in rp.stages.items()}
    except Exception:
        pass
    stage_cfg = profile_stages.get(stage)
    focus = (
        stage_cfg.description if stage_cfg and hasattr(stage_cfg, "description") else ""
    ) or ""
    project_lines = []
    try:
        for sp in db.get_story_projects(story_key):
            proj = db.get_project(sp["project_id"])
            if proj:
                project_lines.append(
                    f"- 仓库 `{proj['repo_path']}`: 分支 `{sp['branch']}`, "
                    f"基线 `{sp.get('base_branch', 'main')}`"
                )
    except Exception:
        pass
    transcript_section = ""
    try:
        transcript_section = get_transcript_context(story_key, workspace, stage) or ""
    except Exception:
        pass
    return planner._build_cli_prompt(
        story_key=story_key,
        title=story.get("title", ""),
        stage=stage,
        focus=focus,
        done_file=stage_done_file_rel(story_key, stage),
        profile_stages=profile_stages,
        prd_path=ctx.get("prd_path", ""),
        project_section="\n".join(project_lines),
        workspace=workspace,
        transcript_section=transcript_section,
        interactive=True,  # 交互式 claude("query",无 MCP):逐问澄清改「终端问人」
    )


def _build_stage_launch_prompt(story: dict) -> str:
    """Build the short read-file instruction that seeds a spawn with the current
    stage's full prompt. Writes the full prompt to
    ``.story/context/<key>/prompt_<stage>.md`` and returns a one-line instruction
    to read+execute it (passed as ``claude "query"`` so claude auto-starts once
    its own startup finishes — no PTY injection / readiness guessing). Empty
    string on failure (spawn proceeds without a seed → blank claude).
    """
    workspace = story.get("workspace", "")
    stage = story.get("current_stage", "design") or "design"
    try:
        from ...infra.story_paths import safe_story_path

        full = _build_interactive_stage_prompt(story, stage)
        pdir = safe_story_path(workspace, ".story", "context", story["story_key"])
        pdir.mkdir(parents=True, exist_ok=True)
        pfile = pdir / f"prompt_{stage}.md"
        pfile.write_text(full, encoding="utf-8")
        return (
            f"请读取 `{pfile}` 并严格按其中的说明执行本阶段({stage})任务,"
            f"完成后按其完成协议写入 done 文件。"
        )
    except Exception:
        return ""


def _build_stage_launch_cmd(story: dict, adapter, model: str) -> tuple[list[str], bool]:
    """Build the claude launch cmd for a story+stage: NEW or RESUME.

    Deterministic session UUID (uuid5 of ``story_key:stage``) + a marker file
    (``.story/context/<key>/session_<stage>.json``) decide:
      NEW    → claude --session-id <uuid> --name <key>-<stage> "<read-file seed>"
               + write marker. (seeds the stage task via claude "query")
      RESUME → claude --resume <uuid> "<continue>"   (loads transcript, continues)

    Both run with cwd=workspace — required: ``--resume`` lookup is cwd-scoped,
    so resume must run from the same dir as the original session. Transcripts
    persist at ``~/.claude/projects/<project>/<uuid>.jsonl`` (claude auto-saves),
    so a killed/orphan claude resumes here with full history.
    Returns ``(cmd, is_resume)``. See docs/handoff-design-hitl.md §11 +
    tests/test_session_resume.py.
    """
    import json as _json
    import uuid as _uuid

    from ...infra.story_paths import safe_story_path

    story_key = story["story_key"]
    workspace = story.get("workspace", "")
    stage = story.get("current_stage", "design") or "design"
    session_id = str(_uuid.uuid5(_uuid.NAMESPACE_DNS, f"{story_key}:{stage}"))
    session_name = f"{story_key}-{stage}"
    marker = (
        safe_story_path(workspace, ".story", "context", story_key)
        / f"session_{stage}.json"
    )
    if marker.exists():
        cmd = adapter.interactive_launch_cmd(
            model,
            prompt="继续上次的任务,完成后按完成协议写入 done 文件。",
            session_id=session_id,
            resume=True,
        )
        return cmd, True
    seed = _build_stage_launch_prompt(story)
    cmd = adapter.interactive_launch_cmd(
        model,
        prompt=seed,
        session_id=session_id,
        session_name=session_name,
        resume=False,
    )
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            _json.dumps(
                {"session_id": session_id, "name": session_name, "stage": stage},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass
    return cmd, False


def _ensure_story_agent_pty(story: dict) -> dict:
    workspace = story.get("workspace", "")
    if not workspace or not Path(workspace).exists():
        raise HTTPException(400, "Invalid workspace")

    profile = resolve_profile(story.get("profile", "minimal"))
    stage = story.get("current_stage", "design") or "design"
    stage_cfg = profile.stage(stage)
    adapter_name = stage_cfg.cli or profile.cli or "claude"
    model = stage_cfg.model or profile.model or "sonnet"
    existing = get_pty(story["story_key"])
    reused = bool(existing and existing.alive and existing.purpose == "agent")
    if reused:
        # 已有存活会话 —— 直接返回,不重复 spawn(避免孤儿/重复)
        return {
            "ok": True,
            "reused": True,
            "resumed": False,
            "purpose": "agent",
            "adapter": adapter_name,
            "model": model,
            "session_id": existing.session_id,
        }

    adapter = get_adapter(adapter_name)
    # 启动(NEW)或续上(RESUME):claude "query" seed / claude --resume <uuid>。
    # claude 自己管 readiness,不用 PTY 注入。见 _build_stage_launch_cmd + handoff §11。
    command, is_resume = _build_stage_launch_cmd(story, adapter, model)
    session_id, pty = ensure_agent_pty(
        story["story_key"],
        command,
        workspace,
        "",  # 不内部注入 —— prompt 已在 launch cmd 里
        readiness_marker=None,  # claude "query"/--resume 自己管 readiness
        startup_delay=0,
    )
    return {
        "ok": True,
        "reused": False,
        "resumed": is_resume,
        "purpose": "agent",
        "adapter": adapter_name,
        "model": model,
        "session_id": session_id,
    }


@app.delete("/api/story/{story_key}/sessions/{session_id}")
def api_kill_session(story_key: str, session_id: str):
    """Kill a specific PTY session."""
    kill_pty(story_key, session_id)
    return {"ok": True}


@app.delete("/api/pty")
def api_kill_all_pty():
    """Cleanly tear down EVERY PTY session across all stories.

    Sends ``/exit`` to each agent first (so claude flushes its transcript —
    needed for a complete ``--resume`` later), force-killing any that don't exit
    in time. Called by the serve-restart bat (before its taskkill) and by serve
    shutdown. For tearing down a single story's sessions use
    ``DELETE /api/pty/{story_id}`` instead. See handoff §12.
    """
    cleanup_all(prefer_clean_exit=True)
    return {"ok": True}


@app.delete("/api/pty/{story_id}")
def api_kill_pty(story_id: str):
    """Kill all PTY sessions for a story."""
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
    show_test: bool = False,
):
    """List stories with optional filters.

    Query params:
        status: Filter by status (active, paused, completed, failed)
        overdue: Only show stories past their deadline
        show_all: Include completed/failed stories
        tapd_type: Filter by type (story/bug/subtask)
        show_completed: Show completed TAPD stories (default hides resolved/rejected/closed)
        show_test: Show is_test=1 stories (default hides test/demo data)
    """
    stories = db.list_visible_stories(
        show_all=show_all,
        status=status,
        item_type=tapd_type,
        show_completed=show_completed,
        overdue=overdue,
        show_test=show_test,
    )

    return JSONResponse([_serialize_story_summary(s) for s in stories])


@app.get("/api/bugs")
def list_bugs(status: str = "", show_all: bool = False):
    """List bug stories. Defaults to open bugs; pass show_all to include resolved/closed."""
    stories = db.list_visible_stories(
        show_all=show_all,
        status=status,
        item_type="bug",
        show_completed=show_all,
    )
    # TAPD closed/resolved/rejected bugs are considered done unless show_all.
    if not show_all:
        done_tapd = {"closed", "resolved", "rejected"}
        stories = [
            s for s in stories if (s.get("tapd_status") or "").lower() not in done_tapd
        ]
    return JSONResponse([_serialize_story_summary(s) for s in stories])


def _story_headless(s: dict) -> bool:
    """Story 是否走 headless 执行(从 profile execution_mode 推导)。

    供前端 ClarifyDialog 决策:headless→MCP clarify 卡片;交互式→终端问人(BUG #9)。
    防御:profile 解析失败 → False(默认交互式)。
    """
    if not s:
        return False
    try:
        from ..engine.execution import headless_from_profile
        from ..engine.profile_loader import resolve_profile

        rp = resolve_profile(s.get("profile", "minimal"))
        return headless_from_profile(rp)
    except Exception:
        return False


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
            "lifecycleState": s.get("lifecycle_state"),
            "releaseTrain": s.get("release_train"),
            "isTest": bool(s.get("is_test")),
            # BUG #9:暴露 headless 让前端 ClarifyDialog 据此决定显隐
            # (headless 路径走 MCP clarify→卡片;交互式路径走"终端问人"→不显示卡片)。
            "headless": _story_headless(s),
        }
    )


@app.get("/api/story/{story_key}/stats")
def get_story_stats(story_key: str):
    """Aggregate quality/progress stats for the detail-page overview cards.

    Returns:
        code_changes: delivery artifacts (PRs/MRs) — units of code change.
        loop_rounds: adversarial plan↔review / code↔review iterations logged.
        findings_open: unresolved findings (status == 'open').
        tokens: aggregated LLM token usage and estimated cost (CNY).
    """
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")

    code_changes = len(db.get_story_delivery_artifacts(story_key))

    findings_open = sum(
        1 for f in db.get_findings_by_story(story_key) if f.get("status") == "open"
    )

    loop_rounds = sum(
        1 for ev in db.get_story_events(story_key) if db.is_adversarial_loop_event(ev)
    )

    tokens = db.get_story_token_usage(story_key)

    return {
        "code_changes": code_changes,
        "loop_rounds": loop_rounds,
        "findings_open": findings_open,
        "tokens": tokens,
    }


@app.get("/api/story/{story_key}/diff")
def get_story_diff(story_key: str):
    """Return git diff for the story's workspace branch vs its base branch.

    Query params (future):
        base: override base branch/ref.

    Returns:
        current_branch, base_branch, diff_range, files[], total_additions,
        total_deletions, total_changes, diff (raw unified diff text).
    """
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")

    try:
        result = get_story_workspace_diff(story_key)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        log.exception("failed to get workspace diff for %s", story_key)
        raise HTTPException(500, f"diff failed: {e}")

    return result


@app.post("/api/story")
def create_story(req: CreateStoryRequest):
    from .story_service import create_and_start_story

    workspace = req.workspace.strip()
    if not workspace:
        raise HTTPException(status_code=400, detail="workspace required")
    if not Path(workspace).is_absolute():
        raise HTTPException(
            status_code=400, detail="workspace must be an absolute path"
        )

    story_key = create_and_start_story(
        story_key=req.key,
        title=req.title,
        profile=req.profile,
        workspace=workspace,
        prd_path=None,
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
        # 确认闸推进:清掉 _stage_gate(进入执行即失效),让 planner 从下一未完成
        # stage 继续。即便 planner 入口也清一遍,这里先清保证 paused 期间语义干净。
        import json as _json

        try:
            _ctx = _json.loads(s.get("context_json") or "{}")
        except (ValueError, TypeError):
            _ctx = {}
        if _ctx.pop("_stage_gate", None) is not None:
            db.update_story(
                story_key,
                context_json=_json.dumps(_ctx, ensure_ascii=False),
            )
        db.update_story(story_key, status="active")
        start_story_async(story_key)
        return {"ok": True, "status": "resumed"}

    return {"ok": True}


@app.post("/api/story/{story_key}/lifecycle/advance")
def advance_lifecycle_state(story_key: str):
    """STORY-STATE-MODEL: 推进 Story 业务状态到下一态(开发→测试→上线)。

    区别于 ``PUT /advance``(那是 driver resume,从 paused 重启执行)。本端点处理
    Story 状态机转移:校验当前状态 stages 全 done(防跳级)→ 清 _story_state_gate
    → 推进 lifecycle_state → 若 next 有 stages 则 start_story_async 跑它们,
    无 stages(终态)则标 completed。由前端 Story 状态闸卡片的「进入下一状态」触发。
    """
    import json as _json

    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")
    try:
        ctx = _json.loads(s.get("context_json") or "{}")
    except (ValueError, TypeError):
        ctx = {}

    gate = ctx.get("_story_state_gate")
    if not gate or not gate.get("awaiting_confirm"):
        raise HTTPException(409, "no pending story_state_gate")

    cur_state = gate.get("from") or s.get("lifecycle_state") or "开发"
    next_state = gate.get("to")
    if not next_state:
        raise HTTPException(409, "story_state_gate has no next state")

    # 推进 lifecycle_state,清闸标记。driver 重进时从 next 状态的 stages 开始
    # (start_idx 跳过 _completed_stages 已含的)。
    ctx.pop("_story_state_gate", None)
    ctx["_lifecycle_state"] = next_state
    db.update_story(
        story_key,
        lifecycle_state=next_state,
        status="active",
        context_json=_json.dumps(ctx, ensure_ascii=False),
    )
    db.log_event(
        story_key,
        s.get("current_stage") or "",
        "story_state_transition",
        {"from": cur_state, "to": next_state, "auto": False},
    )

    # next 状态有无 stages 决定是继续跑还是终态完成
    try:
        rp = resolve_profile(s.get("profile", "minimal"))
        states = rp.story_states or {}
    except Exception:
        states = {}
    next_def = states.get(next_state) or {}
    next_stages = list(next_def.get("stages") or [])

    if not next_stages:
        # 终态:无阶段可跑 → 整个 story 完成
        db.update_story(story_key, status="completed")
        return {"ok": True, "lifecycle_state": next_state, "status": "completed"}

    start_story_async(story_key)
    return {"ok": True, "lifecycle_state": next_state, "status": "active"}


@app.put("/api/story/{story_key}/release-train")
def set_release_train(story_key: str, req: SetReleaseTrainRequest):
    """班车看板:人工调整 story 归属的班车(泳道)。只改 release_train,不动 lifecycle_state。

    Body: {"train": "v3.2"} 或 {"train": null}(清空,回待分配区)。
    """
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")

    train = req.train
    if isinstance(train, str):
        train = train.strip() or None  # 空串归一为 NULL(待分配)

    prev = s.get("release_train")
    db.update_story(story_key, release_train=train)
    db.log_event(
        story_key,
        s.get("current_stage") or "",
        "release_train_changed",
        {"from": prev, "to": train},
    )
    return {"ok": True, "releaseTrain": train}


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


@app.put("/api/story/{story_key}/archive")
def archive_story(story_key: str):
    """Archive a story that has been released and verified.

    Archived stories disappear from the default dashboard list but remain
    queryable via show_all and are not deleted.
    """
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")
    db.update_story(story_key, status="archived")
    db.log_stage(
        story_key,
        s.get("current_stage", ""),
        "archive",
        "User archived story after release",
    )
    return {"ok": True, "status": "archived"}


@app.post("/api/story/{parent_key}/sub")
def api_create_sub_story(parent_key: str, req: CreateSubStoryRequest):
    from .story_service import create_sub_story as svc_create_sub

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
    from .story_service import abort_story as svc_abort

    try:
        svc_abort(story_key, req.reason if req else "User abort")
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@app.post("/api/story/{story_key}/emergency-stop")
def emergency_stop_story(story_key: str):
    """紧急停止:杀运行中 claude 进程 + 释放 driver guard + 标 paused(可恢复)。

    区别于 ``/abort``(标 aborted,不可恢复):紧急停止是"暂停并清理进程",story 仍可用
    ``/advance`` 恢复。用于 build 跑飞/死循环烧 token 等需要立即停的场景。
    force_stop_story bump epoch 让运行中 driver 线程检测到取消自行退出;kill_pty 杀 PTY。
    """
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")
    was_running = force_stop_story(story_key)
    # 杀该 story 的所有 PTY(运行中的 claude/codex 进程树)
    try:
        kill_pty(story_key)
    except Exception:
        pass
    db.update_story(story_key, status="paused", last_error="紧急停止（可恢复）")
    db.log_event(
        story_key,
        s.get("current_stage") or "",
        "emergency_stop",
        {"was_running": was_running},
    )
    log.warning(
        "[%s] emergency stop: killed PTY, paused (was_running=%s)",
        story_key,
        was_running,
    )
    return {"ok": True, "status": "paused", "was_running": was_running}


@app.put("/api/story/{parent_key}/resume")
def api_resume_parent(parent_key: str, req: ResumeParentRequest = None):
    from .story_service import resume_parent as svc_resume

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
            payload = db.parse_event_payload(ev)

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
        payload = db.parse_event_payload(ev)
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
        evidence = detail_data.get("evidence", {}) or {}
        if detail_data.get("evidence_ref"):
            evidence = {**evidence, "evidence_ref": detail_data["evidence_ref"]}
        decisions.append(
            {
                "decision_id": detail_data.get("decision_id", ""),
                "stage": gr.get("stage", ""),
                "decision": gr.get("result", ""),
                "reason_code": detail_data.get("reason_code", gr.get("gate_name", "")),
                "human_message": detail_data.get("summary", ""),
                "evidence": evidence,
                "allowed_actions": [],
                "created_at": gr.get("created_at", ""),
            }
        )

    return {"decisions": decisions}


@app.post("/api/story/{story_key}/gate-results")
def api_create_gate_result(story_key: str, req: CreateGateResultRequest):
    """Record a manual gate result with evidence for story-led delivery."""
    if not db.get_story(story_key):
        raise HTTPException(status_code=404, detail=f"story not found: {story_key}")

    normalized_result = req.result.upper()
    valid_results = {"PASS", "FAIL", "BLOCKED", "PARTIAL", "WAIVED"}
    if normalized_result not in valid_results:
        raise HTTPException(
            status_code=400,
            detail=f"invalid result: {req.result}. Expected one of {sorted(valid_results)}",
        )

    import json as _json

    detail = {
        "reason_code": req.gate_name,
        "summary": req.summary,
        "evidence_ref": req.evidence_ref,
        "evidence": req.evidence or {},
    }
    db.record_gate_result(
        story_key=story_key,
        stage=req.stage,
        gate_name=req.gate_name,
        result=normalized_result,
        detail=_json.dumps(detail, ensure_ascii=False),
    )
    db.log_event(
        story_key,
        stage=req.stage,
        event_type="gate_result_recorded",
        payload={
            "gate_name": req.gate_name,
            "result": normalized_result,
            "summary": req.summary,
            "evidence_ref": req.evidence_ref,
        },
    )
    return {"ok": True, "result": normalized_result}


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
        payload = db.parse_event_payload(ev)
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
    from ..observability.events import build_debug_response

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
        status: Filter by finding status (open, accepted, fixed, verified, ...).
                Defaults to 'open'.
        min_severity: Minimum severity threshold (high, medium, low). Empty = no
                threshold (all severities), matching the findings_open stat.
    """
    # Fetch all findings for the story, then filter in one place. Previously this
    # called get_open_findings, which silently drops low-severity rows via its
    # default min_severity='medium' — so ?min_severity=low could never return
    # low findings, and the default hid them too. (db.SEVERITY_ORDER is the
    # single shared ranking.)
    findings = db.get_findings_by_story(story_key)
    findings = [f for f in findings if f.get("status") == (status or "open")]

    if min_severity:
        min_level = db.SEVERITY_ORDER.get(min_severity, 0)
        findings = [
            f
            for f in findings
            if db.SEVERITY_ORDER.get(f.get("severity", "low"), 0) >= min_level
        ]

    return {"findings": findings}


@app.get("/api/story/{story_key}/quality")
async def get_quality_status(story_key: str):
    from ..evaluation.quality import check_dor, check_dod

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

    from ..evaluation.quality import approve_pattern, activate_pattern

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

    from ..evaluation.quality import reject_pattern

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
    from ..evaluation.quality import approve_pattern, activate_pattern

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
    from ..evaluation.quality import reject_pattern

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

    from ..evaluation.review_feedback import import_review

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
    from ..evaluation.quality import update_finding_status

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
    item_type: str = ""  # "bug" | "story" | "requirement" | ""
    remap_lifecycle: bool = False  # 状态治理:按 tapd_state_map 刷新 lifecycle_state


@app.post("/api/sync/tapd")
def api_sync_tapd(req: SyncRequest):
    """Trigger TAPD sync."""
    from ...sourcing.sources.tapd_source import TapdSource

    config = _load_tapd_config()
    if not config:
        raise HTTPException(
            400, "TAPD not configured. Add 'tapd' section to config.yaml."
        )

    source = TapdSource(config)
    try:
        items = source.fetch_pending(
            fetch_all=req.fetch_all, item_type=req.item_type or None
        )
    except Exception as e:
        raise HTTPException(502, f"TAPD fetch failed: {e}")

    from .sync_service import sync_tapd

    # Require an explicit absolute workspace. The previous `or "."` fallback
    # silently stored the server's CWD as the story workspace, which caused
    # evidence artifacts to land inside the tool's own package directory.
    workspace = req.workspace.strip()
    if not workspace:
        raise HTTPException(
            status_code=400, detail="workspace required (select a project first)"
        )
    if not Path(workspace).is_absolute():
        raise HTTPException(
            status_code=400, detail="workspace must be an absolute path"
        )

    result = sync_tapd(
        items,
        workspace=workspace,
        dry_run=req.dry_run,
        status_only=req.status_only,
        remap_lifecycle=req.remap_lifecycle,
    )

    # Also pull bugs linked to stories via TAPD get_related_bugs, which catches
    # associations that the bug's own story_id field misses.
    if not req.dry_run:
        related = _sync_related_bugs_from_stories(
            source, item_type_filter=req.item_type
        )
        result["related_bugs_synced"] = related["synced"]
        result["related_bugs_failed"] = related["failed"]

    return result


def _sync_related_bugs_from_stories(source, item_type_filter: str = "") -> dict:
    """For every local TAPD story, fetch related bugs and upsert them with parent_key."""
    result = {"synced": 0, "failed": 0}
    # Only run when syncing stories or everything; pure bug-only sync already has its own path.
    if item_type_filter == "bug":
        return result

    # show_test=True:测试 story 的关联 bug 同步不能因 is_test 过滤而漏。
    stories = db.list_visible_stories(show_all=True, item_type="story", show_test=True)
    stories = [
        s for s in stories if s.get("source_type") == "tapd" and s.get("source_id")
    ]

    # Collect related bugs first to avoid concurrent SQLite writes on the same key.
    bug_map: dict[str, tuple[dict, str]] = {}
    failed_stories = 0
    for story in stories:
        try:
            related = source._api.get_related_bugs(story["source_id"]) or []
            for r in related:
                bug_id = (r.get("Bug") or r).get("id")
                if not bug_id or bug_id in bug_map:
                    continue
                detail = source._api.get_bug_detail(bug_id)
                flat = (detail.get("Bug", {}) if detail else {}) or {}
                if flat:
                    bug_map[bug_id] = (flat, story["story_key"])
        except Exception:
            failed_stories += 1

    for bug_id, (flat, parent_key) in bug_map.items():
        try:
            db.upsert_story_from_source(
                source_type="tapd",
                source_id=f"bug_{bug_id}",
                title=flat.get("title", ""),
                tapd_type="bug",
                tapd_status=flat.get("status", ""),
                owner=flat.get("current_owner", ""),
                tapd_url=f"https://www.tapd.cn/{source._api.workspace_id}/bugtrace/bugs/view?bug_id={bug_id}",
                parent_key=parent_key,
            )
            result["synced"] += 1
        except Exception:
            result["failed"] += 1

    if failed_stories:
        result["failed"] += failed_stories
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
        from ..context.resolver import ContextResolver

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
    from ..context.auto_discovery import Scanner, Decider, Handler

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
    from ..context.snapshot import generate_snapshot

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


@app.get("/api/story/{story_key}/prompts")
def api_get_prompts(story_key: str):
    """返回该 story 所有 stage 的 prompt 内容(复盘用)。

    提示词已落盘在 .story/context/<key>/prompt_<stage>.md(每次 stage launch
    时写),原先无查看入口——本端点遍历该目录,把每个 stage 的完整 prompt 拉出来。
    """
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")
    from ...infra.story_paths import safe_story_path

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


@app.get("/api/story/{story_key}/context/pack")
def api_get_context_pack(story_key: str, skill: str = ""):
    """Render a neutral mixed-density context pack for AI injection."""
    try:
        from ..context.pack import generate_pack

        return generate_pack(story_key, skill=skill)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/story/{story_key}/context/release-prompt")
def api_get_release_prompt(story_key: str):
    """Render a pre-release checklist prompt for a code AI."""
    if not db.get_story(story_key):
        raise HTTPException(status_code=404, detail=f"story not found: {story_key}")
    try:
        from ..context.release_prompt import generate_release_prompt

        return generate_release_prompt(story_key)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/story/{story_key}/context/post-release-prompt")
def api_get_post_release_prompt(story_key: str):
    """Render a post-release auto-verification prompt for a code AI."""
    if not db.get_story(story_key):
        raise HTTPException(status_code=404, detail=f"story not found: {story_key}")
    try:
        from ..context.release_prompt import generate_post_release_prompt

        return generate_post_release_prompt(story_key)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/api/story/{story_key}/bugs")
def api_get_related_bugs(story_key: str):
    """List local bug stories linked to this story via parent_key."""
    if not db.get_story(story_key):
        raise HTTPException(status_code=404, detail=f"story not found: {story_key}")
    bugs = db.list_stories_by_parent(story_key, item_type="bug")
    return JSONResponse([_serialize_story_summary(b) for b in bugs])


@app.post("/api/story/{story_key}/sync-related-bugs")
def api_sync_related_bugs(story_key: str):
    """Sync bugs linked to this story (via TAPD get_related_bugs), setting parent_key."""
    story = db.get_story(story_key)
    if not story:
        raise HTTPException(status_code=404, detail=f"story not found: {story_key}")
    if story.get("source_type") not in ("tapd", None):
        return {"synced": 0, "reason": "not a tapd source"}
    # source_id 优先；旧 story 可能只把 TAPD id 编进 story_key（tapd-{id}），从中提取
    tapd_id = story.get("source_id") or (
        story_key[5:] if story_key.startswith("tapd-") else ""
    )
    if not tapd_id:
        return {"synced": 0, "reason": "no tapd id (not a tapd story)"}
    config = _load_tapd_config()
    if not config.get("workspace_id"):
        raise HTTPException(status_code=503, detail="TAPD not configured")
    from ...sourcing.sources.tapd_api import TapdApi

    api = TapdApi(workspace_id=config["workspace_id"])
    related = api.get_related_bugs(tapd_id) or []
    synced = 0
    for r in related:
        bug_id = r.get("bug_id")
        if not bug_id:
            continue
        flat = (api.get_bug_detail(bug_id) or {}).get("Bug", {})
        db.upsert_story_from_source(
            source_type="tapd",
            source_id=f"bug_{bug_id}",
            title=flat.get("title", ""),
            tapd_type="bug",
            tapd_status=flat.get("status", ""),
            owner=flat.get("current_owner", ""),
            tapd_url=f"https://www.tapd.cn/{config['workspace_id']}/bugtrace/bugs/view?bug_id={bug_id}",
            parent_key=story_key,
        )
        synced += 1
    return {"synced": synced, "story_key": story_key}


@app.post("/api/story/{story_key}/bugs/{bug_key}/link")
def api_link_bug_to_story(story_key: str, bug_key: str):
    """Manually bind an unassociated bug to a story."""
    story = db.get_story(story_key)
    if not story:
        raise HTTPException(404, "Story not found")
    bug = db.get_story(bug_key)
    if not bug:
        raise HTTPException(404, "Bug not found")
    if bug.get("tapd_type") != "bug":
        raise HTTPException(400, "Target is not a bug")
    db.update_story(bug_key, parent_key=story_key)
    db.log_stage(
        bug_key, bug.get("current_stage", ""), "link", f"Manually linked to {story_key}"
    )
    return {"ok": True, "parentKey": story_key}


@app.get("/api/story/{story_key}/available-bugs")
def api_list_available_bugs(story_key: str):
    """List bugs that are not linked to any story (for drag-and-drop binding)."""
    story = db.get_story(story_key)
    if not story:
        raise HTTPException(404, "Story not found")
    bugs = db.list_unlinked_bugs()
    return JSONResponse([_serialize_story_summary(b) for b in bugs])


@app.post("/api/story/{story_key}/bugs/{bug_key}/fix-prompt")
def api_get_bugfix_prompt(story_key: str, bug_key: str):
    """Render a bug-fix prompt for a code AI based on the parent story context."""
    if not db.get_story(story_key):
        raise HTTPException(404, "Story not found")
    if not db.get_story(bug_key):
        raise HTTPException(404, "Bug not found")
    try:
        from ..context.release_prompt import generate_bugfix_prompt

        return generate_bugfix_prompt(story_key, bug_key)
    except ValueError as e:
        raise HTTPException(404, detail=str(e))


class BatchFixPromptRequest(BaseModel):
    bug_keys: list[str]


@app.post("/api/story/{story_key}/bugs/fix-prompt")
def api_get_batch_bugfix_prompt(story_key: str, req: BatchFixPromptRequest):
    """Render a combined bug-fix prompt for multiple bugs under a story."""
    if not db.get_story(story_key):
        raise HTTPException(404, "Story not found")
    if not req.bug_keys:
        raise HTTPException(400, "bug_keys is empty")
    try:
        from ..context.release_prompt import generate_batch_bugfix_prompt

        return generate_batch_bugfix_prompt(story_key, req.bug_keys)
    except ValueError as e:
        raise HTTPException(404, detail=str(e))


@app.post("/api/story/{bug_key}/resolve")
def api_resolve_bug(bug_key: str):
    """Mark a bug resolved: update TAPD + local status. Warns if no bugfix-report."""
    story = db.get_story(bug_key)
    if not story:
        raise HTTPException(status_code=404, detail=f"story not found: {bug_key}")
    if story.get("tapd_type") != "bug":
        raise HTTPException(status_code=400, detail="not a bug")
    has_evidence = any(
        d.get("kind") == "bugfix-report" for d in db.get_story_documents(bug_key)
    )
    config = _load_tapd_config()
    if config.get("workspace_id") and story.get("source_id"):
        from ...sourcing.sources.tapd_api import TapdApi

        api = TapdApi(workspace_id=config["workspace_id"])
        bug_id = story["source_id"].removeprefix("bug_")
        api.update_bug(bug_id, {"status": "resolved"})
    db.update_story(bug_key, status="completed", tapd_status="resolved")
    return {"ok": True, "has_bugfix_report": has_evidence}


class AddDocumentRequest(BaseModel):
    kind: str
    ref: str = ""
    summary: str = ""
    evidence_ref: str = ""
    project_id: int | None = None


@app.post("/api/story/{story_key}/context/documents")
def api_add_document(story_key: str, req: AddDocumentRequest):
    """Add a document (prd/spec/plan) — agent backfill."""
    if not db.get_story(story_key):
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
    db.bump_context_revision(story_key)
    return doc


class AddChangeItemRequest(BaseModel):
    kind: str
    ref: str = ""
    summary: str = ""
    evidence_ref: str = ""
    environment: str = ""
    project_id: int | None = None


@app.post("/api/story/{story_key}/context/change-items")
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


class SetBranchRequest(BaseModel):
    project_id: int
    branch: str
    worktree_path: str | None = None
    base_branch: str | None = None
    worktree_state: str | None = None


@app.put("/api/story/{story_key}/context/branch")
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


# -------- Project registry endpoints --------


class CreateProjectRequest(BaseModel):
    name: str
    repo_path: str
    default_branch: str = "main"
    remote_url: str = ""


def _workspace_root_for_project(repo_path: str) -> Path:
    """Infer the story workspace root for a registered project path.

    In a monorepo, a sub-project like ``D:/hc-all/frontends/hc-admin`` should
    resolve to ``D:/hc-all`` when the monorepo root carries ``.story``/``.agents``
    markers. For standalone projects, the project directory itself is the root.
    The walk is bounded by the git top-level (when present) and a small max depth
    so unrelated ancestor directories (e.g. the user's home directory) that happen
    to have markers are not picked.
    """
    import subprocess

    path = Path(repo_path).resolve()

    # Find the git top-level to bound the ancestor walk.
    git_root: Path | None = None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            git_root = Path(result.stdout.strip()).resolve()
    except Exception:
        pass

    max_depth = 5
    candidates = [path]
    for i, parent in enumerate(path.parents):
        if git_root is not None and parent == git_root:
            candidates.append(parent)
            break
        if i >= max_depth:
            break
        candidates.append(parent)

    for candidate in candidates:
        if (
            (candidate / ".story").exists()
            or (candidate / ".agents").exists()
            or (candidate / "AGENTS.md").exists()
        ):
            return candidate
    return git_root if git_root is not None else path


def _workspace_options_from_projects(projects: list[dict]) -> list[dict]:
    """Return unique selectable workspaces derived from registered projects."""
    options: dict[str, dict] = {}
    for project in projects:
        repo_path = project.get("repo_path") or ""
        if not repo_path:
            continue
        root = _workspace_root_for_project(repo_path)
        key = str(root)
        option = options.setdefault(
            key,
            {
                "path": key,
                "name": root.name or key,
                "projectCount": 0,
                "projects": [],
            },
        )
        option["projectCount"] += 1
        option["projects"].append(project.get("name", ""))
    return sorted(options.values(), key=lambda item: item["name"])


@app.get("/api/workspaces")
def api_list_workspaces():
    """List selectable story workspaces inferred from registered projects."""
    return {"workspaces": _workspace_options_from_projects(db.list_projects())}


@app.get("/api/profiles")
def api_list_profiles():
    """List available profiles for the create-story picker."""
    from ..engine.profile_loader import list_profiles

    return {"profiles": list_profiles()}


@app.get("/api/projects")
def api_list_projects():
    """List all registered projects with fresh availability."""
    from ..workspace.project_registry import check_project_availability

    projects = db.list_projects()
    # 刷新每个项目的 availability（轻量 git rev-parse）
    for p in projects:
        check_project_availability(p["id"])
    return {"projects": db.list_projects()}


@app.post("/api/projects")
def api_create_project(req: CreateProjectRequest):
    """Register a new project."""
    from ..workspace.project_registry import register_project

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
    from ..workspace.project_registry import update_project

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
    from ..workspace.worktree.handler import prepare_worktrees

    results = prepare_worktrees(story_key, worktree_root=req.worktree_root)
    return {"results": results}


@app.get("/api/story/{story_key}/worktrees/cleanup-preview")
def api_cleanup_preview(story_key: str):
    """Preview worktree cleanup for a story."""
    from ..workspace.worktree.resolver import resolve_story_worktree
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
    from ..workspace.worktree.handler import cleanup_worktree

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
    content: str = ""  # PRD / 需求正文，开始开发时必填，design 阶段注入给 CLI
    branch: str = ""  # 预生成的分支名（由 intake preview 产出），保存时直接复用


class IntakePreviewRequest(BaseModel):
    source_type: str = "tapd"
    source_id: str


@app.post("/api/intake/preview")
def api_intake_preview(
    source_type: str = Form("tapd"),
    source_id: str = Form(""),
    files: list[UploadFile] = File(default_factory=list),
):
    """Fetch source detail and ask the built-in PRD generator to prefill Intake.

    Accepts optional image uploads so users can supply screenshots that the
    source system cannot fetch automatically (e.g. TAPD images behind login).
    """
    source_id = (source_id or "").strip()
    if not source_id:
        raise HTTPException(status_code=400, detail="source_id required")
    source_type = (source_type or "tapd").strip().lower()

    if source_type != "tapd":
        raise HTTPException(
            status_code=400, detail=f"unsupported source: {source_type}"
        )

    source_id = source_id.removeprefix("tapd-")
    from ...sourcing.sources import tapd_source
    from . import prd_generator

    source = tapd_source.TapdSource(_load_tapd_config())
    item = source.get_detail(source_id)
    if not item:
        raise HTTPException(status_code=404, detail="source story not found")

    local_image_paths: list[str] = []
    if files:
        tmp_dir = Path(tempfile.gettempdir()) / "story-intake-images"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        for upload in files:
            if not upload.filename:
                continue
            # Sanitize upload filename: HTTP multipart filename is fully
            # attacker-controlled. Take basename only and reject traversal.
            safe_name = Path(upload.filename).name
            if not safe_name or safe_name in {".", ".."} or ".." in safe_name:
                continue  # drop suspicious upload silently
            tmp_path = tmp_dir / f"{item.source}-{item.id}_{safe_name}"
            # Blast shield: tmp_path must stay inside tmp_dir.
            try:
                tmp_path.resolve().relative_to(tmp_dir.resolve())
            except ValueError:
                continue
            with tmp_path.open("wb") as f:
                f.write(upload.file.read())
            local_image_paths.append(str(tmp_path))

    snapshot = prd_generator.StorySourceSnapshot(
        story_key=f"{item.source}-{item.id}",
        source_type=item.source,
        source_id=item.id,
        title=item.title,
        description=item.description or "",
        url=item.extra.get("url", ""),
        priority=item.priority,
        owner=item.owner,
        status=item.status,
        local_image_paths=local_image_paths,
    )
    try:
        result = prd_generator.generate_prd_from_source(snapshot)
    except Exception as exc:
        log.exception("prd_generator failed for %s", snapshot.story_key)
        raise HTTPException(
            status_code=502,
            detail=f"PRD 生成失败: {exc}",
        )

    # 预生成分支名，让保存阶段直接复用，避免每次点击保存都调 LLM。
    # 仅当 profile 的 branch_rule 不含 {project} 时才能前置；含 {project} 时
    # 让 start 阶段按项目名动态生成。
    branch = ""
    try:
        from ..engine.profile_loader import load_profile
        from ..workspace.branch_naming import generate_branch_for_story

        profile_raw = load_profile("minimal")
        rule = profile_raw.get("branch_rule", "")
        if rule and "{project}" not in rule:
            branch = (
                generate_branch_for_story(
                    story_key=snapshot.story_key,
                    title=snapshot.title,
                    profile_raw=profile_raw,
                )
                or ""
            )
    except Exception:
        log.exception("branch pre-generation failed for %s", snapshot.story_key)

    return {
        "storyKey": snapshot.story_key,
        "sourceType": snapshot.source_type,
        "sourceId": snapshot.source_id,
        "title": snapshot.title,
        "sourceUrl": snapshot.url,
        "action": result.action,
        "markdown": result.markdown,
        "summary": result.summary,
        "dingtalkLinks": result.dingtalk_links,
        "questions": result.questions,
        "branch": branch,
    }


def _prepare_intake_prd_content(story_key: str, story: dict, content: str):
    """Return (content, error_response) for the start endpoint.

    If the user supplied content, treat it as the PRD/intake material directly.
    Otherwise, ask the built-in PRD generator to prepare PRD from the story source.
    """
    if (content or "").strip():
        return content, None

    source_type = story.get("source_type") or ""
    source_id = story.get("source_id") or ""
    if not source_type or not source_id:
        return "", JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "reasonCode": "content_required",
                "message": "请填写 story 内容 / PRD",
            },
        )

    try:
        source_snapshot = _load_story_source_snapshot(story_key, story)
    except Exception as exc:
        return "", JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "reasonCode": "source_detail_unavailable",
                "message": f"无法读取 story 来源详情: {exc}",
            },
        )
    if not source_snapshot:
        return "", JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "reasonCode": "content_required",
                "message": "请填写 story 内容 / PRD",
            },
        )

    from . import prd_generator

    try:
        result = prd_generator.generate_prd_from_source(source_snapshot)
    except Exception as exc:
        return "", JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "reasonCode": "prd_generation_failed",
                "message": f"PRD 生成失败: {exc}",
            },
        )

    if result.action == "generated" and result.markdown.strip():
        return result.markdown, None

    if result.action == "manual_download_required":
        return "", JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "reasonCode": "dingtalk_download_required",
                "message": result.summary or "请先打开外部文档并下载/复制 PRD 内容",
                "dingtalk_links": result.dingtalk_links,
            },
        )

    if result.action == "needs_clarification":
        return "", JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "reasonCode": "intake_clarification_required",
                "message": result.summary or "PRD 生成前需要补充需求信息",
                "questions": result.questions,
            },
        )

    return "", JSONResponse(
        status_code=409,
        content={
            "ok": False,
            "reasonCode": "prd_generation_failed",
            "message": result.summary or "PRD 生成失败",
        },
    )


def _load_story_source_snapshot(story_key: str, story: dict):
    from . import prd_generator

    source_type = story.get("source_type") or ""
    source_id = story.get("source_id") or ""

    if source_type == "tapd":
        from ...sourcing.sources import tapd_source

        source = tapd_source.TapdSource(_load_tapd_config())
        item = source.get_detail(source_id)
        if not item:
            return None
        return prd_generator.StorySourceSnapshot(
            story_key=story_key,
            source_type=item.source,
            source_id=item.id,
            title=item.title or story.get("title", ""),
            description=item.description or "",
            url=item.extra.get("url", "") or story.get("tapd_url", ""),
            priority=item.priority,
            owner=item.owner,
            status=item.status,
        )

    return prd_generator.StorySourceSnapshot(
        story_key=story_key,
        source_type=source_type,
        source_id=source_id,
        title=story.get("title", ""),
        description="",
        url=story.get("tapd_url", ""),
        priority=story.get("priority", ""),
        owner=story.get("owner", ""),
        status=story.get("tapd_status", ""),
    )


def _bind_story_projects_for_start(
    story_key: str, story: dict, project_ids: list[int], branch: str = ""
):
    sps = db.get_story_projects(story_key)
    if not project_ids:
        return

    all_projects = {p["id"]: p for p in db.list_projects()}
    existing_pids = {sp["project_id"] for sp in sps}
    bound_repo = None
    for pid in project_ids:
        if pid in existing_pids:
            continue
        proj = all_projects.get(pid)
        if not proj:
            continue

        # 优先复用 preview 阶段预生成的分支名，避免保存时重复调 LLM。
        # 若未传入或 profile 规则需要按项目区分，则现场生成。
        if branch:
            per_project_branch = branch
        else:
            from ..engine.profile_loader import load_profile
            from ..workspace.branch_naming import generate_branch_for_story

            profile_raw = load_profile(story.get("profile") or "minimal")
            per_project_branch = (
                generate_branch_for_story(
                    story_key=story_key,
                    title=story.get("title", ""),
                    profile_raw=profile_raw,
                    project_name=proj["name"],
                )
                or f"codex/{story_key}-{proj['name']}"
            )

        repo_path = proj.get("repo_path", "")

        db.bind_story_project(
            story_key=story_key,
            project_id=proj["id"],
            branch=per_project_branch,
            base_branch=proj.get("default_branch", "main"),
            worktree_state="unprepared",
            source="user",
        )
        if not bound_repo and repo_path:
            bound_repo = repo_path
    if bound_repo:
        workspace_root = _workspace_root_for_project(bound_repo)
        db.update_story(story_key, workspace=str(workspace_root))


@app.post("/api/story/{story_key}/start")
def api_start_story(story_key: str, req: StartStoryRequest | None = None):
    """Start a story. Binds projects, promotes to ready, triggers LLM planning."""
    story = db.get_story(story_key)
    if not story:
        raise HTTPException(status_code=404, detail="story not found")

    intake_state = story.get("intake_state", "ready")
    req = req or StartStoryRequest()

    # Intake: user-provided PRD wins; otherwise source-backed stories can ask the
    # built-in PRD generator LLM to prepare or route PRD creation.
    prd_content, intake_error = _prepare_intake_prd_content(
        story_key, story, req.content
    )
    if intake_error:
        return intake_error

    try:
        if intake_state == "candidate":
            # Promote candidate to ready + planning
            db.update_story(story_key, intake_state="ready", status="planning")

        # Project binding is optional during Intake. In monorepos, the selected
        # implementation modules (for example hc-order or hc-limit under hc-all) are
        # discovered later by Design/Build, not modeled as separate repo projects.
        _bind_story_projects_for_start(story_key, story, req.project_ids, req.branch)

        # 保存 PRD 到 story evidence 目录，供 design 阶段注入。
        # 不写入被绑定服务仓库的 prd/，避免污染业务代码仓库。
        story = db.get_story(story_key)
        workspace = (story or {}).get("workspace", "") or ""
        if not workspace:
            return JSONResponse(
                status_code=409,
                content={
                    "ok": False,
                    "reasonCode": "workspace_required",
                    "message": "无法确定工作区，请先选择工作区或注册项目",
                },
            )

        from ...infra.story_paths import story_prd_path

        prd_file = story_prd_path(workspace, story_key, (story or {}).get("title", ""))
        prd_file.parent.mkdir(parents=True, exist_ok=True)
        prd_file.write_text(prd_content, encoding="utf-8")
        db.update_context(story_key, "prd_path", str(prd_file))
        existing_prd = [
            d for d in db.get_story_documents(story_key) if d.get("kind") == "prd"
        ]
        if existing_prd:
            db.update_document(
                existing_prd[0]["id"],
                ref=str(prd_file),
                summary="Intake PRD",
                source="system",
                verification_state="verified",
            )
        else:
            db.create_document(
                story_key,
                "prd",
                ref=str(prd_file),
                summary="Intake PRD",
                source="system",
                verification_state="verified",
            )
        db.bump_context_revision(story_key)

        db.update_story(story_key, intake_state="ready", status="planning")
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("Failed to start story %s", story_key)
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "reasonCode": "start_failed",
                "message": f"启动 Story 失败: {exc}",
            },
        )

    return {"ok": True, "story_key": story_key}


@app.get("/api/story/{story_key}/plan")
def api_get_plan(story_key: str):
    """获取 Story 的当前规划。支持 Agent 模式和 Legacy 模式。"""
    story = db.get_story(story_key)
    if not story:
        raise HTTPException(status_code=404, detail="story not found")

    import json

    ctx = {}
    try:
        ctx = json.loads(story.get("context_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        pass

    plan_summary = ctx.get("plan_summary", "")
    active_exec = ctx.get("_active_execution", {})
    agent_actions = ctx.get("_agent_actions")

    # PLAN-stage-confirm-gate:组装 stages 进度条真实数据 + stage_gate(确认闸卡片)。
    # stages 从 launch actions + _completed_stages 推导 done 标记;前端用 done 驱动
    # 进度状态(✓完成/进行中/待开始)。stage_gate 在 paused 时由前端显示「确认推进」卡片。
    completed_stages = list(ctx.get("_completed_stages", []))
    stages_view = []
    if agent_actions:
        for _a in agent_actions:
            if _a.get("action") != "launch":
                continue
            _st = _a.get("stage", "")
            stages_view.append(
                {
                    "name": _st,
                    "focus": _a.get("focus", ""),
                    "adapter": _a.get("adapter", "claude"),
                    "done": _st in completed_stages,
                }
            )
    stage_gate = ctx.get("_stage_gate")

    # STORY-STATE-MODEL: 组装 Story 业务状态视图(开发/测试/上线)+ 状态闸。
    # storyStates 从 profile.story_states + lifecycle_state + _completed_stages 推导每个
    # 状态的进度(done/进行中/待开始)。前端主进度条用它(替写死阶段)。无 story_states → 空。
    cur_lifecycle = (
        story.get("lifecycle_state") or ctx.get("_lifecycle_state") or "开发"
    )
    story_states_view = []
    try:
        _rp = resolve_profile(story.get("profile", "minimal"))
        _states_cfg = _rp.story_states or {}
    except Exception:
        _states_cfg = {}
    for _sname, _sdef in _states_cfg.items():
        _sdef = _sdef or {}
        _sstages = list(_sdef.get("stages") or [])
        _done_count = sum(1 for _ss in _sstages if _ss in completed_stages)
        story_states_view.append(
            {
                "name": _sname,
                "stages": _sstages,
                "current": _sname == cur_lifecycle,
                "done": bool(_sstages) and _done_count >= len(_sstages),
                "done_count": _done_count,
                "total": len(_sstages),
            }
        )
    story_state_gate = ctx.get("_story_state_gate")

    # 尝试读取 plan 文件内容
    plan_content = ""
    plan_path = ctx.get("plan_path", "")
    if plan_path:
        from pathlib import Path

        p = Path(story.get("workspace", ".")) / plan_path
        if p.exists():
            plan_content = p.read_text(encoding="utf-8", errors="replace")

    result = {
        "story_key": story_key,
        "status": story.get("status"),
        "current_stage": story.get("current_stage"),
        "plan_summary": plan_summary,
        "plan_content": plan_content,
        "adapter": active_exec.get("adapter", ""),
        "confirmed": ctx.get("_plan_confirmed", False),
        "mode": "agent" if agent_actions else "legacy",
        "stages": stages_view,
        "stage_gate": stage_gate,
        "lifecycle_state": cur_lifecycle,
        "story_states": story_states_view,
        "story_state_gate": story_state_gate,
    }

    # Agent 模式：返回结构化 action list
    if agent_actions:
        result["actions"] = agent_actions

    return result


@app.get("/api/story/{story_key}/plan/stream")
async def api_plan_stream(story_key: str):
    """SSE 流式规划 — Agent Function Calling 模式。

    Agent 通过 plan_step/skip_stage 工具调用生成结构化 action list。
    每个 action 实时通过 SSE 推送到前端。
    """

    story = db.get_story(story_key)
    if not story:

        async def error_stream(msg: str):
            yield f"data: {json.dumps({'type': 'error', 'message': msg}, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            error_stream("story not found"), media_type="text/event-stream"
        )

    import json

    async def generate():
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def capture_action(event: dict):
            """线程安全回调：把事件放入 asyncio.Queue，实时推送到 SSE。"""
            loop.call_soon_threadsafe(queue.put_nowait, event)

        # 立即发送 started 事件，让前端知道规划已开始
        yield f"data: {json.dumps({'type': 'started', 'message': 'Agent 开始规划...'}, ensure_ascii=False)}\n\n"

        # 在线程池中执行同步阻塞的 Agent 规划
        def run_planning():
            try:
                result = planner.run_orchestrator_agent(
                    story_key, on_action=capture_action
                )
                loop.call_soon_threadsafe(
                    queue.put_nowait, {"__sentinel__": "done", "result": result}
                )
            except Exception as e:
                import logging

                logging.getLogger("story-lifecycle.api").error(
                    f"Agent planning failed for {story_key}: {e}"
                )
                loop.call_soon_threadsafe(
                    queue.put_nowait, {"__sentinel__": "error", "error": str(e)}
                )

        asyncio.ensure_future(asyncio.to_thread(run_planning))

        # 实时从队列读取并推送
        while True:
            event = await queue.get()
            if "__sentinel__" in event:
                sentinel = event["__sentinel__"]
                if sentinel == "done":
                    result = event["result"]
                    yield f"data: {json.dumps({'type': 'done', 'actions': result.get('actions', [])}, ensure_ascii=False)}\n\n"
                elif sentinel == "error":
                    yield f"data: {json.dumps({'type': 'error', 'message': event['error']}, ensure_ascii=False)}\n\n"
                break
            # 实时推送 action 事件
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/api/story/{story_key}/plan/confirm")
def api_confirm_plan(story_key: str, body: dict | None = Body(default=None)):
    """用户确认规划，启动执行。

    可选 body.actions:用户在前端改过的 per-stage adapter 覆盖,格式
    [{"stage": "design", "adapter": "kimi"}, ...]。覆盖写回 _agent_actions。
    """
    story = db.get_story(story_key)
    if not story:
        raise HTTPException(status_code=404, detail="story not found")

    import json

    ctx = json.loads(story.get("context_json") or "{}")

    # 用户在前端改了 adapter 时,覆盖 _agent_actions
    if body and body.get("actions"):
        _overrides = {
            a["stage"]: a.get("adapter")
            for a in body["actions"]
            if a.get("stage") and a.get("adapter")
        }
        for action in ctx.get("_agent_actions", []):
            _st = action.get("stage")
            if _st in _overrides:
                action["adapter"] = _overrides[_st]

    ctx["_plan_confirmed"] = True

    db.update_story(
        story_key,
        context_json=json.dumps(ctx, ensure_ascii=False),
        status="active",
    )

    start_story_async(story_key)
    return {"ok": True, "story_key": story_key}


@app.post("/api/story/{story_key}/plan/regenerate")
def api_regenerate_plan(story_key: str):
    """重新生成规划（Agent 模式）。"""
    story = db.get_story(story_key)
    if not story:
        raise HTTPException(status_code=404, detail="story not found")

    import json

    # 清除旧的 agent actions
    ctx = json.loads(story.get("context_json") or "{}")
    ctx.pop("_agent_actions", None)
    ctx["_plan_confirmed"] = False
    db.update_story(
        story_key,
        context_json=json.dumps(ctx, ensure_ascii=False),
        status="planning",
    )

    # 重新触发 Agent 规划
    try:
        result = planner.run_orchestrator_agent(story_key)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Agent 规划失败: {e}")

    return {"ok": True, "actions": result.get("actions", [])}


class AnswerRequest(BaseModel):
    answer: str


@app.post("/api/story/{story_key}/answer")
def api_answer_wait(story_key: str, req: AnswerRequest):
    """用户回答 CLI 的等待确认问题（human-in-the-loop）。

    CLI 写入 .story-wait/{stage}.json，用户通过此端点回答。
    Agent 将回答写入 .story-wait/{stage}.answer.json。
    """
    story = db.get_story(story_key)
    if not story:
        raise HTTPException(status_code=404, detail="story not found")

    import json

    workspace = story.get("workspace", "")

    # 查找 wait 文件
    wait_dir = Path(workspace) / ".story-wait"
    wait_files = list(wait_dir.glob(f"{story_key}-*.json")) if wait_dir.exists() else []

    if not wait_files:
        raise HTTPException(status_code=404, detail="No pending wait question found")

    # 处理第一个 wait 文件
    wait_path = wait_files[0]
    answer_path = wait_path.with_suffix(".answer.json")
    answer_path.write_text(
        json.dumps({"answer": req.answer}, ensure_ascii=False),
        encoding="utf-8",
    )

    return {"ok": True, "wait_file": str(wait_path.name), "answer": req.answer}


@app.get("/api/story/{story_key}/wait")
def api_get_wait_question(story_key: str):
    """获取当前 CLI 等待确认的问题。"""
    story = db.get_story(story_key)
    if not story:
        raise HTTPException(status_code=404, detail="story not found")

    import json

    workspace = story.get("workspace", "")
    wait_dir = Path(workspace) / ".story-wait"
    wait_files = list(wait_dir.glob(f"{story_key}-*.json")) if wait_dir.exists() else []

    if not wait_files:
        return {"ok": True, "waiting": False}

    wait_path = wait_files[0]
    try:
        question = json.loads(wait_path.read_text(encoding="utf-8"))
    except Exception:
        question = {
            "raw": wait_path.read_text(encoding="utf-8", errors="replace")[:500]
        }

    return {
        "ok": True,
        "waiting": True,
        "question": question,
        "file": str(wait_path.name),
    }


# -------- design 逐问澄清 HITL(外接 MCP;事件驱动) --------


@app.get("/api/story/{story_key}/clarify")
def api_get_clarify(story_key: str):
    """取当前待答澄清问题(design 逐问 HITL,前端轮询用)。无待答 → {waiting: false}。

    事件驱动:claude 调 ``mcp__lifecycle__clarify`` → MCP server 落 ``clarification_request``
    事件 → 本端点从事件查「最新未答 request」。详见 ``orchestrator/mcp/clarify_server.py``。
    """
    story = db.get_story(story_key)
    if not story:
        raise HTTPException(status_code=404, detail="story not found")
    from ..mcp.clarify_server import get_pending_clarification

    pending = get_pending_clarification(story_key, get_events_fn=db.get_story_events)
    if not pending:
        return {"ok": True, "waiting": False, "status": story.get("status")}
    return {
        "ok": True,
        "waiting": True,
        "status": story.get("status"),
        "question": pending,
    }


class ClarifyAnswerRequest(BaseModel):
    answer: str
    id: str | None = None


@app.post("/api/story/{story_key}/clarify/answer")
def api_clarify_answer(story_key: str, req: ClarifyAnswerRequest):
    """回答当前待答澄清 → 落 clarification_answer 事件 → MCP server 解除 claude 阻塞。

    claude 此刻**阻塞在 mcp__lifecycle__clarify 调用上**(同一进程,不重 spawn);本端点
    只落 answer 事件,MCP server 的 poll_clarify_answer 拾取后返回 → claude 带答继续。
    """
    story = db.get_story(story_key)
    if not story:
        raise HTTPException(status_code=404, detail="story not found")
    from ..mcp.clarify_server import get_pending_clarification

    pending = get_pending_clarification(story_key, get_events_fn=db.get_story_events)
    if not pending:
        raise HTTPException(status_code=404, detail="No pending clarification")
    rid = req.id or pending.get("id")
    db.log_event(
        story_key,
        "design",
        "clarification_answer",
        {"id": rid, "question": pending.get("question"), "answer": req.answer},
    )
    return {
        "ok": True,
        "id": rid,
        "question": pending.get("question"),
        "answer": req.answer,
    }


@app.get("/api/story/{story_key}/clarify/stream")
async def api_clarify_stream(story_key: str):
    """SSE:推 design 澄清事件(clarification_request / clarification_answer)+ 状态。

    复用 plan stream 的 StreamingResponse 模式;轮询 DB event_log + story status,
    有新澄清事件或状态变化即推。前端 EventSource 接;断开会自动重连。
    """
    import json

    story = db.get_story(story_key)
    if not story:

        async def err(msg):
            yield f"data: {json.dumps({'type': 'error', 'message': msg}, ensure_ascii=False)}\n\n"

        return StreamingResponse(err("story not found"), media_type="text/event-stream")

    async def gen():

        yield f"data: {json.dumps({'type': 'status', 'status': story.get('status')}, ensure_ascii=False)}\n\n"
        seen_ids: set[int] = set()
        idle = 0
        # 最多流 ~10min(前端 EventSource 断开会重连);design 澄清一轮通常 < 5min。
        for _ in range(400):
            cur = db.get_story(story_key) or {}
            status = cur.get("status")
            # 推本轮澄清相关事件
            try:
                events = db.get_story_events(story_key)
            except Exception:
                events = []
            for ev in events:
                etype = ev.get("event_type", "")
                if etype not in ("clarification_request", "clarification_answer"):
                    continue
                if ev.get("id") in seen_ids:
                    continue
                seen_ids.add(ev.get("id"))
                payload = ev.get("payload") or {}
                yield f"data: {json.dumps({'type': etype, **payload}, ensure_ascii=False)}\n\n"
            # 状态变化推送
            yield f"data: {json.dumps({'type': 'status', 'status': status}, ensure_ascii=False)}\n\n"
            # 终态:design 已离开 awaiting-clarify 且无新事件 → 收尾
            if status not in ("awaiting-clarify", "active", "implementing", "planning"):
                idle += 1
                if idle > 2 or status in ("completed", "failed"):
                    yield f"data: {json.dumps({'type': 'done', 'status': status}, ensure_ascii=False)}\n\n"
                    return
            else:
                idle = 0
            await asyncio.sleep(1.5)

    return StreamingResponse(gen(), media_type="text/event-stream")


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


# -------- static frontend (must be last) --------

_WEB_DIR = Path(__file__).parent.parent.parent / "entry" / "web"
if _WEB_DIR.is_dir() and any(_WEB_DIR.iterdir()):
    # Mount static assets directly (JS, CSS, favicon, etc.)
    _assets_dir = _WEB_DIR / "assets"
    if _assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(_assets_dir)), name="assets")

    # Favicon
    _favicon = _WEB_DIR / "favicon.svg"
    if _favicon.exists():

        @app.get("/favicon.svg", include_in_schema=False)
        async def favicon():
            return FileResponse(str(_favicon))

        @app.get("/favicon.ico", include_in_schema=False)
        async def favicon_ico():
            return FileResponse(str(_favicon))

    # SPA fallback: serve index.html for all unmatched routes
    _index_html = _WEB_DIR / "index.html"
    if _index_html.exists():

        @app.get("/{path:path}", include_in_schema=False)
        async def spa_fallback(path: str):
            """SPA fallback: serve index.html for all non-API routes."""
            if path.startswith("api/") or path.startswith("ws/"):
                raise HTTPException(404, "Not Found")
            return FileResponse(str(_index_html))

        @app.get("/", include_in_schema=False)
        async def root():
            return FileResponse(str(_index_html))
