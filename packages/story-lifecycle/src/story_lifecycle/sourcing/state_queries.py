"""CQRS 读侧 — 状态查询纯函数。

Grok-build §2.2:xai-chat-state 把读(queries.rs,``&self`` 纯函数)和写
(mutations.rs,``&mut self`` 必产 event)分开。本模块是读侧:所有函数**只读不写**,
签名 ``-> X`` 不带 DB 调用。写侧见 ``state_mutations.py``。

planner.py 的状态转移块(1535-1630)原本把"读拓扑/判能否转移/写 DB/log_event"全混在
一个嵌套块里。本模块抽出纯决策部分,让 planner 只负责"调 query → 收结果 → 持久化"。
"""

from __future__ import annotations

from .execution_status import is_active, is_terminal, normalize_status
from .lifecycle_state import resolve_lifecycle_state

# story_states 拓扑里 confirm 字段的可选类型(与 YAML source_profiles 对齐)。
CONFIRM_NONE = "none"
CONFIRM_UI_BUTTON = "ui_button"
CONFIRM_CONFIG = "config"


def is_story_state_complete(
    lifecycle_state: str,
    completed_stages: list[str],
    story_states: dict,
) -> bool:
    """当前 lifecycle_state 定义的所有 stages 是否都已完成。

    无 story_states 配置 → False(向后兼容,driver 走扁平阶段)。
    当前状态不在 story_states → False。
    当前状态无 stages 定义 → False(无阶段可完成就不算"状态完成")。
    """
    if not story_states or lifecycle_state not in story_states:
        return False
    state_def = story_states[lifecycle_state] or {}
    state_stages = list(state_def.get("stages") or [])
    if not state_stages:
        return False
    return all(ss in completed_stages for ss in state_stages)


def get_next_state(lifecycle_state: str, story_states: dict) -> str | None:
    """当前状态的下一状态(读拓扑 next 链)。无 next / 无配置 → None(终态)。"""
    if not story_states or lifecycle_state not in story_states:
        return None
    return (story_states[lifecycle_state] or {}).get("next")


def get_confirm_type(lifecycle_state: str, story_states: dict) -> str:
    """当前状态的转移闸类型(none/ui_button/config)。无配置 → none。"""
    if not story_states or lifecycle_state not in story_states:
        return CONFIRM_NONE
    confirm = (story_states[lifecycle_state] or {}).get("confirm") or {}
    return confirm.get("type", CONFIRM_NONE)


def is_config_auto_advance(lifecycle_state: str, story_states: dict) -> bool:
    """config 类型的 confirm 是否自动推进(读 STORY_<key> env)。

    planner.py:1551-1557 原逻辑:env STORY_<key> 为 1/true/yes 则 auto。
    """
    if get_confirm_type(lifecycle_state, story_states) != CONFIRM_CONFIG:
        return False
    import os

    confirm = (story_states.get(lifecycle_state) or {}).get("confirm") or {}
    key = confirm.get("key", "")
    if not key:
        return False
    val = os.environ.get(f"STORY_{key}".upper(), "")
    return val.lower() in ("1", "true", "yes")


def get_confirm_label(lifecycle_state: str, story_states: dict) -> str:
    """ui_button 闸的展示文案(默认"进入<下一状态>")。"""
    nxt = get_next_state(lifecycle_state, story_states) or ""
    if not story_states or lifecycle_state not in story_states:
        return f"进入{nxt}"
    confirm = (story_states[lifecycle_state] or {}).get("confirm") or {}
    return confirm.get("label", f"进入{nxt}")


# ---- 复合判断(planner driver 用的便捷查询) ----


def should_auto_advance(
    lifecycle_state: str,
    completed_stages: list[str],
    story_states: dict,
) -> bool:
    """当前状态 stages 全 done 且 confirm 是 none/config-auto → 自动推进。"""
    if not is_story_state_complete(lifecycle_state, completed_stages, story_states):
        return False
    ctype = get_confirm_type(lifecycle_state, story_states)
    if ctype == CONFIRM_NONE:
        return True
    if ctype == CONFIRM_CONFIG:
        return is_config_auto_advance(lifecycle_state, story_states)
    return False


def should_pause_for_gate(
    lifecycle_state: str,
    completed_stages: list[str],
    story_states: dict,
) -> bool:
    """当前状态 stages 全 done 且 confirm 是 ui_button → 暂停等人确认。"""
    if not is_story_state_complete(lifecycle_state, completed_stages, story_states):
        return False
    return get_confirm_type(lifecycle_state, story_states) == CONFIRM_UI_BUTTON


def is_at_terminal_state(
    lifecycle_state: str,
    completed_stages: list[str],
    story_states: dict,
) -> bool:
    """当前状态 stages 全 done 且无 next → 真终态(整个 story 引擎完成)。"""
    if not is_story_state_complete(lifecycle_state, completed_stages, story_states):
        return False
    return get_next_state(lifecycle_state, story_states) is None


# ---- 从 ctx/story 解析当前状态(替 planner.py:862 散落 fallback) ----


def current_lifecycle_state(ctx: dict, story: dict) -> str:
    """读当前 lifecycle_state(ctx > DB > 待启动)。纯函数,不改入参。"""
    return resolve_lifecycle_state(
        ctx.get("_lifecycle_state") or None,
        story.get("lifecycle_state") or None,
    )


__all__ = [
    "CONFIRM_NONE",
    "CONFIRM_UI_BUTTON",
    "CONFIRM_CONFIG",
    "is_story_state_complete",
    "get_next_state",
    "get_confirm_type",
    "is_config_auto_advance",
    "get_confirm_label",
    "should_auto_advance",
    "should_pause_for_gate",
    "is_at_terminal_state",
    "current_lifecycle_state",
    "is_terminal",
    "is_active",
    "normalize_status",
]
