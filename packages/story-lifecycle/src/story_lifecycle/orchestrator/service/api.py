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


class SetLifecycleRequest(BaseModel):
    state: str  # lifecycle_state 目标值(待启动/开发/测试/上线/结项),5 态全开放


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


class SpawnSessionRequest(BaseModel):
    adapter: str = "claude"
    model: str = ""


class WritebackSessionRequest(BaseModel):
    """Agent(在用户终端里)把它自己的 session id 回写给后端。

    stage/adapter 缺省取 story 当前值。session_id 必填(agent 从它自己的 CLI 环境拿到)。
    """

    session_id: str
    stage: str = ""
    adapter: str = ""


@app.get("/api/story/{story_key}/sessions")
def api_list_sessions(story_key: str):
    """List all sessions for a story.

    以 DB story_session 行为主(stage/adapter/session_id/created_at 真实值),
    PTY 注册表只用来查**实时存活态**(running/exited)覆盖 status。

    关联:按 (stage, adapter) 关联 DB 行与 PTY 行 —— 不按 session_id 字符串,
    因为 kimi 的 DB session_id 是捕获值(session_<uuid>),≠ PTY key
    (compute_session_id)。两者都挂在同一 (story,stage,adapter) 上。

    DESIGN-session-pty-id-model.md §3.4 / 问题 2、3:此前 status 直读 DB 静态值
    ('active'),且把 PTY 行当新行 append(去重失败)→ 同一会话出现两次 + 死进程
    显示 active。现在 status 实时从 PTY alive 派生,不再重复 append。
    """
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")
    db_sessions = db.list_sessions_for_story(story_key)
    # 建 (stage, adapter) → PTY 行 映射。
    pty_by_key: dict[tuple[str, str], dict] = {}
    for p in list_pty_sessions(story_key):
        pty_by_key[(p.get("stage", ""), p.get("adapter", ""))] = p
    result = []
    for row in db_sessions:
        stage = row.get("stage", "")
        adapter = row.get("adapter", "")
        # PTY 存活态覆盖 status:PTY 有该 (stage,adapter) 活进程 → running,否则 exited。
        # DB 的 status 是静态业务态(active/completed),不反映进程存活,不进前端 status。
        pty_row = pty_by_key.get((stage, adapter))
        alive = bool(pty_row and pty_row.get("status") == "running")
        result.append(
            {
                "session_id": row.get("session_id") or "",
                # attach_id:WS attach 凭据。kimi 这类 CLI 自分配 sid 的 adapter,
                # DB sid 退出时才捕获回填,运行期间是 "";活 PTY 的注册表 id
                # (compute_session_id)才是 /ws/pty/{story}/{id} 能用的凭据。
                "attach_id": pty_row.get("session_id", "") if alive else "",
                "adapter": adapter,
                "stage": stage,
                "model": "",
                "status": "running" if alive else "exited",
                "started_at": row.get("created_at", ""),
                # 设计12 改动3:stage 完成摘要(judge_stage_completion 的 summary)。
                "completion_summary": row.get("completion_summary") or "",
            }
        )
    return {"sessions": result}


@app.get("/api/story/{story_key}/session")
def api_get_session(story_key: str, stage: str = "", adapter: str = "") -> dict:
    """读已回填的 session id(前端「复制 resume 文案」按钮用)。

    stage/adapter 缺省取 story 当前值。返回该 (story,stage,adapter) 的 session 行;
    无则 session_id=null(前端据此禁用 resume 按钮)。
    """
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")
    _stage = stage or s.get("current_stage", "") or "design"
    _adapter = adapter or ""
    if not _adapter:
        # 兜底:从 _agent_actions 取当前 stage 的 adapter,否则默认 claude。
        try:
            import json as _json

            _ctx = _json.loads(s.get("context_json") or "{}")
            acts = _ctx.get("_agent_actions") or []
            _adapter = (
                next((a.get("adapter") for a in acts if a.get("stage") == _stage), "")
                or "claude"
            )
        except (ValueError, TypeError):
            _adapter = "claude"
    row = db.get_session(story_key, _stage, _adapter)
    return {
        "session_id": row.get("session_id") if row else None,
        "adapter": _adapter,
        "stage": _stage,
        "status": row.get("status") if row else None,
    }


@app.post("/api/story/{story_key}/session")
def api_writeback_session(story_key: str, req: WritebackSessionRequest) -> dict:
    """Agent 回写它自己的 session id(半自动:用户终端里的 agent 调 story session)。

    把 agent 当前会话 id 落进 story_session 表 → 前端「复制 resume 文案」能读到 →
    下次用户复制 `claude --resume <id>` / `kimi -S <id>` 续上,省 token。
    """
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")
    if not req.session_id:
        raise HTTPException(400, "session_id is required")
    _stage = req.stage or s.get("current_stage", "") or "design"
    _adapter = req.adapter or "claude"
    # upsert(若已有行则更新 session_id;COALESCE 在 models 层,这里直接 set)。
    db.upsert_session(story_key, _stage, _adapter, session_id=req.session_id)
    db.log_event(
        story_key,
        _stage,
        "session_writeback",
        {"adapter": _adapter, "session_id": req.session_id},
    )
    return {
        "ok": True,
        "session_id": req.session_id,
        "adapter": _adapter,
        "stage": _stage,
    }


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
    # adapter 来源(req 显式传 > _agent_actions[当前 stage] > profile > claude):
    # 前端 TerminalTab 默认传空,这里走 resolver 拿用户在 plan UI 选的 adapter。
    # 老逻辑硬编码 "claude" → 用户在 plan 改 kimi,点启动终端还是 claude,不一致。
    if req.adapter:
        adapter_name = req.adapter
    else:
        import json as _json

        _ctx = {}
        try:
            _ctx = _json.loads(s.get("context_json") or "{}")
        except (ValueError, TypeError):
            pass
        _stage = s.get("current_stage", "design") or "design"
        _action = next(
            (a for a in (_ctx.get("_agent_actions") or []) if a.get("stage") == _stage),
            None,
        )
        from ..engine.planner import resolve_stage_adapter

        adapter_name = resolve_stage_adapter(s, _stage, action=_action)
    try:
        adapter = get_adapter(adapter_name)
    except ValueError as exc:
        # 未知 adapter:get_adapter 抛 ValueError(消息含 builtin/configured 名单)。
        # 这是客户端错误(用户传了非法 adapter)→ 400,不是 500。原先未捕获直接 500。
        raise HTTPException(status_code=400, detail=str(exc))
    model = req.model or getattr(adapter, "default_model", "sonnet")
    # 复用检查:该 (story, stage, adapter) 已有存活 agent PTY 时直接返回现有
    # session,不重复 spawn(对齐 _ensure_story_agent_pty;此前每次点击都新起
    # 进程并覆盖注册表条目,旧进程泄漏)。用户点「启动终端」想看的往往就是
    # driver 正在跑的那个会话 —— 复用即可 attach。
    _stage = s.get("current_stage", "design") or "design"
    _reuse_sid = db.compute_session_id(story_key, _stage, adapter_name)
    existing = get_pty(story_key, _reuse_sid)
    if existing and existing.alive and existing.purpose == "agent":
        return {
            "session_id": existing.session_id,
            "ok": True,
            "resumed": False,
            "reused": True,
        }
    # adapter-aware spawn:claude 走 prompt-in-cmd,kimi/codex 走 PTY 注入。
    # 老逻辑直接 spawn_pty(command),对 kimi 来说 command 不带 prompt → 空会话。
    session_id, _, is_resume = _spawn_story_agent_pty(s, adapter, model)
    return {"session_id": session_id, "ok": True, "resumed": is_resume}


@app.post("/api/pty/{story_id}/spawn")
def api_spawn_pty(story_id: str):
    """Start or reuse the story's interactive agent PTY (legacy, single-session)."""
    s = db.get_story(story_id)
    if not s:
        raise HTTPException(404, "Story not found")
    return _ensure_story_agent_pty(s)


def _spawn_story_agent_pty(
    story: dict, adapter, model: str
) -> tuple[str, object, bool]:
    """Spawn the story's agent PTY from an adapter SessionSpec.

    Single, adapter-agnostic contract: ask the adapter how it wants the
    session started (``start_session`` returns a SessionSpec with command +
    prompt delivery strategy), then spawn mechanically:
      spawn spec.command → wait spec.readiness_marker → paste spec.pty_prompt.

    No branching on adapter type. claude bakes prompt into command (pty_prompt
    empty); kimi/codex use PTY paste after readiness_marker. Both adapters
    express that via the spec; this helper just executes it.

    Both ``api_spawn_session`` and ``_ensure_story_agent_pty`` go through here
    so the two spawn paths can't drift on prompt delivery (the prior bug:
    sessions/spawn used ``spawn_pty`` which never injects → empty kimi session).

    设计 14 (D3)：spawn 主体收敛到 infra/terminal/spawn_recipe.spawn_agent_pty
    （与 executors.InteractiveStageExecutor.spawn 同源），本函数保留 api 路径
    特有的「死后 resume 重试 + 清 marker」后处理。
    """
    import json as _json

    from ...infra.story_paths import build_story_spawn_env
    from ...infra.terminal.spawn_recipe import spawn_agent_pty

    workspace = story.get("workspace", "")
    story_key = story["story_key"]
    stage = story.get("current_stage", "design") or "design"
    # agent 的 cwd:优先 workspace_path(规划 LLM 决定的隔离空间 D:/worktrees/<slug>/),
    # 没有则退回主 workspace。code agent 在隔离空间里自己 worktree add 项目进来。
    _ctx_spawn = {}
    try:
        _ctx_spawn = _json.loads(story.get("context_json") or "{}")
    except (ValueError, TypeError):
        pass
    spawn_cwd = _ctx_spawn.get("workspace_path") or workspace
    # 确保 cwd 存在：规划 LLM 可能设了 workspace_path（如 D:/worktrees/<slug>/）但
    # 目录还没创建（worktree add 是 code agent 的职责，spawn 时可能还没跑）。
    # claude/opencode 在不存在的 cwd 里启动会立即退出 → session 表空 → 用户看不到终端。
    try:
        Path(spawn_cwd).mkdir(parents=True, exist_ok=True)
        log.info(
            "[%s] spawn cwd ensured: %s (exists=%s)",
            story_key,
            stage,
            Path(spawn_cwd).exists(),
        )
    except Exception as exc:
        log.warning("[%s] mkdir spawn_cwd failed: %s", story_key, exc)

    _adapter_name = getattr(adapter, "name", "") or ""
    # 设计 14 (D4)：seed 构建统一走 prompts.LaunchSeedBuilder（同一份
    # read-file seed 契约）。
    from ..prompts import LaunchSeedBuilder

    seed = LaunchSeedBuilder().build(
        story_key=story_key,
        stage=stage,
        workspace=workspace,
        ctx=_ctx_spawn,
        action={},
    )
    _res = spawn_agent_pty(
        adapter,
        model,
        story_key=story_key,
        stage=stage,
        workspace=workspace,
        spawn_cwd=spawn_cwd,
        seed=seed,
        env=build_story_spawn_env(story, stage, _adapter_name),
    )
    session_id, pty = _res.session_id, _res.pty
    is_resume = _res.is_resume
    marker, _use_sid = _res.marker, _res.use_sid
    _prespecified = bool(getattr(adapter, "prespecified_session_id", False))

    # spawn 后存活检查：PTY 立即死了（cwd 不存在 / resume 死 sid / claude 崩溃）
    # 时清 marker + DB session，让下次 spawn 起新会话而非反复 resume 死 sid 秒退。
    # 现象：spawn 返回 resumed=True 但 claude 进程不存在 → 前端「没有 CLI 会话」死循环。
    import time as _time

    _time.sleep(1.5)  # 给 claude 一点启动时间
    if not getattr(pty, "alive", True):
        # claude 确定性 UUID 的特殊处理：NEW spawn 用 --session-id <uuid>，但 claude
        # 存储里已有这个 UUID（之前 spawn 创建过）→ "already in use" 秒退。
        # 重试：用 --resume <uuid> 加载已有 transcript（claude session 存在就能 resume）。
        if not is_resume and _prespecified:
            log.warning(
                "[%s] PTY died after NEW spawn (stage=%s adapter=%s sid=%s) "
                "— retrying with --resume (session may exist in claude storage)",
                story_key,
                stage,
                _adapter_name,
                _use_sid,
            )
            try:
                pty.kill()
            except Exception:
                pass
            retry_spec = adapter.start_session(
                model,
                prompt="继续上次的任务,完成后按完成协议写入 done 文件。",
                session_id=_use_sid,
                session_name=f"{story_key}-{stage}",
                resume=True,
            )
            session_id, pty = ensure_agent_pty(
                story_key,
                stage,
                _adapter_name,
                retry_spec.command,
                spawn_cwd,
                retry_spec.pty_prompt,
                env=build_story_spawn_env(story, stage, _adapter_name),
                readiness_marker=retry_spec.readiness_marker,
                startup_delay=2.0,
            )
            _time.sleep(1.5)
            if getattr(pty, "alive", False):
                log.info(
                    "[%s] retry --resume succeeded (stage=%s sid=%s)",
                    story_key,
                    stage,
                    _use_sid,
                )
                is_resume = True
                return session_id, pty, is_resume
        # resume 重试也死了（或非 prespecified adapter）→ 清理 marker + DB
        log.warning(
            "[%s] PTY died immediately after spawn (stage=%s adapter=%s "
            "is_resume=%s) — clearing marker for fresh spawn next time",
            story_key,
            stage,
            _adapter_name,
            is_resume,
        )
        try:
            marker.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            db.delete_session(story_key, stage, _adapter_name)
        except Exception:
            pass
        is_resume = False
    return session_id, pty, is_resume


def _ensure_story_agent_pty(story: dict) -> dict:
    workspace = story.get("workspace", "")
    if not workspace or not Path(workspace).exists():
        raise HTTPException(400, "Invalid workspace")

    profile = resolve_profile(story.get("profile", "minimal"))
    stage = story.get("current_stage", "design") or "design"
    # adapter 来源:优先 _agent_actions[当前 stage].adapter(用户在 plan UI 改的),
    # profile cli 仅兜底。两条 spawn 路径(这里 + continue_orchestrator_agent)
    # 必须用同一个 resolver,否则 UI 改 kimi 这里还跑 claude。
    import json as _json

    _ctx = {}
    try:
        _ctx = _json.loads(story.get("context_json") or "{}")
    except (ValueError, TypeError):
        pass
    _action = next(
        (a for a in (_ctx.get("_agent_actions") or []) if a.get("stage") == stage),
        None,
    )
    from ..engine.planner import resolve_stage_adapter

    adapter_name = resolve_stage_adapter(story, stage, profile=profile, action=_action)
    stage_cfg = profile.stage(stage)
    model = stage_cfg.model or profile.model or "sonnet"
    # 复用检查:按 (story, stage, adapter) 精确查注册表(不再是「第一个 alive」)。
    # 之前 get_pty(story) 无 session_id 返回任意 alive 会话,且 existing.session_id
    # 读不存在的属性(问题 5)。现在注册表 key = compute_session_id,能精确命中。
    _reuse_sid = db.compute_session_id(story["story_key"], stage, adapter_name)
    existing = get_pty(story["story_key"], _reuse_sid)
    reused = bool(existing and existing.alive and existing.purpose == "agent")
    if reused:
        # 该 stage 已有存活会话 —— 直接返回,不重复 spawn(避免孤儿/重复)
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
    # adapter-aware spawn:claude prompt-in-cmd,kimi/codex 走 PTY 注入 +
    # readiness_marker。统一走 _spawn_story_agent_pty,两条 spawn 路径不再分叉。
    session_id, _, is_resume = _spawn_story_agent_pty(story, adapter, model)
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
    in time. Called by the serve-restart bat (before its process-tree kill) and by serve
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
    # 设计13：GET /story 去掉副作用（consume_orphan_artifacts 已被全局编排线程
    # 的 poll-artifacts 替代 —— 编排线程每轮检查所有 active story 的成果物，
    # 打开详情页不再需要触发认领）。
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


@app.get("/api/story/{story_key}/llm-calls")
def get_story_llm_calls(story_key: str):
    """Prompt/response/reasoning bodies for every LLM call in a story.

    Audit endpoint: JOINs llm_call (正文) ↔ llm_trace (指标) by trace_id, ordered
    by call id. Use this to inspect what was asked/answered/thought across an
    orchestration run. Bodies are stored unconditionally (no config switch).
    """
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")

    calls = db.get_story_llm_calls(story_key)
    return {"story_key": story_key, "calls": calls}


@app.get("/api/story/{story_key}/diff")
def get_story_diff(story_key: str, project_id: int | None = None):
    """Return git diff for the story's workspace branch vs its base branch.

    Query params:
        project_id: scope the diff to a single bound project — prefers its
            worktree_path (the agent's checkout), falling back to repo_path.
            Omit to diff the story workspace / first viable project (legacy).

    Returns:
        current_branch, base_branch, diff_range, files[], total_additions,
        total_deletions, total_changes, diff (raw unified diff text),
        project_id, repo_path, worktree_path.
    """
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")

    try:
        result = get_story_workspace_diff(story_key, project_id=project_id)
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
        sm_activate(story_key)
        start_story_async(story_key)
        return {"ok": True, "status": "resumed"}

    # Start an active-but-never-started story (single-pass 等 profile 创建即 active,
    # 但执行从未触发:无 _active_execution)。overview 的「开始执行」按钮走这里。
    # 区别于 paused→resume:这是首次启动。已在跑的(有 _active_execution)不重复触发
    # (start_story_async 的 CAS 也会兜底,但这里提前返回避免无谓 claim 抖动)。
    # failed 也走这里:用户点「恢复执行」重试失败 stage（清 _active_execution + 重激活）。
    if s["status"] == "failed":
        import json as _json

        try:
            _ctx = _json.loads(s.get("context_json") or "{}")
        except (ValueError, TypeError):
            _ctx = {}
        # failed → active 重试:清执行标记 + lastError,重新激活。
        _ctx.pop("_active_execution", None)
        sm_activate(story_key, ctx_updates=_ctx)
        start_story_async(story_key)
        return {"ok": True, "status": "retried"}

    if s["status"] == "active":
        import json as _json

        try:
            _ctx = _json.loads(s.get("context_json") or "{}")
        except (ValueError, TypeError):
            _ctx = {}
        if not _ctx.get("_active_execution"):
            start_story_async(story_key)
            return {"ok": True, "status": "started"}

    return {"ok": True}


@app.post("/api/story/{story_key}/lifecycle/advance")
def advance_lifecycle_state(story_key: str):
    """推进 Story 业务状态到下一态(待启动→开发→测试→上线→结项)。

    成果物 gate 驱动:推进前检查该转换的成果物是否全部满足(exists+confirmed
    或 skipped)。不满足则 409 返回缺失列表(前端显示「还差:测试报告」)。
    gate 满足 → 推进 lifecycle_state → 若下一状态有 stages 则 start_story_async,
    无(终态)则标 completed。
    """
    import json as _json

    from ...sourcing.deliverables import gate_for_current_state, gate_satisfied

    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")
    try:
        ctx = _json.loads(s.get("context_json") or "{}")
    except (ValueError, TypeError):
        ctx = {}

    cur_state = s.get("lifecycle_state") or "待启动"

    # 设计12 改动1:LLM 裁判(judge_stage_completion)判的 lifecycle_target 中途遇
    # ui_button 停住(planner 写 _story_state_gate.final_target)。用户在此确认 → 续推:
    # 刚确认的转换 (gate.from → gate.to) 已批准,先落这一格,再从 to 续推到 final_target。
    # 不查交付物 gate —— LLM 已看累积产出判过 target 可达(设计 §1.5)。
    gate = ctx.get("_story_state_gate") or {}
    final_target = gate.get("final_target") if gate.get("awaiting_confirm") else None
    if final_target:
        from ..evaluation.stage_completion import advance_lifecycle_to_target

        try:
            from ...sourcing.source_loader import resolve_source_profile

            states = resolve_source_profile(s.get("source_type")).story_states or {}
        except Exception:
            states = {}
        _confirmed_from = gate.get("from") or cur_state
        _resume_from = gate.get("to") or cur_state
        # 刚确认的闸已处理:清除(advance_lifecycle_to_target 遇下一个 ui_button 会重写)。
        ctx.pop("_story_state_gate", None)
        if _resume_from != _confirmed_from and _resume_from != cur_state:
            # 应用已确认的转换(from → to)
            ctx["_lifecycle_state"] = _resume_from
            db.update_story(
                story_key,
                lifecycle_state=_resume_from,
                context_json=_json.dumps(ctx, ensure_ascii=False),
            )
            db.log_event(
                story_key,
                "",
                "story_state_transition",
                {
                    "from": _confirmed_from,
                    "to": _resume_from,
                    "auto": False,
                    "confirmed": True,
                },
            )
        result = advance_lifecycle_to_target(
            story_key=story_key,
            ctx=ctx,
            current=_resume_from,
            target=final_target,
            story_states=states,
        )
        new_state = result["new_state"]
        if result["paused_for_confirm"]:
            # 又遇 ui_button(多级跳转的中间确认格)→ 继续 paused
            return {"ok": True, "lifecycle_state": new_state, "status": "paused"}
        # 已到 final_target → 看该状态有无 stages:无(终态)则完成,有则继续跑
        next_def = states.get(new_state) or {}
        next_stages = list(next_def.get("stages") or [])
        if not next_stages:
            sm_mark_completed(story_key)
            return {"ok": True, "lifecycle_state": new_state, "status": "completed"}
        start_story_async(story_key)
        return {"ok": True, "lifecycle_state": new_state, "status": "active"}

    # 成果物 gate 检查(取代旧的 _story_state_gate.awaiting_confirm 检查)。
    gate_info = gate_for_current_state(story_key)
    if not gate_info:
        raise HTTPException(409, "已到终态,无法推进")
    next_state = gate_info["to"]
    satisfied, missing = gate_satisfied(story_key, cur_state, next_state)
    if not satisfied:
        raise HTTPException(
            409,
            f"成果物 gate 未满足,还差: {'、'.join(missing)}",
        )

    # gate 满足 → 推进。清旧的 _story_state_gate(向后兼容老数据)。
    ctx.pop("_story_state_gate", None)
    ctx["_lifecycle_state"] = next_state
    sm_activate(story_key, lifecycle_state=next_state, ctx_updates=ctx)
    db.log_event(
        story_key,
        s.get("current_stage") or "",
        "story_state_transition",
        {"from": cur_state, "to": next_state, "auto": False},
    )

    # next 状态有无 stages 决定是继续跑还是终态完成
    try:
        from ...sourcing.source_loader import resolve_source_profile

        states = resolve_source_profile(s.get("source_type")).story_states or {}
    except Exception:
        states = {}
    next_def = states.get(next_state) or {}
    next_stages = list(next_def.get("stages") or [])

    if not next_stages:
        # 终态:无阶段可跑 → 整个 story 完成
        sm_mark_completed(story_key)
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
    sm_activate(story_key)

    # Recover: re-submit to thread pool
    start_story_async(story_key)
    return {"ok": True}


@app.put("/api/story/{story_key}/fail")
def fail_story(story_key: str, req: SkipRequest = None):
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")
    # blocked 合并进 paused;走 story_service.fail_story(写 pause_reason=manual_fail)
    from .story_service import fail_story as _fail

    _fail(story_key, req.reason if req else "Manual fail")
    return {"ok": True}


@app.delete("/api/story/{story_key}")
def delete_story(story_key: str):
    """卡片「删除」— 软删除(置 deleted_at),行保留可 POST /restore 恢复。

    区别于 db.delete_story()(物理删除,一次性脚本用,不暴露到卡片)。
    """
    if not db.soft_delete_story(story_key):
        raise HTTPException(404, "Story not found or already deleted")
    kill_pty(story_key)
    return {"ok": True}


@app.post("/api/story/{story_key}/restore")
def restore_story(story_key: str):
    """恢复软删除的 story(清空 deleted_at)→ 重新出现在原 lifecycle tab。"""
    if not db.restore_story(story_key):
        raise HTTPException(404, "Story not found or not deleted")
    return {"ok": True}


@app.put("/api/story/{story_key}/lifecycle")
def set_lifecycle_state(story_key: str, req: SetLifecycleRequest):
    """人工移动 story 到任意生命周期态(5 态全开放:待启动/开发/测试/上线/结项)。

    与 POST /lifecycle/advance(受 StoryStateGate 约束的单步前进)不同,这里直接
    set lifecycle_state — 卡片「移动到...」菜单用。只改 lifecycle_state,不动引擎
    status。记 story_state_transition 事件(参照 release-train 的 event 模式)。
    """
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")
    valid = {v.value for v in LifecycleState.__members__.values()}
    if req.state not in valid:
        raise HTTPException(400, f"Invalid lifecycle state: {req.state}")
    prev = s.get("lifecycle_state")
    db.update_story(story_key, lifecycle_state=req.state)
    db.log_event(
        story_key,
        s.get("current_stage") or "",
        "story_state_transition",
        {"from": prev, "to": req.state, "source": "card_menu"},
    )
    return {"ok": True, "lifecycleState": req.state}


@app.put("/api/story/{story_key}/archive")
def archive_story(story_key: str):
    """Archive a story that has been released and verified.

    Archived stories disappear from the default dashboard list but remain
    queryable via show_all and are not deleted.
    """
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")
    # archived 移出 status:归档 = 引擎完成 + 业务结项(lifecycle_state=结项)。
    sm_mark_completed(story_key)
    db.update_story(story_key, lifecycle_state="结项")
    db.log_stage(
        story_key,
        s.get("current_stage", ""),
        "archive",
        "User archived story after release",
    )
    return {"ok": True, "status": "completed"}


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
    sm_pause(story_key, error="紧急停止（可恢复）")
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
# -------- Timeline API (Task 3.1) --------
# -------- Gate History API (Task 3.2) --------
# -------- Loop Trace API (Task 3.3) --------
# -------- Findings API enhancement (Task 3.4) --------
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
# -------- TAPD Sync API --------


class SyncRequest(BaseModel):
    workspace: str = ""
    autostart: bool = True
    dry_run: bool = False
    status_only: bool = False
    fetch_all: bool = False
    item_type: str = ""  # "bug" | "story" | "requirement" | ""


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


@app.get("/api/analysis/prompts")
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
class AddDocumentRequest(BaseModel):
    kind: str
    ref: str = ""
    summary: str = ""
    evidence_ref: str = ""
    project_id: int | None = None


@app.post("/api/story/{story_key}/context/documents")
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
        from ...infra.doc_sync import register_doc_dual_write

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
@app.get("/api/profiles")
def api_list_profiles():
    """List available profiles for the create-story picker."""
    from ..engine.profile_loader import list_profiles

    return {"profiles": list_profiles()}
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


class StartStoryRequest(BaseModel):
    project_ids: list[int] = []
    content: str = ""  # PRD / 需求正文，开始开发时必填，design 阶段注入给 CLI
    seed_context: str = ""  # 接手中途需求:已有工作说明,写入 context_json.seed_context
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
    # 覆盖语义：用户本次提交的 project_ids 代表完整期望绑定集合。
    # 先清除旧绑定（story key 复用/intake 重走时避免残留）。
    # intake 阶段绑定均为 worktree_state="unprepared"，无 worktree 副作用，可直接删。
    for sp in db.get_story_projects(story_key):
        try:
            db.unbind_story_project(story_key, sp["project_id"])
        except Exception:
            log.debug(
                "[%s] unbind stale project %s failed", story_key, sp.get("project_id")
            )

    if not project_ids:
        return

    all_projects = {p["id"]: p for p in db.list_projects()}
    bound_repo = None
    for pid in project_ids:
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

    # 接手中途需求:用户可能只填了 seed_context(已有工作说明)没填 PRD content。
    # 用 seed_context 兜底当 PRD 正文——接手说明本身描述了需求,写成 PRD 文件后
    # design/verify 阶段的 prd_path 注入仍能工作。用户填了 content 则 content 优先。
    effective_content = req.content or req.seed_context

    # Intake: user-provided PRD wins; otherwise source-backed stories can ask the
    # built-in PRD generator LLM to prepare or route PRD creation.
    prd_content, intake_error = _prepare_intake_prd_content(
        story_key, story, effective_content
    )
    if intake_error:
        return intake_error

    try:
        if intake_state == "candidate":
            # Promote candidate to ready. planning 移出 status(归 lifecycle_state=待启动);
            # /start 后引擎开始跑规划,算 active。
            db.update_story(story_key, intake_state="ready", status="active")

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
        # 接手中途需求:把 seed_context 写进 context_json,供规划 LLM
        # (run_orchestrator_agent)和执行 prompt(prompts.py 的
        # "### 已有工作(接手)" section)读取。
        if req.seed_context.strip():
            db.update_context(story_key, "seed_context", req.seed_context.strip())
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
        # Dual-write: also version the PRD body into story_doc so the docs UI
        # (version history / diff / search) sees intake-created PRDs. Goes
        # through the shared helper to keep both tables in sync — same path
        # the AI stage outputs and the web editor use.
        try:
            from ...infra.doc_sync import register_doc_dual_write

            register_doc_dual_write(
                story_key,
                "prd",
                str(prd_file),
                content=prd_content,
                change_reason="Intake PRD 初始导入",
                author="system",
                workspace=workspace,
                summary="Intake PRD",
                source="system",
                verification_state="verified",
            )
        except Exception as exc:  # noqa: BLE001 — versioning is best-effort
            log.debug("[%s] PRD dual-write skipped: %s", story_key, exc)
        db.bump_context_revision(story_key)

        db.update_story(story_key, intake_state="ready", status="active")

        # Intake 后触发 LLM 规划（兑现 docstring "triggers LLM planning"）。
        # BUG FIX: 原本 /start 只改 status=active 却不调规划，而 run_story 无条件调
        # continue_orchestrator_agent(它要求 ctx["_agent_actions"] 已存在)→ 规划永远
        # 不跑 → 用户点「开始执行」必命中 "No actions to execute" false-failed。
        # 这里同步跑规划（用户在 intake 弹窗等「处理中...」，阻塞可接受），
        # 规划失败不阻断 /start（用户可在详情页 regenerate 重试）。
        try:
            planner.run_orchestrator_agent(story_key)
        except Exception as plan_exc:  # noqa: BLE001 — 规划失败不阻断 intake
            log.warning(
                "[%s] intake planning failed (user can regenerate): %s",
                story_key,
                plan_exc,
            )
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
    # storyStates 从 source profile.story_states + lifecycle_state + _completed_stages
    # 推导每个状态的进度(done/进行中/待开始)。前端主进度条用它。无 story_states → 空。
    # SOURCE-DRIVEN-MODEL: 按 source_type 查(不再从 profile 读);无 source → default 四状态。
    cur_lifecycle = (
        story.get("lifecycle_state") or ctx.get("_lifecycle_state") or "待启动"
    )
    story_states_view = []
    try:
        from ...sourcing.source_loader import resolve_source_profile

        _sp = resolve_source_profile(story.get("source_type"))
        _states_cfg = _sp.story_states or {}
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

    sm_activate(story_key, ctx_updates=ctx, lifecycle_state="开发")

    start_story_async(story_key)
    return {"ok": True, "story_key": story_key}


class UpdateActionAdapterRequest(BaseModel):
    adapter: str


@app.patch("/api/story/{story_key}/plan/actions/{stage}")
def api_update_action_adapter(
    story_key: str, stage: str, req: UpdateActionAdapterRequest
):
    """改某个 stage 的 CLI 类型,立即落 DB,不启动执行。

    替代「下拉改了只存前端 state、确认规划时才生效」的旧行为 —— 现在
    下拉 onChange 直接调这个端点,改完再刷新 plan query 就能看到回写。
    只在 planning 阶段允许(已确认/执行中改 adapter 没意义,执行已经按
    原adapter 跑起来了)。
    """
    story = db.get_story(story_key)
    if not story:
        raise HTTPException(status_code=404, detail="story not found")
    # planning 移出 status:adapter 只在「待启动」(未确认规划)阶段允许改,
    # 判 lifecycle_state 而非 status。
    if story.get("lifecycle_state") != "待启动":
        raise HTTPException(
            status_code=409,
            detail=f"can only change adapter before plan confirm (lifecycle_state={story.get('lifecycle_state')})",
        )
    if req.adapter not in ("claude", "codex", "kimi", "opencode"):
        raise HTTPException(status_code=400, detail=f"unknown adapter: {req.adapter}")

    import json

    ctx = json.loads(story.get("context_json") or "{}")
    actions = ctx.get("_agent_actions") or []
    matched = False
    for action in actions:
        if action.get("stage") == stage:
            action["adapter"] = req.adapter
            matched = True
    if not matched:
        raise HTTPException(status_code=404, detail=f"stage not found in plan: {stage}")

    db.update_story(story_key, context_json=json.dumps(ctx, ensure_ascii=False))
    return {"ok": True, "stage": stage, "adapter": req.adapter}


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
    sm_activate(story_key, ctx_updates=ctx)

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
    _req_stage = pending.get("stage", "unknown")
    db.log_event(
        story_key,
        _req_stage,
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
            # 4 态合并后:active/implementing 都归 active;planning 移出 status。
            # "还活着"的状态 = awaiting-clarify / active(paused 在这收尾吗?不——
            # paused 也要等,但 SSE 场景里 status 来自 stage 执行流,paused 时
            # driver 已退出,这里收尾是对的)。
            if status not in ("awaiting-clarify", "active", "paused"):
                idle += 1
                from ...sourcing.execution_status import is_terminal

                if idle > 2 or is_terminal(status):
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



