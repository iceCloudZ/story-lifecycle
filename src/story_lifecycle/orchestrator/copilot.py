"""P1/P2/P3 Ask Copilot — LLM-powered diagnostic assistant.

P1: redacted Debug Packet + user question → CopilotResponse with suggestions.
P2: adds SuggestedAction — confirmable actions with risk levels.
P3: adds Policy Engine — DecisionEnvelope with autonomy levels.
Never auto-executes state-changing operations.
"""

from __future__ import annotations

import json
import logging
import re

from ..llm_client import get_llm
from .debug_packet import build_debug_packet, redact_mapping

log = logging.getLogger("story-lifecycle.copilot")

# Valid copilot-suggested actions → risk level
VALID_ACTIONS: dict[str, str] = {
    "package_diagnostics": "read_only",
    "run_doctor": "read_only",
    "enter_terminal": "local_config",
    "run_setup": "local_config",
    "resume_story": "workflow_state",
    "skip_stage": "workflow_state",
    "fail_story": "workflow_state",
    "abort_story": "workflow_state",
}


def ask_copilot(story_key: str, question: str) -> dict:
    """Ask the Copilot a question about a story.

    Returns a CopilotResponse dict with keys:
      - suggestions: list of {action, summary, confidence}
      - questions: list of follow-up questions (optional)
      - error: present only on failure
    """
    llm = get_llm()
    if not llm.api_key:
        return {
            "error": "LLM 未配置，请先运行 story setup",
            "suggestions": [],
            "questions": [],
        }

    packet = build_debug_packet(story_key)
    if "error" in packet:
        return {
            "error": packet["error"],
            "suggestions": [],
            "questions": [],
        }

    redacted = redact_mapping(packet)

    prompt = _build_prompt(redacted, question)

    try:
        raw = llm.invoke(prompt, temperature=0.1, timeout=90)
        result = _parse_copilot_response(raw)
        # P3: wrap actions with policy engine evaluation
        raw_actions = result.get("actions", [])
        if raw_actions:
            result["actions"] = _wrap_actions_policy(raw_actions, story_key)
        return result
    except Exception as exc:
        log.warning(f"Copilot LLM call failed: {exc}")
        return {
            "error": f"LLM 调用失败: {exc}",
            "suggestions": [],
            "questions": [],
        }


def _build_prompt(packet: dict, question: str) -> str:
    return f"""你是一个 Story Lifecycle 诊断助手（Copilot）。你的任务是分析 Story 的诊断数据包，回答用户的问题，并在高置信度时推荐可执行的操作。

## 角色约束
- 你可以推荐操作，但绝不会自动执行；所有 workflow_state 操作必须经用户确认
- 只推荐诊断数据包中证据充分支持的操作
- 不确定时宁可少推荐，不要乱推荐
- 用中文回复

## 可用操作（仅限以下 8 个）
| 操作名 | 风险等级 | 说明 |
|---|---|---|
| package_diagnostics | read_only | 打包当前 Story 诊断信息 |
| run_doctor | read_only | 运行环境检查 |
| enter_terminal | local_config | 进入 Story 终端 session |
| run_setup | local_config | 重新配置 LLM / provider |
| resume_story | workflow_state | 恢复 Story 执行 |
| skip_stage | workflow_state | 跳过当前阶段 |
| fail_story | workflow_state | 标记 Story 失败 |
| abort_story | workflow_state | 中止 Story 执行 |

## Story 诊断数据包（已脱敏）
```json
{json.dumps(packet, ensure_ascii=False, indent=2)}
```

## 用户问题
{question}

## 输出格式
返回严格的 JSON 对象（不要包含 markdown 代码块标记）：
{{
  "questions": ["需要向用户确认的问题（可选，没有则为空数组）"],
  "suggestions": [
    {{
      "action": "建议用户执行的具体操作（如：查看某文件、运行某命令、检查某配置）",
      "summary": "简要说明为什么建议这个操作",
      "confidence": "high|medium|low"
    }}
  ],
  "actions": [
    {{
      "action": "上述 8 个操作名之一",
      "label": "对用户友好的操作说明（10 字以内）",
      "risk": "该操作对应的风险等级",
      "reason": "为什么建议执行这个操作，必须有诊断数据包中的证据支撑"
    }}
  ]
}}

## 规则
- suggestions 至少 1 条、最多 5 条，是用户手动执行的排查建议
- actions 可选、最多 3 条，是系统可代为执行的操作
- workflow_state 操作仅在 stuck_reason 明确指向该问题时推荐（如 cli_exited_without_done → resume_story）
- read_only 操作在任何诊断场景下都可以推荐
- actions 为空数组时表示当前不需要系统操作
- 优先关注 stuck_reason、done_state、session_state 中的异常信号"""


def _parse_copilot_response(content: str) -> dict:
    """Parse LLM response into CopilotResponse dict, with tolerance."""
    # Direct parse
    try:
        data = json.loads(content)
        return _normalize_response(data)
    except json.JSONDecodeError:
        pass

    # Extract from markdown code fence
    m = re.search(r"```(?:json)?\s*\n(.*?)\n\s*```", content, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            return _normalize_response(data)
        except json.JSONDecodeError:
            pass

    # Bracket-counting extraction
    depth = 0
    start = None
    for i, ch in enumerate(content):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    data = json.loads(content[start : i + 1])
                    return _normalize_response(data)
                except json.JSONDecodeError:
                    pass

    log.warning(f"Failed to parse copilot response: {content[:200]}")
    return {
        "suggestions": [
            {
                "action": "查看终端输出或事件日志以获取更多信息",
                "summary": "LLM 返回了无法解析的响应，请手动检查",
                "confidence": "low",
            }
        ],
        "questions": [],
        "parse_error": True,
    }


def _normalize_response(data: dict) -> dict:
    """Ensure CopilotResponse has required fields with valid values."""
    suggestions = []
    for s in data.get("suggestions", [])[:5]:
        if not isinstance(s, dict):
            continue
        action = str(s.get("action", "")).strip()
        if not action:
            continue
        confidence = str(s.get("confidence", "medium")).lower()
        if confidence not in ("high", "medium", "low"):
            confidence = "medium"
        suggestions.append(
            {
                "action": action,
                "summary": str(s.get("summary", "")).strip(),
                "confidence": confidence,
            }
        )

    questions = data.get("questions", [])
    if not isinstance(questions, list):
        questions = []

    actions = _normalize_actions(data.get("actions", []))

    return {
        "suggestions": suggestions,
        "questions": [str(q) for q in questions if isinstance(q, str) and q.strip()],
        "actions": actions,
    }


def _normalize_actions(raw_actions: list) -> list[dict]:
    """Validate and normalize SuggestedAction entries."""
    valid: list[dict] = []
    seen = set()
    for a in (raw_actions or [])[:3]:
        if not isinstance(a, dict):
            continue
        name = str(a.get("action", "")).strip()
        if name not in VALID_ACTIONS:
            continue
        if name in seen:
            continue
        seen.add(name)
        risk = VALID_ACTIONS[name]
        valid.append(
            {
                "action": name,
                "label": str(a.get("label", name)).strip() or name,
                "risk": risk,
                "reason": str(a.get("reason", "")).strip(),
                "requires_confirm": risk in ("workflow_state", "local_config"),
            }
        )
    return valid


def _wrap_actions_policy(raw_actions: list[dict], story_key: str) -> list[dict]:
    """Wrap normalized actions in DecisionEnvelope with policy evaluation."""
    from .policy_engine import wrap_actions

    envelopes = wrap_actions(raw_actions, story_key)
    return [
        {
            "action": e.action,
            "label": e.label,
            "risk": e.risk,
            "reason": e.reason,
            "requires_confirm": e.requires_confirm,
            "policy": {
                "level": e.policy.level.value,
                "reason": e.policy.reason,
                "matched_rule": e.policy.matched_rule,
                "rejection_count": e.policy.rejection_count,
            },
        }
        for e in envelopes
    ]
