"""引擎执行状态 enum + 归一化。

Grok-build §2.2(CQRS)借鉴:状态值 enum 化,消灭散落的字符串字面量。
status 字段收敛到 4 核心态(原 11 值合并:implementing→active,
blocked/waiting_subtasks→paused,aborted→failed;planning/idle/archived 移出
status,归 lifecycle_state/intake_state 维度)。

读侧(DB / API 响应 / 老数据)统一过 ``normalize_status`` 归一,调用方只面对 4 值。
"""

from __future__ import annotations

from enum import Enum


class ExecutionStatus(str, Enum):
    """引擎执行状态 — claude 进程在干嘛(与业务 lifecycle_state 正交)。"""

    ACTIVE = "active"  # 在跑(含原 implementing 救援重试)
    PAUSED = "paused"  # 暂停(含原 blocked/waiting_subtasks;子原因走 ctx._pause_reason)
    COMPLETED = "completed"  # 引擎跑完所有阶段
    FAILED = "failed"  # 失败终态(含原 aborted 手动终止)


# 旧 status 值 → 新值。读老数据 / 老调用点时归一用。
# planning/idle/archived 不在此表 — 它们移出了 status 语义,读到时按"非执行态"处理
# (调用方应改判 lifecycle_state / intake_state,不该再靠这三个 status 值)。
_LEGACY_STATUS_MAP: dict[str, str] = {
    "implementing": ExecutionStatus.ACTIVE.value,
    "blocked": ExecutionStatus.PAUSED.value,
    "waiting_subtasks": ExecutionStatus.PAUSED.value,
    "aborted": ExecutionStatus.FAILED.value,
}


def normalize_status(raw: str | None) -> str:
    """老 status 值归一到 4 核心态。

    读 DB / API 响应 / 前端 badge 时调。未知值原样返回(向前兼容未来新值)。
    """
    if not raw:
        return raw or ""
    return _LEGACY_STATUS_MAP.get(raw, raw)


# 终态集合(原 entry.py:_FINISHED_STATUSES / graph.py:408 consume_orphan_done 的并集)。
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {
        ExecutionStatus.COMPLETED.value,
        ExecutionStatus.FAILED.value,
    }
)

# 活跃集合(引擎在跑或暂停,driver 关心)。
ACTIVE_STATUSES: frozenset[str] = frozenset(
    {
        ExecutionStatus.ACTIVE.value,
        ExecutionStatus.PAUSED.value,
    }
)


def is_terminal(status: str | None) -> bool:
    """status 是否为终态(completed/failed)。"""
    return normalize_status(status) in TERMINAL_STATUSES


def is_active(status: str | None) -> bool:
    """status 是否为活跃态(active/paused)。"""
    return normalize_status(status) in ACTIVE_STATUSES
