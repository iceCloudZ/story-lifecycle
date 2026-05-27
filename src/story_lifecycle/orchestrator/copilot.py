"""P1/P2 Ask Copilot — LLM-powered diagnostic assistant.

P1: redacted Debug Packet + user question → CopilotResponse with suggestions.
P2: adds SuggestedAction — confirmable actions with risk levels.
Never auto-executes state-changing operations.
"""

from __future__ import annotations

import json
import os
import re
import time
import logging

import httpx

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


def _api_config() -> tuple[str, str, str]:
    return (
        os.environ.get("STORY_LLM_API_KEY", ""),
        os.environ.get("STORY_LLM_BASE_URL", "https://api.deepseek.com"),
        os.environ.get("STORY_LLM_MODEL", "deepseek-v4-pro"),
    )


def ask_copilot(story_key: str, question: str) -> dict:
    """Ask the Copilot a question about a story.

    Returns a CopilotResponse dict with keys:
      - suggestions: list of {action, summary, confidence}
      - questions: list of follow-up questions (optional)
      - error: present only on failure
    """
    api_key, base_url, model = _api_config()
    if not api_key:
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
        raw = _call_llm(base_url, api_key, model, prompt, story_key=story_key)
        return _parse_copilot_response(raw)
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


def _call_llm(
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    *,
    story_key: str = "",
) -> str:
    """Call LLM and return raw content string."""
    t0 = time.monotonic()
    resp = httpx.post(
        f"{base_url}/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 1024,
        },
        timeout=90,
    )
    resp.raise_for_status()
    body = resp.json()
    msg = body["choices"][0]["message"]
    content = msg.get("content", "") or msg.get("reasoning_content", "")
    usage = body.get("usage", {})

    _trace_llm(
        model=model,
        usage=usage,
        duration_ms=int((time.monotonic() - t0) * 1000),
        story_key=story_key,
    )

    if not content.strip():
        raise RuntimeError("LLM returned empty content")

    return content


def _trace_llm(
    *,
    model: str,
    usage: dict,
    duration_ms: int,
    story_key: str = "",
    success: bool = True,
    error: str = "",
):
    """Record LLM call trace to DB."""
    try:
        from ..db.models import log_llm_trace

        log_llm_trace(
            story_key=story_key,
            stage="",
            operation="ask_copilot",
            model=model,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
            duration_ms=duration_ms,
            success=success,
            error=error,
        )
    except Exception:
        pass


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
