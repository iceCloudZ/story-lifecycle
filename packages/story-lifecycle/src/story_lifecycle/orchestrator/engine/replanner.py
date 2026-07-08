"""Replanner(层2 执行反馈→重规划)。

当 ``decide_transition`` 选 ``swap_approach`` 或一个 stage 的做法整条错时,拿**执行反馈**
重规划:复用 ``planner.run_orchestrator_agent`` 的 ``invoke_with_tools`` + ``plan_step``/
``skip_stage`` 工具(同一套 schema),让 LLM 基于失败反馈产新 action list。

**设计**:
- ``build_replan_messages`` —— 纯 Decider:把 story facts + 反馈 + prior actions 压成重规划
  messages(system 指令 + user 反馈)。喂结构化 facts,不喂原始日志(§2.2 #4)。
- ``replan`` —— 循环:注入的 ``invoke_with_tools`` 调 LLM → 收 plan_step/skip_stage → actions。
  LLM/工具全注入(可测),零硬副作用。复用 ``stage_done_file_rel`` 给 done_file 默认值。

注:``replan`` 不直接落 ctx/DB(副作用归 caller Handler);只算新 actions 返回。
"""

from __future__ import annotations

import json
from typing import Callable

from ...infra.paths import stage_done_file_rel

# 重规划的最大轮次(防 LLM 反复读 tool;正常 1-2 轮就给完 plan)
_MAX_REPLAN_ROUNDS = 10


def build_replan_messages(
    *,
    story_facts: dict,
    feedback: dict,
    prior_actions: list[dict],
) -> list[dict]:
    """Pure Decider:story facts + 执行反馈 + prior actions → 重规划 messages。

    Args:
        story_facts: {story_key, stage, summary, ...}。
        feedback: {stage, failure_mode?, reason, attempted_adapters?} —— 哪里失败了。
        prior_actions: 之前试过的 action list(给 LLM 看别重复同样的)。

    Returns:
        ``[{role: system}, {role: user}]`` —— 喂 invoke_with_tools 的初始 messages。
    """
    prior_summary = (
        ", ".join(
            f"{a.get('stage', '?')}@{a.get('adapter', '?')}"
            for a in (prior_actions or [])
        )
        or "(无)"
    )
    system = (
        "你是 story 执行的重规划器。之前的做法失败了,基于反馈用 plan_step / skip_stage 工具"
        "产一个**改过的新计划**(换 adapter / 换 focus / 插救援 stage / 跳过)。\n"
        f"Story 上下文: {json.dumps(story_facts, ensure_ascii=False)}\n"
        "只返回 tool 调用,不要额外文字。"
    )
    user = (
        f"失败的 stage: {feedback.get('stage', '?')}\n"
        f"失败模式: {feedback.get('failure_mode', '?')}\n"
        f"失败原因: {feedback.get('reason', '?')}\n"
        f"已试过(别重复): {prior_summary}\n"
        "请给出修订后的计划。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def replan(
    *,
    story_facts: dict,
    feedback: dict,
    prior_actions: list[dict],
    invoke_with_tools: Callable,
    tools: list[dict],
) -> list[dict]:
    """执行反馈 → 重规划,产新 action list(复用 plan_step/skip_stage 工具)。

    Args:
        invoke_with_tools: 注入的 LLM 调用,签名 ``(messages, tools, **kw) -> {message, tool_calls, content}``
            (同 ``LLMClient.invoke_with_tools``,但 messages/tools 位置参)。
        tools: 工具 schema(ORCHESTRATOR_TOOLS)。

    Returns:
        新 action list(``{action: launch/skip, ...}``)。不落 DB(归 caller)。
    """
    messages = build_replan_messages(
        story_facts=story_facts, feedback=feedback, prior_actions=prior_actions
    )
    story_key = story_facts.get("story_key", "")
    actions: list[dict] = []

    for _round in range(_MAX_REPLAN_ROUNDS):
        resp = invoke_with_tools(
            messages, tools, tool_choice="auto", temperature=0.1, timeout=90
        )
        tool_calls = resp.get("tool_calls") or []
        # 记 assistant 回复(多轮 tool-calling 协议要求)
        messages.append(
            resp.get("message")
            or {"role": "assistant", "content": resp.get("content", "")}
        )
        if not tool_calls:
            break
        for tc in tool_calls:
            action = _tool_call_to_action(tc, story_key)
            if action:
                actions.append(action)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": json.dumps({"status": "recorded"}, ensure_ascii=False),
                }
            )
    return actions


def _tool_call_to_action(tc: dict, story_key: str) -> dict | None:
    """plan_step/skip_stage tool call → action dict(与 run_orchestrator_agent 一致)。"""
    fn = tc.get("function", {}) or {}
    name = fn.get("name", "")
    args = fn.get("arguments", {})
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
    if name == "plan_step":
        stage = args.get("stage", "")
        return {
            "action": "launch",
            "adapter": args.get("adapter", "claude"),
            "stage": stage,
            "focus": args.get("focus", ""),
            "done_file": args.get("done_file", stage_done_file_rel(story_key, stage)),
        }
    if name == "skip_stage":
        return {
            "action": "skip",
            "stage": args.get("stage", ""),
            "reason": args.get("reason", ""),
        }
    return None
