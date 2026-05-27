"""P1 Ask Copilot — LLM-powered diagnostic assistant.

Read-only: takes a redacted Debug Packet + user question, returns
structured CopilotResponse with suggestions. Never modifies workflow state.
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
    return f"""你是一个 Story Lifecycle 诊断助手（Copilot）。你的任务是分析 Story 的诊断数据包，回答用户的问题。

## 角色约束
- 你只有只读权限，不能修改任何工作流状态
- 不要建议 skip、fail、retry、advance 等工作流操作
- 你的建议应该是用户可以手动执行的操作（查看文件、运行命令、检查配置等）
- 如果不确定，请降低 confidence 并建议用户收集更多信息

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
  ]
}}

## 规则
- suggestions 至少提供 1 条，最多 5 条
- confidence 根据数据包中的证据充分程度判断
- 优先关注 stuck_reason、done_state、session_state 中的异常信号
- 如果 recent_events 中有错误事件，应重点关注
- 用中文回复"""


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

    return {
        "suggestions": suggestions,
        "questions": [str(q) for q in questions if isinstance(q, str) and q.strip()],
    }
