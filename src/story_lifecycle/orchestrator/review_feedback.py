"""Review Feedback Intake — LLM-assisted candidate finding extraction.

Flow:
  review markdown/json
    -> extract candidate findings (LLM or rule fallback)
    -> validate schema
    -> dedupe against same-story existing findings
    -> write to DB as status=open
    -> return summary
"""

from __future__ import annotations

import json
import logging
import os
import re
import httpx

from ..db import models as db

log = logging.getLogger("story-lifecycle.review_feedback")

# ── Constants ──

VALID_SEVERITIES = frozenset({"high", "medium", "low"})
VALID_CATEGORIES = frozenset(
    {
        "field-propagation",
        "error_handling",
        "input_validation",
        "sql_injection",
        "state_leak",
        "missing_log",
        "missing_test",
        "routing",
        "schema_change",
        "cross-service",
        "performance",
        "security",
        "configuration",
        "unknown",
    }
)
VALID_CONFIDENCE = frozenset({"high", "medium", "low"})
MAX_CANDIDATES = 20


# ── LLM extraction prompt ──

_EXTRACTION_PROMPT = """你是 Quality Flywheel 的 review 分析师。从以下 review 内容中提取结构化的 candidate findings。

## Review 内容

{review_content}

## 严格约束

1. 只提取真正的质量问题，不要提取正面评价或无关信息
2. 最多生成 {max_candidates} 条 findings
3. severity 必须是 high/medium/low
4. category 从以下列表选择：{categories}
5. 每条 finding 必须有 description
6. evidence 列出原始 review 中的对应位置或引用

## 输出格式

严格返回 JSON（不要 markdown 包裹）:

{{
  "candidate_findings": [
    {{
      "severity": "high",
      "category": "error_handling",
      "description": "问题描述",
      "location": "文件:行号",
      "recommendation": "修复建议",
      "root_cause": "根因分析",
      "evidence": ["原文证据1"],
      "confidence": "high"
    }}
  ],
  "summary": "1-2 句话概述 review 发现的质量要点"
}}"""


# ── Rule-based fallback parser ──


def _parse_json_review(content: str) -> list[dict]:
    """Try parsing content as JSON with findings."""
    try:
        data = json.loads(content)
        if isinstance(data, dict):
            for key in ("findings", "issues", "candidate_findings"):
                if key in data and isinstance(data[key], list):
                    return data[key]
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, TypeError):
        pass
    return []


# ── Schema validation ──


def validate_candidates(raw: list[dict]) -> tuple[list[dict], list[str]]:
    """Validate candidate findings. Returns (valid_list, warnings)."""
    warnings: list[str] = []
    valid: list[dict] = []

    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            warnings.append(f"candidate[{i}]: not a dict, skipped")
            continue

        if not item.get("description"):
            warnings.append(f"candidate[{i}]: missing description, skipped")
            continue

        severity = item.get("severity", "medium")
        if severity not in VALID_SEVERITIES:
            warnings.append(
                f"candidate[{i}]: invalid severity '{severity}', defaulted to 'medium'"
            )
            severity = "medium"
        item["severity"] = severity

        category = item.get("category", "unknown")
        if category not in VALID_CATEGORIES:
            warnings.append(
                f"candidate[{i}]: unknown category '{category}', set to 'unknown'"
            )
            category = "unknown"
        item["category"] = category

        confidence = item.get("confidence", "medium")
        if confidence not in VALID_CONFIDENCE:
            confidence = "medium"
        item["confidence"] = confidence

        item.setdefault("location", "")
        item.setdefault("recommendation", "")
        item.setdefault("root_cause", "")
        item.setdefault("evidence", [])

        valid.append(item)

    return valid[:MAX_CANDIDATES], warnings


# ── Dedupe / merge ──


def dedupe_candidates(
    candidates: list[dict],
    story_key: str | None = None,
) -> list[dict]:
    """Merge candidates with same category+location, and dedupe against DB.

    Merges findings about the same type of issue in the same file/location
    even if described differently — keeps highest severity and concatenates
    descriptions.
    """
    seen: dict[str, dict] = {}
    for c in candidates:
        loc = c.get("location", "")
        if loc:
            key = f"{c['category']}|{loc}"
        else:
            # description fingerprint to avoid merging unrelated items
            desc_key = c["description"][:80].lower().strip()
            key = f"{c['category']}|_desc_{desc_key}"
        if key in seen:
            existing = seen[key]
            sev_order = {"high": 3, "medium": 2, "low": 1}
            if sev_order.get(c["severity"], 0) > sev_order.get(existing["severity"], 0):
                existing["severity"] = c["severity"]
            if c["description"] not in existing["description"]:
                existing["description"] += f"; {c['description']}"
        else:
            seen[key] = dict(c)

    merged = list(seen.values())

    # Dedupe against existing DB findings for same story
    if story_key:
        try:
            existing = db.get_findings_by_story(story_key)
            existing_keys = {
                f"{f['category']}|{f.get('location', '')}" for f in existing
            }
            merged = [
                c
                for c in merged
                if f"{c['category']}|{c.get('location', '')}" not in existing_keys
            ]
        except Exception:
            pass

    return merged


# ── Main extraction function ──


def extract_candidate_findings(
    content: str,
    story_key: str,
) -> dict:
    """Extract candidate findings from review content.

    Returns: {"mode": "llm"|"error", "candidates": [...], "summary": str}
    """
    # Try JSON input first (no LLM needed)
    json_candidates = _parse_json_review(content)
    if json_candidates:
        validated, warnings = validate_candidates(json_candidates)
        return {
            "mode": "llm",
            "candidates": validated,
            "summary": f"Parsed {len(validated)} findings from JSON input",
            "warnings": warnings,
        }

    # LLM extraction
    try:
        candidates = _llm_extract(content)
        if candidates is not None:
            validated, warnings = validate_candidates(candidates)
            return {
                "mode": "llm",
                "candidates": validated,
                "summary": f"LLM extracted {len(validated)} candidate findings",
                "warnings": warnings,
            }
    except Exception as exc:
        log.warning(f"LLM extraction failed: {exc}")

    return {
        "mode": "error",
        "candidates": [],
        "summary": "LLM extraction failed: no findings extracted",
        "warnings": ["LLM extraction failed"],
    }


def _llm_extract(content: str) -> list[dict] | None:
    """Call LLM to extract candidate findings. Returns list or None on failure."""
    api_key = os.environ.get("STORY_LLM_API_KEY", "")
    base_url = os.environ.get("STORY_LLM_BASE_URL", "https://api.deepseek.com")
    model = os.environ.get("STORY_LLM_MODEL", "deepseek-v4-pro")

    prompt = _EXTRACTION_PROMPT.format(
        review_content=content[:6000],
        max_candidates=MAX_CANDIDATES,
        categories=", ".join(sorted(VALID_CATEGORIES)),
    )

    resp = httpx.post(
        f"{base_url}/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
        },
        timeout=60,
    )
    resp.raise_for_status()
    body = resp.json()
    raw_content = body["choices"][0]["message"].get("content", "")

    if not raw_content.strip():
        return None

    # Parse JSON from LLM response
    parsed = _parse_llm_json(raw_content)
    if parsed is None:
        return None

    return parsed.get("candidate_findings", [])


def _parse_llm_json(content: str) -> dict | None:
    """Parse LLM response as JSON, handling markdown fences."""
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
                    return json.loads(content[start : i + 1])
                except (json.JSONDecodeError, TypeError):
                    pass
    return None


# ── Import (write to DB) ──


def import_review(story_key: str, content: str) -> dict:
    """Full import flow: extract -> validate -> dedupe -> write to DB.

    Returns: {"imported": int, "skipped": int, "warnings": list[str]}
    """
    extraction = extract_candidate_findings(content, story_key)
    candidates = extraction["candidates"]

    # Dedupe against existing findings
    candidates = dedupe_candidates(candidates, story_key=story_key)

    imported = 0
    skipped = 0
    warnings = list(extraction.get("warnings", []))

    for c in candidates:
        try:
            fid = db.create_finding(
                story_key=story_key,
                stage="review",
                source="review_feedback",
                severity=c["severity"],
                category=c["category"],
                description=c["description"],
                location=c.get("location", ""),
                recommendation=c.get("recommendation", ""),
                root_cause=c.get("root_cause", ""),
                evidence=c.get("evidence", []),
            )
            # Log import event
            db.log_event(
                story_key,
                "review",
                "review_feedback_imported",
                {
                    "finding_id": fid,
                    "mode": extraction["mode"],
                    "confidence": c.get("confidence", "medium"),
                    "evidence": c.get("evidence", []),
                },
            )
            imported += 1
        except Exception as exc:
            skipped += 1
            warnings.append(f"Failed to write finding: {exc}")

    return {
        "imported": imported,
        "skipped": skipped,
        "warnings": warnings,
        "mode": extraction["mode"],
    }
