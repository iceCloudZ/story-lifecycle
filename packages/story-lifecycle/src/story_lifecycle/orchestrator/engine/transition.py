"""Transition Decider(层2 stage 转移)。

gate 给出 pass/fail 后,``decide_transition`` 决定怎么转移:推进 / 重试 / 换法 /
插入救援 stage / 跳过 / 上交。替 ``planner.py:769-797`` 的硬编码 ``actions.insert()``。

**纯 Decider(§2.2 #1)**:零副作用,规则驱动 + ``history_facts``(注入历史决策)。
关键策略:**历史里"同类失败 → 换法成功"优先于无脑 retry**(否则反复 retry 同一失败法,
是硬编码 insert 的反面教材)。replanner(执行反馈→重规划)是后续 Handler,复用
``planner.run_orchestrator_agent.invoke_with_tools``。

action 取值:
- ``proceed``:gate 过 → 下一 stage。
- ``retry``:可恢复失败首次 → 同 stage 重试。
- ``swap_approach``:历史证明换法(换 adapter / 换策略)能解同类失败 → 换。
- ``insert_rescue_stage``:缺依赖等 → 插一个救援 stage 先补(带 ``rescue_stage``)。
- ``escalate``:反复失败超限 → 上交人。
- ``skip``:policy 判低价值 → 跳(本基础版不主动触发)。
"""

from __future__ import annotations

from typing import Optional

from ...infra.story_paths import safe_segment

# 默认同 stage 重试上限(超过 → escalate)
_DEFAULT_MAX_RETRIES = 3


def decide_transition(
    *,
    gate_decision: dict | bool,
    failure_mode: Optional[str],
    history_facts: Optional[dict] = None,
) -> dict:
    """Pure Decider. Pick a stage transition action after a gate decision.

    Args:
        gate_decision: gate 结果。dict(取 ``pass``)或裸 bool。
        failure_mode: 失败模式(build/tests/quality/timeout/missing_dependency/None)。
        history_facts: 注入的历史决策:
            - ``same_failure_swap_succeeded`` (bool):历史上同类失败是否被"换法"解过。
            - ``failure_count_on_stage`` (int):同 stage 已连续失败次数。
            - ``max_retries`` (int, 默认 3)。
            - ``missing_dep`` (str):缺的依赖名(insert_rescue_stage 用)。

    Returns:
        ``{"action": str, "reason": str, rescue_stage?: str}``。
    """
    history_facts = history_facts or {}

    # gate 过 → 推进
    if _gate_passed(gate_decision):
        return {"action": "proceed", "reason": "gate 通过 → 推进下一 stage"}

    repeat = history_facts.get("failure_count_on_stage", 0) or 0
    max_retries = history_facts.get("max_retries", _DEFAULT_MAX_RETRIES)

    # (1) 缺依赖 → 插救援 stage 先补
    if failure_mode == "missing_dependency":
        dep = history_facts.get("missing_dep", "unknown")
        return {
            "action": "insert_rescue_stage",
            "rescue_stage": "setup_dependency",
            "reason": f"缺依赖({dep})→ 插救援 stage setup_dependency 先补",
        }

    # (2) 历史"换法解过同类失败" → swap_approach(优先于 retry)
    if history_facts.get("same_failure_swap_succeeded"):
        return {
            "action": "swap_approach",
            "reason": "历史:同类失败曾被换法(adapter/策略)解过 → 换法,避免重复 retry 同一失败",
        }

    # (3) 反复失败超限 → escalate
    if repeat >= max_retries:
        return {
            "action": "escalate",
            "reason": f"同 stage 反复失败 {repeat} 次(≥上限 {max_retries})→ 上交人",
        }

    # (4) 可恢复失败首次 → retry
    return {
        "action": "retry",
        "reason": f"可恢复失败({failure_mode or 'unknown'})→ 同 stage 重试({repeat + 1}/{max_retries})",
    }


def _gate_passed(gate_decision: dict | bool) -> bool:
    if isinstance(gate_decision, dict):
        return bool(gate_decision.get("pass", False))
    return bool(gate_decision)


# ---- Handler-side 映射:decide_transition 决策 → planner 可插入的 action ----

# swap_approach 时轮转的 adapter 序(与 recovery 一致)
_SWAP_ADAPTER_ORDER: tuple[str, ...] = ("codex", "claude", "kimi")


def build_repair_action(
    *,
    transition_decision: dict,
    story_key: str,
    gate_result: dict,
    adapter_name: str,
) -> dict | None:
    """把 ``decide_transition`` 的决策映射成 planner 可 ``actions.insert`` 的 action dict。

    Returns:
        - ``retry`` → verify 修复 action(同 adapter)。
        - ``swap_approach`` → verify 修复 action(**换** adapter,轮转序下一个)。
        - ``insert_rescue_stage`` → 救援 stage action(stage=``rescue_stage``)。
        - ``proceed`` / ``escalate`` / ``skip`` → ``None``(caller 自己处理推进/标失败)。
    """
    action = transition_decision.get("action")
    if action in ("proceed", "escalate", "skip", None):
        return None

    reason = transition_decision.get("reason", "")
    round_n = (gate_result or {}).get("round", 1)
    seg = safe_segment(story_key)

    if action == "insert_rescue_stage":
        rescue = transition_decision.get("rescue_stage", "setup_dependency")
        return {
            "action": "launch",
            "stage": rescue,
            "adapter": adapter_name,
            "focus": f"rescue stage — {reason}",
            "done_file": f".story/done/{seg}/{rescue}.json",
        }

    # retry 或 swap_approach → verify 修复(done_file 与原 verify-gate round 格式一致)
    if action == "swap_approach":
        repair_adapter = _next_adapter(adapter_name, list(_SWAP_ADAPTER_ORDER))
    else:
        repair_adapter = adapter_name
    return {
        "action": "launch",
        "stage": "verify",
        "adapter": repair_adapter,
        "focus": f"repair round {round_n} — {reason}",
        "done_file": f".story/done/{seg}/verify-round{round_n}.json",
    }


def _next_adapter(current: str, order: list[str]) -> str:
    """下一个 adapter(current 不在 order → order[0])。"""
    if not order:
        return current
    if current not in order:
        return order[0]
    return order[(order.index(current) + 1) % len(order)]
