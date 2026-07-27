"""调度点② 卡住 LLM 诊断(STEP 2.3,DESIGN §4.3)。

supervisor 规则检测到卡住(STEP 1 的 detect_stuck)→ 本模块诊断:
- **第一步:摘要先行**(默认路径):预处理摘要(最后 N 条 events + 错误行 + idle 时长)
  喂纯判定函数,判 5 类卡因(真卡/提问/跑偏/慢/失败)。**纯判定,零工具**。
- **例外升级 agentic**(规则触发):同 stage 第二次卡住 / 摘要检测到循环模式 → 升级
  agentic 深读 events.jsonl(只读 read_file 工具 + 调用 ≤5)。

**红线**:
- agentic 是**例外路径不是默认**(评审 B / 设计 §4.3)。规则触发才升级。
- **只读工具**(read_file events.jsonl),调用上限 ≤5。
- **无打字纠偏**(评审 C):决策只有 restart(杀+带 seed 重起)/ escalate_human / wait。
  不往 PTY 注入纠偏文字(时序脆弱 + 单次带歪防不住)。

输出决策 → Handler(planner)执行副作用。本模块是 Decider(除 log_decision 审计)。
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

log = logging.getLogger("story-lifecycle.stuck_diagnose")

# 摘要喂 LLM 的 events 条数(§4.3:最后 N 条 + 错误行 + idle 时长)。
SUMMARY_EVENTS_N = 15
# agentic 调用上限(红线:≤5)。
AGENTIC_MAX_CALLS = 5
# 循环模式检测:events 文本 hash 重复次数阈值(同一段输出反复出现 → 循环)。
LOOP_REPEAT_THRESHOLD = 3


# ---- Pydantic schema ----


class StuckDiagnosis(BaseModel):
    """卡住诊断的 LLM 输出结构(summary + agentic 共用)。"""

    cause: Literal["truly_stuck", "asking", "detoured", "slow", "failed"]
    action: Literal["restart", "escalate", "wait"]
    seed: str = ""  # restart 时带的卡因诊断 seed(给 code agent 干净连贯的下一轮起点)
    reason: str = ""


# ---- 触发规则:是否升级 agentic ----


def should_upgrade_agentic(
    story_key: str,
    stage: str,
    detection: dict,
    events: list[dict] | None = None,
    *,
    db_module=None,
) -> bool:
    """是否升级 agentic 深读(规则触发,§4.3)。

    两条规则(任一命中 → True):
    1. 同 stage 第二次卡住(查 orchestrator_decision 该 stage stuck_* 触发数 >= 1)。
    2. 摘要检测到循环模式(events 文本 hash 重复 >= LOOP_REPEAT_THRESHOLD)。

    agentic 是例外路径(红线),默认不升级。
    """
    if db_module is None:
        from ...infra.db import models as db_module

    # 规则 1:同 stage 第二次卡住。
    try:
        # 只数 stuck 触发的(trigger 是 stuck_summary / stuck_agentic)
        stuck_decisions = db_module.get_decisions(story_key, stage, limit=20)
        stuck_triggers = {
            "stuck_summary",
            "stuck_agentic",
        }
        prior_stuck = sum(
            1 for d in stuck_decisions if d.get("trigger") in stuck_triggers
        )
        if prior_stuck >= 1:
            log.info(
                "[%s/%s] 升级 agentic:同 stage 已卡过 %d 次",
                story_key,
                stage,
                prior_stuck,
            )
            return True
    except Exception as exc:  # noqa: BLE001
        log.debug("[%s/%s] stuck_count query failed: %s", story_key, stage, exc)

    # 规则 2:循环模式(events 文本 hash 重复)。
    if events and _detect_loop_pattern(events):
        log.info("[%s/%s] 升级 agentic:摘要检测到循环模式", story_key, stage)
        return True

    return False


def _detect_loop_pattern(events: list[dict]) -> bool:
    """检测 events 是否有循环模式(同一段文本反复出现 ≥ LOOP_REPEAT_THRESHOLD)。

    把每条 event 的文本归一化 hash,统计重复。spinner 字符(✻✽✶ 等)不算内容,先剥。
    """
    hashes: dict[str, int] = {}
    for ev in events:
        text = (ev.get("text") or "").strip()
        if not text:
            continue
        # 剥 spinner / 控制字符(它们反复出现但不是循环内容)
        import re

        clean = re.sub(r"[\x00-\x1f✻✽✶✢✻⏵⏸↻…Garnishing]", "", text).strip()
        if len(clean) < 10:  # 太短不统计(spinner 残留)
            continue
        h = hashlib.sha256(clean.encode("utf-8")).hexdigest()[:8]
        hashes[h] = hashes.get(h, 0) + 1
        if hashes[h] >= LOOP_REPEAT_THRESHOLD:
            return True
    return False


# ---- 第一步:摘要先行(纯判定,默认路径)----


def diagnose_stuck_summary(
    *,
    story_key: str,
    stage: str,
    detection: dict,
    events: list[dict] | None,
    story_facts: dict,
    llm=None,
    db_module=None,
) -> dict:
    """摘要先行纯判定(默认路径,零工具,§4.3 第一步)。

    喂预处理摘要(最后 N 条 events + 错误行 + idle 时长)给 LLM,判 5 类卡因。
    LLM 不可用 → fallback:规则 detection 的 reason 直接转 escalate(零 LLM 兜底)。
    """
    if db_module is None:
        from ...infra.db import models as db_module

    summary = _preprocess_summary(detection, events)
    facts_text = f"adapter={story_facts.get('adapter', '?')}, stage={stage}, idle={detection.get('duration', 0)}s"

    if llm is None:
        from ...infra.llm_client import get_llm

        llm = get_llm()
    if not llm.api_key:
        log.warning("[%s/%s] no LLM, fallback escalate(规则检测)", story_key, stage)
        return _fallback_escalate(
            story_key, stage, detection, db_module, "LLM 不可用,规则检测转人"
        )

    prompt = _build_summary_prompt(summary, facts_text)
    try:
        result = llm.invoke_structured(
            prompt, StuckDiagnosis, temperature=0.1, timeout=60
        )
        cause = result.cause
        action = result.action
        seed = result.seed
        reason = result.reason or f"summary diagnose: {cause}"
        llm_model = getattr(llm, "model", "")
    except Exception as exc:
        log.warning(
            "[%s/%s] summary diagnose LLM failed, fallback escalate: %s",
            story_key,
            stage,
            exc,
        )
        return _fallback_escalate(
            story_key, stage, detection, db_module, f"LLM 失败:{exc}"
        )

    rid = _log_stuck_decision(
        db_module,
        story_key,
        stage,
        "stuck_summary",
        action,
        reason,
        cause,
        seed,
        llm_model,
    )
    log.info(
        "[%s/%s] stuck summary diagnose: %s → %s (%s)",
        story_key,
        stage,
        cause,
        action,
        reason[:80],
    )
    return {
        "cause": cause,
        "action": action,
        "seed": seed,
        "reason": reason,
        "trigger": "stuck_summary",
        "logged_decision_id": rid,
    }


# ---- 例外:agentic 深读(只读 read_file + ≤5 调用)----


def diagnose_stuck_agentic(
    *,
    story_key: str,
    stage: str,
    detection: dict,
    events_path: str,
    story_facts: dict,
    llm=None,
    db_module=None,
) -> dict:
    """agentic 深读 events.jsonl(例外路径,红线:只读 + ≤5 调用,§4.3)。

    用 read_file 工具读 events.jsonl 中段(summary 看不到的部分),多轮调用 ≤5,
    读完输出决策。**只读,不改文件;无打字纠偏**(评审 C)。
    """
    if db_module is None:
        from ...infra.db import models as db_module

    if llm is None:
        from ...infra.llm_client import get_llm

        llm = get_llm()
    if not llm.api_key:
        return _fallback_escalate(
            story_key, stage, detection, db_module, "agentic 无 LLM,转人"
        )

    # read_file 工具(只读):读 events.jsonl 片段。LLM 调它查中段。
    def _read_file(path: str, offset: int = 0, limit: int = 50) -> str:
        try:
            p = Path(path)
            if not p.exists():
                return f"(文件不存在: {path})"
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(lines[offset : offset + limit])
        except OSError as exc:
            return f"(读失败: {exc})"

    read_tool = {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读 events.jsonl 的指定行段(只读,查卡住中段证据)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "offset": {
                        "type": "integer",
                        "description": "起始行(0-based)",
                        "default": 0,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "读多少行",
                        "default": 50,
                    },
                },
                "required": ["path"],
            },
        },
    }

    system = (
        "你是卡住诊断器。code agent 卡住了,summary 判不了(第二次卡住/循环模式)。"
        "用 read_file 工具深读 events.jsonl 中段查证据(最多调用 5 次),"
        "然后判 5 类卡因 + 决策(restart/escalate/wait)。**只读,不改文件;无打字纠偏**。"
    )
    messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": (
                f"story={story_key} stage={stage} adapter={story_facts.get('adapter', '?')}\n"
                f"规则检测:{detection.get('reason', '?')}\n"
                f"events.jsonl 路径:{events_path}\n\n"
                f"用 read_file 查 events.jsonl 中段(行 50-200 区间,summary 没看到的),"
                f"找出真正卡因,然后给决策。最多调 5 次 read_file。"
            ),
        },
    ]

    cause, action, seed, reason = "failed", "escalate", "", "agentic 未出决策"
    llm_model = getattr(llm, "model", "")
    try:
        for _call in range(AGENTIC_MAX_CALLS):
            resp = llm.invoke_with_tools(
                messages, [read_tool], temperature=0.1, timeout=60
            )
            tool_calls = resp.get("tool_calls") or []
            if not tool_calls:
                # 没工具调用 → LLM 给了最终答案,尝试解析
                content = resp.get("content", "")
                parsed = _try_parse_diagnosis(content)
                if parsed:
                    cause, action, seed, reason = parsed
                break
            # 执行 read_file 工具(只读),把结果加回 messages 继续多轮
            for tc in tool_calls:
                fn = tc.get("function", {})
                if fn.get("name") != "read_file":
                    continue
                args = fn.get("arguments") or {}
                tool_result = _read_file(
                    args.get("path", events_path),
                    int(args.get("offset", 0)),
                    int(args.get("limit", 50)),
                )
                messages.append(
                    {"role": "assistant", "content": "", "tool_calls": [tc]}
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": tool_result[:2000],
                    }
                )
        else:
            # 用满 5 次还没出最终答案 → escalate
            reason = "agentic 用满 5 次调用仍未确诊 → 转人"
            action = "escalate"
    except Exception as exc:
        log.warning(
            "[%s/%s] agentic diagnose failed, fallback escalate: %s",
            story_key,
            stage,
            exc,
        )
        return _fallback_escalate(
            story_key, stage, detection, db_module, f"agentic 失败:{exc}"
        )

    rid = _log_stuck_decision(
        db_module,
        story_key,
        stage,
        "stuck_agentic",
        action,
        reason,
        cause,
        seed,
        llm_model,
    )
    log.info(
        "[%s/%s] stuck agentic diagnose: %s → %s (%s)",
        story_key,
        stage,
        cause,
        action,
        reason[:80],
    )
    return {
        "cause": cause,
        "action": action,
        "seed": seed,
        "reason": reason,
        "trigger": "stuck_agentic",
        "logged_decision_id": rid,
    }


# ---- 预处理摘要 + prompt ----


def _preprocess_summary(detection: dict, events: list[dict] | None) -> dict:
    """预处理喂 summary LLM 的摘要(§4.3:最后 N 条 events + 错误行 + idle 时长)。"""
    events = events or []
    tail = events[-SUMMARY_EVENTS_N:]
    # 提取错误行(含 error/traceback)
    error_lines = []
    import re

    for ev in events:
        text = (ev.get("text") or "").strip()
        if re.search(r"error|traceback|exception|失败", text, re.IGNORECASE):
            error_lines.append(text[:200])
    return {
        "rule": detection.get("rule"),
        "reason": detection.get("reason"),
        "idle_seconds": detection.get("duration", 0),
        "last_events": [
            {
                "ts": e.get("ts"),
                "dir": e.get("dir"),
                "text": (e.get("text") or "").strip()[:150],
            }
            for e in tail
        ],
        "error_lines": error_lines[-5:],  # 最近 5 条错误
    }


def _build_summary_prompt(summary: dict, facts_text: str) -> str:
    import json as _json

    return f"""你是 code agent 卡住诊断器(摘要先行)。基于预处理摘要判卡因。

## 上下文
{facts_text}

## 规则检测结果
{summary.get("reason", "?")}(规则:{summary.get("rule")})

## 最后 {len(summary.get("last_events", []))} 条 events
{_json.dumps(summary.get("last_events", []), ensure_ascii=False, indent=2)}

## 错误行(若有)
{_json.dumps(summary.get("error_lines", []), ensure_ascii=False)}

## 5 类卡因
- truly_stuck:真卡死(无进展,可能死锁/无限循环)
- asking:在等人答问题(澄清/确认)—— 不该 restart,应 escalate 让人答
- detoured:跑偏了(在做无关的事)—— restart 带纠正 seed
- slow:只是慢(大任务)—— wait 延长超时
- failed:反复报错 —— restart 换思路 seed 或 escalate

## 决策(action)
- restart:杀掉,带 seed 重起(seed 写清卡因 + 该怎么继续,给 code agent 干净起点)。**无打字纠偏**。
- escalate:转人(asking 类 / 不确定 / 多次 restart 仍卡)。
- wait:延长超时(slow 类)。

输出 JSON:
```json
{{
  "cause": "truly_stuck|asking|detoured|slow|failed",
  "action": "restart|escalate|wait",
  "seed": "restart 时的卡因诊断 + 继续指引(action=restart 时必填)",
  "reason": "简短诊断"
}}
```"""


def _try_parse_diagnosis(content: str) -> tuple[str, str, str, str] | None:
    """尝试从 LLM 文本响应解析诊断(agentic 最终轮无 tool_call 时)。"""
    import json as _json
    import re

    # 找 JSON 块
    m = re.search(r"\{[^{}]*\}", content, re.S)
    if not m:
        return None
    try:
        data = _json.loads(m.group(0))
        return (
            data.get("cause", "failed"),
            data.get("action", "escalate"),
            data.get("seed", ""),
            data.get("reason", ""),
        )
    except _json.JSONDecodeError:
        return None


# ---- 审计 + fallback ----


def _log_stuck_decision(
    db_module, story_key, stage, trigger, action, reason, cause, seed, llm_model
) -> int:
    """落 orchestrator_decision(审计,trigger=stuck_summary/stuck_agentic)。"""
    try:
        return db_module.log_decision(
            story_key,
            stage,
            trigger,
            action,
            reason=reason,
            action_taken=f"cause={cause}",
            action_payload={"cause": cause, "seed": seed[:500]}
            if seed
            else {"cause": cause},
            llm_model=llm_model,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("[%s/%s] log stuck decision failed: %s", story_key, stage, exc)
        return 0


def _fallback_escalate(
    story_key: str, stage: str, detection: dict, db_module, reason: str
) -> dict:
    """LLM 不可用时:规则 detection 的 reason 直接转 escalate(零 LLM 兜底)。"""
    rid = _log_stuck_decision(
        db_module,
        story_key,
        stage,
        "stuck_summary",
        "escalate",
        f"[FALLBACK] {reason};规则:{detection.get('reason')}",
        "failed",
        "",
        "",
    )
    return {
        "cause": "failed",
        "action": "escalate",
        "seed": "",
        "reason": f"[FALLBACK] {reason}",
        "trigger": "stuck_summary",
        "logged_decision_id": rid,
        "fallback": True,
    }
