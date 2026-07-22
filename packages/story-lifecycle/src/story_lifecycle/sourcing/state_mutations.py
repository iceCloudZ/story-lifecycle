"""CQRS 写侧 — 状态变更事件(dataclass)。

Grok-build §2.2:mutations.rs 用 ``&mut self`` 且每次必产 event,event 不含持久化
职责(持久化由外层 actor 做)。本模块是写侧:函数算出"要变成什么"+ 产出事件对象,
**不调 db.update_story**。planner driver 收到事件后负责持久化(DB 写 + log_event)。

这样改审计/事件产出时不会碰坏状态决策;改状态机时不会动到持久化层。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .lifecycle_state import LifecycleState
from .state_queries import (
    get_confirm_label,
    get_next_state,
    should_auto_advance,
    should_pause_for_gate,
)


@dataclass
class StoryStateTransitionEvent:
    """lifecycle_state 转移事件(planner 收到后写 DB + log_event)。"""

    story_key: str
    from_state: str
    to_state: str
    auto: bool = True
    event_type: str = "story_state_transition"


@dataclass
class StoryStateGateEvent:
    """状态闸到达事件(ui_button 类型,需人工确认)。"""

    story_key: str
    from_state: str
    to_state: str
    label: str
    awaiting_confirm: bool = True
    # 写入 ctx 的 gate dict(planner 直接塞 ctx["_story_state_gate"])
    gate: dict = field(default_factory=dict)
    event_type: str = "story_state_gate_reached"


@dataclass
class TerminalStateEvent:
    """到达真终态事件(无 next,整个 story 引擎完成)。"""

    story_key: str
    final_state: str
    event_type: str = "story_state_terminal"


def decide_state_transition(
    story_key: str,
    lifecycle_state: str,
    completed_stages: list[str],
    story_states: dict,
) -> StoryStateTransitionEvent | StoryStateGateEvent | TerminalStateEvent | None:
    """纯决策:当前 lifecycle_state 的 stages 全 done 后该怎么走。

    返回事件对象(由 planner 持久化),或 None(未到转移点 / 无 story_states)。
    **不写 DB,不改 completed_stages**。

    决策优先级(与 planner.py:1535-1630 原逻辑一致):
      1. auto advance(none/config-auto)→ StoryStateTransitionEvent
      2. ui_button gate → StoryStateGateEvent
      3. 无 next(终态)→ TerminalStateEvent
      4. 其余 → None(状态未完成 / 无配置)
    """
    if should_auto_advance(lifecycle_state, completed_stages, story_states):
        nxt = get_next_state(lifecycle_state, story_states)
        if nxt:
            return StoryStateTransitionEvent(
                story_key=story_key,
                from_state=lifecycle_state,
                to_state=nxt,
                auto=True,
            )

    if should_pause_for_gate(lifecycle_state, completed_stages, story_states):
        nxt = get_next_state(lifecycle_state, story_states)
        if nxt:
            label = get_confirm_label(lifecycle_state, story_states)
            return StoryStateGateEvent(
                story_key=story_key,
                from_state=lifecycle_state,
                to_state=nxt,
                label=label,
                gate={
                    "from": lifecycle_state,
                    "to": nxt,
                    "awaiting_confirm": True,
                    "label": label,
                },
            )

    # 终态:stages 全 done 但无 next
    from .state_queries import is_at_terminal_state

    if is_at_terminal_state(lifecycle_state, completed_stages, story_states):
        return TerminalStateEvent(
            story_key=story_key,
            final_state=lifecycle_state,
        )

    return None


def build_advance_from_gate(
    story_key: str, gate: dict
) -> StoryStateTransitionEvent:
    """从 _story_state_gate 构造确认推进事件(/lifecycle/advance 端点用)。

    gate 是 ctx 里的 _story_state_gate dict(from/to/label)。
    """
    return StoryStateTransitionEvent(
        story_key=story_key,
        from_state=gate.get("from", LifecycleState.DEV.value),
        to_state=gate["to"],
        auto=False,
    )


__all__ = [
    "StoryStateTransitionEvent",
    "StoryStateGateEvent",
    "TerminalStateEvent",
    "decide_state_transition",
    "build_advance_from_gate",
]
