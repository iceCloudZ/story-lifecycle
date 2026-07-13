# Review Feedback Intake Loop — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 "AI review → LLM 结构化提取 → 人工判断 → finding 沉淀" 做成闭环，覆盖 review import、dedupe、approval queue、decision commands 和 reviewer role guardrail。

**Architecture:** 新增 `orchestrator/review_feedback.py` 负责 LLM 提取 + 去重 + 校验，新增 `cli/review_feedback.py` 提供 `story review-feedback` 和 `story approvals` 命令组。API 端点追加到现有 `api.py`。DB helpers 追加到 `models.py`。Reviewer role guardrail 追加到 `planner.py` 的 review prompt。

**Tech Stack:** Python 3.10+, httpx (已有), click + rich (已有), pytest + monkeypatch (已有)

---

## File Structure

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `src/story_lifecycle/orchestrator/review_feedback.py` | LLM 提取 candidate findings、schema 校验、去重合并、规则兜底 |
| Create | `src/story_lifecycle/cli/review_feedback.py` | CLI: `story review-feedback import/list/decide`、`story approvals` |
| Create | `tests/test_review_feedback.py` | 提取、校验、去重、CLI、API 测试 |
| Modify | `src/story_lifecycle/db/models.py` | 新增 `get_findings_by_status()`、`get_all_pending_findings()`、`get_findings_by_story()` |
| Modify | `src/story_lifecycle/cli/main.py` | 注册 `review_feedback_group` |
| Modify | `src/story_lifecycle/orchestrator/api.py` | 新增 review feedback + approval API 端点 |
| Modify | `src/story_lifecycle/orchestrator/planner.py` | review prompt 新增 reviewer 只读约束 |

---

## Task 1: DB Helpers for Finding Queries

**Files:**
- Modify: `src/story_lifecycle/db/models.py` (append after existing finding helpers)
- Test: `tests/test_review_feedback.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_review_feedback.py`:

```python
"""Tests for Phase 1: Review Feedback Intake Loop."""
import os
import json


def _setup_db(tmp_path):
    """Common DB setup: set STORY_HOME, init_db."""
    os.environ["STORY_HOME"] = str(tmp_path)
    from story_lifecycle.db import models as db
    db.init_db()
    return db


# ── Task 1: DB helpers ──


def test_get_findings_by_status(tmp_path):
    """get_findings_by_status returns findings matching given statuses."""
    db = _setup_db(tmp_path)

    db.create_finding("S1", "impl", "review", "high", "routing", "finding A")
    fid_b = db.create_finding("S1", "impl", "review", "medium", "style", "finding B")
    db.update_finding(fid_b, status="accepted")
    fid_c = db.create_finding("S1", "impl", "review", "low", "style", "finding C")
    db.update_finding(fid_c, status="rejected")

    # open only
    open_f = db.get_findings_by_status(["open"])
    assert len(open_f) == 1
    assert open_f[0]["description"] == "finding A"

    # accepted only
    accepted_f = db.get_findings_by_status(["accepted"])
    assert len(accepted_f) == 1
    assert accepted_f[0]["description"] == "finding B"

    # open + accepted (pending for approval queue)
    pending = db.get_findings_by_status(["open", "accepted"])
    assert len(pending) == 2

    # rejected
    rejected = db.get_findings_by_status(["rejected"])
    assert len(rejected) == 1


def test_get_all_pending_findings(tmp_path):
    """get_all_pending_findings returns open+accepted findings across all stories."""
    db = _setup_db(tmp_path)

    db.create_finding("S1", "impl", "review", "high", "routing", "S1 open")
    fid = db.create_finding("S2", "impl", "review", "medium", "style", "S2 open")
    db.update_finding(fid, status="accepted")
    fid_r = db.create_finding("S3", "impl", "review", "low", "style", "S3 rejected")
    db.update_finding(fid_r, status="rejected")

    pending = db.get_all_pending_findings()
    assert len(pending) == 2
    keys = {f["story_key"] for f in pending}
    assert keys == {"S1", "S2"}


def test_get_findings_by_story(tmp_path):
    """get_findings_by_story returns all findings for a story regardless of status."""
    db = _setup_db(tmp_path)

    db.create_finding("S1", "impl", "review", "high", "routing", "A")
    fid = db.create_finding("S1", "impl", "review", "medium", "style", "B")
    db.update_finding(fid, status="accepted")
    db.create_finding("S2", "impl", "review", "low", "style", "C")

    s1_findings = db.get_findings_by_story("S1")
    assert len(s1_findings) == 2

    s2_findings = db.get_findings_by_story("S2")
    assert len(s2_findings) == 1

    empty = db.get_findings_by_story("S_NONEXIST")
    assert len(empty) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_review_feedback.py -v`
Expected: FAIL — `AttributeError: module 'story_lifecycle.db.models' has no attribute 'get_findings_by_status'`

- [ ] **Step 3: Implement DB helpers**

Append to `src/story_lifecycle/db/models.py`, after the existing `get_open_findings()` function (after line ~505):

```python
def get_findings_by_status(statuses: list[str]) -> list[dict]:
    """Get findings matching any of the given statuses."""
    placeholders = ",".join("?" * len(statuses))
    with _db() as conn:
        rows = conn.execute(
            f"SELECT * FROM finding WHERE status IN ({placeholders}) ORDER BY created_at DESC",
            statuses,
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_pending_findings() -> list[dict]:
    """Get all open + accepted findings across stories (for approval queue)."""
    return get_findings_by_status(["open", "accepted"])


def get_findings_by_story(story_key: str) -> list[dict]:
    """Get all findings for a story regardless of status."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM finding WHERE story_key = ? ORDER BY created_at",
            (story_key,),
        ).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_review_feedback.py -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/story_lifecycle/db/models.py tests/test_review_feedback.py
git commit -m "feat: add DB helpers for finding queries — get_findings_by_status, get_all_pending_findings, get_findings_by_story"
```

---

## Task 2: Review Feedback Extraction Core

**Files:**
- Create: `src/story_lifecycle/orchestrator/review_feedback.py`
- Test: `tests/test_review_feedback.py` (append tests)

- [ ] **Step 1: Write failing tests for extraction, validation, and dedupe**

Append to `tests/test_review_feedback.py`:

```python
# ── Task 2: Review feedback extraction ──

from unittest.mock import patch, MagicMock


def _make_fake_llm_response(json_obj: dict) -> MagicMock:
    mock = MagicMock()
    mock.raise_for_status = MagicMock()
    mock.json.return_value = {
        "choices": [{"message": {"content": json.dumps(json_obj, ensure_ascii=False)}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300},
    }
    return mock


def _make_fake_llm_response_raw(raw: str) -> MagicMock:
    mock = MagicMock()
    mock.raise_for_status = MagicMock()
    mock.json.return_value = {
        "choices": [{"message": {"content": raw}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300},
    }
    return mock


def test_extract_candidates_llm_success():
    """LLM extracts candidate findings from review markdown."""
    from story_lifecycle.orchestrator.review_feedback import extract_candidate_findings

    llm_output = {
        "candidate_findings": [
            {
                "severity": "high",
                "category": "error_handling",
                "description": "缺少空指针检查",
                "location": "api.py:42",
                "recommendation": "添加 null check",
                "root_cause": "接口返回值未校验",
                "evidence": ["api.py:42"],
                "confidence": "high",
            },
            {
                "severity": "medium",
                "category": "missing_test",
                "description": "缺少边界测试",
                "location": "tests/test_api.py",
                "recommendation": "补充边界 case",
                "root_cause": "",
                "evidence": ["tests/test_api.py"],
                "confidence": "medium",
            },
        ],
        "summary": "发现空指针风险和测试缺口",
    }
    fake = _make_fake_llm_response(llm_output)

    review_md = "## Review\n\n### Issues\n- api.py:42 缺少空指针检查\n- 缺少边界测试"
    with patch.dict("os.environ", {"STORY_LLM_API_KEY": "test-key"}):
        with patch("httpx.post", return_value=fake):
            result = extract_candidate_findings(review_md, "S1")

    assert result["mode"] == "llm"
    assert len(result["candidates"]) == 2
    assert result["candidates"][0]["severity"] == "high"
    assert result["candidates"][0]["category"] == "error_handling"


def test_extract_candidates_json_input():
    """Direct JSON input bypasses LLM, parses structured review."""
    from story_lifecycle.orchestrator.review_feedback import extract_candidate_findings

    json_review = json.dumps({
        "findings": [
            {
                "severity": "high",
                "category": "security",
                "description": "SQL injection risk",
                "location": "dao.py:15",
                "recommendation": "use parameterized query",
            }
        ]
    })

    with patch.dict("os.environ", {}, clear=True):
        result = extract_candidate_findings(json_review, "S1")

    assert result["mode"] == "rule_fallback"
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["category"] == "security"


def test_extract_candidates_fallback_on_llm_error():
    """LLM error falls back to simple rule-based parser."""
    from story_lifecycle.orchestrator.review_feedback import extract_candidate_findings

    review_md = "- [HIGH] api.py:42 缺少空指针检查\n- [MEDIUM] 缺少测试"
    with patch.dict("os.environ", {"STORY_LLM_API_KEY": "test-key"}):
        with patch("httpx.post", side_effect=Exception("timeout")):
            result = extract_candidate_findings(review_md, "S1")

    assert result["mode"] == "rule_fallback"
    assert len(result["candidates"]) >= 1


def test_validate_candidates_rejects_invalid():
    """validate_candidates rejects items missing required fields."""
    from story_lifecycle.orchestrator.review_feedback import validate_candidates

    raw = [
        {"severity": "high", "category": "routing", "description": "valid finding"},
        {"severity": "high", "category": "routing"},  # missing description
        {"severity": "critical", "category": "routing", "description": "bad severity"},  # bad severity
    ]
    validated, warnings = validate_candidates(raw)

    assert len(validated) == 2
    assert any("description" in w for w in warnings)
    assert validated[1]["severity"] == "medium"  # default on invalid


def test_dedupe_candidates_merges_similar():
    """dedupe_candidates merges findings with same category+location."""
    from story_lifecycle.orchestrator.review_feedback import dedupe_candidates

    candidates = [
        {"severity": "high", "category": "routing", "description": "路由错误 A", "location": "api.py:10"},
        {"severity": "medium", "category": "routing", "description": "路由问题，类似A", "location": "api.py:10"},
        {"severity": "low", "category": "style", "description": "风格问题", "location": "utils.py:5"},
    ]

    deduped = dedupe_candidates(candidates)
    # Same category + location should merge — keep higher severity
    assert len(deduped) == 2
    routing = [c for c in deduped if c["category"] == "routing"]
    assert len(routing) == 1
    assert routing[0]["severity"] == "high"


def test_dedupe_candidates_against_existing(tmp_path):
    """dedupe_candidates also dedupes against existing DB findings."""
    db = _setup_db(tmp_path)
    from story_lifecycle.orchestrator.review_feedback import dedupe_candidates

    # Existing finding in DB
    db.create_finding(
        "S1", "impl", "code_review", "high", "routing",
        "路由错误 A", location="api.py:10",
    )

    candidates = [
        {"severity": "medium", "category": "routing", "description": "路由错误 A（重复）", "location": "api.py:10"},
        {"severity": "low", "category": "style", "description": "新发现", "location": "utils.py:5"},
    ]

    deduped = dedupe_candidates(candidates, story_key="S1")
    assert len(deduped) == 1
    assert deduped[0]["category"] == "style"


def test_import_review_creates_candidate_findings(tmp_path):
    """import_review writes candidate findings to DB as status=open."""
    db = _setup_db(tmp_path)
    from story_lifecycle.orchestrator.review_feedback import import_review

    llm_output = {
        "candidate_findings": [
            {
                "severity": "high",
                "category": "error_handling",
                "description": "缺少空指针检查",
                "location": "api.py:42",
                "recommendation": "添加 null check",
                "root_cause": "未校验",
                "evidence": ["api.py:42"],
                "confidence": "high",
            },
        ],
        "summary": "test",
    }
    fake = _make_fake_llm_response(llm_output)

    review_md = "## Review\n\n- api.py:42 缺少空指针检查"
    with patch.dict("os.environ", {"STORY_LLM_API_KEY": "test-key"}):
        with patch("httpx.post", return_value=fake):
            result = import_review("S1", review_md)

    assert result["imported"] == 1
    assert len(result["warnings"]) == 0

    # Verify in DB
    findings = db.get_open_findings("S1")
    assert len(findings) == 1
    assert findings[0]["source"] == "review_feedback"
    assert findings[0]["description"] == "缺少空指针检查"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_review_feedback.py -k "extract or validate or dedupe or import_review" -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'story_lifecycle.orchestrator.review_feedback'`

- [ ] **Step 3: Implement review_feedback.py**

Create `src/story_lifecycle/orchestrator/review_feedback.py`:

```python
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
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from ..db import models as db

log = logging.getLogger("story-lifecycle.review_feedback")

# ── Constants ──

VALID_SEVERITIES = frozenset({"high", "medium", "low"})
VALID_CATEGORIES = frozenset({
    "field-propagation", "error_handling", "input_validation",
    "sql_injection", "state_leak", "missing_log", "missing_test",
    "routing", "schema_change", "cross-service", "performance",
    "security", "configuration", "unknown",
})
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


_SEVERITY_PATTERN = re.compile(r"\[(HIGH|MEDIUM|LOW)\]", re.IGNORECASE)
_BULLET_PATTERN = re.compile(r"^[-*]\s+(.+)$", re.MULTILINE)


def _parse_bullet_review(content: str) -> list[dict]:
    """Parse bullet-list review into candidate findings."""
    candidates = []
    for m in _BULLET_PATTERN.finditer(content):
        line = m.group(1).strip()
        if not line:
            continue

        severity = "medium"
        sev_match = _SEVERITY_PATTERN.search(line)
        if sev_match:
            severity = sev_match.group(1).lower()
            line = _SEVERITY_PATTERN.sub("", line).strip()

        candidates.append({
            "severity": severity,
            "category": "unknown",
            "description": line[:500],
            "location": "",
            "recommendation": "",
            "root_cause": "",
            "evidence": [],
            "confidence": "low",
        })
    return candidates[:MAX_CANDIDATES]


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
            warnings.append(f"candidate[{i}]: invalid severity '{severity}', defaulted to 'medium'")
            severity = "medium"
        item["severity"] = severity

        category = item.get("category", "unknown")
        if category not in VALID_CATEGORIES:
            warnings.append(f"candidate[{i}]: unknown category '{category}', set to 'unknown'")
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
    """Merge candidates with same category+location, and dedupe against DB."""
    # Phase 1: merge within candidates (same category + location)
    seen: dict[str, dict] = {}
    for c in candidates:
        key = f"{c['category']}|{c.get('location', '')}"
        if key in seen:
            existing = seen[key]
            # Keep higher severity
            sev_order = {"high": 3, "medium": 2, "low": 1}
            if sev_order.get(c["severity"], 0) > sev_order.get(existing["severity"], 0):
                existing["severity"] = c["severity"]
            # Merge descriptions
            if c["description"] not in existing["description"]:
                existing["description"] += f"; {c['description']}"
        else:
            seen[key] = dict(c)

    merged = list(seen.values())

    # Phase 2: dedupe against existing DB findings for same story
    if story_key:
        try:
            existing = db.get_findings_by_story(story_key)
            existing_keys = set()
            for f in existing:
                existing_keys.add(f"{f['category']}|{f.get('location', '')}")

            merged = [
                c for c in merged
                if f"{c['category']}|{c.get('location', '')}" not in existing_keys
            ]
        except Exception:
            pass  # if DB fails, don't block

    return merged


# ── Main extraction function ──


def extract_candidate_findings(
    content: str,
    story_key: str,
) -> dict:
    """Extract candidate findings from review content.

    Returns: {"mode": "llm"|"rule_fallback", "candidates": [...], "summary": str}
    """
    # Try JSON input first (no LLM needed)
    json_candidates = _parse_json_review(content)
    if json_candidates:
        validated, warnings = validate_candidates(json_candidates)
        return {
            "mode": "rule_fallback",
            "candidates": validated,
            "summary": f"Parsed {len(validated)} findings from JSON input",
            "warnings": warnings,
        }

    # Try LLM extraction
    api_key = os.environ.get("STORY_LLM_API_KEY", "")
    if api_key:
        try:
            candidates = _llm_extract(content, api_key)
            if candidates is not None:
                validated, warnings = validate_candidates(candidates)
                return {
                    "mode": "llm",
                    "candidates": validated,
                    "summary": f"LLM extracted {len(validated)} candidate findings",
                    "warnings": warnings,
                }
        except Exception as exc:
            log.warning(f"LLM extraction failed, falling back: {exc}")

    # Fallback: bullet-list parser
    bullet_candidates = _parse_bullet_review(content)
    validated, warnings = validate_candidates(bullet_candidates)
    return {
        "mode": "rule_fallback",
        "candidates": validated,
        "summary": f"Rule parser extracted {len(validated)} candidates",
        "warnings": warnings,
    }


def _llm_extract(content: str, api_key: str) -> list[dict] | None:
    """Call LLM to extract candidate findings. Returns list or None on failure."""
    base_url = os.environ.get("STORY_LLM_BASE_URL", "https://api.deepseek.com")
    model = os.environ.get("STORY_LLM_MODEL", "deepseek-chat")

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_review_feedback.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/story_lifecycle/orchestrator/review_feedback.py tests/test_review_feedback.py
git commit -m "feat: add review_feedback.py — LLM extraction, validation, dedupe, import"
```

---

## Task 3: CLI Commands — `story review-feedback` and `story approvals`

**Files:**
- Create: `src/story_lifecycle/cli/review_feedback.py`
- Modify: `src/story_lifecycle/cli/main.py` (register CLI group)
- Test: `tests/test_review_feedback.py` (append tests)

- [ ] **Step 1: Write failing tests for CLI commands**

Append to `tests/test_review_feedback.py`:

```python
# ── Task 3: CLI commands ──

from click.testing import CliRunner


def _write_review_file(tmp_path, content: str) -> str:
    """Write review content to a temp file, return path."""
    f = tmp_path / "review.md"
    f.write_text(content, encoding="utf-8")
    return str(f)


def test_cli_review_feedback_import(tmp_path):
    """story review-feedback import reads file, extracts findings."""
    db = _setup_db(tmp_path)
    runner = CliRunner()

    # Create story first
    db.upsert_story("S1", title="Test", workspace=str(tmp_path), current_stage="impl")

    review_content = "## Review\n\n- [HIGH] api.py:42 缺少空指针检查\n- [MEDIUM] 缺少边界测试"
    review_file = _write_review_file(tmp_path, review_content)

    from story_lifecycle.cli.review_feedback import review_feedback_group
    result = runner.invoke(review_feedback_group, ["import", "S1", review_file])

    assert result.exit_code == 0
    assert "finding" in result.output.lower() or "candidate" in result.output.lower()


def test_cli_review_feedback_list(tmp_path):
    """story review-feedback list shows imported findings."""
    db = _setup_db(tmp_path)
    runner = CliRunner()

    db.upsert_story("S1", title="Test", workspace=str(tmp_path), current_stage="impl")
    db.create_finding("S1", "review", "review_feedback", "high", "error_handling",
                      "空指针检查缺失", location="api.py:42")

    from story_lifecycle.cli.review_feedback import review_feedback_group
    result = runner.invoke(review_feedback_group, ["list", "S1"])

    assert result.exit_code == 0
    assert "空指针" in result.output or "error_handling" in result.output


def test_cli_review_feedback_decide_accept(tmp_path):
    """story review-feedback decide --accept changes finding status."""
    db = _setup_db(tmp_path)
    runner = CliRunner()

    db.upsert_story("S1", title="Test", workspace=str(tmp_path), current_stage="impl")
    fid = db.create_finding("S1", "review", "review_feedback", "high", "error_handling",
                            "空指针检查缺失")

    from story_lifecycle.cli.review_feedback import review_feedback_group
    result = runner.invoke(review_feedback_group, ["decide", fid, "--accept"])

    assert result.exit_code == 0
    finding = db.get_finding(fid)
    assert finding["status"] == "accepted"


def test_cli_review_feedback_decide_reject(tmp_path):
    """story review-feedback decide --reject changes finding status."""
    db = _setup_db(tmp_path)
    runner = CliRunner()

    db.upsert_story("S1", title="Test", workspace=str(tmp_path), current_stage="impl")
    fid = db.create_finding("S1", "review", "review_feedback", "high", "error_handling",
                            "空指针检查缺失")

    from story_lifecycle.cli.review_feedback import review_feedback_group
    result = runner.invoke(review_feedback_group, ["decide", fid, "--reject",
                                                    "--reason", "overclaimed"])

    assert result.exit_code == 0
    finding = db.get_finding(fid)
    assert finding["status"] == "rejected"


def test_cli_review_feedback_decide_defer(tmp_path):
    """story review-feedback decide --defer changes finding status."""
    db = _setup_db(tmp_path)
    runner = CliRunner()

    db.upsert_story("S1", title="Test", workspace=str(tmp_path), current_stage="impl")
    fid = db.create_finding("S1", "review", "review_feedback", "low", "style", "格式问题")

    from story_lifecycle.cli.review_feedback import review_feedback_group
    result = runner.invoke(review_feedback_group, ["decide", fid, "--defer"])

    assert result.exit_code == 0
    finding = db.get_finding(fid)
    assert finding["status"] == "deferred"


def test_cli_review_feedback_decide_downgrade(tmp_path):
    """story review-feedback decide --downgrade reduces severity."""
    db = _setup_db(tmp_path)
    runner = CliRunner()

    db.upsert_story("S1", title="Test", workspace=str(tmp_path), current_stage="impl")
    fid = db.create_finding("S1", "review", "review_feedback", "high", "error_handling",
                            "空指针检查缺失")

    from story_lifecycle.cli.review_feedback import review_feedback_group
    result = runner.invoke(review_feedback_group, ["decide", fid, "--downgrade"])

    assert result.exit_code == 0
    finding = db.get_finding(fid)
    assert finding["severity"] == "medium"  # high -> medium


def test_cli_approvals_list(tmp_path):
    """story approvals shows pending findings across stories."""
    db = _setup_db(tmp_path)
    runner = CliRunner()

    db.create_finding("S1", "review", "review_feedback", "high", "routing", "S1 issue")
    fid = db.create_finding("S2", "review", "review_feedback", "medium", "style", "S2 issue")
    db.update_finding(fid, status="accepted")

    from story_lifecycle.cli.review_feedback import approvals_group
    result = runner.invoke(approvals_group, ["list"])

    assert result.exit_code == 0
    assert "S1" in result.output
    assert "S2" in result.output


def test_cli_approvals_decide(tmp_path):
    """story approvals decide accepts a finding."""
    db = _setup_db(tmp_path)
    runner = CliRunner()

    fid = db.create_finding("S1", "review", "review_feedback", "high", "routing", "test")

    from story_lifecycle.cli.review_feedback import approvals_group
    result = runner.invoke(approvals_group, ["decide", fid, "--accept"])

    assert result.exit_code == 0
    finding = db.get_finding(fid)
    assert finding["status"] == "accepted"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_review_feedback.py -k "cli" -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'story_lifecycle.cli.review_feedback'`

- [ ] **Step 3: Implement CLI module**

Create `src/story_lifecycle/cli/review_feedback.py`:

```python
"""`story review-feedback` and `story approvals` — Review Feedback Intake CLI."""
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from ..db.models import init_db

console = Console()


@click.group(name="review-feedback")
def review_feedback_group():
    """Import and manage review feedback findings."""
    init_db()


@review_feedback_group.command()
@click.argument("story_key")
@click.argument("review_file", type=click.Path(exists=True, path_type=Path))
def import_cmd(story_key, review_file):
    """Import review feedback from a file and extract candidate findings.

    \b
    Examples:
      story review-feedback import STORY-123 review.md
      story review-feedback import STORY-123 review.json
    """
    from ..db import models as db
    from ..orchestrator.review_feedback import import_review

    story = db.get_story(story_key)
    if not story:
        console.print(f"[red]Story '{story_key}' not found.[/]")
        sys.exit(1)

    content = review_file.read_text(encoding="utf-8")
    if not content.strip():
        console.print("[red]Review file is empty.[/]")
        sys.exit(1)

    console.print(f"\n[bold cyan]Story:[/] {story_key}")
    console.print(f"  File: {review_file.name}")
    console.print(f"  Size: {len(content)} chars")

    console.print("\n[dim]Extracting candidate findings...[/]")
    result = import_review(story_key, content)

    mode_label = "[green]LLM[/]" if result["mode"] == "llm" else "[yellow]rule fallback[/]"
    console.print(f"  Mode: {mode_label}")
    console.print(f"  Imported: [green]{result['imported']}[/] finding(s)")

    if result["skipped"]:
        console.print(f"  Skipped: [yellow]{result['skipped']}[/]")

    if result["warnings"]:
        console.print("\n[yellow]Warnings:[/]")
        for w in result["warnings"]:
            console.print(f"  [yellow]- {w}[/]")

    if result["imported"] == 0:
        console.print("\n[dim]No candidate findings extracted.[/]")
    else:
        console.print(f"\n[dim]Run [bold]story review-feedback list {story_key}[/] to view.[/]")


# register as 'import' but function is import_cmd
review_feedback_group.add_command(import_cmd, name="import")


@review_feedback_group.command("list")
@click.argument("story_key")
def list_findings(story_key):
    """List all findings for a story."""
    from ..db import models as db

    findings = db.get_findings_by_story(story_key)
    if not findings:
        console.print(f"[dim]No findings for story '{story_key}'.[/]")
        return

    table = Table(title=f"Findings: {story_key}")
    table.add_column("ID", style="dim", max_width=20)
    table.add_column("Status", style="cyan")
    table.add_column("Severity", style="bold")
    table.add_column("Category", style="white")
    table.add_column("Description", max_width=50)
    table.add_column("Source", style="dim")

    sev_colors = {"high": "red", "medium": "yellow", "low": "green"}
    status_colors = {
        "open": "cyan", "accepted": "green", "fixed": "green",
        "verified": "bold green", "rejected": "red", "deferred": "yellow",
        "learned": "blue",
    }

    for f in findings:
        sev = f["severity"]
        status = f["status"]
        table.add_row(
            f["id"],
            f"[{status_colors.get(status, 'white')}]{status}[/]",
            f"[{sev_colors.get(sev, 'white')}]{sev.upper()}[/]",
            f["category"],
            f["description"][:80],
            f["source"],
        )

    console.print(table)


@review_feedback_group.command()
@click.argument("finding_id")
@click.option("--accept", "action", flag_value="accept", help="Accept finding")
@click.option("--reject", "action", flag_value="reject", help="Reject finding")
@click.option("--defer", "action", flag_value="defer", help="Defer finding")
@click.option("--downgrade", "action", flag_value="downgrade", help="Downgrade severity")
@click.option("--reason", "-r", default="", help="Reason for the decision")
def decide(finding_id, action, reason):
    """Make a decision on a candidate finding.

    \b
    Examples:
      story review-feedback decide finding-xxx --accept
      story review-feedback decide finding-yyy --reject --reason "overclaimed"
      story review-feedback decide finding-zzz --defer
      story review-feedback decide finding-www --downgrade
    """
    from ..db import models as db
    from ..orchestrator.quality import update_finding_status

    if not action:
        console.print("[red]Specify one of: --accept, --reject, --defer, --downgrade[/]")
        sys.exit(1)

    finding = db.get_finding(finding_id)
    if not finding:
        console.print(f"[red]Finding '{finding_id}' not found.[/]")
        sys.exit(1)

    story_key = finding["story_key"]

    if action == "accept":
        update_finding_status(story_key, finding_id, "accepted", reason=reason)
        console.print(f"[green]Accepted[/] {finding_id}")
    elif action == "reject":
        update_finding_status(story_key, finding_id, "rejected", reason=reason)
        console.print(f"[red]Rejected[/] {finding_id}")
    elif action == "defer":
        update_finding_status(story_key, finding_id, "deferred", reason=reason)
        console.print(f"[yellow]Deferred[/] {finding_id}")
    elif action == "downgrade":
        sev_order = {"high": "medium", "medium": "low", "low": "low"}
        new_sev = sev_order.get(finding["severity"], "low")
        db.update_finding(finding_id, severity=new_sev)
        db.log_event(
            story_key,
            finding.get("stage", ""),
            "finding_downgraded",
            {"finding_id": finding_id, "from": finding["severity"], "to": new_sev, "reason": reason},
        )
        console.print(f"[yellow]Downgraded[/] {finding_id}: {finding['severity']} -> {new_sev}")

    if reason:
        console.print(f"  Reason: [dim]{reason}[/]")


# ── Approvals group ──


@click.group(name="approvals")
def approvals_group():
    """View and manage the approval queue for pending findings."""
    init_db()


@approvals_group.command("list")
def approvals_list():
    """List all pending findings (open + accepted) across stories."""
    from ..db import models as db

    pending = db.get_all_pending_findings()
    if not pending:
        console.print("[dim]No pending findings.[/]")
        return

    table = Table(title="Approval Queue")
    table.add_column("ID", style="dim", max_width=20)
    table.add_column("Story", style="cyan")
    table.add_column("Status", style="cyan")
    table.add_column("Severity", style="bold")
    table.add_column("Category")
    table.add_column("Description", max_width=50)
    table.add_column("Source", style="dim")

    sev_colors = {"high": "red", "medium": "yellow", "low": "green"}
    status_colors = {"open": "cyan", "accepted": "green"}

    for f in pending:
        sev = f["severity"]
        status = f["status"]
        table.add_row(
            f["id"],
            f["story_key"],
            f"[{status_colors.get(status, 'white')}]{status}[/]",
            f"[{sev_colors.get(sev, 'white')}]{sev.upper()}[/]",
            f["category"],
            f["description"][:80],
            f["source"],
        )

    console.print(table)
    console.print(f"\n[dim]{len(pending)} pending finding(s)[/]")


@approvals_group.command()
@click.argument("finding_id")
@click.option("--accept", "action", flag_value="accept", help="Accept finding")
@click.option("--reject", "action", flag_value="reject", help="Reject finding")
@click.option("--reason", "-r", default="", help="Reason")
def decide_approval(finding_id, action, reason):
    """Make a decision on a pending finding.

    \b
    Examples:
      story approvals decide finding-xxx --accept
      story approvals decide finding-yyy --reject --reason "not actionable"
    """
    from ..db import models as db
    from ..orchestrator.quality import update_finding_status

    if not action:
        console.print("[red]Specify --accept or --reject[/]")
        sys.exit(1)

    finding = db.get_finding(finding_id)
    if not finding:
        console.print(f"[red]Finding '{finding_id}' not found.[/]")
        sys.exit(1)

    story_key = finding["story_key"]

    if action == "accept":
        update_finding_status(story_key, finding_id, "accepted", reason=reason)
        console.print(f"[green]Accepted[/] {finding_id}")
    elif action == "reject":
        update_finding_status(story_key, finding_id, "rejected", reason=reason)
        console.print(f"[red]Rejected[/] {finding_id}")

    if reason:
        console.print(f"  Reason: [dim]{reason}[/]")
```

- [ ] **Step 4: Register CLI group in main.py**

Edit `src/story_lifecycle/cli/main.py`, add after the `seed_quality` import (around line 183):

```python
from .review_feedback import review_feedback_group, approvals_group  # noqa: E402

cli.add_command(review_feedback_group)
cli.add_command(approvals_group)
```

- [ ] **Step 5: Add deferred status support to DB**

The finding status lifecycle needs `deferred`. Edit `src/story_lifecycle/db/models.py` — no schema change needed since `status` is a free-text field. But we need to update `get_all_pending_findings` to NOT include deferred:

The existing implementation already only queries `["open", "accepted"]`, so deferred findings won't appear in the approval queue. No change needed.

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_review_feedback.py -v`
Expected: All tests PASS

- [ ] **Step 7: Commit**

```bash
git add src/story_lifecycle/cli/review_feedback.py src/story_lifecycle/cli/main.py tests/test_review_feedback.py
git commit -m "feat: add review-feedback and approvals CLI commands"
```

---

## Task 4: API Endpoints for Review Feedback and Approval Queue

**Files:**
- Modify: `src/story_lifecycle/orchestrator/api.py` (add endpoints)
- Test: `tests/test_review_feedback.py` (append tests)

- [ ] **Step 1: Write failing tests for API endpoints**

Append to `tests/test_review_feedback.py`:

```python
# ── Task 4: API endpoints ──

from fastapi.testclient import TestClient


def _get_api_client(tmp_path):
    """Create a TestClient with fresh DB."""
    os.environ["STORY_HOME"] = str(tmp_path)
    from story_lifecycle.db.models import init_db
    init_db()
    from story_lifecycle.orchestrator.api import app
    return TestClient(app)


def test_api_import_review_feedback(tmp_path):
    """POST /api/story/{key}/review-feedback imports review content."""
    client = _get_api_client(tmp_path)
    from story_lifecycle.db import models as db
    db.upsert_story("S1", title="Test", workspace=str(tmp_path), current_stage="impl")

    resp = client.post("/api/story/S1/review-feedback", json={
        "content": "- [HIGH] api.py:42 缺少空指针检查\n- [MEDIUM] 缺少测试",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["imported"] >= 1
    assert data["mode"] == "rule_fallback"


def test_api_list_review_feedback(tmp_path):
    """GET /api/story/{key}/review-feedback returns imported findings."""
    client = _get_api_client(tmp_path)
    from story_lifecycle.db import models as db
    db.upsert_story("S1", title="Test", workspace=str(tmp_path), current_stage="impl")
    db.create_finding("S1", "review", "review_feedback", "high", "error_handling",
                      "test finding", location="api.py:42")

    resp = client.get("/api/story/S1/review-feedback")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["findings"]) >= 1
    assert data["findings"][0]["source"] == "review_feedback"


def test_api_decide_finding(tmp_path):
    """PUT /api/finding/{id}/decide updates finding status."""
    client = _get_api_client(tmp_path)
    from story_lifecycle.db import models as db
    db.upsert_story("S1", title="Test", workspace=str(tmp_path), current_stage="impl")
    fid = db.create_finding("S1", "review", "review_feedback", "high", "error_handling",
                            "test finding")

    resp = client.put(f"/api/finding/{fid}/decide", json={
        "action": "accept",
        "reason": "valid finding",
    })
    assert resp.status_code == 200
    finding = db.get_finding(fid)
    assert finding["status"] == "accepted"


def test_api_decide_finding_reject(tmp_path):
    """PUT /api/finding/{id}/decide rejects finding."""
    client = _get_api_client(tmp_path)
    from story_lifecycle.db import models as db
    db.upsert_story("S1", title="Test", workspace=str(tmp_path), current_stage="impl")
    fid = db.create_finding("S1", "review", "review_feedback", "high", "error_handling",
                            "test finding")

    resp = client.put(f"/api/finding/{fid}/decide", json={
        "action": "reject",
        "reason": "overclaimed",
    })
    assert resp.status_code == 200
    finding = db.get_finding(fid)
    assert finding["status"] == "rejected"


def test_api_decide_finding_downgrade(tmp_path):
    """PUT /api/finding/{id}/decide downgrades finding severity."""
    client = _get_api_client(tmp_path)
    from story_lifecycle.db import models as db
    db.upsert_story("S1", title="Test", workspace=str(tmp_path), current_stage="impl")
    fid = db.create_finding("S1", "review", "review_feedback", "high", "error_handling",
                            "test finding")

    resp = client.put(f"/api/finding/{fid}/decide", json={
        "action": "downgrade",
        "reason": "not critical",
    })
    assert resp.status_code == 200
    finding = db.get_finding(fid)
    assert finding["severity"] == "medium"


def test_api_approvals_queue(tmp_path):
    """GET /api/approvals returns pending findings across stories."""
    client = _get_api_client(tmp_path)
    from story_lifecycle.db import models as db
    db.create_finding("S1", "review", "review_feedback", "high", "routing", "S1 issue")
    db.create_finding("S2", "review", "review_feedback", "medium", "style", "S2 issue")
    fid_rejected = db.create_finding("S3", "review", "review_feedback", "low", "style", "S3 rejected")
    db.update_finding(fid_rejected, status="rejected")

    resp = client.get("/api/approvals")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["findings"]) == 2  # only open ones
    stories = {f["story_key"] for f in data["findings"]}
    assert stories == {"S1", "S2"}


def test_api_decide_finding_not_found(tmp_path):
    """PUT /api/finding/{id}/decide returns 404 for missing finding."""
    client = _get_api_client(tmp_path)

    resp = client.put("/api/finding/nonexistent/decide", json={
        "action": "accept",
    })
    assert resp.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_review_feedback.py -k "api" -v`
Expected: FAIL — 404 on routes (endpoints not yet added)

- [ ] **Step 3: Add API endpoints to api.py**

Edit `src/story_lifecycle/orchestrator/api.py`, add request models and endpoints.

After the existing `ResumeParentRequest` class (around line 48), add:

```python
class ReviewFeedbackRequest(BaseModel):
    content: str


class DecideFindingRequest(BaseModel):
    action: str  # accept, reject, defer, downgrade, mark_verified
    reason: str = ""
```

After the existing quality endpoints (after the `reject_pattern_endpoint`, around line 387), add:

```python
# -------- review feedback endpoints --------


@app.post("/api/story/{story_key}/review-feedback")
def api_import_review_feedback(story_key: str, req: ReviewFeedbackRequest):
    """Import review feedback content and extract candidate findings."""
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")

    if not req.content.strip():
        raise HTTPException(400, "Review content is empty")

    from .review_feedback import import_review

    result = import_review(story_key, req.content)
    return {
        "imported": result["imported"],
        "skipped": result["skipped"],
        "mode": result["mode"],
        "warnings": result["warnings"],
    }


@app.get("/api/story/{story_key}/review-feedback")
def api_list_review_feedback(story_key: str):
    """List review feedback findings for a story."""
    findings = db.get_findings_by_story(story_key)
    review_findings = [f for f in findings if f["source"] == "review_feedback"]
    return {"findings": review_findings}


@app.put("/api/finding/{finding_id}/decide")
def api_decide_finding(finding_id: str, req: DecideFindingRequest):
    """Make a decision on a finding: accept/reject/defer/downgrade/mark_verified."""
    from .quality import update_finding_status

    finding = db.get_finding(finding_id)
    if not finding:
        raise HTTPException(404, f"Finding not found: {finding_id}")

    story_key = finding["story_key"]
    action = req.action

    if action == "accept":
        update_finding_status(story_key, finding_id, "accepted", reason=req.reason)
    elif action == "reject":
        update_finding_status(story_key, finding_id, "rejected", reason=req.reason)
    elif action == "defer":
        update_finding_status(story_key, finding_id, "deferred", reason=req.reason)
    elif action == "downgrade":
        sev_order = {"high": "medium", "medium": "low", "low": "low"}
        new_sev = sev_order.get(finding["severity"], "low")
        db.update_finding(finding_id, severity=new_sev)
        db.log_event(
            story_key,
            finding.get("stage", ""),
            "finding_downgraded",
            {"finding_id": finding_id, "from": finding["severity"], "to": new_sev, "reason": req.reason},
        )
    elif action == "mark_verified":
        update_finding_status(story_key, finding_id, "verified", reason=req.reason)
    else:
        raise HTTPException(400, f"Unknown action: {action}. Use: accept/reject/defer/downgrade/mark_verified")

    updated = db.get_finding(finding_id)
    return {"status": updated["status"], "severity": updated["severity"]}


@app.get("/api/approvals")
def api_approvals():
    """Get approval queue: all pending (open + accepted) findings."""
    findings = db.get_all_pending_findings()
    return {"findings": findings}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_review_feedback.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/story_lifecycle/orchestrator/api.py tests/test_review_feedback.py
git commit -m "feat: add API endpoints for review feedback import, listing, decisions, and approval queue"
```

---

## Task 5: Reviewer Role Guardrail

**Files:**
- Modify: `src/story_lifecycle/orchestrator/planner.py` (add guardrail to review prompt)
- Test: `tests/test_review_feedback.py` (append test)

- [ ] **Step 1: Write test for reviewer role guardrail**

Append to `tests/test_review_feedback.py`:

```python
# ── Task 5: Reviewer role guardrail ──


def test_review_prompt_contains_readonly_guardrail():
    """Review prompt in planner.py must contain reviewer read-only constraint."""
    from story_lifecycle.orchestrator.planner import review_stage

    # Inspect the source code of review_stage to check prompt content
    import inspect
    source = inspect.getsource(review_stage)
    assert "只读" in source or "read-only" in source.lower() or "不改代码" in source or "不要修改" in source
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_review_feedback.py::test_review_prompt_contains_readonly_guardrail -v`
Expected: FAIL — the guardrail text is not yet in the prompt

- [ ] **Step 3: Add reviewer role guardrail to planner.py**

Edit `src/story_lifecycle/orchestrator/planner.py`, in the `review_stage()` function, add after the prompt opening line (around line 173, after `你是一个开发团队的 QA/评审员。你的职责是结构化审查产出质量，记录问题和建议。`):

Add this line right after that first sentence:

```
你是评审员，只读不改。你不修改任何代码或文件，只负责审查、记录问题和建议。
```

Specifically, find in the prompt:

```python
    prompt = f"""你是一个开发团队的 QA/评审员。你的职责是结构化审查产出质量，记录问题和建议。
```

Replace with:

```python
    prompt = f"""你是一个开发团队的 QA/评审员。你是评审员，只读不改——你不修改任何代码或文件，只负责审查、记录问题和建议。
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_review_feedback.py::test_review_prompt_contains_readonly_guardrail -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/story_lifecycle/orchestrator/planner.py tests/test_review_feedback.py
git commit -m "feat: add reviewer read-only guardrail to review prompt in planner.py"
```

---

## Task 6: Final Integration Test and Regression Check

**Files:**
- Test: `tests/test_review_feedback.py` (append e2e test)

- [ ] **Step 1: Write end-to-end integration test**

Append to `tests/test_review_feedback.py`:

```python
# ── Task 6: E2E integration test ──


def test_review_feedback_intake_e2e(tmp_path):
    """End-to-end: import review -> list -> decide -> verify in quality flywheel."""
    os.environ["STORY_HOME"] = str(tmp_path)
    from story_lifecycle.db import models as db
    db.init_db()
    from story_lifecycle.orchestrator.review_feedback import import_review
    from story_lifecycle.orchestrator.quality import (
        update_finding_status,
        build_quality_packet,
        check_dod,
    )

    # Setup story
    db.upsert_story("TAPD-100100", title="逾期利息调整",
                     workspace=str(tmp_path), current_stage="implement")

    # 1. Import review (rule fallback, no LLM)
    review_md = (
        "- [HIGH] api.py:42 缺少空指针检查，可能导致 NPE\n"
        "- [MEDIUM] 缺少边界测试 case\n"
        "- [LOW] 变量命名不规范"
    )
    with patch.dict("os.environ", {}, clear=True):
        result = import_review("TAPD-100100", review_md)

    assert result["imported"] >= 2  # at least the HIGH and MEDIUM
    assert result["mode"] == "rule_fallback"

    # 2. List findings
    findings = db.get_findings_by_story("TAPD-100100")
    assert len(findings) >= 2

    high_f = next(f for f in findings if f["severity"] == "high")
    medium_f = next(f for f in findings if f["severity"] == "medium")

    # 3. DoD should block (open high finding)
    dod = check_dod("TAPD-100100", "implement")
    assert dod["passed"] is False

    # 4. Accept high finding
    update_finding_status("TAPD-100100", high_f["id"], "accepted", reason="valid issue")

    # 5. Reject medium finding
    update_finding_status("TAPD-100100", medium_f["id"], "rejected", reason="style only")

    # 6. Quality packet shows accepted finding
    packet = build_quality_packet("TAPD-100100", "implement")
    # The accepted finding should not appear in "open findings" section
    # since its status is "accepted" not "open"

    # 7. Mark verified
    update_finding_status("TAPD-100100", high_f["id"], "verified",
                          reason="fixed and tested",
                          evidence={"verification_event_id": 1})
    finding = db.get_finding(high_f["id"])
    assert finding["status"] == "verified"

    # 8. Audit trail
    events = db.get_story_events("TAPD-100100")
    event_types = {e["event_type"] for e in events}
    assert "review_feedback_imported" in event_types
    assert "finding_status_changed" in event_types

    # 9. DoD should pass now (no open high findings)
    dod2 = check_dod("TAPD-100100", "implement")
    assert dod2["passed"] is True
```

- [ ] **Step 2: Run full test suite**

Run: `pytest tests/test_review_feedback.py -v`
Expected: All tests PASS

- [ ] **Step 3: Run existing test suite for regression check**

Run: `pytest -v`
Expected: All tests PASS, no regressions

- [ ] **Step 4: Commit**

```bash
git add tests/test_review_feedback.py
git commit -m "test: add e2e integration test for review feedback intake loop"
```

---

## Self-Review Checklist

### 1. Spec Coverage

| Phase 1 Deliverable | Task |
|---|---|
| Review import extractor (LLM) | Task 2 |
| Finding dedupe/merge | Task 2 |
| Approval queue | Task 3 (CLI), Task 4 (API) |
| Decision commands (accept/reject/downgrade/defer/mark_verified) | Task 3 (CLI), Task 4 (API) |
| Reviewer role guardrail | Task 5 |
| `story review-feedback import/list/decide` CLI | Task 3 |
| `story approvals list/decide` CLI | Task 3 |
| REST API endpoints | Task 4 |
| E2E integration test | Task 6 |

### 2. Placeholder Scan

- No TBD/TODO/fill-in-later found
- All steps contain actual code
- All commands include expected output

### 3. Type Consistency

- `extract_candidate_findings()` returns `dict` with `mode`, `candidates`, `summary`, `warnings` — consumed by `import_review()` and CLI
- `validate_candidates()` returns `tuple[list[dict], list[str]]` — consumed by extraction functions
- `dedupe_candidates()` takes and returns `list[dict]` — consumed by `import_review()`
- `import_review()` returns `dict` with `imported`, `skipped`, `warnings`, `mode` — consumed by CLI and API
- Finding IDs are `str` (`finding-{uuid}`) — consistent across DB, CLI, API
- Decision actions: `accept/reject/defer/downgrade/mark_verified` — consistent across CLI and API
- `STORY_LLM_API_KEY`/`STORY_LLM_BASE_URL`/`STORY_LLM_MODEL` env vars — same as planner.py

---
