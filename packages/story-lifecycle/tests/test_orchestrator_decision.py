"""2.1 — orchestrator_decision 表 + reject 上限防护测试。

设计依据:DESIGN §4.9 / 评审 A2。reject 上限 + 理由去重 + 超限强制 escalate,
防 false reject 打回循环烧 token。
"""

from __future__ import annotations

import pytest

from story_lifecycle.infra.db import models as db
from story_lifecycle.orchestrator.evaluation.reject_budget import (
    REJECT_LIMIT,
    _normalize_reason,
    check_reject_budget,
)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("STORY_HOME", str(tmp_path / ".story-home"))
    db.init_db()
    db.create_story("OD-TEST", "t", str(tmp_path), profile="minimal", current_stage="design")


# ---- log_decision / get_decisions / count_decisions ----


def test_log_decision_and_get_back():
    rid = db.log_decision(
        "OD-TEST", "design", "boundary_judge", "approve",
        reason="spec 完整", llm_model="gpt-4",
    )
    assert isinstance(rid, int) and rid > 0
    decisions = db.get_decisions("OD-TEST", "design")
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "approve"
    assert decisions[0]["reason"] == "spec 完整"
    assert decisions[0]["llm_model"] == "gpt-4"


def test_get_decisions_filters_by_stage_and_trigger():
    db.log_decision("OD-TEST", "design", "boundary_judge", "approve")
    db.log_decision("OD-TEST", "build", "boundary_judge", "reject")
    db.log_decision("OD-TEST", "design", "stuck_summary", "restart")
    # stage filter
    design_only = db.get_decisions("OD-TEST", "design")
    assert all(d["stage"] == "design" for d in design_only)
    assert len(design_only) == 2
    # trigger filter
    boundary_only = db.get_decisions("OD-TEST", "design", "boundary_judge")
    assert len(boundary_only) == 1
    assert boundary_only[0]["trigger"] == "boundary_judge"


def test_get_decisions_newest_first():
    db.log_decision("OD-TEST", "design", "boundary_judge", "approve", reason="first")
    db.log_decision("OD-TEST", "design", "boundary_judge", "approve", reason="second")
    decisions = db.get_decisions("OD-TEST", "design")
    # 最近的在前(id 大 / decided_at 新)
    assert decisions[0]["reason"] == "second"
    assert decisions[1]["reason"] == "first"


def test_count_decisions():
    db.log_decision("OD-TEST", "design", "boundary_judge", "approve")
    db.log_decision("OD-TEST", "design", "boundary_judge", "reject")
    db.log_decision("OD-TEST", "design", "boundary_judge", "reject")
    db.log_decision("OD-TEST", "design", "stuck_summary", "restart")
    assert db.count_decisions("OD-TEST", "design", "reject") == 2
    assert db.count_decisions("OD-TEST", "design", "reject", "boundary_judge") == 2
    assert db.count_decisions("OD-TEST", "design", "approve") == 1
    assert db.count_decisions("OD-TEST", "design", "restart") == 1


def test_log_decision_action_payload_serialized():
    db.log_decision(
        "OD-TEST", "design", "stuck_agentic", "restart",
        action_taken="killed+respawn",
        action_payload={"seed": "重试,卡因:git push 失败", "adapter": "kimi"},
    )
    import json

    d = db.get_decisions("OD-TEST", "design")[0]
    payload = json.loads(d["action_payload"])
    assert payload["adapter"] == "kimi"
    assert "git push" in payload["seed"]


# ---- check_reject_budget ----


def test_reject_allowed_under_limit():
    """未到上限 + 理由不同 → 允许 reject。"""
    result = check_reject_budget("OD-TEST", "design", "spec 不够详细")
    assert result["allow"] is True
    assert result["force"] is None
    assert result["count"] == 0


def test_reject_blocked_at_limit_forces_escalate():
    """reject 数 >= 上限 → 强制 escalate(防打回循环)。"""
    # 先记 REJECT_LIMIT 次 reject(用不同理由)
    for i in range(REJECT_LIMIT):
        db.log_decision(
            "OD-TEST", "design", "boundary_judge", "reject",
            reason=f"理由 {i}",
        )
    result = check_reject_budget("OD-TEST", "design", "新的不同理由")
    assert result["allow"] is False
    assert result["force"] == "escalate"
    assert result["count"] == REJECT_LIMIT
    assert "超上限" in result["warn"]


def test_reject_blocked_when_reason_repeats():
    """新 reject 理由与上次重复 → judge 抖,强制 escalate(评审 A2)。"""
    db.log_decision(
        "OD-TEST", "design", "boundary_judge", "reject", reason="缺测试用例"
    )
    # 同理由(标点不同也判重复)
    result = check_reject_budget("OD-TEST", "design", "缺测试用例!")
    assert result["allow"] is False
    assert result["force"] == "escalate"
    assert "重复" in result["warn"]


def test_reject_allowed_with_different_reason_after_prior_reject():
    """前次 reject 后,新理由不同 → 允许(给 code agent 重试机会)。"""
    db.log_decision(
        "OD-TEST", "design", "boundary_judge", "reject", reason="spec 太短"
    )
    result = check_reject_budget("OD-TEST", "design", "这次 spec 够长但漏了边界")
    assert result["allow"] is True
    assert result["count"] == 1


# ---- 理由归一化 ----


def test_normalize_reason_strips_punctuation_and_case():
    """归一化去标点/空白/大小写,保留中英文/数字。"""
    assert _normalize_reason("缺测试。") == _normalize_reason("缺测试")
    assert _normalize_reason("Missing Tests!") == _normalize_reason("missing tests")
    assert _normalize_reason("  空白  ") == _normalize_reason("空白")
    assert _normalize_reason("") == ""
    # 中文 + 英文混合
    assert _normalize_reason("spec 不完整") == _normalize_reason("spec不完整")


def test_reject_budget_custom_limit():
    """自定义 limit(测试用 / 紧凑场景)。"""
    db.log_decision("OD-TEST", "design", "boundary_judge", "reject", reason="r1")
    # limit=1 → 已有 1 次,第 2 次 reject 应被拦
    result = check_reject_budget("OD-TEST", "design", "r2", limit=1)
    assert result["allow"] is False
    assert result["force"] == "escalate"


def test_reject_budget_only_counts_boundary_judge_rejects():
    """reject 上限只数 boundary_judge 触发的 reject,不数 stuck 的 restart。"""
    db.log_decision("OD-TEST", "design", "stuck_summary", "restart", reason="卡住")
    db.log_decision("OD-TEST", "design", "boundary_judge", "reject", reason="r1")
    # stuck 的 restart 不算 reject 预算
    result = check_reject_budget("OD-TEST", "design", "r2")
    assert result["count"] == 1  # 只数了 boundary_judge 的 1 次
    assert result["allow"] is True
