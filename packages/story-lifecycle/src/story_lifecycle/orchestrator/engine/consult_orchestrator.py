"""consult orchestrator —— 编排 LLM 的 FC loop,决定 spawn / synthesize / finalize。

**复用 ``replanner.replan()`` 的 loop 骨架**(读 tool_calls → 执行 → 塞回 messages → 再调),
但输入/输出/工具完全不同(DESIGN §4.4):
- 输入:code agent 的 consult 请求(question/context/urgency)
- 工具:spawn_reviewer(调 consult_runner.run_consult_sync) + finalize_advice(终止信号)
- 输出:advisory 文本(str) + 诊断字段

分层(AGENTS.md):
- **Decider**(纯):``build_consult_messages`` —— 把 consult 请求压成 system+user,
  不执行副作用、不读 DB(只读传入的 story_facts dict)。
- **Handler**:``run_consult_orchestrator`` 的 spawn_reviewer 分支调用注入的 spawn_fn
  (生产是 consult_runner.run_consult_sync,真副作用);decorrelation 硬校验也在这一层。

设计原则(DESIGN §5.6):
- 零 DB 副作用(DB 事件归 consult_cmd)
- 全注入可测(invoke_with_tools / spawn_fn / clock_fn 都能注入)
- ``terminated_by`` 是开集诊断字段 —— 契约测试只断言存在,不断言取值
"""

from __future__ import annotations

import json
import time
from typing import Callable

# ── FC 工具 schema(对齐 agent_tools.py 的 OpenAI FC 格式)──────────────

SPAWN_REVIEWER_TOOL = {
    "type": "function",
    "function": {
        "name": "spawn_reviewer",
        "description": (
            "Spawn an external reviewer CLI (headless) to investigate a sub-question. "
            "The reviewer investigates the same workspace, writes its findings to "
            ".story/consult/<review_id>.json, and the findings are returned to you. "
            "Use this to get a second opinion from another model (decorrelation), "
            "or to investigate a sub-problem with fresh context. "
            "The adapter MUST differ from the consulting code agent's adapter "
            "(given in the system prompt) — cross-model decorrelation is the point."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "adapter": {
                    "type": "string",
                    "enum": ["claude", "kimi"],  # codex 无 headless,见 DESIGN §3.6
                    "description": (
                        "Which CLI to spawn. MUST differ from the consulting code "
                        "agent's adapter for decorrelation."
                    ),
                },
                "focus": {
                    "type": "string",
                    "description": (
                        "Concrete investigation directive (2-3 sentences). "
                        "Tell the reviewer exactly what to check."
                    ),
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Max seconds to wait. Default 180.",
                    "default": 180,
                },
            },
            "required": ["adapter", "focus"],
        },
    },
}

FINALIZE_ADVICE_TOOL = {
    "type": "function",
    "function": {
        "name": "finalize_advice",
        "description": (
            "Finalize and return the advisory to the consulting code agent. "
            "Call this when you have enough information to give a useful answer. "
            "The advice should be concrete, actionable, and cite evidence from "
            "reviewer findings. Mark it as advisory (the code agent may choose "
            "not to follow it)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "advice": {
                    "type": "string",
                    "description": "The advisory text. Concrete, actionable, with evidence.",
                },
                "confidence": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": "How confident you are in this advice.",
                },
                "followed_up": {
                    "type": "boolean",
                    "description": (
                        "Whether you spawned reviewer(s) to verify. If false, "
                        "advice is based on reasoning only."
                    ),
                },
            },
            "required": ["advice", "confidence"],
        },
    },
}

CONSULT_TOOLS = [SPAWN_REVIEWER_TOOL, FINALIZE_ADVICE_TOOL]

_MAX_CONSULT_ROUNDS = 5
_HARD_TIMEOUT_S = 480  # consult 全流程硬上限(前台 Bash 600s 上限留余量,DESIGN §5.1)


def build_consult_messages(
    *,
    consult_request: dict,
    story_facts: dict,
) -> list[dict]:
    """Pure Decider:code agent 的 consult 请求 → 编排 LLM 的初始 messages。

    Args:
        consult_request: {question, context, urgency, request_id, adapter_of_caller}
        story_facts: {story_key, stage, ...}(只读 dict,本函数不去 DB 查)

    Returns:
        ``[{role: system}, {role: user}]`` —— 喂 invoke_with_tools。
    """
    caller = consult_request.get("adapter_of_caller", "?")
    system = (
        "你是 story 编排层,被 code agent 通过 consult 求助。你的任务是:\n"
        "1. 判断这个问题需不需要 spawn 外援(跨模型 decorrelation / 实地调查)\n"
        "2. 如果需要,调 spawn_reviewer(adapter=...) spawn 外援 CLI 调查\n"
        "3. 拿到外援 findings 后,调 finalize_advice 综合给 code agent advisory\n"
        "4. 如果问题简单(你自己能答),直接调 finalize_advice 不 spawn\n\n"
        "**纪律**:\n"
        f"- 求助方 code agent 的 adapter 是 **{caller}**。spawn 的 adapter 必须与其**不同**"
        "(跨模型 decorrelation 是 consult 的核心价值;同模型 fresh context 是次优,仅当"
        "异 adapter spawn 失败后才可考虑,且要在 advice 里标注 decorrelation 弱)\n"
        "- advisory 是建议(不是命令),code agent 可不采纳\n"
        "- 你的建议要 cite 外援的 evidence(具体代码位置),不要泛泛而谈\n"
        "- 最多 spawn 2 个外援(避免过度调查),总轮次 ≤ 5\n\n"
        f"Story 上下文: {json.dumps(story_facts, ensure_ascii=False)}"
    )
    user = (
        f"## Code agent 的 consult 请求\n"
        f"**求助方 adapter**: {caller}\n"
        f"**urgency**: {consult_request.get('urgency', 'medium')}\n"
        f"**问题**: {consult_request.get('question', '')}\n"
        f"**上下文**:\n{consult_request.get('context', '')}\n\n"
        f"请决定:spawn 外援调查,还是直接 finalize_advice?"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def run_consult_orchestrator(
    *,
    consult_request: dict,
    story_facts: dict,
    workspace: str,
    # 注入点(测试用)
    invoke_with_tools: Callable,
    spawn_fn: Callable,  # = consult_runner.run_consult_sync
    tools: list[dict] | None = None,
    max_rounds: int = _MAX_CONSULT_ROUNDS,
    hard_timeout_s: float = _HARD_TIMEOUT_S,
    clock_fn: Callable[[], float] = time.monotonic,
) -> dict:
    """编排 LLM 的 FC loop → 返 advisory。

    Args:
        consult_request: {question, context, urgency, request_id, adapter_of_caller}
        story_facts: story 上下文(供 LLM 决策)
        workspace: 工作区根(传给 spawn_fn)
        invoke_with_tools: 注入的 LLM FC 调用,签名同 LLMClient.invoke_with_tools
        spawn_fn: 注入的 spawn 函数,签名同 run_consult_sync(关键字参数)
        tools: FC 工具表(缺省 = CONSULT_TOOLS)
        max_rounds: 最大轮次(缺省 5)
        hard_timeout_s: 硬超时秒(缺省 480)
        clock_fn: 时钟注入(测试可控制时间)

    Returns:
        dict,保证字段:
        - advice: str(最终给 code agent 的 advisory)
        - confidence: "low"|"medium"|"high"
        - followed_up: bool(是否 spawn 过外援)
        - rounds: int(实际跑了多少轮)
        - terminated_by: str(开集诊断,见模块 docstring)
        - spawn_results: list[dict](每次 spawn 的结果,审计用)
    """
    messages = build_consult_messages(
        consult_request=consult_request, story_facts=story_facts
    )
    tools = tools if tools is not None else CONSULT_TOOLS
    start = clock_fn()
    spawn_results: list[dict] = []
    request_id = consult_request.get("request_id", "")
    caller_adapter = consult_request.get("adapter_of_caller", "")

    for round_n in range(1, max_rounds + 1):
        # 硬超时检查
        if clock_fn() - start > hard_timeout_s:
            return _fallback_advice(
                spawn_results,
                terminated_by="hard_timeout",
                reason=f"hard timeout {hard_timeout_s}s reached at round {round_n}",
            )

        # 一次 FC 调用
        try:
            resp = invoke_with_tools(
                messages, tools, tool_choice="auto", temperature=0.1, timeout=90
            )
        except Exception as exc:
            # LLM 抖动 → 用已有 spawn_results 综合(没有就 fallback)
            return _fallback_advice(
                spawn_results,
                terminated_by="llm_failed",
                reason=f"invoke_with_tools failed at round {round_n}: {exc}",
            )

        tool_calls = resp.get("tool_calls") or []
        messages.append(
            resp.get("message")
            or {"role": "assistant", "content": resp.get("content", "")}
        )

        # 纯文本(没调工具)→ 当 advisory 返回
        if not tool_calls:
            text = (resp.get("content", "") or "").strip()
            if text:
                return {
                    "advice": text,
                    "confidence": "medium",
                    "followed_up": bool(spawn_results),
                    "rounds": round_n,
                    "terminated_by": "text",
                    "spawn_results": spawn_results,
                }
            # 空文本 + 无 tool_calls → 异常,fallback
            return _fallback_advice(
                spawn_results,
                terminated_by="empty_text",
                reason=f"LLM returned empty text at round {round_n}",
            )

        # 处理 tool_calls
        # 注:当前串行处理同轮多个 tool_calls。若未来改并行,需重新审 request_id 后缀约定。
        for tc in tool_calls:
            fn = tc.get("function", {}) or {}
            name = fn.get("name", "")
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}

            if name == "finalize_advice":
                # 终止信号 → 直接返
                return {
                    "advice": str(args.get("advice", "")),
                    "confidence": args.get("confidence", "medium"),
                    "followed_up": bool(spawn_results),
                    "rounds": round_n,
                    "terminated_by": "finalize",
                    "spawn_results": spawn_results,
                }

            if name == "spawn_reviewer":
                adapter = args.get("adapter", "")
                focus = args.get("focus", "")
                timeout = args.get("timeout_seconds", 180)
                # decorrelation 硬校验(Handler 层,DESIGN §4.3):
                # 与 caller 同 adapter → 不 spawn,塞回违规提示让 LLM 换 adapter
                if caller_adapter and adapter == caller_adapter:
                    tool_result_text = json.dumps(
                        {
                            "status": "decorrelation_violation",
                            "error": (
                                f"adapter {adapter!r} equals caller's adapter; "
                                "pick a DIFFERENT adapter for decorrelation"
                            ),
                        },
                        ensure_ascii=False,
                    )
                else:
                    spawn_result = spawn_fn(
                        adapter_name=adapter,
                        focus=focus,
                        workspace=workspace,
                        request_id=f"{request_id}_r{round_n}_{adapter}",
                        timeout=timeout,
                    )
                    spawn_results.append(
                        {
                            "round": round_n,
                            "adapter": adapter,
                            "focus": focus,
                            "result": spawn_result,
                        }
                    )
                    tool_result_text = json.dumps(spawn_result, ensure_ascii=False)
            else:
                tool_result_text = json.dumps(
                    {"error": f"unknown tool {name!r}"}, ensure_ascii=False
                )

            # 把 tool_result 塞回 messages(FC 协议要求)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": tool_result_text,
                }
            )

    # 达 max_rounds 仍没 finalize → 用最后一轮的 spawn_results 综合
    return _fallback_advice(
        spawn_results,
        terminated_by="max_rounds",
        reason=f"reached max_rounds={max_rounds} without finalize",
    )


def _fallback_advice(
    spawn_results: list[dict], *, terminated_by: str, reason: str
) -> dict:
    """降级路径:把已有 spawn_results 拼成 advisory,标注低置信。

    任何失败路径都走这里 —— 永远返回非空 advice(不阻塞 code agent)。
    """
    if not spawn_results:
        return {
            "advice": (
                f"(consult 降级: {reason}。编排层未能提供有效建议,"
                f"请自行决断并在 done summary 说明)"
            ),
            "confidence": "low",
            "followed_up": False,
            "rounds": 0,
            "terminated_by": terminated_by,
            "spawn_results": spawn_results,
        }
    # 拼 findings
    findings_lines = []
    for sr in spawn_results:
        r = sr.get("result", {})
        if r.get("status") != "ok":
            findings_lines.append(
                f"- [{sr.get('adapter', '?')}] 调查失败: {r.get('error', '?')}"
            )
            continue
        f = r.get("findings", {})
        summary = f.get("summary", "")
        rec = f.get("recommendation", "")
        findings_lines.append(f"- [{sr.get('adapter', '?')}] {summary}")
        if rec:
            findings_lines.append(f"  建议: {rec}")
    advice = f"(consult 降级综合,置信低 — {reason})\n" + "\n".join(findings_lines)
    return {
        "advice": advice,
        "confidence": "low",
        "followed_up": True,
        "rounds": len(spawn_results),
        "terminated_by": terminated_by,
        "spawn_results": spawn_results,
    }


__all__ = [
    "SPAWN_REVIEWER_TOOL",
    "FINALIZE_ADVICE_TOOL",
    "CONSULT_TOOLS",
    "build_consult_messages",
    "run_consult_orchestrator",
]
