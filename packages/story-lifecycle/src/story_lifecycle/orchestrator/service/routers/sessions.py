"""routers/sessions — sessions domain API（设计15 阶段C 拆自 api.py）。

含 PTY spawn cluster（_spawn_story_agent_pty / _ensure_story_agent_pty /
_story_headless）—— 设计14 已把核心收敛到 infra/terminal/spawn_recipe.py，
此处是 api 路径的薄壳 + 死后 resume 重试后处理。
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ....infra.db import models as db
from ....infra.terminal.pty import (
    cleanup_all,
    ensure_agent_pty,
    get_pty,
    kill_pty,
    list_pty_sessions,
)
from ....infra.terminal.sid_capture import arm_sid_capture, now_utc_iso
from ....infra.story_paths import build_story_spawn_env
from ....knowledge.adapters import get_adapter
from ...engine.profile_loader import resolve_profile

log = logging.getLogger("story-lifecycle.api.sessions")

router = APIRouter(tags=["sessions"])

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

    from ....infra.story_paths import build_story_spawn_env
    from ....infra.terminal.spawn_recipe import spawn_agent_pty

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
    from ...prompts import LaunchSeedBuilder

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
    from ...engine.planner import resolve_stage_adapter

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


def _story_headless(s: dict) -> bool:
    """Story 是否走 headless 执行(从 profile execution_mode 推导)。

    供前端 ClarifyDialog 决策:headless→MCP clarify 卡片;交互式→终端问人(BUG #9)。
    防御:profile 解析失败 → False(默认交互式)。
    """
    if not s:
        return False
    try:
        from ...engine.execution import headless_from_profile
        from ...engine.profile_loader import resolve_profile

        rp = resolve_profile(s.get("profile", "minimal"))
        return headless_from_profile(rp)
    except Exception:
        return False


@router.get("/api/story/{story_key}/sessions")
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


@router.get("/api/story/{story_key}/session")
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


@router.post("/api/story/{story_key}/session")
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


@router.post("/api/story/{story_key}/sessions/spawn")
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
        from ...engine.planner import resolve_stage_adapter

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


@router.post("/api/pty/{story_id}/spawn")
def api_spawn_pty(story_id: str):
    """Start or reuse the story's interactive agent PTY (legacy, single-session)."""
    s = db.get_story(story_id)
    if not s:
        raise HTTPException(404, "Story not found")
    return _ensure_story_agent_pty(s)


@router.delete("/api/story/{story_key}/sessions/{session_id}")
def api_kill_session(story_key: str, session_id: str):
    """Kill a specific PTY session."""
    kill_pty(story_key, session_id)
    return {"ok": True}


@router.delete("/api/pty")
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


@router.delete("/api/pty/{story_id}")
def api_kill_pty(story_id: str):
    """Kill all PTY sessions for a story."""
    kill_pty(story_id)
    return {"ok": True}


@router.get("/api/session/terminal/{story_key}")
def get_terminal(story_key: str):
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")

    info = _ensure_story_agent_pty(s)
    info["url"] = f"/ws/pty/{story_key}"
    return JSONResponse(info)

