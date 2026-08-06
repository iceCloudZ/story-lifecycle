"""routers/plan — plan domain API（设计15 阶段C 拆自 api.py）。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ....infra.db import models as db
from ....sourcing.state_machine import activate as sm_activate
from ...engine import planner
from ...engine.graph import start_story_async
from .._shared import _workspace_root_for_project
from .._shared import (
    _load_tapd_config,
    _serialize_story_summary,
    _story_list_json,
)

router = APIRouter(tags=["plan"])
class UpdateActionAdapterRequest(BaseModel):
    adapter: str


class AnswerRequest(BaseModel):
    answer: str


class ClarifyAnswerRequest(BaseModel):
    answer: str
    id: str | None = None
@router.get("/api/story/{story_key}/plan")
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
        from ....sourcing.source_loader import resolve_source_profile

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


@router.post("/api/story/{story_key}/plan/confirm")
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


@router.patch("/api/story/{story_key}/plan/actions/{stage}")
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


@router.post("/api/story/{story_key}/plan/regenerate")
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


@router.post("/api/story/{story_key}/answer")
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


@router.get("/api/story/{story_key}/wait")
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


@router.get("/api/story/{story_key}/clarify")
def api_get_clarify(story_key: str):
    """取当前待答澄清问题(design 逐问 HITL,前端轮询用)。无待答 → {waiting: false}。

    事件驱动:claude 调 ``mcp__lifecycle__clarify`` → MCP server 落 ``clarification_request``
    事件 → 本端点从事件查「最新未答 request」。详见 ``orchestrator/mcp/clarify_server.py``。
    """
    story = db.get_story(story_key)
    if not story:
        raise HTTPException(status_code=404, detail="story not found")
    from ...mcp.clarify_server import get_pending_clarification

    pending = get_pending_clarification(story_key, get_events_fn=db.get_story_events)
    if not pending:
        return {"ok": True, "waiting": False, "status": story.get("status")}
    return {
        "ok": True,
        "waiting": True,
        "status": story.get("status"),
        "question": pending,
    }


@router.post("/api/story/{story_key}/clarify/answer")
def api_clarify_answer(story_key: str, req: ClarifyAnswerRequest):
    """回答当前待答澄清 → 落 clarification_answer 事件 → MCP server 解除 claude 阻塞。

    claude 此刻**阻塞在 mcp__lifecycle__clarify 调用上**(同一进程,不重 spawn);本端点
    只落 answer 事件,MCP server 的 poll_clarify_answer 拾取后返回 → claude 带答继续。
    """
    story = db.get_story(story_key)
    if not story:
        raise HTTPException(status_code=404, detail="story not found")
    from ...mcp.clarify_server import get_pending_clarification

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


@router.get("/api/story/{story_key}/tapd-writeback-suggestion")
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
@router.get("/api/story/{story_key}/plan/stream")
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


@router.get("/api/story/{story_key}/clarify/stream")
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
                from ....sourcing.execution_status import is_terminal

                if idle > 2 or is_terminal(status):
                    yield f"data: {json.dumps({'type': 'done', 'status': status}, ensure_ascii=False)}\n\n"
                    return
            else:
                idle = 0
            await asyncio.sleep(1.5)

    return StreamingResponse(gen(), media_type="text/event-stream")
