"""T1.2 · gate 三判定分支覆盖(advance/retry/fail).

覆盖 run_verify_gate 的三个判定出口:
- advance: 无 HIGH findings
- retry:  有 HIGH findings 且 round <= max_retries
- fail:   有 HIGH findings 且 round > max_retries

约束:mock build_repair_packet(它由 T1.4 单独测),本卡只测判定逻辑。
"""

from __future__ import annotations

import pytest

from story_lifecycle.orchestrator.evaluation.gate import (
    increment_review_round_count,
    run_verify_gate,
)
from story_lifecycle.orchestrator.evaluation import judge as judge_mod
from story_lifecycle.orchestrator.evaluation import evaluator_loop as el_mod
from story_lifecycle.infra.db import models as db_mod


@pytest.fixture
def quality_cfg() -> dict:
    return {"enabled": True, "block_on_open_high_findings": True}


@pytest.fixture
def gate_ctx() -> dict:
    return {
        "plan_summary": "verify: 全阶段总览",
        "last_verify_summary": "verify stage completed",
    }


def _patch_gate_dependencies(monkeypatch, tmp_path):
    """Stub judge, DB, packet builder and report writer so tests exercise only
    the gate decision branches."""
    monkeypatch.setattr(
        judge_mod,
        "judge_verify_stage",
        lambda **kwargs: {"pass": True, "reason": "ok", "rework_point": None},
    )
    monkeypatch.setattr(db_mod, "log_event", lambda *a, **kw: None)
    monkeypatch.setattr(
        el_mod,
        "build_repair_packet",
        lambda **kwargs: str(tmp_path / "repair.md"),
    )


def test_gate_advance_when_no_high_findings(
    monkeypatch, tmp_path, quality_cfg, gate_ctx
):
    """advance branch: 0 HIGH finding + round not exceeded."""
    _patch_gate_dependencies(monkeypatch, tmp_path)
    monkeypatch.setattr(
        db_mod,
        "get_open_findings",
        lambda story_key, min_severity="high": [],
    )

    result = run_verify_gate(
        story_key="STORY-ADV",
        stage="verify",
        workspace=str(tmp_path),
        context=gate_ctx,
        quality_cfg=quality_cfg,
        max_retries=2,
    )

    assert result["decision"] == "advance"
    assert "no open HIGH findings" in result["reason"]


def test_gate_retry_when_high_findings_and_round_below_limit(
    monkeypatch, tmp_path, quality_cfg, gate_ctx
):
    """retry branch: HIGH findings exist and round < max_retries."""
    _patch_gate_dependencies(monkeypatch, tmp_path)
    monkeypatch.setattr(
        db_mod,
        "get_open_findings",
        lambda story_key, min_severity="high": [
            {"id": 1, "severity": "high", "description": "bug"}
        ],
    )

    result = run_verify_gate(
        story_key="STORY-RETRY",
        stage="verify",
        workspace=str(tmp_path),
        context=gate_ctx,
        quality_cfg=quality_cfg,
        max_retries=2,
    )

    assert result["decision"] == "retry"
    # human_message is returned as "reason"; it must contain the round count.
    assert "1/2" in result["reason"] or "round 1" in result["reason"].lower()
    assert result["round"] == 1
    assert result["retry_limit"] == 2


def test_gate_fail_when_high_findings_and_round_at_limit_boundary(
    monkeypatch, tmp_path, quality_cfg, gate_ctx
):
    """fail branch: HIGH findings exist and round exceeds max_retries.

    We pre-load the context so that after run_verify_gate's internal
    increment_review_round_count the count becomes max_retries + 1.
    """
    _patch_gate_dependencies(monkeypatch, tmp_path)
    monkeypatch.setattr(
        db_mod,
        "get_open_findings",
        lambda story_key, min_severity="high": [
            {"id": 2, "severity": "high", "description": "bad bug"}
        ],
    )

    max_retries = 2
    for _ in range(max_retries):
        increment_review_round_count(gate_ctx, "verify")

    result = run_verify_gate(
        story_key="STORY-FAIL",
        stage="verify",
        workspace=str(tmp_path),
        context=gate_ctx,
        quality_cfg=quality_cfg,
        max_retries=max_retries,
    )

    assert result["decision"] == "fail"
    assert "HIGH findings persist after" in result["reason"]
    assert "repair rounds" in result["reason"]
