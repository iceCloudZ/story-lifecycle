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


# ── Pattern Recurrence Matching ──

PATTERN_RECURRENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "matches": {
            "type": "array",
        },
        "confidence": {"enum": ["high", "medium", "low"]},
    },
    "required": ["matches"],
}


def _keyword_match_pattern(issue_text: str, pattern_name: str, rule: str) -> bool:
    """Simple keyword matching — adapted from nodes._match_pattern for Chinese text.

    Unlike the original which requires >=2 keyword hits (designed for space-delimited
    languages), this version accepts >=1 match because Chinese patterns produce fewer
    space-split tokens.
    """
    keywords = (pattern_name + " " + rule).lower().split()
    matches = sum(1 for kw in keywords if len(kw) >= 2 and kw in issue_text)
    return matches >= 1


def match_pattern_recurrence(issue: dict, patterns: list[dict]) -> SemanticResult:
    """Check if a review issue matches any active learned patterns via LLM semantics."""
    if not patterns:
        return _fallback_result({"matches": []}, ["no candidate patterns"])

    issue_desc = issue.get("description", "")
    issue_cat = issue.get("category", "")
    issue_text = f"{issue_cat} {issue_desc}".lower()

    patterns_desc = "\n".join(
        f"- ID: {p['id']}, pattern: {p.get('pattern', '')}, rule: {p.get('rule', '')}"
        for p in patterns
    )

    prompt = f"""判断以下 review issue 是否复发了某个 learned pattern。

Issue:
- category: {issue_cat}
- description: {issue_desc}

Candidate Patterns:
{patterns_desc}

对每个 pattern，判断是否语义匹配（不需要完全相同的措辞，语义相同即可）。
只输出 JSON:
{{
  "matches": [
    {{
      "pattern_id": "pattern id",
      "matched": true/false,
      "confidence": "high|medium|low",
      "reasoning": "为什么匹配或不匹配",
      "evidence": ["具体证据"]
    }}
  ]
}}

只输出 confidence 为 high 或 medium 的匹配。low confidence 的标记 matched=false。"""

    llm_result = _call_semantic_llm(prompt, PATTERN_RECURRENCE_SCHEMA)

    if llm_result["ok"] and llm_result["mode"] == "llm":
        # Filter: only keep high/medium confidence matches
        filtered = []
        for m in llm_result["data"].get("matches", []):
            if m.get("matched") and m.get("confidence") in ("high", "medium"):
                filtered.append(m)
        llm_result["data"]["matches"] = filtered
        return llm_result

    # Fallback to keyword matching
    fallback_matches = []
    for p in patterns:
        if _keyword_match_pattern(issue_text, p.get("pattern", ""), p.get("rule", "")):
            fallback_matches.append(
                {
                    "pattern_id": p["id"],
                    "matched": True,
                    "confidence": "low",
                    "reasoning": "keyword fallback match",
                    "evidence": [],
                }
            )

    return _fallback_result({"matches": fallback_matches})


# ── Quality Packet Pattern Rerank ──


def rerank_relevant_patterns(
    story_context: dict,
    candidate_patterns: list[dict],
    limit: int = 5,
) -> SemanticResult:
    """Use LLM to rerank candidate patterns by actual relevance to the story."""
    if not candidate_patterns:
        return _fallback_result({"selected": [], "rejected": []})

    candidates_desc = "\n".join(
        f"- ID: {p['id']}, pattern: {p.get('pattern', '')}, rule: {p.get('rule', '')}, "
        f"applies_to: {p.get('applies_to', [])}, confidence: {p.get('confidence', '')}"
        for p in candidate_patterns
    )

    story_title = story_context.get("title", "")
    story_stage = story_context.get("stage", "")
    story_summary = story_context.get("summary", "")[:500]

    prompt = f"""从以下 candidate learned patterns 中选择与当前 story 真正相关的 pattern。

Story: {story_title}
Stage: {story_stage}
Summary: {story_summary}

Candidates:
{candidates_desc}

只输出 JSON:
{{
  "selected": [
    {{
      "pattern_id": "id",
      "relevance": "high|medium",
      "reasoning": "为什么相关"
    }}
  ],
  "rejected": [
    {{
      "pattern_id": "id",
      "reasoning": "为什么不相关"
    }}
  ]
}}

只选择真正能帮助当前 story 避免重蹈覆辙的 pattern。最多选 {limit} 个。"""

    schema = {
        "type": "object",
        "properties": {
            "selected": {"type": "array"},
            "rejected": {"type": "array"},
        },
        "required": ["selected"],
    }

    llm_result = _call_semantic_llm(prompt, schema)

    if llm_result["ok"] and llm_result["mode"] == "llm":
        llm_result["data"]["selected"] = llm_result["data"].get("selected", [])[:limit]
        return llm_result

    # Fallback: return candidates as-is, truncated to limit
    selected = [
        {
            "pattern_id": p["id"],
            "relevance": "medium",
            "reasoning": "tag pre-filter fallback",
        }
        for p in candidate_patterns[:limit]
    ]
    return _fallback_result({"selected": selected, "rejected": []})


# ── Review Summary for Learning ──

REVIEW_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "quality": {"enum": ["pass", "revise", "fail", "unknown"]},
        "key_issues": {"type": "array"},
        "useful_for_learning": {"type": "boolean"},
        "summary": {"type": "string"},
        "confidence": {"enum": ["high", "medium", "low"]},
    },
    "required": ["quality", "summary"],
}


def _marker_summarize_review(content: str) -> dict:
    """Fallback marker-based review summary (same logic as existing seed_pipeline)."""
    markers = ["quality", "issues", "suggestions", "评分", "review", "问题"]
    found = []
    for line in content.splitlines():
        for m in markers:
            if m.lower() in line.lower():
                found.append(line.strip()[:300])
    return {
        "quality": "unknown",
        "key_issues": [],
        "useful_for_learning": bool(found),
        "summary": "\n".join(found) if found else content[:1500],
    }


def summarize_review_for_learning(review_markdown: str) -> SemanticResult:
    """Summarize a review for seed pipeline learning via LLM."""
    # If review is structured JSON, parse directly
    try:
        data = json.loads(review_markdown)
        if isinstance(data, dict) and "quality" in data:
            return _ok_result(
                {
                    "quality": data.get("quality", "unknown"),
                    "key_issues": data.get("issues", data.get("key_issues", [])),
                    "useful_for_learning": True,
                    "summary": json.dumps(data, ensure_ascii=False)[:1000],
                }
            )
    except (json.JSONDecodeError, TypeError):
        pass

    prompt = f"""分析以下 code review 结果，提取对质量学习有价值的信息。只输出 JSON。

Review 内容:
{review_markdown[:3000]}

输出格式:
{{
  "quality": "pass|revise|fail|unknown",
  "key_issues": [
    {{
      "severity": "high|medium|low",
      "description": "问题描述",
      "evidence": "原文证据或文件位置",
      "recommendation": "建议"
    }}
  ],
  "useful_for_learning": true/false,
  "summary": "适合喂给后续分析的压缩摘要",
  "confidence": "high|medium|low"
}}"""

    llm_result = _call_semantic_llm(prompt, REVIEW_SUMMARY_SCHEMA)

    if llm_result["ok"] and llm_result["mode"] == "llm":
        return llm_result

    return _fallback_result(_marker_summarize_review(review_markdown))


# ── Debug Recovery Recommendation ──

RECOVERY_SCHEMA = {
    "type": "object",
    "properties": {
        "failure_type": {
            "enum": [
                "done_file_parse_error",
                "missing_expected_outputs",
                "dor_blocked",
                "dod_blocked",
                "tool_crash",
                "review_retry_exhausted",
                "unknown",
            ]
        },
        "likely_cause": {"type": "string"},
        "recommended_action": {
            "enum": [
                "retry",
                "retry_with_prompt",
                "fix_input",
                "ask_human",
                "defer",
                "fail",
            ]
        },
        "safe_to_retry": {"type": "boolean"},
        "confidence": {"enum": ["high", "medium", "low"]},
        "evidence": {"type": "array"},
        "human_message": {"type": "string"},
    },
    "required": ["failure_type", "recommended_action", "safe_to_retry"],
}


def recommend_recovery(debug_packet: dict) -> SemanticResult:
    """Recommend recovery action for a failed story based on debug data."""
    fallback_data = {
        "failure_type": "unknown",
        "recommended_action": "ask_human",
        "safe_to_retry": False,
        "confidence": "low",
        "evidence": [],
        "human_message": "LLM 不可用，建议人工检查",
    }

    if not _get_api_key():
        return SemanticResult(
            ok=False,
            mode="unavailable",  # type: ignore[arg-type]
            confidence="low",  # type: ignore[arg-type]
            data=fallback_data,
            warnings=["LLM not configured"],
        )

    packet_str = json.dumps(debug_packet, ensure_ascii=False, default=str)[:3000]

    prompt = f"""分析以下 story debug 数据，判断失败类型并推荐恢复动作。只输出 JSON。

Debug 数据:
{packet_str}

输出格式:
{{
  "failure_type": "done_file_parse_error|missing_expected_outputs|dor_blocked|dod_blocked|tool_crash|review_retry_exhausted|unknown",
  "likely_cause": "一句话说明",
  "recommended_action": "retry|retry_with_prompt|fix_input|ask_human|defer|fail",
  "safe_to_retry": true/false,
  "confidence": "high|medium|low",
  "evidence": ["证据1", "证据2"],
  "human_message": "给操作者看的中文说明"
}}

规则:
- failure_type=unknown 时，recommended_action 必须是 ask_human
- safe_to_retry=false 时，不能建议 retry 或 retry_with_prompt
- 只基于输入数据推断，不要编造证据"""

    llm_result = _call_semantic_llm(prompt, RECOVERY_SCHEMA)

    if llm_result["ok"] and llm_result["mode"] == "llm":
        # Enforce safety rules
        data = llm_result["data"]
        if data.get("failure_type") == "unknown":
            data["recommended_action"] = "ask_human"
            data["safe_to_retry"] = False
        if not data.get("safe_to_retry", True):
            if data.get("recommended_action") in ("retry", "retry_with_prompt"):
                data["recommended_action"] = "ask_human"
        llm_result["data"] = data
        return llm_result

    fallback_data["human_message"] = "LLM 调用失败，建议人工检查"
    return SemanticResult(
        ok=False,
        mode="error",  # type: ignore[arg-type]
        confidence="low",  # type: ignore[arg-type]
        data=fallback_data,
        warnings=["LLM call failed"],
    )
