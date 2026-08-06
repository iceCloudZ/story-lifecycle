"""routers/lifecycle — lifecycle domain API（设计15 阶段C 拆自 api.py）。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ....infra.db import models as db
from ....infra.terminal.pty import kill_pty
from ....sourcing.lifecycle_state import LifecycleState
from ....sourcing.state_machine import (
    activate as sm_activate,
    mark_completed as sm_mark_completed,
    pause as sm_pause,
)
from ...engine.graph import force_stop_story, start_story_async

log = logging.getLogger("story-lifecycle.api.lifecycle")

router = APIRouter(tags=["lifecycle"])


class AdvanceRequest(BaseModel):
    description: str = ""


class SetReleaseTrainRequest(BaseModel):
    train: str | None = None


class SkipRequest(BaseModel):
    reason: str = ""


class SetLifecycleRequest(BaseModel):
    state: str  # lifecycle_state 目标值(待启动/开发/测试/上线/结项),5 态全开放


class CreateSubStoryRequest(BaseModel):
    sub_type: str = ""
    start_stage: str = ""
    description: str


class AbortRequest(BaseModel):
    reason: str = "User abort"


class ResumeParentRequest(BaseModel):
    strategy: str = "pause_subs"  # pause_subs | abort_subs


@router.put("/api/story/{story_key}/advance")
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


@router.post("/api/story/{story_key}/lifecycle/advance")
def advance_lifecycle_state(story_key: str):
    """推进 Story 业务状态到下一态(待启动→开发→测试→上线→结项)。

    成果物 gate 驱动:推进前检查该转换的成果物是否全部满足(exists+confirmed
    或 skipped)。不满足则 409 返回缺失列表(前端显示「还差:测试报告」)。
    gate 满足 → 推进 lifecycle_state → 若下一状态有 stages 则 start_story_async,
    无(终态)则标 completed。
    """
    import json as _json

    from ....sourcing.deliverables import gate_for_current_state, gate_satisfied

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
        from ...evaluation.stage_completion import advance_lifecycle_to_target

        try:
            from ....sourcing.source_loader import resolve_source_profile

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
        from ....sourcing.source_loader import resolve_source_profile

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


@router.put("/api/story/{story_key}/release-train")
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


@router.put("/api/story/{story_key}/skip/{stage}")
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


@router.put("/api/story/{story_key}/fail")
def fail_story(story_key: str, req: SkipRequest = None):
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")
    # blocked 合并进 paused;走 story_service.fail_story(写 pause_reason=manual_fail)
    from ..story_service import fail_story as _fail

    _fail(story_key, req.reason if req else "Manual fail")
    return {"ok": True}


@router.delete("/api/story/{story_key}")
def delete_story(story_key: str):
    """卡片「删除」— 软删除(置 deleted_at),行保留可 POST /restore 恢复。

    区别于 db.delete_story()(物理删除,一次性脚本用,不暴露到卡片)。
    """
    if not db.soft_delete_story(story_key):
        raise HTTPException(404, "Story not found or already deleted")
    kill_pty(story_key)
    return {"ok": True}


@router.post("/api/story/{story_key}/restore")
def restore_story(story_key: str):
    """恢复软删除的 story(清空 deleted_at)→ 重新出现在原 lifecycle tab。"""
    if not db.restore_story(story_key):
        raise HTTPException(404, "Story not found or not deleted")
    return {"ok": True}


@router.put("/api/story/{story_key}/lifecycle")
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


@router.put("/api/story/{story_key}/archive")
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


@router.post("/api/story/{parent_key}/sub")
def api_create_sub_story(parent_key: str, req: CreateSubStoryRequest):
    from ..story_service import create_sub_story as svc_create_sub

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


@router.post("/api/story/{story_key}/abort")
def api_abort_story(story_key: str, req: AbortRequest = None):
    from ..story_service import abort_story as svc_abort

    try:
        svc_abort(story_key, req.reason if req else "User abort")
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@router.post("/api/story/{story_key}/emergency-stop")
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


@router.put("/api/story/{parent_key}/resume")
def api_resume_parent(parent_key: str, req: ResumeParentRequest = None):
    from ..story_service import resume_parent as svc_resume

    strategy = req.strategy if req else "pause_subs"
    try:
        svc_resume(parent_key, strategy=strategy)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}
