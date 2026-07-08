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
) -> dict:
    """Pure Decider. Choose a response for a code-agent question.

    Args:
        question: code agent 提出的问题/要做的选择(结构化文本)。
        options: 可选项列表(choice 必须是其中之一,后续测试驱动校验)。
        story_facts: 结构化 story 上下文(story_key/stage/profile/已做决策等)。
        llm_invoke: 注入的 LLM 调用,prompt -> JSON 字符串。

    Returns:
        {"choice": str, "reason": str}
    """
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
    try:
        decision = decide_response(
            question=question,
            options=options,
            story_facts=story_facts,
            llm_invoke=llm_invoke,
        )
    except Exception as exc:  # noqa: BLE001 — Handler 边界降级,绝不让 LLM 失常炸掉 PTY session
        log.warning("supervisor decide failed for %s: %s", story_facts.get("story_key"), exc)
        return False
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
