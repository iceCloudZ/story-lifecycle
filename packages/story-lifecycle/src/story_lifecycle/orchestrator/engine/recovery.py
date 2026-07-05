"""Recovery Decider(层3 失败恢复)。

当 stage 执行抛错(graph.py run_story except / planner 轮询超时)时,``decide_recovery``
决定救法:换 adapter 重试 / 降级人工 / 跳过 stage / 上交人 / 终止。

**纯 Decider(§2.2 #1)**:零副作用,不读 DB、不写文件、不起进程。规则驱动(策略确定);
``recovery_facts`` 注入历史/上限(可测)。LLM / policy_engine 可后置扩展,但基础救法无需 LLM
(recovery 频次低 + 规则更稳;守 §2.2 #7 限频)。

action 取值:
- ``retry_new_adapter``:瞬时错,未达上限 → 换 adapter 重试(带 ``new_adapter``)。
- ``escalate_human``:auth/config 错,或高价值(P0/P1)story 反复失败 → 上交人。
- ``downgrade_to_manual``:中价值(P2)story 达上限 → 降级人工接手(不丢,但不烧机)。
- ``skip_stage``:低价值(P3+)story 达上限 → 跳过该 stage。
- ``abort``:policy_engine 判定彻底无解(本基础版不主动触发,留给 policy 接入)。
"""

from __future__ import annotations

from typing import Optional

# auth/config 类错误关键词(无 LLM 也能判;这类错重试无用,直接上交人)
_AUTH_MARKERS: tuple[str, ...] = (
    "api key",
    "api_key",
    "unauthorized",
    "401",
    "not configured",
    "cloud config",
    "auth",
    "credential",
)

# 默认 adapter 轮转序(3 轨)
_DEFAULT_ADAPTER_ORDER: tuple[str, ...] = ("codex", "claude", "kimi")

# 默认重试上限(超过 → 按优先级降级/跳过/上交)
_DEFAULT_MAX_ATTEMPTS = 3

# 高价值优先级(反复失败也不丢 → escalate_human)
_HIGH_VALUE = {"P0", "P1", "p0", "p1"}
# 低价值优先级(达上限 → skip_stage)
_LOW_VALUE = {"P3", "P4", "P5", "p3", "p4", "p5"}


def decide_recovery(
    *,
    exc: BaseException,
    story_facts: dict,
    adapter: str,
    attempt_count: int,
    recovery_facts: Optional[dict] = None,
) -> dict:
    """Pure Decider. Pick a recovery action for a failed stage.

    Args:
        exc: the exception that caused the failure.
        story_facts: structured story context (story_key/stage/priority/...).
        adapter: the CLI adapter that failed (codex/claude/kimi/...).
        attempt_count: 1-based count of attempts so far on this stage.
        recovery_facts: optional injected policy:
            - ``max_attempts`` (int, default 3)
            - ``adapter_order`` (list[str], default codex/claude/kimi)

    Returns:
        ``{"action": str, "reason": str, "new_adapter"?: str}``.
        ``new_adapter`` only present when action == "retry_new_adapter".
    """
    recovery_facts = recovery_facts or {}
    max_attempts = recovery_facts.get("max_attempts", _DEFAULT_MAX_ATTEMPTS)
    exc_name = type(exc).__name__
    msg = (str(exc) or "").lower()

    # (1) auth/config 错 → 直接上交人(重试无用)
    if any(marker in msg for marker in _AUTH_MARKERS):
        return {
            "action": "escalate_human",
            "reason": f"auth/config 类错误({exc_name}),重试无价值 → 上交人处理",
        }

    # (2) 达上限 → 按优先级分流
    if attempt_count >= max_attempts:
        priority = story_facts.get("priority", "P2")
        if priority in _HIGH_VALUE:
            return {
                "action": "escalate_human",
                "reason": f"高价值({priority})story 达重试上限({attempt_count})→ 上交人",
            }
        if priority in _LOW_VALUE:
            return {
                "action": "skip_stage",
                "reason": f"低价值({priority})story 达重试上限({attempt_count})→ 跳过 stage",
            }
        return {
            "action": "downgrade_to_manual",
            "reason": f"中价值({priority})story 达重试上限({attempt_count})→ 降级人工接手",
        }

    # (3) 瞬时错,未达上限 → 换 adapter 重试
    order = recovery_facts.get("adapter_order") or list(_DEFAULT_ADAPTER_ORDER)
    new_adapter = _next_adapter(adapter, order)
    return {
        "action": "retry_new_adapter",
        "failed_adapter": adapter,
        "new_adapter": new_adapter,
        "reason": f"瞬时错误({exc_name});换 adapter {adapter}→{new_adapter} 重试"
        f"(attempt {attempt_count}/{max_attempts})",
    }


def _next_adapter(current: str, order: list[str]) -> str:
    """Pick the next adapter after ``current`` in ``order``; wrap around.

    If ``current`` is unknown, fall back to ``order[0]``.
    """
    if not order:
        return current
    if current not in order:
        return order[0]
    idx = order.index(current)
    return order[(idx + 1) % len(order)]


def rescue_story(
    *,
    story_key: str,
    recovery_decision: dict,
    ctx: dict,
    current_stage: str,
    max_attempts: int = 3,
) -> dict:
    """Handler:把 ``retry_new_adapter`` 决策落到 ctx,为重试做准备。

    - 在 ``ctx["_agent_actions"]`` 里找失败 stage(``current_stage``)的 launch action,
      把它的 adapter 换成 ``recovery_decision["new_adapter"]``。
    - bump ``ctx["_recovery_attempt"]``(重试次数上限的依据)。

    不在此处重新执行(planner 重启归 run_story 的有界重试循环),只做 ctx 外科 + 计数。

    Args:
        recovery_decision: ``decide_recovery`` 的输出。
        ctx: story context dict(就地修改:_agent_actions 里换 adapter、_recovery_attempt bump)。
        current_stage: 失败的 stage 名(定位要换 adapter 的 action)。
        max_attempts: 重试上限(超过 → 不安排)。

    Returns:
        ``{"scheduled": bool, "new_adapter"?, "attempt"?, "reason"?}``。
        scheduled=True 表示已为重试准备好(ctx 已改);caller 据此决定是否重跑。
    """
    if recovery_decision.get("action") != "retry_new_adapter":
        return {
            "scheduled": False,
            "reason": f"recovery action={recovery_decision.get('action')} 不是 retry",
        }

    attempt = int(ctx.get("_recovery_attempt", 0)) + 1
    if attempt > max_attempts:
        return {
            "scheduled": False,
            "reason": f"重试次数 {attempt} 超上限 {max_attempts}",
        }

    new_adapter = recovery_decision.get("new_adapter")
    actions = ctx.get("_agent_actions") or []
    swapped = False
    for a in actions:
        if (
            isinstance(a, dict)
            and a.get("action") == "launch"
            and a.get("stage") == current_stage
        ):
            a["adapter"] = new_adapter
            swapped = True
            break

    ctx["_recovery_attempt"] = attempt
    if not swapped:
        return {
            "scheduled": False,
            "attempt": attempt,
            "reason": f"ctx 里找不到 stage={current_stage} 的 launch action,无法换 adapter",
        }
    return {"scheduled": True, "new_adapter": new_adapter, "attempt": attempt}
