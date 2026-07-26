"""2.2 — 调度点① 边界纯判定 LLM 测试。

设计依据:DESIGN §4.2 / 评审 B / 红线 1。纯判定函数,非 agentic(无工具)。
approve/reject/escalate;reject 上限;LLM 不可用 fallback approve(confirm 兜底)。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from story_lifecycle.infra.db import models as db
from story_lifecycle.orchestrator.evaluation.boundary_judge import judge_boundary
from story_lifecycle.orchestrator.evaluation.reject_budget import REJECT_LIMIT


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("STORY_HOME", str(tmp_path / ".story-home"))
    db.init_db()
    db.create_story("BJ-1", "t", str(tmp_path), profile="minimal", current_stage="design")


def _mock_llm(decision: str, reason: str = "", verdict: str = "pass"):
    """Mock LLM:返回固定的 BoundaryDecision。"""
    from story_lifecycle.orchestrator.evaluation.boundary_judge import (
        BoundaryDecision,
    )

    llm = MagicMock()
    llm.api_key = "fake"
    llm.model = "test-model"
    llm.invoke_structured.return_value = BoundaryDecision(
        decision=decision, verdict=verdict, reason=reason, findings=[]
    )
    return llm


# ---- approve ----


def test_approve_when_quality_good(tmp_path):
    """成果物质量好 → approve。"""
    llm = _mock_llm("approve", "spec 完整覆盖 PRD")
    result = judge_boundary(
        story_key="BJ-1", stage="design", workspace=str(tmp_path),
        ctx={}, artifacts=["story/spec.md"], llm=llm,
    )
    assert result["decision"] == "approve"
    assert "spec 完整" in result["reason"]
    # 落了 orchestrator_decision
    decisions = db.get_decisions("BJ-1", "design")
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "approve"


# ---- reject ----


def test_reject_with_reason(tmp_path):
    """明显缺陷 → reject 带理由。"""
    llm = _mock_llm("reject", "spec 漏了错误处理设计", verdict="rework")
    result = judge_boundary(
        story_key="BJ-1", stage="design", workspace=str(tmp_path),
        ctx={}, artifacts=["story/spec.md"], llm=llm,
    )
    assert result["decision"] == "reject"
    assert "错误处理" in result["reason"]
    assert result["verdict"] == "rework"


def test_reject_does_not_repeat_reason_passes_budget(tmp_path):
    """reject 理由与上次不同 → 允许(给重试机会)。"""
    db.log_decision("BJ-1", "design", "boundary_judge", "reject", reason="上次理由A")
    llm = _mock_llm("reject", "这次理由B完全不同")
    result = judge_boundary(
        story_key="BJ-1", stage="design", workspace=str(tmp_path),
        ctx={}, artifacts=["story/spec.md"], llm=llm,
    )
    assert result["decision"] == "reject"


def test_reject_at_limit_forces_escalate(tmp_path):
    """reject 到上限 → 强制 escalate(防打回循环)。"""
    for i in range(REJECT_LIMIT):
        db.log_decision(
            "BJ-1", "design", "boundary_judge", "reject", reason=f"理由 {i}"
        )
    llm = _mock_llm("reject", "新的不同理由")
    result = judge_boundary(
        story_key="BJ-1", stage="design", workspace=str(tmp_path),
        ctx={}, artifacts=["story/spec.md"], llm=llm,
    )
    assert result["decision"] == "escalate"
    assert "超上限" in result["reason"] or "防打回" in result["reason"]


def test_reject_repeated_reason_forces_escalate(tmp_path):
    """reject 理由与上次重复 → 强制 escalate(评审 A2)。"""
    db.log_decision(
        "BJ-1", "design", "boundary_judge", "reject", reason="spec 不够详细"
    )
    llm = _mock_llm("reject", "spec 不够详细")  # 同理由
    result = judge_boundary(
        story_key="BJ-1", stage="design", workspace=str(tmp_path),
        ctx={}, artifacts=["story/spec.md"], llm=llm,
    )
    assert result["decision"] == "escalate"
    assert "重复" in result["reason"] or "防打回" in result["reason"]


# ---- escalate ----


def test_escalate_passes_through(tmp_path):
    """LLM 直接判 escalate → escalate。"""
    llm = _mock_llm("escalate", "不确定需求边界,需人判断")
    result = judge_boundary(
        story_key="BJ-1", stage="design", workspace=str(tmp_path),
        ctx={}, artifacts=["story/spec.md"], llm=llm,
    )
    assert result["decision"] == "escalate"


# ---- LLM 不可用 fallback ----


def test_fallback_approve_when_no_api_key(tmp_path):
    """LLM 无 api_key → fallback approve(让人兜底,红线 4)。"""
    llm = MagicMock()
    llm.api_key = ""  # 无 key
    result = judge_boundary(
        story_key="BJ-1", stage="design", workspace=str(tmp_path),
        ctx={}, artifacts=["story/spec.md"], llm=llm,
    )
    assert result["decision"] == "approve"
    assert result.get("fallback") is True
    assert "FALLBACK" in result["reason"]
    llm.invoke_structured.assert_not_called()  # 没调 LLM


def test_fallback_approve_when_llm_raises(tmp_path):
    """LLM 调用抛异常 → fallback approve(不崩)。"""
    llm = MagicMock()
    llm.api_key = "fake"
    llm.model = "test"
    llm.invoke_structured.side_effect = RuntimeError("LLM 超时")
    result = judge_boundary(
        story_key="BJ-1", stage="design", workspace=str(tmp_path),
        ctx={}, artifacts=["story/spec.md"], llm=llm,
    )
    assert result["decision"] == "approve"
    assert result.get("fallback") is True


# ---- 纯 Decider(无副作用,除审计)----


def test_judge_does_not_mutate_story_state(tmp_path):
    """judge_boundary 是 Decider,不改 story 状态(只 log_decision 审计)。"""
    llm = _mock_llm("approve", "ok")
    story_before = db.get_story("BJ-1")
    judge_boundary(
        story_key="BJ-1", stage="design", workspace=str(tmp_path),
        ctx={}, artifacts=["story/spec.md"], llm=llm,
    )
    story_after = db.get_story("BJ-1")
    # story 表的 status / context_json 没被 judge_boundary 改
    assert story_before["status"] == story_after["status"]
    assert story_before["context_json"] == story_after["context_json"]


# ---- verify stage 分支 ----


def test_verify_stage_quality_judgment(tmp_path, monkeypatch):
    """verify stage:LLM 判 approve(测试报告质量 + findings)。"""
    llm = _mock_llm("approve", "测试报告完整,17 测全过")
    # mock get_open_findings(verify 分支会查)
    monkeypatch.setattr(db, "get_open_findings", lambda *a, **k: [])
    result = judge_boundary(
        story_key="BJ-1", stage="verify", workspace=str(tmp_path),
        ctx={}, artifacts=["story/test-report.md"], llm=llm,
    )
    assert result["decision"] == "approve"
