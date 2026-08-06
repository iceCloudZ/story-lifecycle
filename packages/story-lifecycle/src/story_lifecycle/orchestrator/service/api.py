"""FastAPI server — REST API for story management and terminal access."""

import asyncio
import json
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
)
from ...infra.terminal.sid_capture import arm_sid_capture, now_utc_iso
from ..engine.graph import (
    start_story_async,
    recover_orphan_stories,
    force_stop_story,
)
from ..engine.profile_loader import resolve_profile
from ..engine import planner
from ...sourcing.state_machine import (
    activate as sm_activate,
    mark_completed as sm_mark_completed,
    pause as sm_pause,
)
from ...sourcing.lifecycle_state import LifecycleState


log = logging.getLogger("story-lifecycle.api")

from .routers.sessions import (  # noqa: F401  (设计15 C3b: 测试直接 import 路由函数)
    _story_headless,
    _spawn_story_agent_pty,
    _ensure_story_agent_pty,
    api_list_sessions,
    api_get_session,
    api_writeback_session,
    api_spawn_session,
    api_spawn_pty,
    api_kill_session,
    api_kill_all_pty,
    api_kill_pty,
    get_terminal,
    SpawnSessionRequest,
    WritebackSessionRequest,
)

from ._shared import _workspace_root_for_project

from ._shared import (
    _load_tapd_config,
    _serialize_story_summary,
    _story_list_json,
    _resolve_workspace_or_404,
    _wiki_knowledge_root,
    _get_story_documents,
    _get_story_change_items,
)


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
    # 设计13:全局编排线程替代 _watch_interactive_done_files（watcher）+
    # consume_orphan_artifacts（GET /story 副作用）—— 一个线程管所有 story 的 PTY。
    from ..scheduler import get_orchestrator, stop_orchestrator

    get_orchestrator()
    try:
        yield
    finally:
        stop_orchestrator()
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

    # 每连接一个 tap(广播副本),不再竞争性消费主 _queue:多客户端各自收全量,
    # 旧连接残留的 reader 也不会偷走新连接的输出。
    tap = pty.add_tap()

    async def read_and_send():
        # 先回放 scrollback:tab 切换/刷新后的重连能补回屏幕内容(此前 attach
        # 只转发新输出,空闲会话重连 = 黑屏)。tap 在回放前注册,回放期间到的
        # 新输出进 tap 随后续直播发出,不丢。
        backlog = pty.scrollback()
        if backlog:
            await ws.send_bytes(backlog)
        while pty.alive:
            try:
                data = await asyncio.wait_for(tap.get(), timeout=0.5)
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
    finally:
        pty.remove_tap(tap)


@app.websocket("/ws/pty/{story_id}/{session_id}")
async def pty_ws_multi(ws: WebSocket, story_id: str, session_id: str):
    """Multi-session PTY WebSocket."""
    await _pty_ws_handler(ws, story_id, session_id)


@app.websocket("/ws/pty/{story_id}")
async def pty_ws(ws: WebSocket, story_id: str):
    """Legacy single-PTY WebSocket."""
    await _pty_ws_handler(ws, story_id, "")


# ---- Multi-Session Management API ----
# -------- story CRUD --------
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
# -------- Timeline API (Task 3.1) --------
# -------- Gate History API (Task 3.2) --------
# -------- Loop Trace API (Task 3.3) --------
# -------- Findings API enhancement (Task 3.4) --------
# -------- quality endpoints --------
# -------- Dependency Graph API (Task 3.5) --------
# -------- Patterns API enhancement (Task 3.7) --------
# -------- observability / debug --------
# -------- TAPD Sync API --------
# -------- Context endpoints --------
# -------- Project registry endpoints --------
# -------- Workspace entity endpoints (11-workspace-entity-design.md Phase 2) --------
# 与 /api/workspaces(旧含义:intake 主工作区目录选项)区分:
# workspace-entities 是新的业务项目实体。旧端点保持不动,前端 IntakeStartModal 继续用。
# -------- Test environment endpoints(测试 tab) --------
# -------- Wiki endpoints(11-workspace-entity-design.md §4/§5, Phase 3) --------
# -------- Worktree endpoints --------
# -------- Delivery artifact endpoints --------
# -------- Versioned docs endpoints (story_doc / story_doc_version) --------
# DB is the source of truth for business docs (prd/spec/plan/research/...).
# The API layer syncs the latest version to a local .md cache on save so code
# agents read files (not DB) and execution stays independent of DB availability.
# -------- Lifecycle endpoints --------
# -------- design 逐问澄清 HITL(外接 MCP;事件驱动) --------
# -------- helpers --------


# -------- domain routers（设计15 阶段C） --------

from .routers import (
    change_items,
    deliverables,
    deliveries,
    diagnostics,
    patterns,
    projects,
    wiki,
    worktrees,
)
for _mod in (
    diagnostics,
    change_items,
    deliverables,
    worktrees,
    deliveries,
    projects,
    wiki,
    patterns,
):
    app.include_router(_mod.router)



# -------- domain routers C2（设计15 阶段C） --------

from .routers import bugs, documents, quality, timeline, workspaces
for _mod in (
    timeline,
    quality,
    documents,
    bugs,
    workspaces,
):
    app.include_router(_mod.router)



# -------- domain routers C3（设计15 阶段C） --------

from .routers import context
for _mod in (context,):
    app.include_router(_mod.router)



# -------- domain routers C3b（设计15 阶段C） --------

from .routers import sessions
for _mod in (sessions,):
    app.include_router(_mod.router)



# -------- domain routers C3c（设计15 阶段C） --------

from .routers import intake, lifecycle, plan, stories, sync
for _mod in (stories, lifecycle, plan, sync, intake):
    app.include_router(_mod.router)

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



