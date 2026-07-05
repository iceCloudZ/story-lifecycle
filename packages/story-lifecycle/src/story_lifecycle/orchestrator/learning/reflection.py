"""Reflection Decider(层5 反思学习)。

读决策事件流(``supervisor_decision`` / ``recovery_action`` / ``judge_verdict`` /
``transition_decision``)→ 沉淀可复用 ``playbook``。打通飞轮的"反思→知识"环节:
跑 N story 后,把"换 adapter 解过同类失败"这类经验沉淀成规则,供层2 transition 的
``history_facts`` / context_providers 回注,新 story 受益。

**verifier 形态(§2.2 #6)**:基于事件 ground truth(recovery 后是否真 pass)判成功,
**非 verbal reflection**(LLM 自我对话易自欺)。纯函数,零副作用(§2.2 #1)。

当前规则提取(确定性,无需 LLM —— 反思是低频批处理,规则稳;LLM 可后置):
- 同一 story 出现 ``recovery_action(retry_new_adapter, failed=X→new=Y)`` 且后续有 pass 事件
  → 记"adapter X 失败 → 换 Y 成功",support 累加。
- 没 pass 兜底的 recovery 不沉淀(避免学错)。
"""

from __future__ import annotations

from collections import Counter, defaultdict

# 视为"成功"的事件类型(及 payload 里的布尔键)
_PASS_EVENT_TYPES = ("judge_verdict", "gate_result", "transition_decision")


def reflect(*, events: list[dict]) -> dict:
    """Pure Decider. Distill a playbook from the decision-event stream.

    Args:
        events: list of ``{event_type, story_key, payload(dict)}``.
            (Handler 从 event_log 行解析 payload JSON 后传入。)

    Returns:
        ``{"playbook": [{rule, support, evidence}], "stats": {event_type: count}}``.
        playbook 按 support 降序(高支撑规则在前)。
    """
    stats: Counter = Counter()
    by_story: dict[str, list[dict]] = defaultdict(list)

    for e in events:
        etype = e.get("event_type") or "?"
        stats[etype] += 1
        by_story[e.get("story_key", "")].append(e)

    swap_evidence: Counter = Counter()  # (failed_adapter, new_adapter) -> 支撑数

    for _story, evs in by_story.items():
        # 该 story 是否最终 pass(任意 pass 类事件 + payload 里 passed/pass=True)
        passed = any(
            e.get("event_type") in _PASS_EVENT_TYPES
            and _payload_bool(e.get("payload"), ("passed", "pass"))
            for e in evs
        )
        if not passed:
            continue  # 没成功兜底 → 不沉淀(避免学错)
        for e in evs:
            if e.get("event_type") != "recovery_action":
                continue
            payload = e.get("payload") or {}
            if payload.get("action") != "retry_new_adapter":
                continue
            failed = payload.get("failed_adapter") or payload.get("adapter") or "?"
            new = payload.get("new_adapter")
            if not new:
                continue
            swap_evidence[(failed, new)] += 1

    playbook = [
        {
            "rule": f"adapter {failed} 失败 → 换 {new} 成功",
            "support": cnt,
            "evidence": "recovery_action(retry_new_adapter) + 后续 pass",
        }
        for (failed, new), cnt in swap_evidence.items()
    ]
    playbook.sort(key=lambda r: -r["support"])
    return {"playbook": playbook, "stats": dict(stats)}


def _payload_bool(payload, keys) -> bool:
    """payload 里任一布尔键为 True → True。"""
    if not isinstance(payload, dict):
        return False
    return any(payload.get(k) is True for k in keys)


def build_transition_history_facts(
    *,
    events: list[dict],
    failed_adapter: str,
    gate_round: int,
    retry_limit: int,
) -> dict:
    """层5 回注:把 reflect 的 playbook 翻译成 transition 的 ``history_facts``。

    飞轮闭环:recovery 换 adapter 成功 → ``reflect`` 沉淀"adapter X 失败 → 换 Y 成功"
    → 本函数检查 playbook 里是否有以 ``failed_adapter`` 为失败方的规则 →
    ``same_failure_swap_succeeded=True`` → ``decide_transition`` 返回 ``swap_approach``
    (替硬编码 False,让 swap 真触发)。

    Args:
        events: 决策事件流(同 ``reflect``,caller 从 event_log 查近期事件传入)。
        failed_adapter: 当前 verify-gate 失败的 adapter(要查"换法是否解过它")。
        gate_round: 当前 verify 修复轮次 → ``failure_count_on_stage``。
        retry_limit: gate 重试上限 → ``max_retries``。

    Returns:
        ``{"failure_count_on_stage", "max_retries", "same_failure_swap_succeeded"}``,
        可直接喂 ``decide_transition`` 的 ``history_facts``。
    """
    playbook = reflect(events=events)["playbook"]
    # reflect 的 rule 形如 "adapter codex 失败 → 换 claude 成功"
    needle = f"adapter {failed_adapter} 失败"
    swap_worked = any(rule["rule"].startswith(needle) for rule in playbook)
    return {
        "failure_count_on_stage": gate_round,
        "max_retries": retry_limit,
        "same_failure_swap_succeeded": swap_worked,
    }
