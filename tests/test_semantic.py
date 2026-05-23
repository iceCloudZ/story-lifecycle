"""Tests for orchestrator/semantic.py — LLM semantic extraction layer."""

import json
from unittest.mock import patch, MagicMock


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
    mock.json.return_value = {"choices": [{"message": {"content": raw}}]}
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
        with patch(
            "httpx.post",
            side_effect=httpx.HTTPStatusError(
                "503", request=MagicMock(), response=MagicMock()
            ),
        ):
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
            result = extract_bug_context(
                "## 现象\n页面白屏\n\n## 复现步骤\n输入特殊字符", "登录页面崩溃"
            )

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
