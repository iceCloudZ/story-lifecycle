"""LLM Semantic Extraction Layer.

Provides unified structured-output calls for semantic tasks.
Each function returns SemanticResult with mode/confidence/fallback.
Now uses LLMClient + Pydantic models instead of raw httpx + manual JSON parsing.
"""

from __future__ import annotations

import json
import logging
from typing import Literal, TypedDict

from ...infra.llm_client import get_llm
from ...infra.schemas import (
    BugContextResult,
    PatternRecurrenceResult,
    RerankResult,
    ReviewSummaryResult,
    RecoveryRecommendation,
)

log = logging.getLogger("story-lifecycle.semantic")


class SemanticResult(TypedDict):
    ok: bool
    mode: Literal["llm", "error"]
    confidence: Literal["high", "medium", "low"]
    data: dict
    warnings: list[str]


def _ok_result(data: dict, confidence: str = "high") -> SemanticResult:
    return SemanticResult(
        ok=True,
        mode="llm",
        confidence=confidence,  # type: ignore[arg-type]
        data=data,
        warnings=[],
    )


def _error_result(error: str) -> SemanticResult:
    return SemanticResult(
        ok=False,
        mode="error",  # type: ignore[arg-type]
        confidence="low",  # type: ignore[arg-type]
        data={},
        warnings=[error],
    )


def _invoke_structured(
    prompt: str, schema, *, temperature: float = 0.1
) -> SemanticResult:
    """Call LLM with structured output, return SemanticResult."""
    try:
        result = get_llm().invoke_structured(
            prompt, schema, temperature=temperature, timeout=30
        )
        data = result.model_dump()
        confidence = data.pop("confidence", "medium")
        if confidence not in ("high", "medium", "low"):
            confidence = "low"
        return SemanticResult(
            ok=True,
            mode="llm",
            confidence=confidence,  # type: ignore[arg-type]
            data=data,
            warnings=[],
        )
    except Exception as exc:
        log.warning(f"Semantic LLM call failed: {exc}")
        return _error_result(str(exc))


# ── Bug Context Extraction ──


def extract_bug_context(markdown: str, title: str = "") -> SemanticResult:
    """Extract structured bug context from markdown via LLM."""
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

    result = _invoke_structured(prompt, BugContextResult)
    if result["ok"]:
        result["data"]["raw_markdown"] = markdown
    return result


# ── Pattern Recurrence Matching ──


def match_pattern_recurrence(issue: dict, patterns: list[dict]) -> SemanticResult:
    """Check if a review issue matches any active learned patterns via LLM semantics."""
    if not patterns:
        return _error_result("no candidate patterns")

    issue_desc = issue.get("description", "")
    issue_cat = issue.get("category", "")

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

    result = _invoke_structured(prompt, PatternRecurrenceResult)
    if result["ok"] and result["mode"] == "llm":
        filtered = [
            m
            for m in result["data"].get("matches", [])
            if m.get("matched") and m.get("confidence") in ("high", "medium")
        ]
        result["data"]["matches"] = filtered
    return result


# ── Quality Packet Pattern Rerank ──


def rerank_relevant_patterns(
    story_context: dict,
    candidate_patterns: list[dict],
    limit: int = 5,
) -> SemanticResult:
    """Use LLM to rerank candidate patterns by actual relevance to the story."""
    if not candidate_patterns:
        return _error_result("no candidate patterns")

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

    result = _invoke_structured(prompt, RerankResult)
    if result["ok"]:
        result["data"]["selected"] = result["data"].get("selected", [])[:limit]
    return result


# ── Review Summary for Learning ──


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

    return _invoke_structured(prompt, ReviewSummaryResult)


# ── Debug Recovery Recommendation ──


FALLBACK_RECOVERY = {
    "failure_type": "unknown",
    "recommended_action": "ask_human",
    "safe_to_retry": False,
    "confidence": "low",
    "evidence": [],
    "human_message": "LLM 不可用，建议人工检查",
}


def recommend_recovery(debug_packet: dict) -> SemanticResult:
    """Recommend recovery action for a failed story based on debug data."""
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

    result = _invoke_structured(prompt, RecoveryRecommendation)
    if result["ok"] and result["mode"] == "llm":
        # Enforce safety rules
        data = result["data"]
        if data.get("failure_type") == "unknown":
            data["recommended_action"] = "ask_human"
            data["safe_to_retry"] = False
        if not data.get("safe_to_retry", True):
            if data.get("recommended_action") in ("retry", "retry_with_prompt"):
                data["recommended_action"] = "ask_human"
        result["data"] = data
        return result

    fallback = dict(FALLBACK_RECOVERY)
    fallback["human_message"] = "LLM 调用失败，建议人工检查"
    return SemanticResult(
        ok=False,
        mode="error",  # type: ignore[arg-type]
        confidence="low",  # type: ignore[arg-type]
        data=fallback,
        warnings=["LLM call failed"],
    )
