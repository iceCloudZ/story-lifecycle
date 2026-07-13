"""Tests for LLM-based task_type classification (BUG #16).

Validates that "Loan Disclosure 展示" (frontend) is no longer misclassified
as fund-flow by the keyword-first-match classifier — the LLM classifier
takes priority, with keywords as fallback.
"""

from unittest.mock import patch, MagicMock


def _fake_llm_json(return_value):
    """Build a mock get_llm whose invoke_json returns *return_value*."""
    mock = MagicMock()
    mock.invoke_json.return_value = return_value
    return mock


def test_llm_classifies_frontend_not_fund_flow():
    """'Loan Disclosure 展示' should be frontend, not fund-flow (the bug)."""
    from story_lifecycle.orchestrator.service.story_service import (
        _classify_task_type_llm,
    )

    # LLM correctly says frontend (关键词会误分 fund-flow)
    mock = _fake_llm_json({"task_type": "frontend"})
    with patch(
        "story_lifecycle.sourcing.planner.llm.get_llm", return_value=mock
    ):
        result = _classify_task_type_llm(
            "【HC】新增Loan Disclosure Statement展示+贷款协议更新",
            "前端展示组件 + 协议更新",
        )
    assert result == "frontend"


def test_llm_returns_none_falls_back_to_keywords():
    """When LLM returns None, caller falls back to keyword classifier."""
    from story_lifecycle.orchestrator.service.story_service import (
        _classify_task_type_llm,
    )
    from story_lifecycle.orchestrator.engine.prompt_sections import (
        classify_task_type,
    )

    mock = _fake_llm_json(None)
    with patch(
        "story_lifecycle.sourcing.planner.llm.get_llm", return_value=mock
    ):
        llm_result = _classify_task_type_llm("放款流程优化", "")
    assert llm_result is None
    # 关键词兜底应命中 fund-flow
    assert classify_task_type("放款流程优化", "") == "fund-flow"


def test_llm_invalid_value_returns_none():
    """LLM returns a value not in the controlled vocabulary → None."""
    from story_lifecycle.orchestrator.service.story_service import (
        _classify_task_type_llm,
    )

    mock = _fake_llm_json({"task_type": "some-made-up-type"})
    with patch(
        "story_lifecycle.sourcing.planner.llm.get_llm", return_value=mock
    ):
        result = _classify_task_type_llm("某需求", "")
    assert result is None


def test_llm_exception_returns_none():
    """LLM raises → _classify_task_type_llm returns None (no raise)."""
    from story_lifecycle.orchestrator.service.story_service import (
        _classify_task_type_llm,
    )

    mock = MagicMock()
    mock.invoke_json.side_effect = RuntimeError("network error")
    with patch(
        "story_lifecycle.sourcing.planner.llm.get_llm", return_value=mock
    ):
        result = _classify_task_type_llm("某需求", "")
    assert result is None


def test_full_create_uses_llm_classification(isolated_story_home):
    """create_and_start_story end-to-end: LLM result lands in context_json."""
    from story_lifecycle.orchestrator.service.story_service import (
        create_and_start_story,
    )
    from story_lifecycle.infra.db import models as db
    import json
    import tempfile

    db.init_db()
    mock = _fake_llm_json({"task_type": "frontend"})
    with patch(
        "story_lifecycle.sourcing.planner.llm.get_llm", return_value=mock
    ):
        with tempfile.TemporaryDirectory() as tmp:
            create_and_start_story(
                story_key="TEST-FRONTEND-001",
                title="新增Loan Disclosure展示",
                profile="minimal",
                workspace=tmp,
            )
            ctx = json.loads(
                db.get_story("TEST-FRONTEND-001")["context_json"] or "{}"
            )
    assert ctx.get("task_type") == "frontend"
