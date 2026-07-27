"""Supervisor Decider — 监督交互式 code agent 的提问。

监督交互式 code agent (claude/codex/kimi):当 agent 提问/要选择时,``handle_pty_output``
按 supervision 模式分流:

- **默认(人工盯,``auto_confirm=False``)**:不调 LLM、不写 PTY。仅落 ``awaiting_confirm``
  事件(审计可见)+ 桌面通知(复用 ``notify.send``)。人工在终端自己看到确认提示、自己答。
  零 token 消耗,绝不往 PTY 塞噪声输入。
- **全自动(``auto_confirm=True``,仅 benchmark/CI 显式配置)**:``decide_response`` 用注入的
  LLM 决策返回 {choice, reason},Handler 回写 PTY。

设计原则:
- **纯 Decider**(``decide_response``):不读 DB、不写文件、不起进程;LLM 通过 ``llm_invoke``
  参数注入(可测)。
- **零副作用**:所有 I/O(写 PTY、log_event、notify)归 Handler(``handle_pty_output``)。
- **决策上下文喂结构化 facts**(LangGraph 范式),不喂原始 PTY/stream 文本。
- 模式由 ``story_facts["auto_confirm"]`` 门控,planner 从 profile 的 ``auto_confirm`` 注入。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Callable

log = logging.getLogger(__name__)

# pty.alive 轮询周期。真实 ManagedPty 进程死时 _read_loop 退出但**不往 tap 推 sentinel**,
# 故 supervise_pty_session 用 wait_for 超时后检查 pty.alive 退出,避免 task 永久阻塞。
_POLL_SECONDS = 1.0

# STEP 1.7c:规则卡住检测阈值(DESIGN §4.3,纯确定性零 LLM)。
# 超时无新输出(秒):code agent 在 N 秒内没产新输出 → 视为卡住。可被环境变量
# STORY_STUCK_TIMEOUT 覆盖(planner / 测试注入)。默认 300s(5min)—— 触发事故是
# design 卡 25min,300s 留足正常思考余量又不至于让事故再跑 25min 才被发现。
_DEFAULT_STUCK_TIMEOUT = 300
STUCK_TIMEOUT_SECONDS = int(
    os.environ.get("STORY_STUCK_TIMEOUT", _DEFAULT_STUCK_TIMEOUT)
)
# events.jsonl 里连续 K 条 error 行 → 反复报错(卡住信号)。
STUCK_REPEATED_ERRORS = 5


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


def _notify_awaiting(story_key, stage: str, adapter: str, question: str) -> None:
    """人工模式下命中 code-agent 提问时弹桌面通知(复用 ``notify.send``)。

    ``notify.send`` 是软依赖(plyer 不可用时静默跳过),故包 try/except 不抛 ——
    通知失败绝不能影响 supervisor 主循环。question 截断到 120 字防通知溢出。
    """
    try:
        from .notify import send as notify

        notify(
            f"[{story_key}] {adapter} 需要确认",
            f"({stage}) {question[:120]}",
        )
    except Exception:  # noqa: BLE001 — 通知是 best-effort,绝不炸 supervisor
        log.debug("awaiting notify skipped for %s", story_key)


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

    Returns: True 命中(auto_confirm 时已应答+log;人工模式时仅落 awaiting_confirm 事件 +
    桌面通知,不写 PTY),False 未命中(不调 LLM、不写 PTY、不 log)。
    """
    hit = is_awaiting_fn(buffer)
    if not hit:
        return False
    question, options = hit
    story_key = story_facts.get("story_key")
    stage = story_facts.get("stage", "")

    # §supervision-mode:默认人工盯(不 auto_confirm)。
    #   auto_confirm=False → **不**调 LLM、**不**往 PTY 写答案:
    #     人工在终端自己看到确认提示、自己键入答案。supervisor 仅落 awaiting_confirm
    #     事件(审计可见)+ 桌面通知(复用 notify.send),零 token 消耗。
    #   auto_confirm=True  → 原全自动逻辑:decide_response(LLM) + pty.write(应答)。
    #     仅 benchmark/CI 等显式配置的场景启用,避免对人工盯着的故事自动塞噪声输入。
    if not story_facts.get("auto_confirm"):
        log_event_fn(
            story_key,
            stage=stage,
            event_type="awaiting_confirm",
            payload={
                "adapter": adapter,
                "question": question,
                "options": options,
            },
        )
        _notify_awaiting(story_key, stage, adapter, question)
        return True

    try:
        decision = decide_response(
            question=question,
            options=options,
            story_facts=story_facts,
            llm_invoke=llm_invoke,
        )
    except Exception as exc:  # noqa: BLE001 — Handler 边界降级,绝不让 LLM 失常炸掉 PTY session
        log.warning(
            "supervisor decide failed for %s: %s", story_facts.get("story_key"), exc
        )
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


def _build_decision_prompt(question: str, options: list[str], story_facts: dict) -> str:
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


# ---------------------------------------------------------------------------
# STEP 1.7c:规则卡住检测 + escalate_human(纯确定性,零 LLM)。
# DESIGN-artifact-driven-stage-completion §4.3 / STEP 1 子任务 1.7。
#
# 红线:STEP 1 的卡住检测是**纯规则 + escalate_human,零 LLM**。
# 不动 supervisor 的 LLM 判定(那是 STEP 2)。这里只:规则判卡住 → 落 awaiting_confirm
# 事件 + 桌面通知,不调 LLM。这是 Resolver(规则检测)层,Handler 执行副作用。
# ---------------------------------------------------------------------------


def detect_stuck(
    *,
    last_output_ts: float | None,
    now_ts: float,
    process_alive: bool,
    events: list[dict] | None = None,
    timeout_seconds: float | None = None,
) -> dict | None:
    """Pure rule-based stuck detector (Resolver,零 LLM)。

    Args:
        last_output_ts: code agent 最后一次产输出的时间戳(epoch 秒)。None = 从未产输出。
        now_ts: 当前时间戳(epoch 秒)。
        process_alive: code agent 进程是否还活着。
        events: 可选,events.jsonl 最近若干条(查反复报错用)。None 时不查 error 规则。
        timeout_seconds: 无新输出超时阈值(默认 STUCK_TIMEOUT_SECONDS)。

    Returns:
        None = 没卡住;dict = 卡住了,含 {"reason", "duration", "rule"} 给 escalate。
        reason 是给人看的中文卡因(纯规则判,非 LLM 诊断 —— 那是 STEP 2)。

    三条规则(任一命中即卡住):
      1. 超时无新输出:process 活着但 last_output 距 now > timeout → idle 卡死。
      2. 进程活但从未输出(last_output_ts is None)+ 已过宽限期(>60s)→ 启动卡死。
      3. 反复报错:events.jsonl 末尾连续 >= STUCK_REPEATED_ERRORS 条 error → 错误循环。
    """
    timeout = timeout_seconds if timeout_seconds is not None else STUCK_TIMEOUT_SECONDS

    # 规则 3:反复报错(连续 error 行)。
    if events:
        tail = events[-(STUCK_REPEATED_ERRORS + 5) :]  # 多看几条防抖
        consecutive_err = 0
        for ev in reversed(tail):
            ev_type = str(ev.get("type", "")).lower()
            ev_text = str(ev.get("text", "")).lower()
            if "error" in ev_type or "error" in ev_text or "traceback" in ev_text:
                consecutive_err += 1
                if consecutive_err >= STUCK_REPEATED_ERRORS:
                    return {
                        "rule": "repeated_errors",
                        "reason": f"反复报错(events.jsonl 末尾连续 {consecutive_err} 条 error)",
                        "duration": 0,
                        "consecutive_errors": consecutive_err,
                    }
            else:
                break  # 连续中断,重新数

    # 规则 1 / 2:超时无新输出。process 不活 → 不算"卡住"(那是死了,归别处处理)。
    if not process_alive:
        return None
    if last_output_ts is None:
        # 从未输出。给 60s 启动宽限(claude/opencode 启动慢)。
        return (
            None  # 启动宽限由调用方用 now_ts - spawn_ts 单独判,这里只看"有过输出后卡住"
        )
    idle = now_ts - last_output_ts
    if idle > timeout:
        return {
            "rule": "no_output_timeout",
            "reason": f"超时无新输出(idle {int(idle)}s > {int(timeout)}s)",
            "duration": int(idle),
        }
    return None


def escalate_stuck(
    *,
    story_key: str,
    stage: str,
    adapter: str,
    detection: dict,
    log_event_fn: Callable,
    notify_fn: Callable[[str, str], None] | None = None,
) -> None:
    """Handler:规则检测到卡住 → 落 awaiting_confirm 事件 + 桌面通知(零 LLM)。

    复用 awaiting_confirm 事件类型(前端 / 人已知这个语义"需人介入"),payload 含
    stuck_reason / rule / duration。notify_fn 注入(默认调本包 notify.send)。

    STEP 1 不调 LLM —— 纯规则检测 + 人升级。LLM 卡因诊断是 STEP 2(调度点②)。
    """
    if notify_fn is None:
        from .notify import send as notify_fn
    payload = {
        "adapter": adapter,
        "stuck": True,
        "rule": detection.get("rule", "unknown"),
        "stuck_reason": detection.get("reason", ""),
        "duration": detection.get("duration", 0),
    }
    log_event_fn(
        story_key,
        stage=stage,
        event_type="awaiting_confirm",
        payload=payload,
    )
    try:
        notify_fn(
            f"[{story_key}] {adapter} 卡住需介入",
            f"({stage}) {detection.get('reason', '')[:120]}",
        )
    except Exception:  # noqa: BLE001 — 通知 best-effort
        log.debug("stuck notify failed (non-fatal)", exc_info=True)
    log.warning(
        "[%s/%s] stuck detected (%s): %s — escalated to human",
        story_key,
        stage,
        detection.get("rule"),
        detection.get("reason"),
    )
