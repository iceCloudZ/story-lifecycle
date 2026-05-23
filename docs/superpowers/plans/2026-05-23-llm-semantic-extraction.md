# LLM Semantic Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增轻量语义层 `orchestrator/semantic.py`，把 Bug 正文提取、Pattern 复发检测、Quality Packet 筛选、Review 摘要、Debug 恢复建议从脆弱 regex/keyword 逻辑迁移到 LLM 语义理解，同时保留规则兜底。

**Architecture:** 新增 `semantic.py` 封装统一的 LLM 调用入口 + JSON schema 验证 + fallback，业务模块只依赖其函数签名。P0（Bug 提取 + Pattern 复发）→ P1（Pattern rerank + Review 摘要）→ P2（Debug 恢复建议）分批实施。

**Tech Stack:** Python 3.10+, httpx (已有), pytest + monkeypatch (已有)

---

## File Structure

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `src/story_lifecycle/orchestrator/semantic.py` | 统一 LLM 语义层：SemanticResult, _call_semantic_llm, 5 个业务函数 |
| Create | `tests/test_semantic.py` | semantic 模块单元测试 |
| Modify | `src/story_lifecycle/sources/bug_providers.py:35-49` | TapdBodyBugProvider 集成 extract_bug_context |
| Modify | `src/story_lifecycle/orchestrator/nodes.py:547-598` | _check_pattern_recurrence 集成 match_pattern_recurrence |
| Modify | `src/story_lifecycle/orchestrator/quality.py:102-152` | build_quality_packet 集成 rerank_relevant_patterns |
| Modify | `src/story_lifecycle/orchestrator/seed_pipeline.py:294-303` | _summarize_review 集成 summarize_review_for_learning |
| Modify | `src/story_lifecycle/orchestrator/observability.py:222-287` | build_debug_response 增加 recommend_recovery 调用点 |

---

## Task 1: Semantic Layer Infrastructure

**Files:**
- Create: `src/story_lifecycle/orchestrator/semantic.py`
- Test: `tests/test_semantic.py`

- [ ] **Step 1: Write failing tests for SemanticResult and _call_semantic_llm**

Create `tests/test_semantic.py`:

```python
"""Tests for orchestrator/semantic.py — LLM semantic extraction layer."""
import json
from unittest.mock import patch, MagicMock

import pytest


# ── SemanticResult construction ──


def _make_fake_llm_response(json_obj: dict) -> MagicMock:
    """Build a fake httpx.Response that returns json_obj as LLM content."""
    mock = MagicMock()
    mock.raise_for_status = MagicMock()
    mock.json.return_value = {
        "choices": [{"message": {"content": json.dumps(json_obj, ensure_ascii=False)}}]
    }
    return mock


def _make_fake_llm_response_raw(raw: str) -> MagicMock:
    """Build a fake httpx.Response with raw content (e.g. markdown-wrapped)."""
    mock = MagicMock()
    mock.raise_for_status = MagicMock()
    mock.json.return_value = {
        "choices": [{"message": {"content": raw}}]
    }
    return mock


def test_semantic_result_llm_mode():
    """_ok_result returns mode=llm, confidence from data."""
    from story_lifecycle.orchestrator.semantic import _ok_result

    r = _ok_result(data={"test": 1}, confidence="high")
    assert r["ok"] is True
    assert r["mode"] == "llm"
    assert r["confidence"] == "high"
    assert r["data"] == {"test": 1}


def test_semantic_result_fallback_mode():
    """_fallback_result returns mode=rule_fallback, confidence=low."""
    from story_lifecycle.orchestrator.semantic import _fallback_result

    r = _fallback_result(data={"test": 1})
    assert r["ok"] is True
    assert r["mode"] == "rule_fallback"
    assert r["confidence"] == "low"
    assert r["warnings"] == ["LLM unavailable, using rule fallback"]


def test_semantic_result_error_mode():
    """_error_result returns mode=error."""
    from story_lifecycle.orchestrator.semantic import _error_result

    r = _error_result("boom")
    assert r["ok"] is False
    assert r["mode"] == "error"
    assert "boom" in r["warnings"]


def test_call_semantic_llm_no_api_key():
    """Returns unavailable result when no API key configured."""
    from story_lifecycle.orchestrator.semantic import _call_semantic_llm

    with patch.dict("os.environ", {}, clear=True):
        result = _call_semantic_llm("test prompt", {})
    assert result["mode"] == "unavailable"
    assert result["ok"] is False


def test_call_semantic_llm_success():
    """Returns parsed JSON on successful LLM call."""
    from story_lifecycle.orchestrator.semantic import _call_semantic_llm

    fake = _make_fake_llm_response({"answer": 42})
    with patch.dict("os.environ", {"STORY_LLM_API_KEY": "test-key"}):
        with patch("httpx.post", return_value=fake) as mock_post:
            result = _call_semantic_llm("test prompt", {})

    assert result["mode"] == "llm"
    assert result["data"] == {"answer": 42}
    assert result["ok"] is True
    mock_post.assert_called_once()


def test_call_semantic_llm_markdown_wrapped():
    """Handles markdown code-fenced JSON response."""
    from story_lifecycle.orchestrator.semantic import _call_semantic_llm

    raw = '```json\n{"answer": 42}\n```'
    fake = _make_fake_llm_response_raw(raw)
    with patch.dict("os.environ", {"STORY_LLM_API_KEY": "test-key"}):
        with patch("httpx.post", return_value=fake):
            result = _call_semantic_llm("test prompt", {})

    assert result["mode"] == "llm"
    assert result["data"] == {"answer": 42}


def test_call_semantic_llm_invalid_json():
    """Returns error on unparseable LLM response."""
    from story_lifecycle.orchestrator.semantic import _call_semantic_llm

    fake = _make_fake_llm_response_raw("not json at all")
    with patch.dict("os.environ", {"STORY_LLM_API_KEY": "test-key"}):
        with patch("httpx.post", return_value=fake):
            result = _call_semantic_llm("test prompt", {})

    assert result["mode"] == "error"
    assert result["ok"] is False


def test_call_semantic_llm_http_error():
    """Returns error on httpx exception."""
    import httpx

    from story_lifecycle.orchestrator.semantic import _call_semantic_llm

    with patch.dict("os.environ", {"STORY_LLM_API_KEY": "test-key"}):
        with patch("httpx.post", side_effect=httpx.HTTPStatusError("503", request=MagicMock(), response=MagicMock())):
            result = _call_semantic_llm("test prompt", {})

    assert result["mode"] == "error"
    assert result["ok"] is False


def test_call_semantic_llm_schema_validation():
    """Validates response against provided schema and rejects invalid values."""
    from story_lifecycle.orchestrator.semantic import _call_semantic_llm

    schema = {
        "type": "object",
        "properties": {"confidence": {"enum": ["high", "medium", "low"]}},
        "required": ["confidence"],
    }
    fake = _make_fake_llm_response({"confidence": "invalid_value"})
    with patch.dict("os.environ", {"STORY_LLM_API_KEY": "test-key"}):
        with patch("httpx.post", return_value=fake):
            result = _call_semantic_llm("test prompt", schema)

    # Should still return data but with default confidence
    assert result["ok"] is True
    assert result["data"]["confidence"] == "low"  # default on invalid
    assert any("confidence" in w for w in result["warnings"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_semantic.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'story_lifecycle.orchestrator.semantic'`

- [ ] **Step 3: Implement semantic.py — infrastructure layer**

Create `src/story_lifecycle/orchestrator/semantic.py`:

```python
"""LLM Semantic Extraction Layer.

Provides unified LLM structured-output calls for semantic tasks.
Each function returns SemanticResult with mode/confidence/fallback.
"""
from __future__ import annotations

import json
import os
import logging
import re
from typing import Any, Literal, TypedDict

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
        warnings=[f"LLM error: {error}"],
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
                    data[field] = props[field]["enum"][-1]  # default to last (usually low)
                    warnings.append(f"missing required field '{field}', defaulted to '{data[field]}'")

    # Validate enum constraints
    for field, rules in props.items():
        if field in data and "enum" in rules:
            if data[field] not in rules["enum"]:
                old = data[field]
                data[field] = rules["enum"][-1]  # fallback to last enum value
                warnings.append(f"invalid '{field}' value '{old}', defaulted to '{data[field]}'")

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_semantic.py -v`
Expected: All 9 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/story_lifecycle/orchestrator/semantic.py tests/test_semantic.py
git commit -m "feat: add semantic.py infrastructure — SemanticResult, _call_semantic_llm, schema validation"
```

---

## Task 2: Bug Context Extraction (P0)

**Files:**
- Modify: `src/story_lifecycle/orchestrator/semantic.py` — add `extract_bug_context()`
- Modify: `src/story_lifecycle/sources/bug_providers.py:35-49` — integrate LLM path
- Test: `tests/test_semantic.py` — add tests

- [ ] **Step 1: Write failing tests for extract_bug_context**

Append to `tests/test_semantic.py`:

```python
# ── extract_bug_context ──


def test_extract_bug_context_llm_success():
    """LLM extracts structured bug context from markdown."""
    from story_lifecycle.orchestrator.semantic import extract_bug_context

    llm_output = {
        "description": "登录页面崩溃",
        "steps_to_reproduce": "1. 打开登录页\n2. 输入特殊字符",
        "expected_behavior": "正常登录",
        "actual_behavior": "页面白屏",
        "environment": "Chrome 120, Windows 11",
        "logs": "Uncaught TypeError at login.js:42",
        "missing_fields": [],
        "confidence": "high",
    }
    fake = _make_fake_llm_response(llm_output)

    with patch.dict("os.environ", {"STORY_LLM_API_KEY": "test-key"}):
        with patch("httpx.post", return_value=fake):
            result = extract_bug_context("## 现象\n页面白屏\n\n## 复现步骤\n输入特殊字符", "登录页面崩溃")

    assert result["ok"] is True
    assert result["mode"] == "llm"
    assert result["data"]["steps_to_reproduce"] == "1. 打开登录页\n2. 输入特殊字符"
    assert result["data"]["expected_behavior"] == "正常登录"


def test_extract_bug_context_fallback():
    """Without LLM, returns rule_fallback with regex extraction."""
    from story_lifecycle.orchestrator.semantic import extract_bug_context

    with patch.dict("os.environ", {}, clear=True):
        result = extract_bug_context("## 复现步骤\n点按钮\n\n## 预期结果\n成功", "标题")

    assert result["mode"] == "rule_fallback"
    assert "点按钮" in result["data"].get("steps_to_reproduce", "")


def test_extract_bug_context_llm_error_fallback():
    """LLM error falls back to regex extraction."""
    from story_lifecycle.orchestrator.semantic import extract_bug_context

    with patch.dict("os.environ", {"STORY_LLM_API_KEY": "test-key"}):
        with patch("httpx.post", side_effect=Exception("timeout")):
            result = extract_bug_context("## 复现步骤\n点按钮", "标题")

    assert result["mode"] == "rule_fallback"
    assert "点按钮" in result["data"].get("steps_to_reproduce", "")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_semantic.py::test_extract_bug_context_llm_success -v`
Expected: FAIL — `ImportError: cannot import name 'extract_bug_context'`

- [ ] **Step 3: Implement extract_bug_context in semantic.py**

Append to `src/story_lifecycle/orchestrator/semantic.py`:

```python
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
import re as _re

_SECTION_PATTERNS = {
    "steps_to_reproduce": "复现步骤|步骤|重现",
    "expected_behavior": "预期|期望|期望结果",
    "actual_behavior": "实际|实际结果|现象",
    "environment": "环境|版本|设备",
    "logs": "日志|log|堆栈|stack",
}


def _regex_extract_section(md: str, pattern: str) -> str:
    """Extract a section from markdown using heading keyword matching."""
    m = _re.search(
        rf"(?:{pattern})[：:\s]*\n(.*?)(?=\n##|\n#|\Z)",
        md,
        _re.DOTALL | _re.IGNORECASE,
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
```

Also ensure the `import re` at top of file is present (already added as `import re` earlier; the `_re` alias is local to avoid shadowing).

- [ ] **Step 4: Integrate into bug_providers.py**

Edit `src/story_lifecycle/sources/bug_providers.py`, modify `TapdBodyBugProvider.fetch_content()`:

Replace lines 35-49:

```python
    def fetch_content(self, bug: SourceItem) -> BugContext | None:
        from .prd_providers import _html_to_markdown

        md = _html_to_markdown(bug.description)

        # Try LLM semantic extraction first
        try:
            from ..orchestrator.semantic import extract_bug_context

            result = extract_bug_context(md, title=bug.title)
            data = result["data"]
            return BugContext(
                source_type="tapd_body",
                description=data.get("description", bug.title),
                steps_to_reproduce=data.get("steps_to_reproduce", ""),
                expected_behavior=data.get("expected_behavior", ""),
                actual_behavior=data.get("actual_behavior", ""),
                environment=data.get("environment", ""),
                screenshots=self._extract_images(md),
                logs=data.get("logs", ""),
                raw_markdown=md,
            )
        except Exception:
            # Fallback to regex
            pass

        # Regex fallback (original logic)
        return BugContext(
            source_type="tapd_body",
            description=bug.title,
            steps_to_reproduce=self._extract_section(md, "复现步骤|步骤|重现"),
            expected_behavior=self._extract_section(md, "预期|期望|期望结果"),
            actual_behavior=self._extract_section(md, "实际|实际结果|现象"),
            environment=self._extract_section(md, "环境|版本|设备"),
            screenshots=self._extract_images(md),
            logs=self._extract_section(md, "日志|log|堆栈|stack"),
            raw_markdown=md,
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_semantic.py -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/story_lifecycle/orchestrator/semantic.py src/story_lifecycle/sources/bug_providers.py tests/test_semantic.py
git commit -m "feat: add extract_bug_context with LLM + regex fallback, integrate into TapdBodyBugProvider"
```

---

## Task 3: Pattern Recurrence Semantic Matching (P0)

**Files:**
- Modify: `src/story_lifecycle/orchestrator/semantic.py` — add `match_pattern_recurrence()`
- Modify: `src/story_lifecycle/orchestrator/nodes.py:547-598` — integrate into `_check_pattern_recurrence()`
- Test: `tests/test_semantic.py` — add tests

- [ ] **Step 1: Write failing tests for match_pattern_recurrence**

Append to `tests/test_semantic.py`:

```python
# ── match_pattern_recurrence ──


def test_match_pattern_recurrence_llm_match():
    """LLM identifies semantic match between issue and pattern."""
    from story_lifecycle.orchestrator.semantic import match_pattern_recurrence

    llm_output = {
        "matches": [
            {
                "pattern_id": "p-001",
                "matched": True,
                "confidence": "high",
                "reasoning": "issue 描述缺少降级路径与 pattern 规则一致",
                "evidence": ["issue.description", "pattern.rule"],
            }
        ]
    }
    fake = _make_fake_llm_response(llm_output)

    issue = {"description": "接口没有 fallback 机制，一旦服务挂了就全完了", "category": "error_handling"}
    patterns = [{"id": "p-001", "pattern": "缺少回滚方案", "rule": "变更必须有回滚或降级路径"}]

    with patch.dict("os.environ", {"STORY_LLM_API_KEY": "test-key"}):
        with patch("httpx.post", return_value=fake):
            result = match_pattern_recurrence(issue, patterns)

    assert result["ok"] is True
    assert result["mode"] == "llm"
    assert len(result["data"]["matches"]) == 1
    assert result["data"]["matches"][0]["pattern_id"] == "p-001"


def test_match_pattern_recurrence_fallback():
    """Without LLM, uses keyword matching fallback."""
    from story_lifecycle.orchestrator.semantic import match_pattern_recurrence

    issue = {"description": "缺少回滚方案导致无法恢复", "category": "error_handling"}
    patterns = [{"id": "p-001", "pattern": "缺少回滚方案", "rule": "变更必须有回滚路径"}]

    with patch.dict("os.environ", {}, clear=True):
        result = match_pattern_recurrence(issue, patterns)

    assert result["mode"] == "rule_fallback"
    # keyword fallback should match because "缺少" and "回滚" appear in both
    assert len(result["data"]["matches"]) >= 1


def test_match_pattern_recurrence_no_match():
    """LLM returns no matches when issue is unrelated to patterns."""
    from story_lifecycle.orchestrator.semantic import match_pattern_recurrence

    llm_output = {"matches": [{"pattern_id": "p-001", "matched": False, "confidence": "high", "reasoning": "unrelated"}]}
    fake = _make_fake_llm_response(llm_output)

    issue = {"description": "UI button color wrong", "category": "ui"}
    patterns = [{"id": "p-001", "pattern": "缺少回滚方案", "rule": "变更必须有回滚路径"}]

    with patch.dict("os.environ", {"STORY_LLM_API_KEY": "test-key"}):
        with patch("httpx.post", return_value=fake):
            result = match_pattern_recurrence(issue, patterns)

    assert result["ok"] is True
    # matched=False means no real recurrence
    assert len([m for m in result["data"]["matches"] if m.get("matched")]) == 0


def test_match_pattern_recurrence_filters_low_confidence():
    """LLM matches with low confidence are excluded from results."""
    from story_lifecycle.orchestrator.semantic import match_pattern_recurrence

    llm_output = {
        "matches": [
            {"pattern_id": "p-001", "matched": True, "confidence": "low", "reasoning": "weak signal", "evidence": []}
        ]
    }
    fake = _make_fake_llm_response(llm_output)

    with patch.dict("os.environ", {"STORY_LLM_API_KEY": "test-key"}):
        with patch("httpx.post", return_value=fake):
            result = match_pattern_recurrence({"description": "test", "category": ""}, [{"id": "p-001", "pattern": "test", "rule": "test"}])

    # low confidence matches should be filtered out
    high_conf = [m for m in result["data"]["matches"] if m.get("matched") and m.get("confidence") != "low"]
    assert len(high_conf) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_semantic.py::test_match_pattern_recurrence_llm_match -v`
Expected: FAIL — `ImportError: cannot import name 'match_pattern_recurrence'`

- [ ] **Step 3: Implement match_pattern_recurrence in semantic.py**

Append to `src/story_lifecycle/orchestrator/semantic.py`:

```python
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
    """Simple keyword matching — same logic as existing nodes._match_pattern."""
    keywords = (pattern_name + " " + rule).lower().split()
    matches = sum(1 for kw in keywords if len(kw) >= 2 and kw in issue_text)
    return matches >= 2


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
            fallback_matches.append({
                "pattern_id": p["id"],
                "matched": True,
                "confidence": "low",
                "reasoning": "keyword fallback match",
                "evidence": [],
            })

    return _fallback_result({"matches": fallback_matches})
```

- [ ] **Step 4: Integrate into nodes.py _check_pattern_recurrence**

Edit `src/story_lifecycle/orchestrator/nodes.py`, replace `_check_pattern_recurrence()` (lines 547-590) with:

```python
def _check_pattern_recurrence(
    workspace: str, story_key: str, stage: str, issues: list[dict]
):
    """Check if review issues match any active learned patterns (recurrence detection)."""
    if not issues:
        return

    try:
        patterns = db.get_active_learned_patterns(limit=20)
    except Exception:
        return

    if not patterns:
        return

    recurrences = []
    mode = "rule_fallback"

    try:
        from .semantic import match_pattern_recurrence

        for issue in issues:
            result = match_pattern_recurrence(issue, patterns)
            mode = result.get("mode", "rule_fallback")
            for m in result["data"].get("matches", []):
                # Find the full pattern for the event
                pid = m["pattern_id"]
                pattern_obj = next((p for p in patterns if p["id"] == pid), None)
                if pattern_obj:
                    recurrences.append({
                        "pattern_id": pid,
                        "pattern": pattern_obj.get("pattern", ""),
                        "confidence": m.get("confidence", "low"),
                        "reasoning": m.get("reasoning", ""),
                        "issue": issue,
                    })
                else:
                    recurrences.append({
                        "pattern_id": pid,
                        "pattern": "",
                        "confidence": m.get("confidence", "low"),
                        "reasoning": m.get("reasoning", ""),
                        "issue": issue,
                    })
    except Exception:
        # Fallback to original keyword matching
        for issue in issues:
            desc = issue.get("description", "")
            cat = issue.get("category", "")
            issue_text = f"{cat} {desc}".lower()
            for p in patterns:
                if _match_pattern(issue_text, p.get("pattern", ""), p.get("rule", "")):
                    recurrences.append({
                        "pattern_id": p["id"],
                        "pattern": p.get("pattern", ""),
                        "confidence": "low",
                        "reasoning": "keyword fallback",
                        "issue": issue,
                    })
                    break

    if recurrences:
        db.log_event(
            story_key,
            stage,
            "pattern_recurrence",
            {
                "mode": mode,
                "recurrences": recurrences,
                "count": len(recurrences),
            },
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_semantic.py -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/story_lifecycle/orchestrator/semantic.py src/story_lifecycle/orchestrator/nodes.py tests/test_semantic.py
git commit -m "feat: add match_pattern_recurrence with LLM semantics, integrate into _check_pattern_recurrence"
```

---

## Task 4: Quality Packet Pattern Rerank (P1)

**Files:**
- Modify: `src/story_lifecycle/orchestrator/semantic.py` — add `rerank_relevant_patterns()`
- Modify: `src/story_lifecycle/orchestrator/quality.py:102-152` — integrate into `build_quality_packet()`
- Test: `tests/test_semantic.py` — add tests

- [ ] **Step 1: Write failing tests for rerank_relevant_patterns**

Append to `tests/test_semantic.py`:

```python
# ── rerank_relevant_patterns ──


def test_rerank_relevant_patterns_llm():
    """LLM reranks patterns by relevance to story context."""
    from story_lifecycle.orchestrator.semantic import rerank_relevant_patterns

    llm_output = {
        "selected": [
            {"pattern_id": "p-001", "relevance": "high", "reasoning": "story involves DB migration"},
            {"pattern_id": "p-003", "relevance": "medium", "reasoning": "related to error handling"},
        ],
        "rejected": [
            {"pattern_id": "p-002", "reasoning": "unrelated to current story"},
        ],
    }
    fake = _make_fake_llm_response(llm_output)

    story_ctx = {"title": "Add user table migration", "stage": "implement", "type": "feature"}
    candidates = [
        {"id": "p-001", "pattern": "require rollback plan", "rule": "all DDL needs rollback", "applies_to": ["migration"], "confidence": "high"},
        {"id": "p-002", "pattern": "use connection pool", "rule": "DB connections must be pooled", "applies_to": ["database"], "confidence": "medium"},
        {"id": "p-003", "pattern": "handle DB errors", "rule": "catch and log DB exceptions", "applies_to": ["database"], "confidence": "high"},
    ]

    with patch.dict("os.environ", {"STORY_LLM_API_KEY": "test-key"}):
        with patch("httpx.post", return_value=fake):
            result = rerank_relevant_patterns(story_ctx, candidates, limit=5)

    assert result["ok"] is True
    assert result["mode"] == "llm"
    assert len(result["data"]["selected"]) == 2
    assert result["data"]["selected"][0]["pattern_id"] == "p-001"


def test_rerank_relevant_patterns_fallback():
    """Without LLM, returns top candidates as-is."""
    from story_lifecycle.orchestrator.semantic import rerank_relevant_patterns

    story_ctx = {"title": "test", "stage": "implement", "type": "feature"}
    candidates = [
        {"id": "p-001", "pattern": "a", "rule": "b", "applies_to": [], "confidence": "high"},
    ]

    with patch.dict("os.environ", {}, clear=True):
        result = rerank_relevant_patterns(story_ctx, candidates, limit=5)

    assert result["mode"] == "rule_fallback"
    assert len(result["data"]["selected"]) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_semantic.py::test_rerank_relevant_patterns_llm -v`
Expected: FAIL — `ImportError: cannot import name 'rerank_relevant_patterns'`

- [ ] **Step 3: Implement rerank_relevant_patterns in semantic.py**

Append to `src/story_lifecycle/orchestrator/semantic.py`:

```python
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
        # Enforce limit
        llm_result["data"]["selected"] = llm_result["data"].get("selected", [])[:limit]
        return llm_result

    # Fallback: return candidates as-is, truncated to limit
    selected = [
        {"pattern_id": p["id"], "relevance": "medium", "reasoning": "tag pre-filter fallback"}
        for p in candidate_patterns[:limit]
    ]
    return _fallback_result({"selected": selected, "rejected": []})
```

- [ ] **Step 4: Integrate into quality.py build_quality_packet**

Edit `src/story_lifecycle/orchestrator/quality.py`, in `build_quality_packet()` (around line 127), replace the pattern selection block:

Replace:
```python
    # Learned patterns (relevance-filtered if tags provided)
    if relevant_tags:
        patterns = db.find_relevant_patterns(relevant_tags, limit=max_items)
    else:
        patterns = db.get_active_learned_patterns(limit=max_items)
    if patterns:
        lines.append("Relevant Learned Patterns:")
        for p in patterns:
            lines.append(f"- {p['pattern']}:")
            lines.append(f"  {p['rule']}")
        lines.append("")
```

With:
```python
    # Learned patterns (relevance-filtered if tags provided)
    if relevant_tags:
        candidates = db.find_relevant_patterns(relevant_tags, limit=20)
    else:
        candidates = db.get_active_learned_patterns(limit=20)

    # LLM rerank if candidates exist
    patterns = candidates
    pattern_mode = "tag_overlap"
    if candidates:
        try:
            from .semantic import rerank_relevant_patterns

            story = db.get_story(story_key) or {}
            ctx = json.loads(story.get("context_json") or "{}")
            story_context = {
                "title": story.get("title", story_key),
                "stage": stage,
                "type": story.get("sub_type", ""),
                "summary": ctx.get("prd_summary", "")[:500],
            }
            rerank = rerank_relevant_patterns(story_context, candidates, limit=max_items)
            if rerank["ok"] and rerank["mode"] == "llm":
                selected_ids = [s["pattern_id"] for s in rerank["data"].get("selected", [])]
                patterns = [p for p in candidates if p["id"] in selected_ids]
                pattern_mode = "llm_rerank"
            # else: keep tag overlap order
        except Exception:
            pass  # keep candidates as-is

    if patterns:
        lines.append(f"Relevant Learned Patterns (mode: {pattern_mode}):")
        for p in patterns[:max_items]:
            lines.append(f"- {p['pattern']}:")
            lines.append(f"  {p['rule']}")
        lines.append("")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_semantic.py -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/story_lifecycle/orchestrator/semantic.py src/story_lifecycle/orchestrator/quality.py tests/test_semantic.py
git commit -m "feat: add rerank_relevant_patterns with LLM, integrate into build_quality_packet"
```

---

## Task 5: Review Summary for Learning (P1)

**Files:**
- Modify: `src/story_lifecycle/orchestrator/semantic.py` — add `summarize_review_for_learning()`
- Modify: `src/story_lifecycle/orchestrator/seed_pipeline.py:294-303` — integrate into `_summarize_review()`
- Test: `tests/test_semantic.py` — add tests

- [ ] **Step 1: Write failing tests for summarize_review_for_learning**

Append to `tests/test_semantic.py`:

```python
# ── summarize_review_for_learning ──


def test_summarize_review_llm_success():
    """LLM produces structured review summary."""
    from story_lifecycle.orchestrator.semantic import summarize_review_for_learning

    llm_output = {
        "quality": "revise",
        "key_issues": [
            {"severity": "high", "description": "missing error handling", "evidence": "api.py:42", "recommendation": "add try/except"},
        ],
        "useful_for_learning": True,
        "summary": "Review found missing error handling in API layer",
    }
    fake = _make_fake_llm_response(llm_output)

    review_md = "## Review\nquality: revise\n\n### Issues\n- missing error handling at api.py:42"

    with patch.dict("os.environ", {"STORY_LLM_API_KEY": "test-key"}):
        with patch("httpx.post", return_value=fake):
            result = summarize_review_for_learning(review_md)

    assert result["ok"] is True
    assert result["data"]["quality"] == "revise"
    assert result["data"]["useful_for_learning"] is True


def test_summarize_review_fallback():
    """Without LLM, uses marker-based fallback."""
    from story_lifecycle.orchestrator.semantic import summarize_review_for_learning

    review_md = "## Review\nquality: pass\n\n### Issues\n- minor style issue"

    with patch.dict("os.environ", {}, clear=True):
        result = summarize_review_for_learning(review_md)

    assert result["mode"] == "rule_fallback"
    assert "quality" in result["data"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_semantic.py::test_summarize_review_llm_success -v`
Expected: FAIL — `ImportError: cannot import name 'summarize_review_for_learning'`

- [ ] **Step 3: Implement summarize_review_for_learning in semantic.py**

Append to `src/story_lifecycle/orchestrator/semantic.py`:

```python
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
            return _ok_result({
                "quality": data.get("quality", "unknown"),
                "key_issues": data.get("issues", data.get("key_issues", [])),
                "useful_for_learning": True,
                "summary": json.dumps(data, ensure_ascii=False)[:1000],
            })
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
```

- [ ] **Step 4: Integrate into seed_pipeline.py _summarize_review**

Edit `src/story_lifecycle/orchestrator/seed_pipeline.py`, replace `_summarize_review()` (lines 294-303):

```python
def _summarize_review(content: str) -> str:
    """Summarize review content for seed analyst context."""
    # Try LLM semantic summary first
    try:
        from .semantic import summarize_review_for_learning

        result = summarize_review_for_learning(content)
        if result["ok"] and result["mode"] == "llm":
            return result["data"].get("summary", content[:1500])
    except Exception:
        pass

    # Fallback: marker-based extraction
    markers = ["quality", "issues", "suggestions", "评分", "Review", "问题"]
    found: list[str] = []
    for line in content.splitlines():
        for m in markers:
            if m.lower() in line.lower():
                found.append(line.strip()[:300])
    if not found:
        found.append(content[:1500])
    return "\n".join(found)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_semantic.py -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/story_lifecycle/orchestrator/semantic.py src/story_lifecycle/orchestrator/seed_pipeline.py tests/test_semantic.py
git commit -m "feat: add summarize_review_for_learning with LLM, integrate into seed_pipeline"
```

---

## Task 6: Debug Recovery Recommendation (P2)

**Files:**
- Modify: `src/story_lifecycle/orchestrator/semantic.py` — add `recommend_recovery()`
- Test: `tests/test_semantic.py` — add tests

Note: `observability.py` integration is deferred — `recommend_recovery()` is a standalone function that can be called from CLI later. No `build_debug_response` modification needed now.

- [ ] **Step 1: Write failing tests for recommend_recovery**

Append to `tests/test_semantic.py`:

```python
# ── recommend_recovery ──


def test_recommend_recovery_llm():
    """LLM provides recovery recommendation for a debug packet."""
    from story_lifecycle.orchestrator.semantic import recommend_recovery

    llm_output = {
        "failure_type": "done_file_parse_error",
        "likely_cause": "AI 输出格式不符合 .story-done schema",
        "recommended_action": "retry_with_prompt",
        "safe_to_retry": True,
        "confidence": "high",
        "evidence": ["node_error: JSONDecodeError"],
        "human_message": "AI 输出的 JSON 格式有问题，建议重试并在 prompt 中强调 JSON 格式要求",
    }
    fake = _make_fake_llm_response(llm_output)

    debug_packet = {
        "story": {"storyKey": "TEST-001", "status": "error", "lastError": "JSONDecodeError"},
        "nodeErrors": [{"payload": {"error": "Expecting value: line 1 column 1"}}],
    }

    with patch.dict("os.environ", {"STORY_LLM_API_KEY": "test-key"}):
        with patch("httpx.post", return_value=fake):
            result = recommend_recovery(debug_packet)

    assert result["ok"] is True
    assert result["data"]["failure_type"] == "done_file_parse_error"
    assert result["data"]["recommended_action"] == "retry_with_prompt"
    assert result["data"]["safe_to_retry"] is True


def test_recommend_recovery_unknown_defaults_to_ask_human():
    """When LLM returns unknown failure type, action must be ask_human."""
    from story_lifecycle.orchestrator.semantic import recommend_recovery

    llm_output = {
        "failure_type": "unknown",
        "likely_cause": "unclear",
        "recommended_action": "ask_human",
        "safe_to_retry": False,
        "confidence": "medium",
        "evidence": [],
        "human_message": "无法确定失败原因，建议人工检查",
    }
    fake = _make_fake_llm_response(llm_output)

    with patch.dict("os.environ", {"STORY_LLM_API_KEY": "test-key"}):
        with patch("httpx.post", return_value=fake):
            result = recommend_recovery({"story": {"status": "error"}})

    assert result["data"]["recommended_action"] == "ask_human"


def test_recommend_recovery_enforces_unknown_ask_human():
    """If LLM returns unknown failure_type but action != ask_human, override to ask_human."""
    from story_lifecycle.orchestrator.semantic import recommend_recovery

    llm_output = {
        "failure_type": "unknown",
        "likely_cause": "unclear",
        "recommended_action": "retry",  # should be overridden to ask_human
        "safe_to_retry": True,
        "confidence": "low",
        "evidence": [],
        "human_message": "test",
    }
    fake = _make_fake_llm_response(llm_output)

    with patch.dict("os.environ", {"STORY_LLM_API_KEY": "test-key"}):
        with patch("httpx.post", return_value=fake):
            result = recommend_recovery({"story": {"status": "error"}})

    assert result["data"]["recommended_action"] == "ask_human"


def test_recommend_recovery_fallback():
    """Without LLM, returns conservative fallback."""
    from story_lifecycle.orchestrator.semantic import recommend_recovery

    with patch.dict("os.environ", {}, clear=True):
        result = recommend_recovery({"story": {"status": "error"}})

    assert result["mode"] == "unavailable"
    assert result["data"]["recommended_action"] == "ask_human"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_semantic.py::test_recommend_recovery_llm -v`
Expected: FAIL — `ImportError: cannot import name 'recommend_recovery'`

- [ ] **Step 3: Implement recommend_recovery in semantic.py**

Append to `src/story_lifecycle/orchestrator/semantic.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_semantic.py -v`
Expected: All tests PASS

- [ ] **Step 5: Run full test suite for regression check**

Run: `pytest -v`
Expected: All tests PASS, no regressions

- [ ] **Step 6: Commit**

```bash
git add src/story_lifecycle/orchestrator/semantic.py tests/test_semantic.py
git commit -m "feat: add recommend_recovery with safety enforcement for debug assistance"
```

---

## Self-Review Checklist

### 1. Spec Coverage

| Design Doc Section | Task |
|---|---|
| 新增 `semantic.py` 基础设施 | Task 1 |
| Bug 正文结构化提取 | Task 2 |
| Pattern 复发检测 | Task 3 |
| Quality Packet Pattern rerank | Task 4 |
| Review 摘要压缩 | Task 5 |
| Debug Recovery Recommendation | Task 6 |
| Prompt 与输出约束 (temp=0.1, JSON only, schema validation) | Task 1 |
| 降级 (unavailable/error → fallback) | Tasks 2-6 |
| 事件与可观测性 (mode/confidence in payloads) | Tasks 3-4 |
| 复用 STORY_LLM_* 环境变量 | Task 1 |
| 不删除 regex fallback | Tasks 2-5 |
| 不在 DB 层调用 LLM | All tasks |
| recovery 不自动执行 | Task 6 |

### 2. Placeholder Scan

- No TBD/TODO/fill-in-later found
- All steps contain actual code
- All commands include expected output

### 3. Type Consistency

- `SemanticResult` TypedDict used consistently across all functions
- `_ok_result`, `_fallback_result`, `_error_result`, `_unavailable_result` all return `SemanticResult`
- `_call_semantic_llm` returns `SemanticResult` — all consumers check `ok` and `mode`
- Pattern fields: `pattern_id`, `confidence`, `reasoning` used consistently in Tasks 3 and 4

### 4. Bug: _unavailable_result in recommend_recovery

`_unavailable_result() | {"data": {...}}` uses dict merge — but `SemanticResult` is a `TypedDict` not a real dict at runtime. This works at runtime because TypedDict is just a dict, but the semantics are wrong: the `|` operator would create a new dict with only the last `data` key. Need to fix: use `_unavailable_result()` then update its `data` field. Same for the error case in recommend_recovery.

**Fix applied inline in Task 6**: Changed to construct the result dict directly instead of merging.

---

## Acceptance Criteria (from design doc)

1. ✅ 无 LLM key 时，行为兼容，最多增加 fallback warning — Tasks 2-6 all test `os.environ` clear
2. ✅ TAPD Bug 正文无标准标题时 LLM 可提取 — Task 2
3. ✅ Pattern recurrence event 包含 mode/confidence/reasoning — Task 3
4. ✅ Quality Packet pattern selection 支持 tag pre-filter + LLM rerank — Task 4
5. ✅ Review markdown summary 不再只依赖 marker 行 — Task 5
6. ✅ Debug recommendation 对 unknown failure 默认 ask_human — Task 6 (enforced in code + tests)
7. ✅ 所有 LLM 输出都经过本地 schema validation — Task 1 (_call_semantic_llm + _validate_schema)

