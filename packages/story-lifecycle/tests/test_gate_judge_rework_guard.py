"""T1.6 · judge rework 路径 max_retries 兜底回归测试.

背景(见 reports/T1.1-gate-hard-fail.md Findings + reports/T1.6-judge-rework-guard.md):
gate.py 的 judge 路径(judge_verify_stage 判 rework)原本 increment 后直接 retry,
不检查 round_count > max_retries —— 理论上能无限 retry。T1.6 修复:judge rework
超 max_retries 时也硬闸 fail,与 HIGH-findings 路径(gate.py:247)同语义。

本测试断言:
1. judge 判 rework 且 round 未超限 → retry(正常修复循环)。
2. judge 判 rework 且 round 超限 → fail(硬闸兜底,不无限 retry)。
"""

from __future__ import annotations

import pytest

from story_lifecycle.orchestrator.evaluation.gate import (
    increment_review_round_count,
    run_verify_gate,
)
from story_lifecycle.orchestrator.evaluation import judge as judge_mod
from story_lifecycle.infra.db import models as db_mod


@pytest.fixture
def quality_cfg() -> dict:
    return {"enabled": True, "block_on_open_high_findings": True}


@pytest.fixture
def gate_ctx(tmp_path):
    return {
        "plan_summary": "verify: 全阶段总览",
        "last_verify_summary": "verify stage completed",
    }


def _patch_gate(
    monkeypatch,
    *,
    judge_pass: bool,
    judge_reason: str = "needs rework",
    judge_rework_point: str | None = "tests",
):
    """Patch judge + log_event so the gate's judge path is deterministic.

    judge_pass=False forces the judge-rework branch (the path T1.6 guards).
    """
    monkeypatch.setattr(
        judge_mod,
        "judge_verify_stage",
        lambda **kwargs: {
            "pass": judge_pass,
            "reason": judge_reason,
            "rework_point": judge_rework_point,
        },
    )
    monkeypatch.setattr(db_mod, "log_event", lambda *a, **kw: None)
    # HIGH-findings 路径不应被触及(judge 先于它 short-circuit);置空以防干扰。
    monkeypatch.setattr(
        db_mod,
        "get_open_findings",
        lambda story_key, min_severity="high": [],
    )


def test_judge_rework_within_budget_retries(monkeypatch, tmp_path, quality_cfg, gate_ctx):
    """judge 判 rework 且 round 未超限 → retry(正常修复循环)。"""
    _patch_gate(monkeypatch, judge_pass=False)

    max_retries = 2
    # 不预置 round —— 第一次 rework,increment 后 round=1,未超 max_retries=2
    result = run_verify_gate(
        story_key="STORY-JUDGE-RETRY",
        stage="verify",
        workspace=str(tmp_path),
        context=gate_ctx,
        quality_cfg=quality_cfg,
        max_retries=max_retries,
    )

    assert result["decision"] == "retry"
    assert result["round"] == 1
    assert "judge rework" in result["reason"]


def test_judge_rework_exceeding_budget_fails(monkeypatch, tmp_path, quality_cfg, gate_ctx):
    """T1.6 核心:judge 判 rework 且 round 超限 → 硬闸 fail,不无限 retry。"""
    _patch_gate(monkeypatch, judge_pass=False)

    max_retries = 2
    # 预置到 max_retries,run_verify_gate 内会再 increment 到 max_retries+1 → 超限
    for _ in range(max_retries):
        increment_review_round_count(gate_ctx, "verify")

    result = run_verify_gate(
        story_key="STORY-JUDGE-FAIL",
        stage="verify",
        workspace=str(tmp_path),
        context=gate_ctx,
        quality_cfg=quality_cfg,
        max_retries=max_retries,
    )

    assert result["decision"] == "fail"
    assert "judge rework persists after" in result["reason"]
    assert f"{max_retries} repair rounds" in result["reason"]
    assert result["round"] == max_retries + 1
