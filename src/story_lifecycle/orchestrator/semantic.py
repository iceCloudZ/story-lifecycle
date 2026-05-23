"""LLM Semantic Extraction Layer.

Provides unified LLM structured-output calls for semantic tasks.
Each function returns SemanticResult with mode/confidence/fallback.
"""

from __future__ import annotations

import json
import os
import logging
import re
from typing import Literal, TypedDict

import httpx

log = logging.getLogger("story-lifecycle.semantic")


class SemanticResult(TypedDict):
    ok: bool
    mode: Literal["llm", "rule_fallback", "unavailable", "error"]
    confidence: Literal["high", "medium", "low"]
    data: dict
    warnings: list[str]


# ── internal helpers ──


def _get_api_key() -> str:
    return os.environ.get("STORY_LLM_API_KEY", "")


def _get_base_url() -> str:
    return os.environ.get("STORY_LLM_BASE_URL", "https://api.deepseek.com")


def _get_model() -> str:
    return os.environ.get("STORY_LLM_MODEL", "deepseek-chat")


def _ok_result(data: dict, confidence: str = "high") -> SemanticResult:
    return SemanticResult(
        ok=True,
        mode="llm",
        confidence=confidence,  # type: ignore[arg-type]
        data=data,
        warnings=[],
    )


def _fallback_result(data: dict, warnings: list[str] | None = None) -> SemanticResult:
    return SemanticResult(
        ok=True,
        mode="rule_fallback",
        confidence="low",  # type: ignore[arg-type]
        data=data,
        warnings=warnings or ["LLM unavailable, using rule fallback"],
    )


def _error_result(error: str) -> SemanticResult:
    return SemanticResult(
        ok=False,
        mode="error",  # type: ignore[arg-type]
        confidence="low",  # type: ignore[arg-type]
        data={},
        warnings=[error],
    )


def _unavailable_result(reason: str = "LLM not configured") -> SemanticResult:
    return SemanticResult(
        ok=False,
        mode="unavailable",  # type: ignore[arg-type]
        confidence="low",  # type: ignore[arg-type]
        data={},
        warnings=[reason],
    )


def _extract_json_object(text: str) -> str | None:
    """Extract first complete JSON object via bracket counting."""
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                return text[start : i + 1]
    return None


def _parse_llm_json(content: str) -> dict | None:
    """Parse LLM response as JSON with markdown fence + bracket fallback."""
    # Direct parse
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        pass

    # Markdown code fence
    m = re.search(r"```(?:json)?\s*\n(.*?)\n\s*```", content, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except (json.JSONDecodeError, TypeError):
            pass

    # Bracket counting
    extracted = _extract_json_object(content)
    if extracted:
        try:
            return json.loads(extracted)
        except (json.JSONDecodeError, TypeError):
            pass

    return None


def _validate_schema(data: dict, schema: dict) -> tuple[dict, list[str]]:
    """Lightweight schema validation. Returns (data, warnings).

    Only validates enum constraints on top-level fields and truncates
    string fields to 2000 chars. Sets defaults for missing required fields.
    """
    warnings: list[str] = []
    props = schema.get("properties", {})
    required = schema.get("required", [])

    # Add defaults for missing required fields
    for field in required:
        if field not in data:
            if field in props:
                if "enum" in props[field]:
                    data[field] = props[field]["enum"][
                        -1
                    ]  # default to last (usually low)
                    warnings.append(
                        f"missing required field '{field}', defaulted to '{data[field]}'"
                    )

    # Validate enum constraints
    for field, rules in props.items():
        if field in data and "enum" in rules:
            if data[field] not in rules["enum"]:
                old = data[field]
                data[field] = rules["enum"][-1]  # fallback to last enum value
                warnings.append(
                    f"invalid '{field}' value '{old}', defaulted to '{data[field]}'"
                )

    # Truncate long strings
    for field, value in data.items():
        if isinstance(value, str) and len(value) > 2000:
            data[field] = value[:2000]
            warnings.append(f"field '{field}' truncated to 2000 chars")

    return data, warnings


def _call_semantic_llm(prompt: str, schema: dict) -> SemanticResult:
    """Call LLM with prompt, parse JSON response, validate against schema."""
    if not _get_api_key():
        return _unavailable_result()

    try:
        resp = httpx.post(
            f"{_get_base_url()}/v1/chat/completions",
            headers={"Authorization": f"Bearer {_get_api_key()}"},
            json={
                "model": _get_model(),
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
            },
            timeout=30,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        log.warning(f"Semantic LLM call failed: {exc}")
        return _error_result(str(exc))

    parsed = _parse_llm_json(content)
    if parsed is None:
        return _error_result("LLM response not valid JSON")

    if schema:
        parsed, warnings = _validate_schema(parsed, schema)
    else:
        warnings = []

    confidence = parsed.get("confidence", "medium")
    if confidence not in ("high", "medium", "low"):
        confidence = "low"

    return SemanticResult(
        ok=True,
        mode="llm",
        confidence=confidence,  # type: ignore[arg-type]
        data=parsed,
        warnings=warnings,
    )


# ── Bug Context Extraction ──

BUG_CONTEXT_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "steps_to_reproduce": {"type": "string"},
        "expected_behavior": {"type": "string"},
        "actual_behavior": {"type": "string"},
        "environment": {"type": "string"},
        "logs": {"type": "string"},
        "missing_fields": {"type": "array"},
        "confidence": {"enum": ["high", "medium", "low"]},
    },
    "required": ["description", "confidence"],
}

# Regex fallback patterns (same logic as existing bug_providers.py)

_SECTION_PATTERNS = {
    "steps_to_reproduce": "复现步骤|步骤|重现",
    "expected_behavior": "预期|期望|期望结果",
    "actual_behavior": "实际|实际结果|现象",
    "environment": "环境|版本|设备",
    "logs": "日志|log|堆栈|stack",
}


def _regex_extract_section(md: str, pattern: str) -> str:
    """Extract a section from markdown using heading keyword matching."""
    m = re.search(
        rf"(?:{pattern})[：:\s]*\n(.*?)(?=\n##|\n#|\Z)",
        md,
        re.DOTALL | re.IGNORECASE,
    )
    return m.group(1).strip() if m else ""


def _regex_extract_bug_context(md: str, title: str) -> dict:
    """Fallback regex-based bug context extraction."""
    data = {"description": title, "missing_fields": [], "confidence": "low"}
    for field, pattern in _SECTION_PATTERNS.items():
        value = _regex_extract_section(md, pattern)
        data[field] = value
        if not value:
            data["missing_fields"].append(field)
    data["raw_markdown"] = md
    return data


def extract_bug_context(markdown: str, title: str = "") -> SemanticResult:
    """Extract structured bug context from markdown via LLM, with regex fallback."""
    prompt = f"""分析以下 Bug 报告，提取结构化信息。只输出 JSON，不要其他内容。

标题: {title}

正文:
{markdown[:3000]}

输出格式:
{{
  "description": "简短问题概述",
  "steps_to_reproduce": "复现步骤，保留编号或要点",
  "expected_behavior": "预期结果",
  "actual_behavior": "实际结果",
  "environment": "环境、版本、设备",
  "logs": "错误日志、堆栈、接口返回",
  "missing_fields": ["字段名"],
  "confidence": "high|medium|low"
}}

如果某个字段在正文中找不到，加入 missing_fields。confidence 根据信息完整度判断。"""

    llm_result = _call_semantic_llm(prompt, BUG_CONTEXT_SCHEMA)

    if llm_result["ok"] and llm_result["mode"] == "llm":
        # Ensure raw_markdown is preserved
        llm_result["data"]["raw_markdown"] = markdown
        return llm_result

    # Fallback to regex
    fallback_data = _regex_extract_bug_context(markdown, title)
    return _fallback_result(fallback_data)
