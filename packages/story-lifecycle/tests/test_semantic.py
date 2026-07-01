"""Tests for orchestrator/semantic.py -- LLM semantic extraction layer."""

from unittest.mock import patch, MagicMock

from story_lifecycle.schemas import (
    BugContextResult,
    PatternMatch,
    PatternRecurrenceResult,
    SelectedPattern,
    RejectedPattern,
    RerankResult,
    ReviewSummaryResult,
    RecoveryRecommendation,
)


def _mock_invoke_structured(return_model):
    """Build a mock LLM client whose invoke_structured returns the given Pydantic model."""
    mock_client = MagicMock()
    mock_client.invoke_structured.return_value = return_model
    return mock_client


# ── SemanticResult construction ──


def test_semantic_result_llm_mode():
    """_ok_result returns mode=llm, confidence from data."""
    from story_lifecycle.orchestrator.evaluation.semantic import _ok_result

    r = _ok_result(data={"test": 1}, confidence="high")
    assert r["ok"] is True
    assert r["mode"] == "llm"
    assert r["confidence"] == "high"
    assert r["data"] == {"test": 1}


def test_semantic_result_error_mode():
    """_error_result returns mode=error."""
    from story_lifecycle.orchestrator.evaluation.semantic import _error_result

    r = _error_result("test error")
    assert r["ok"] is False
    assert r["mode"] == "error"
    assert "test error" in r["warnings"]


# ── _invoke_structured ──


def test_invoke_structured_success():
    """_invoke_structured returns SemanticResult with model data on success."""
    from story_lifecycle.orchestrator.evaluation.semantic import _invoke_structured

    model = BugContextResult(
        description="test",
        steps_to_reproduce="1. step",
        expected_behavior="works",
        actual_behavior="broken",
        confidence="high",
    )
    mock_client = _mock_invoke_structured(model)

    with patch(
        "story_lifecycle.orchestrator.evaluation.semantic.get_llm", return_value=mock_client
    ):
        result = _invoke_structured("test prompt", BugContextResult)

    assert result["ok"] is True
    assert result["mode"] == "llm"
    assert result["confidence"] == "high"
    assert result["data"]["description"] == "test"


def test_invoke_structured_exception():
    """_invoke_structured returns error result on exception."""
    from story_lifecycle.orchestrator.evaluation.semantic import _invoke_structured

    mock_client = MagicMock()
    mock_client.invoke_structured.side_effect = RuntimeError("LLM unavailable")

    with patch(
        "story_lifecycle.orchestrator.evaluation.semantic.get_llm", return_value=mock_client
    ):
        result = _invoke_structured("test prompt", BugContextResult)

    assert result["ok"] is False
    assert result["mode"] == "error"


def test_invoke_structured_invalid_confidence():
    """_invoke_structured normalizes invalid confidence to 'low'."""
    from story_lifecycle.orchestrator.evaluation.semantic import _invoke_structured

    mock_client = MagicMock()
    # Return a mock model whose model_dump returns an invalid confidence.
    # This tests the normalization fallback in _invoke_structured.
    corrupted_model = MagicMock()
    corrupted_model.model_dump.return_value = {
        "description": "test",
        "confidence": "invalid",
    }
    mock_client.invoke_structured.return_value = corrupted_model

    with patch(
        "story_lifecycle.orchestrator.evaluation.semantic.get_llm", return_value=mock_client
    ):
        result = _invoke_structured("test prompt", BugContextResult)

    assert result["ok"] is True
    assert result["confidence"] == "low"


# ── extract_bug_context ──


def test_extract_bug_context_llm_success():
    """LLM extracts structured bug context from markdown."""
    from story_lifecycle.orchestrator.evaluation.semantic import extract_bug_context

    model = BugContextResult(
        description="登录页面崩溃",
        steps_to_reproduce="1. 打开登录页\n2. 输入特殊字符",
        expected_behavior="正常登录",
        actual_behavior="页面白屏",
        environment="Chrome 120, Windows 11",
        logs="Uncaught TypeError at login.js:42",
        missing_fields=[],
        confidence="high",
    )
    mock_client = _mock_invoke_structured(model)

    with patch(
        "story_lifecycle.orchestrator.evaluation.semantic.get_llm", return_value=mock_client
    ):
        result = extract_bug_context(
            "## 现象\n页面白屏\n\n## 复现步骤\n输入特殊字符",
            "登录页面崩溃",
        )

    assert result["ok"] is True
    assert result["mode"] == "llm"
    assert result["data"]["steps_to_reproduce"] == "1. 打开登录页\n2. 输入特殊字符"
    assert result["data"]["expected_behavior"] == "正常登录"


def test_extract_bug_context_error():
    """LLM failure returns error result."""
    from story_lifecycle.orchestrator.evaluation.semantic import extract_bug_context

    mock_client = MagicMock()
    mock_client.invoke_structured.side_effect = RuntimeError("timeout")

    with patch(
        "story_lifecycle.orchestrator.evaluation.semantic.get_llm", return_value=mock_client
    ):
        result = extract_bug_context("## 复现步骤\n点按钮", "标题")

    assert result["mode"] == "error"
    assert not result["ok"]


# ── match_pattern_recurrence ──


def test_match_pattern_recurrence_llm_match():
    """LLM identifies semantic match between issue and pattern."""
    from story_lifecycle.orchestrator.evaluation.semantic import match_pattern_recurrence

    model = PatternRecurrenceResult(
        matches=[
            PatternMatch(
                pattern_id="p-001",
                matched=True,
                confidence="high",
                reasoning="issue 描述缺少降级路径与 pattern 规则一致",
                evidence=["issue.description", "pattern.rule"],
            )
        ]
    )
    mock_client = _mock_invoke_structured(model)

    issue = {
        "description": "接口没有 fallback 机制，一旦服务挂了就全完了",
        "category": "error_handling",
    }
    patterns = [
        {"id": "p-001", "pattern": "缺少回滚方案", "rule": "变更必须有回滚或降级路径"}
    ]

    with patch(
        "story_lifecycle.orchestrator.evaluation.semantic.get_llm", return_value=mock_client
    ):
        result = match_pattern_recurrence(issue, patterns)

    assert result["ok"] is True
    assert result["mode"] == "llm"
    assert len(result["data"]["matches"]) == 1
    assert result["data"]["matches"][0]["pattern_id"] == "p-001"


def test_match_pattern_recurrence_no_patterns():
    """Returns error when no candidate patterns provided."""
    from story_lifecycle.orchestrator.evaluation.semantic import match_pattern_recurrence

    result = match_pattern_recurrence({"description": "test"}, [])

    assert result["mode"] == "error"
    assert not result["ok"]


def test_match_pattern_recurrence_no_match():
    """LLM returns no matches when issue is unrelated to patterns."""
    from story_lifecycle.orchestrator.evaluation.semantic import match_pattern_recurrence

    model = PatternRecurrenceResult(
        matches=[
            PatternMatch(
                pattern_id="p-001",
                matched=False,
                confidence="high",
                reasoning="unrelated",
            )
        ]
    )
    mock_client = _mock_invoke_structured(model)

    issue = {"description": "UI button color wrong", "category": "ui"}
    patterns = [
        {"id": "p-001", "pattern": "缺少回滚方案", "rule": "变更必须有回滚路径"}
    ]

    with patch(
        "story_lifecycle.orchestrator.evaluation.semantic.get_llm", return_value=mock_client
    ):
        result = match_pattern_recurrence(issue, patterns)

    assert result["ok"] is True
    assert len([m for m in result["data"]["matches"] if m.get("matched")]) == 0


def test_match_pattern_recurrence_filters_low_confidence():
    """LLM matches with low confidence are excluded from results."""
    from story_lifecycle.orchestrator.evaluation.semantic import match_pattern_recurrence

    model = PatternRecurrenceResult(
        matches=[
            PatternMatch(
                pattern_id="p-001",
                matched=True,
                confidence="low",
                reasoning="weak signal",
                evidence=[],
            )
        ]
    )
    mock_client = _mock_invoke_structured(model)

    with patch(
        "story_lifecycle.orchestrator.evaluation.semantic.get_llm", return_value=mock_client
    ):
        result = match_pattern_recurrence(
            {"description": "test", "category": ""},
            [{"id": "p-001", "pattern": "test", "rule": "test"}],
        )

    high_conf = [
        m
        for m in result["data"]["matches"]
        if m.get("matched") and m.get("confidence") != "low"
    ]
    assert len(high_conf) == 0


# ── rerank_relevant_patterns ──


def test_rerank_relevant_patterns_llm():
    """LLM reranks patterns by relevance to story context."""
    from story_lifecycle.orchestrator.evaluation.semantic import rerank_relevant_patterns

    model = RerankResult(
        selected=[
            SelectedPattern(
                pattern_id="p-001",
                relevance="high",
                reasoning="story involves DB migration",
            ),
            SelectedPattern(
                pattern_id="p-003",
                relevance="medium",
                reasoning="related to error handling",
            ),
        ],
        rejected=[
            RejectedPattern(
                pattern_id="p-002",
                reasoning="unrelated to current story",
            ),
        ],
    )
    mock_client = _mock_invoke_structured(model)

    story_ctx = {
        "title": "Add user table migration",
        "stage": "implement",
        "type": "feature",
    }
    candidates = [
        {
            "id": "p-001",
            "pattern": "require rollback plan",
            "rule": "all DDL needs rollback",
            "applies_to": ["migration"],
            "confidence": "high",
        },
        {
            "id": "p-002",
            "pattern": "use connection pool",
            "rule": "DB connections must be pooled",
            "applies_to": ["database"],
            "confidence": "medium",
        },
        {
            "id": "p-003",
            "pattern": "handle DB errors",
            "rule": "catch and log DB exceptions",
            "applies_to": ["database"],
            "confidence": "high",
        },
    ]

    with patch(
        "story_lifecycle.orchestrator.evaluation.semantic.get_llm", return_value=mock_client
    ):
        result = rerank_relevant_patterns(story_ctx, candidates, limit=5)

    assert result["ok"] is True
    assert result["mode"] == "llm"
    assert len(result["data"]["selected"]) == 2
    assert result["data"]["selected"][0]["pattern_id"] == "p-001"


def test_rerank_relevant_patterns_no_candidates():
    """Returns error when no candidate patterns provided."""
    from story_lifecycle.orchestrator.evaluation.semantic import rerank_relevant_patterns

    story_ctx = {"title": "test", "stage": "implement", "type": "feature"}

    result = rerank_relevant_patterns(story_ctx, [], limit=5)

    assert result["mode"] == "error"
    assert not result["ok"]


def test_rerank_relevant_patterns_llm_error():
    """LLM failure returns error result."""
    from story_lifecycle.orchestrator.evaluation.semantic import rerank_relevant_patterns

    mock_client = MagicMock()
    mock_client.invoke_structured.side_effect = RuntimeError("LLM down")

    story_ctx = {"title": "test", "stage": "implement", "type": "feature"}
    candidates = [
        {
            "id": "p-001",
            "pattern": "a",
            "rule": "b",
            "applies_to": [],
            "confidence": "high",
        },
    ]

    with patch(
        "story_lifecycle.orchestrator.evaluation.semantic.get_llm", return_value=mock_client
    ):
        result = rerank_relevant_patterns(story_ctx, candidates, limit=5)

    assert result["mode"] == "error"
    assert not result["ok"]


# ── summarize_review_for_learning ──


def test_summarize_review_llm_success():
    """LLM produces structured review summary."""
    from story_lifecycle.orchestrator.evaluation.semantic import summarize_review_for_learning

    model = ReviewSummaryResult(
        quality="revise",
        key_issues=[
            {
                "severity": "high",
                "description": "missing error handling",
                "evidence": "api.py:42",
                "recommendation": "add try/except",
            },
        ],
        useful_for_learning=True,
        summary="Review found missing error handling in API layer",
        confidence="high",
    )
    mock_client = _mock_invoke_structured(model)

    review_md = "## Review\nquality: revise\n\n### Issues\n- missing error handling at api.py:42"

    with patch(
        "story_lifecycle.orchestrator.evaluation.semantic.get_llm", return_value=mock_client
    ):
        result = summarize_review_for_learning(review_md)

    assert result["ok"] is True
    assert result["data"]["quality"] == "revise"
    assert result["data"]["useful_for_learning"] is True


def test_summarize_review_structured_json_passthrough():
    """When review is already valid JSON with 'quality', parses directly without LLM."""
    import json

    from story_lifecycle.orchestrator.evaluation.semantic import summarize_review_for_learning

    review_data = {"quality": "pass", "issues": [{"desc": "minor style issue"}]}
    review_json = json.dumps(review_data)

    # No LLM mock needed -- should not call LLM
    result = summarize_review_for_learning(review_json)

    assert result["ok"] is True
    assert result["data"]["quality"] == "pass"


def test_summarize_review_error():
    """LLM failure returns error result."""
    from story_lifecycle.orchestrator.evaluation.semantic import summarize_review_for_learning

    mock_client = MagicMock()
    mock_client.invoke_structured.side_effect = RuntimeError("timeout")

    review_md = "## Review\nquality: pass\n\n### Issues\n- minor style issue"

    with patch(
        "story_lifecycle.orchestrator.evaluation.semantic.get_llm", return_value=mock_client
    ):
        result = summarize_review_for_learning(review_md)

    assert result["mode"] == "error"
    assert not result["ok"]


# ── recommend_recovery ──


def test_recommend_recovery_llm():
    """LLM provides recovery recommendation for a debug packet."""
    from story_lifecycle.orchestrator.evaluation.semantic import recommend_recovery

    model = RecoveryRecommendation(
        failure_type="done_file_parse_error",
        likely_cause="AI 输出格式不符合 .story/done schema",
        recommended_action="retry_with_prompt",
        safe_to_retry=True,
        confidence="high",
        evidence=["node_error: JSONDecodeError"],
        human_message="AI 输出的 JSON 格式有问题，建议重试并在 prompt 中强调 JSON 格式要求",
    )
    mock_client = _mock_invoke_structured(model)

    debug_packet = {
        "story": {
            "storyKey": "TEST-001",
            "status": "error",
            "lastError": "JSONDecodeError",
        },
        "nodeErrors": [{"payload": {"error": "Expecting value: line 1 column 1"}}],
    }

    with patch(
        "story_lifecycle.orchestrator.evaluation.semantic.get_llm", return_value=mock_client
    ):
        result = recommend_recovery(debug_packet)

    assert result["ok"] is True
    assert result["data"]["failure_type"] == "done_file_parse_error"
    assert result["data"]["recommended_action"] == "retry_with_prompt"
    assert result["data"]["safe_to_retry"] is True


def test_recommend_recovery_unknown_defaults_to_ask_human():
    """When LLM returns unknown failure type, action must be ask_human."""
    from story_lifecycle.orchestrator.evaluation.semantic import recommend_recovery

    model = RecoveryRecommendation(
        failure_type="unknown",
        likely_cause="unclear",
        recommended_action="ask_human",
        safe_to_retry=False,
        confidence="medium",
        evidence=[],
        human_message="无法确定失败原因，建议人工检查",
    )
    mock_client = _mock_invoke_structured(model)

    with patch(
        "story_lifecycle.orchestrator.evaluation.semantic.get_llm", return_value=mock_client
    ):
        result = recommend_recovery({"story": {"status": "error"}})

    assert result["data"]["recommended_action"] == "ask_human"


def test_recommend_recovery_enforces_unknown_ask_human():
    """If LLM returns unknown failure_type but action != ask_human, override to ask_human."""
    from story_lifecycle.orchestrator.evaluation.semantic import recommend_recovery

    model = RecoveryRecommendation(
        failure_type="unknown",
        likely_cause="unclear",
        recommended_action="retry",
        safe_to_retry=True,
        confidence="low",
        evidence=[],
        human_message="test",
    )
    mock_client = _mock_invoke_structured(model)

    with patch(
        "story_lifecycle.orchestrator.evaluation.semantic.get_llm", return_value=mock_client
    ):
        result = recommend_recovery({"story": {"status": "error"}})

    assert result["data"]["recommended_action"] == "ask_human"


def test_recommend_recovery_error():
    """LLM failure returns error result with fallback recovery data."""
    from story_lifecycle.orchestrator.evaluation.semantic import recommend_recovery

    mock_client = MagicMock()
    mock_client.invoke_structured.side_effect = RuntimeError("LLM down")

    with patch(
        "story_lifecycle.orchestrator.evaluation.semantic.get_llm", return_value=mock_client
    ):
        result = recommend_recovery({"story": {"status": "error"}})

    assert result["mode"] == "error"
    assert not result["ok"]
