"""Supervisor Decider — LLM-driven response to code-agent questions.

监督交互式 code agent (claude/codex/kimi):当 agent 提问/要选择时,
``decide_response`` 用注入的 LLM 决策返回 {choice, reason}。

设计原则(阶段 0 立骨架,后续层复用):
- **纯 Decider**:不读 DB、不写文件、不起进程;LLM 通过 ``llm_invoke`` 参数注入(可测)。
- **零副作用**:所有 I/O(写 PTY、log_event)归 Handler,不在此处。
- **决策上下文喂结构化 facts**(LangGraph 范式),不喂原始 PTY/stream 文本。
- 两轨(Claude stream-json / codex-kimi PTY)共用此决策大脑。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable

log = logging.getLogger(__name__)

# pty.alive 轮询周期。真实 ManagedPty 进程死时 _read_loop 退出但**不往 tap 推 sentinel**,
# 故 supervise_pty_session 用 wait_for 超时后检查 pty.alive 退出,避免 task 永久阻塞。
_POLL_SECONDS = 1.0


def decide_response(
    *,
    question: str,
    options: list[str],
    story_facts: dict,
    llm_invoke: Callable[[str], str],
    hitl: bool = False,
    ai_suggestion: str | None = None,
) -> dict:
    """Pure Decider. Choose a response for a code-agent question.

    Args:
        question: code agent 提出的问题/要做的选择(结构化文本)。
        options: 可选项列表(choice 必须是其中之一,后续测试驱动校验)。
        story_facts: 结构化 story 上下文(story_key/stage/profile/已做决策等)。
        llm_invoke: 注入的 LLM 调用,prompt -> JSON 字符串。
        hitl: Human-In-The-Loop——design 阶段「claude 逐问 + 人答」。True 时**不调
            llm_invoke**,改返回 ``{mode: "hitl", pause: True, clarification_request}``:
            检测到提问即暂停 story 等人答(DB/SSE/回注由 Handler/编排层接,本函数纯)。
            详见 docs/design-hitl-runbook.md。默认 False = 旧自动答行为。
        ai_suggestion: 可选预填建议(HITL 时随 clarification_request 推前端作参考,
            非绑定;默认 None)。

    Returns:
        自动答: ``{"choice": str, "reason": str}``。
        HITL: ``{"mode": "hitl", "pause": True, "clarification_request": {...}}``。
    """
    if hitl:
        return {
            "mode": "hitl",
            "pause": True,
            "clarification_request": {
                "question": question,
                "options": list(options),
                "ai_suggestion": ai_suggestion,
            },
        }
    prompt = _build_decision_prompt(question, options, story_facts)
    raw = llm_invoke(prompt)
    decision = _parse_decision(raw)
    if decision["choice"] not in options:
        raise ValueError(
            f"LLM choice {decision['choice']!r} not in allowed options {options}"
        )
    return decision


def log_decision(
    *,
    story_key: str,
    stage: str,
    adapter: str,
    question: str,
    options: list[str],
    decision: dict,
    log_event_fn: Callable,
) -> None:
    """Handler: 把 supervisor 决策落 log_event(supervisor_decision)。

    注入 log_event_fn(story_key, *, stage, event_type, payload)可测。
    决策事件流——两轨(Claude / codex-kimi)共用,事后审计 + 喂反思层(阶段 4)。
    """
    log_event_fn(
        story_key,
        stage=stage,
        event_type="supervisor_decision",
        payload={
            "adapter": adapter,
            "question": question,
            "options": options,
            "choice": decision["choice"],
            "reason": decision["reason"],
        },
    )


def emit_clarification_request(
    *,
    story_key: str,
    stage: str,
    question: str,
    options: list[str],
    header: str = "",
    ai_suggestion: str | None = None,
    context: str | None = None,
    log_event_fn: Callable,
    id_factory: Callable[[], str] | None = None,
) -> str:
    """Handler: 落 ``clarification_request`` 事件 + 生成/返回 id。

    design 阶段「claude 逐问 + 人答」的提问侧(详见 docs/design-hitl-runbook.md):
    编排层检测到 claude 的提问(侧文件 clarify_request.json / stream marker /
    AskUserQuestion tool_use)→ 调本 Handler → 暂停 story、推前端、等人答。
    返回的 ``id`` 供回注端 ``POST /clarify/answer {id, answer}`` 引用本轮提问。

    非纯(id 生成 + log_event I/O)——与 ``log_decision`` 同属 Handler 层;
    纯 Decider(``decide_response``)不碰 id / DB。

    Args:
        header: 前端展示主标题;缺省取 ``question`` 文本。
        ai_suggestion: 可选预填建议(前端展示、非绑定)。
        context: 可选提问上下文(前端展示辅助说明)。
        id_factory: 可选 id 生成器(测试注入);缺省 ``uuid4().hex[:12]``。
    """
    import uuid

    rid = (id_factory or (lambda: uuid.uuid4().hex[:12]))()
    log_event_fn(
        story_key,
        stage=stage,
        event_type="clarification_request",
        payload={
            "id": rid,
            "header": header or question,
            "question": question,
            "options": list(options),
            "ai_suggestion": ai_suggestion,
            "context": context,
        },
    )
    return rid


def handle_pty_output(
    *,
    buffer: str,
    pty,
    adapter: str,
    story_facts: dict,
    is_awaiting_fn: Callable,
    llm_invoke: Callable[[str], str],
    log_event_fn: Callable,
) -> bool:
    """Handler: buffer 命中"AI 在等人"则决策+应答+log。PTY 轨(codex/kimi)同步核心。

    ``is_awaiting_fn(buffer) -> (question, options) | None``:注入的识别器
    (0c 借 agent-yes 三层 pattern: readyPatterns/enterPatterns/fatalPatterns)。
    async 循环 ``supervise_pty_session`` 消费 tap queue,每个 chunk 调本函数。

    Returns: True 命中并应答,False 未命中(不调 LLM、不写 PTY、不 log)。
    """
    hit = is_awaiting_fn(buffer)
    if not hit:
        return False
    question, options = hit
    decision = decide_response(
        question=question,
        options=options,
        story_facts=story_facts,
        llm_invoke=llm_invoke,
    )
    pty.write((decision["choice"] + "\r").encode("utf-8"))
    log_decision(
        story_key=story_facts["story_key"],
        stage=story_facts.get("stage", ""),
        adapter=adapter,
        question=question,
        options=options,
        decision=decision,
        log_event_fn=log_event_fn,
    )
    return True


async def supervise_pty_session(
    *,
    pty,
    adapter: str,
    story_facts: dict,
    is_awaiting_fn: Callable,
    llm_invoke: Callable[[str], str],
    log_event_fn: Callable,
    buffer_bytes: int = 2000,
) -> None:
    """持续监督一个 PTY session(消费 ``add_tap`` 旁路 queue)。

    codex/kimi 轨异步闭环::

        add_tap → 每条输出解码追加到滑窗 buffer → handle_pty_output
        (命中"AI 在等人"则决策 + pty.write 应答 + log)→ 命中后清 buffer。

    退出条件:tap 收到 ``None`` sentinel,或 ``pty.alive`` 变 False。
    ``finally`` 必 ``remove_tap`` 防泄漏。

    真实 ``ManagedPty`` 进程死时 ``_read_loop`` 退出但**不推 sentinel**,
    故用 ``wait_for`` 超时后轮询 ``pty.alive`` 退出(每 ``_POLL_SECONDS`` 一次),
    避免 task 在死 PTY 上永久阻塞。
    """
    tap = pty.add_tap()
    buffer = ""
    try:
        while getattr(pty, "alive", True):
            try:
                data = await asyncio.wait_for(tap.get(), timeout=_POLL_SECONDS)
            except asyncio.TimeoutError:
                continue
            if data is None:
                break  # sentinel(测试 + 优雅关闭信号)
            text = (
                data.decode("utf-8", errors="replace")
                if isinstance(data, (bytes, bytearray))
                else str(data)
            )
            buffer = buffer + text
            if len(buffer) > buffer_bytes:
                buffer = buffer[-buffer_bytes:]
            answered = handle_pty_output(
                buffer=buffer,
                pty=pty,
                adapter=adapter,
                story_facts=story_facts,
                is_awaiting_fn=is_awaiting_fn,
                llm_invoke=llm_invoke,
                log_event_fn=log_event_fn,
            )
            if answered:
                buffer = ""  # 应答后清窗,避免同问题重复触发
    finally:
        pty.remove_tap(tap)


def _build_decision_prompt(
    question: str, options: list[str], story_facts: dict
) -> str:
    """Assemble the decision prompt. Feeds structured facts, not raw output."""
    return (
        "你是 code agent 的监督决策器。基于 story 上下文,为 agent 的提问选最佳回应。\n"
        f"Story 上下文: {json.dumps(story_facts, ensure_ascii=False)}\n"
        f"Agent 提问: {question}\n"
        f"可选项: {json.dumps(options, ensure_ascii=False)}\n"
        "只返回 JSON,不要任何额外文字:\n"
        ' {"choice": "<必须是可选项之一>", "reason": "<简短理由>"}'
    )


def _parse_decision(raw: str) -> dict:
    """Parse LLM JSON response into {"choice", "reason"}.

    剥离 markdown 代码块(```json ... ```),LLM 常这样包裹 JSON 输出。
    """
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    data = json.loads(text)
    return {"choice": data["choice"], "reason": data["reason"]}
