"""引擎 status 变更的唯一收口 — State Machine mutation 层。

Grok-build §2.2 + 业界共识(workflow engine 用收口 mutation,非转移表):
本模块是 ``status`` 字段的**唯一写入通道**。所有 status 变更必须经过这里的 4 个
mutation 函数,禁止在调用方裸调 ``db.update_story(status=...)``。

为什么收口:原 22 个裸 ``db.update_story(status=)`` 散落在 planner/graph/api/story_service,
各自处理 last_error 截断 / log / 清 gate / pause_reason,不一致且易漏(有的 failed
截断 ``[:500]`` 有的没截;有的 paused 写 pause_reason 有的忘写)。收口后副作用只在一处。

与 CQRS 读侧(state_queries)的关系:本模块是写侧(mutation),只负责"怎么变 + 副作用"。
"能不能变 / 变成什么"的决策在调用方(它们知道触发条件),或 lifecycle_state 维度
靠 state_mutations.decide_state_transition()。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..infra.db import models as db
from .execution_status import ExecutionStatus

log = logging.getLogger(__name__)

# last_error 统一截断长度(原各处 500 / 无截断混用,现统一)。
_MAX_ERROR_LEN = 500


def _truncate(text: str) -> str:
    return (text or "")[:_MAX_ERROR_LEN]


def _merge_ctx(story_key: str, updates: dict[str, Any]) -> str | None:
    """读当前 ctx,合并 updates,返回新 ctx_json(无 updates 则 None)。

    放在 mutation 里而非调用方,避免每处各自 json.loads/dumps + update_story。
    """
    if not updates:
        return None
    s = db.get_story(story_key)
    try:
        ctx = json.loads(s.get("context_json") or "{}") if s else {}
    except (ValueError, TypeError):
        ctx = {}
    ctx.update(updates)
    return json.dumps(ctx, ensure_ascii=False)


# ---- 4 个 mutation:status 变更的唯一入口 ----


def mark_failed(story_key: str, error: str, *, ctx_updates: dict | None = None) -> None:
    """转 FAILED(失败终态,含原 aborted)。

    统一处理:last_error 截断 + ctx 合并。
    """
    kwargs: dict[str, Any] = {
        "status": ExecutionStatus.FAILED.value,
        "last_error": _truncate(error),
    }
    ctx_json = _merge_ctx(story_key, ctx_updates or {})
    if ctx_json:
        kwargs["context_json"] = ctx_json
    db.update_story(story_key, **kwargs)


def mark_completed(story_key: str, *, ctx_updates: dict | None = None) -> None:
    """转 COMPLETED(引擎跑完所有阶段)。

    注意:completed 是引擎层"跑完了",不等于业务结项(lifecycle_state=结项)。
    业务结项由归档 /lifecycle/advance 终态推进,那里写 lifecycle_state。
    """
    kwargs: dict[str, Any] = {"status": ExecutionStatus.COMPLETED.value}
    ctx_json = _merge_ctx(story_key, ctx_updates or {})
    if ctx_json:
        kwargs["context_json"] = ctx_json
    db.update_story(story_key, **kwargs)


def activate(
    story_key: str,
    *,
    clear_gates: bool = False,
    clear_pause_reason: bool = False,
    ctx_updates: dict | None = None,
    lifecycle_state: str | None = None,
) -> None:
    """转 ACTIVE(进入/恢复执行)。

    - clear_gates:清 _stage_gate / _story_state_gate(确认闸推进后重进执行时用)。
    - clear_pause_reason:清 ctx._pause_reason(paused→active 恢复时用)。
    - lifecycle_state:同时推进业务状态(/plan/confirm / /lifecycle/advance 用)。
    """
    updates: dict[str, Any] = {}
    if clear_gates or clear_pause_reason:
        s = db.get_story(story_key)
        try:
            ctx = json.loads(s.get("context_json") or "{}") if s else {}
        except (ValueError, TypeError):
            ctx = {}
        if clear_gates:
            ctx.pop("_stage_gate", None)
            ctx.pop("_story_state_gate", None)
        if clear_pause_reason:
            ctx.pop("_pause_reason", None)
        updates.update(ctx)
    if ctx_updates:
        updates.update(ctx_updates)
    kwargs: dict[str, Any] = {"status": ExecutionStatus.ACTIVE.value}
    if updates:
        kwargs["context_json"] = json.dumps(updates, ensure_ascii=False)
    if lifecycle_state:
        kwargs["lifecycle_state"] = lifecycle_state
    db.update_story(story_key, **kwargs)


def pause(
    story_key: str,
    *,
    reason: str | None = None,
    error: str | None = None,
    ctx_updates: dict | None = None,
) -> None:
    """转 PAUSED(暂停,含原 blocked/waiting_subtasks)。

    - reason:ctx._pause_reason(子原因,debug_packet/resume_parent 鉴别用)。
      值域:"waiting_subtasks" / "manual_fail" / "emergency_stop" / None(确认闸暂停)。
    - error:last_error(紧急停止/手动 fail 带错误信息)。
    """
    updates = dict(ctx_updates or {})
    if reason:
        updates["_pause_reason"] = reason
    kwargs: dict[str, Any] = {"status": ExecutionStatus.PAUSED.value}
    if updates:
        kwargs["context_json"] = _merge_ctx(story_key, updates) or json.dumps(
            updates, ensure_ascii=False
        )
    if error:
        kwargs["last_error"] = _truncate(error)
    db.update_story(story_key, **kwargs)


__all__ = ["mark_failed", "mark_completed", "activate", "pause"]
