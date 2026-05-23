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
