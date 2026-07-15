"""Reflection Decider(层5 反思学习)。

读决策事件流(``supervisor_decision`` / ``recovery_action`` / ``judge_verdict`` /
``transition_decision``)→ 沉淀可复用 ``playbook``。打通飞轮的"反思→知识"环节:
跑 N story 后,把"换 adapter 解过同类失败"这类经验沉淀成规则,供层2 transition 的
``history_facts`` / context_providers 回注,新 story 受益。

**verifier 形态(§2.2 #6)**:基于事件 ground truth(recovery 后是否真 pass)判成功,
**非 verbal reflection**(LLM 自我对话易自欺)。纯函数,零副作用(§2.2 #1)。

规则提取(确定性,无需 LLM —— 反思是低频批处理,规则稳;LLM 可后置):
- adapter-routing: 同一 story 出现 ``recovery_action(retry_new_adapter, failed=X→new=Y)``
  且后续有 pass 事件 → 记"adapter X 失败 → 换 Y 成功",support 累加。
- failure-pattern: 同 stage 反复 retry(transition_decision.action=retry 连续 ≥2 次)
  → 记"stage X 反复失败",support 累加。
- rescue: ``recovery_action(insert_rescue_stage)`` 后 pass → 记"插救援 stage 能解"。
- 没 pass 兜底的 recovery 不沉淀(避免学错)。

每条 rule 带 ``dimension`` 字段,供落库时分类到不同文件(见 ``write_playbook_file``)。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

# 视为"成功"的事件类型(及 payload 里的布尔键)
_PASS_EVENT_TYPES = ("judge_verdict", "gate_result", "transition_decision")

# dimension → 落库文件名映射(供 write_playbook_file 分文件)
_DIMENSION_FILES = {
    "adapter-routing": "adapter-routing.md",
    "failure-pattern": "failure-patterns.md",
    "rescue": "rescue.md",
}


def reflect(*, events: list[dict]) -> dict:
    """Pure Decider. Distill a playbook from the decision-event stream.

    Args:
        events: list of ``{event_type, story_key, payload(dict)}``.
            (Handler 从 event_log 行解析 payload JSON 后传入。)

    Returns:
        ``{"playbook": [{rule, dimension, support, evidence}], "stats": {event_type: count}}``.
        playbook 按 support 降序(高支撑规则在前)。
    """
    stats: Counter = Counter()
    by_story: dict[str, list[dict]] = defaultdict(list)

    for e in events:
        etype = e.get("event_type") or "?"
        stats[etype] += 1
        by_story[e.get("story_key", "")].append(e)

    # 三类沉淀的累积器:key → {support, reason(最新)}
    swap_evidence: Counter = Counter()           # (failed, new) -> support
    swap_reasons: dict[tuple, str] = {}           # (failed, new) -> 最新 reason
    stage_failures: Counter = Counter()           # stage -> 连续 retry 次数
    stage_fail_reasons: dict[str, str] = {}       # stage -> 最新 reason
    rescue_evidence: Counter = Counter()          # stage -> support
    rescue_reasons: dict[str, str] = {}           # stage -> 最新 reason

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
            etype = e.get("event_type")
            payload = e.get("payload") or {}

            # adapter-routing: recovery_action(retry_new_adapter)
            if etype == "recovery_action" and payload.get("action") == "retry_new_adapter":
                failed = payload.get("failed_adapter") or payload.get("adapter") or "?"
                new = payload.get("new_adapter")
                if not new:
                    continue
                key = (failed, new)
                swap_evidence[key] += 1
                swap_reasons[key] = payload.get("reason", "")  # 存原文(§5.1.1 Q3)

            # rescue: recovery_action(insert_rescue_stage)
            if etype == "recovery_action" and payload.get("action") == "insert_rescue_stage":
                stage = payload.get("rescue_stage") or payload.get("stage") or "unknown"
                rescue_evidence[stage] += 1
                rescue_reasons[stage] = payload.get("reason", "")

            # failure-pattern: transition_decision(retry) — 同 stage 反复重试
            if etype == "transition_decision" and payload.get("action") == "retry":
                stage = payload.get("stage") or "unknown"
                stage_failures[stage] += 1
                stage_fail_reasons[stage] = payload.get("reason", "")

    playbook: list[dict] = []

    # adapter-routing 规则
    for (failed, new), cnt in swap_evidence.items():
        playbook.append({
            "rule": f"adapter {failed} 失败 → 换 {new} 成功",
            "dimension": "adapter-routing",
            "support": cnt,
            "evidence": swap_reasons.get((failed, new)) or "recovery_action(retry_new_adapter) + 后续 pass",
        })

    # failure-pattern 规则(只沉淀反复出现 ≥2 次的)
    for stage, cnt in stage_failures.items():
        if cnt < 2:
            continue  # 单次 retry 不算 pattern
        playbook.append({
            "rule": f"stage {stage} 反复失败({cnt} 次)",
            "dimension": "failure-pattern",
            "support": cnt,
            "evidence": stage_fail_reasons.get(stage, ""),
        })

    # rescue 规则
    for stage, cnt in rescue_evidence.items():
        playbook.append({
            "rule": f"插救援 stage {stage} 后 pass",
            "dimension": "rescue",
            "support": cnt,
            "evidence": rescue_reasons.get(stage, "recovery_action(insert_rescue_stage) + 后续 pass"),
        })

    playbook.sort(key=lambda r: -r["support"])
    return {"playbook": playbook, "stats": dict(stats)}


def _payload_bool(payload, keys) -> bool:
    """payload 里任一布尔键为 True → True。"""
    if not isinstance(payload, dict):
        return False
    return any(payload.get(k) is True for k in keys)


def write_playbook_file(
    *, workspace: str, task_type: str, dimension: str, playbook: list[dict]
) -> str | None:
    """把 reflect 的 playbook 按 task_type 分层落盘(REFACTOR §5.1.2)。

    路径: ``<workspace>/.story/knowledge/playbooks/<task_type>/<dimension>.md``
    去重: 按结构化 key 合并,support 累加(不是文本匹配)。
    best-effort: 写失败只 warning,不影响 story 完成。

    分层设计(防 skill 库相变崩溃,arxiv 2601.04748):task_type 子目录隔离,
    模型查询时只看当前 task_type 的子集 + 全局维度,不面对全量。
    """
    if not task_type or not playbook:
        return None

    fname = _DIMENSION_FILES.get(dimension)
    if not fname:
        return None

    from ...infra.story_paths import safe_story_path
    path = safe_story_path(
        workspace, ".story", "knowledge", "playbooks", task_type, fname
    )

    try:
        # 读现有 playbook(如果文件已存在)
        existing: dict[str, dict] = {}  # rule 文本 -> {support, evidence}
        if path.exists():
            _parse_existing_playbook(path, existing)

        # 合并新 playbook(按 rule 文本去重,support 累加,evidence 取最新)
        for entry in playbook:
            if entry.get("dimension") != dimension:
                continue
            rule = entry["rule"]
            if rule in existing:
                existing[rule]["support"] += entry.get("support", 1)
                existing[rule]["evidence"] = entry.get("evidence", existing[rule]["evidence"])
            else:
                existing[rule] = {
                    "support": entry.get("support", 1),
                    "evidence": entry.get("evidence", ""),
                }

        # 按支持度降序写回
        sorted_rules = sorted(existing.items(), key=lambda x: -x[1]["support"])
        lines = [f"# Playbook: {dimension} (task_type: {task_type})", ""]
        lines.append("> 自动生成(reflect 落库)。support = 历史出现次数。")
        lines.append("")
        for rule, data in sorted_rules:
            lines.append(f"- **{rule}** (support: {data['support']})")
            if data.get("evidence"):
                lines.append(f"  - {data['evidence']}")
            lines.append("")

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")
        return str(path.relative_to(workspace))
    except Exception as exc:
        import logging
        logging.getLogger("story-lifecycle.reflection").warning(
            "write_playbook_file failed for %s/%s: %s", task_type, dimension, exc
        )
        return None


def _parse_existing_playbook(path: Path, existing: dict[str, dict]) -> None:
    """解析现有 playbook 文件,填充 existing(rule -> {support, evidence})。

    文件格式(由 write_playbook_file 写):
        - **<rule>** (support: N)
          - <evidence>
    """
    try:
        text = path.read_text(encoding="utf-8")
        current_rule = None
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("- **") and "**" in line[4:]:
                # 提取 rule 文本和 support
                end = line.index("**", 4)
                current_rule = line[4:end]
                # support 在后面的括号里
                sup = 1
                if "(support:" in line:
                    try:
                        sup = int(line.split("(support:")[1].rstrip(")"))
                    except (ValueError, IndexError):
                        pass
                existing[current_rule] = {"support": sup, "evidence": ""}
            elif line.startswith("- ") and current_rule:
                # evidence 行
                existing[current_rule]["evidence"] = line[2:].strip()
    except Exception:
        pass  # 解析失败→当空文件处理,不崩


def persist_playbook(*, workspace: str, story_key: str, events: list[dict], task_type: str) -> None:
    """story 完成时调用:reflect → 按 dimension 分文件落盘。

    挂在 ``_write_retrospect`` 旁边(planner.py:1272, 1433)。
    best-effort,只在 completed 路径触发(failed 不触发)。

    Args:
        workspace: 工作区根。
        story_key: 当前 story。
        events: 决策事件流(caller 从 event_log 查近期事件传入,
            复用 ``_build_verify_history_facts`` 的事件查询逻辑)。
        task_type: 当前 story 的 task_type(从 context_json 取)。
    """
    if not task_type:
        return  # task_type 为空不落库(冷启动期可能未分类)
    try:
        result = reflect(events=events)
        playbook = result.get("playbook") or []
        if not playbook:
            return
        # 按 dimension 分文件写
        dimensions_seen = {entry.get("dimension") for entry in playbook if entry.get("dimension")}
        for dim in dimensions_seen:
            write_playbook_file(
                workspace=workspace, task_type=task_type, dimension=dim, playbook=playbook
            )
        import logging
        logging.getLogger("story-lifecycle.reflection").info(
            "[%s] persisted playbook for task_type=%s (%d dimensions, %d rules)",
            story_key, task_type, len(dimensions_seen), len(playbook),
        )
    except Exception as exc:
        import logging
        logging.getLogger("story-lifecycle.reflection").warning(
            "[%s] persist_playbook failed: %s", story_key, exc
        )


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
