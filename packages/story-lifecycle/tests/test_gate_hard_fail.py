"""T1.1 · gate 硬闸不可绕(max_retries 强制 fail).

目标:证明 ``round_count > max_retries`` 时代码路径必然走到 fail 分支。
约束:不修改 gate.py;若发现可绕过则标阻塞。
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
def gate_ctx_at_max(tmp_path):
    """Context whose review_round_count_verify already equals max_retries."""
    ctx = {
        "plan_summary": "verify: 全阶段总览",
        "last_verify_summary": "verify stage completed",
    }
    return ctx


def _patch_judge_and_log(monkeypatch):
    """Stub judge to pass and log_event to no-op so tests are deterministic and fast."""
    monkeypatch.setattr(
        judge_mod,
        "judge_verify_stage",
        lambda **kwargs: {"pass": True, "reason": "ok", "rework_point": None},
    )
    monkeypatch.setattr(db_mod, "log_event", lambda *a, **kw: None)


def test_high_findings_exceeding_max_retries_fail(
    monkeypatch, tmp_path, quality_cfg, gate_ctx_at_max
):
    """When round_count > max_retries with HIGH findings, gate must fail."""
    _patch_judge_and_log(monkeypatch)

    max_retries = 2
    # Push context to the limit using the canonical helper.
    for _ in range(max_retries):
        increment_review_round_count(gate_ctx_at_max, "verify")

    monkeypatch.setattr(
        db_mod,
        "get_open_findings",
        lambda story_key, min_severity="high": [
            {"id": 1, "severity": "high", "description": "bad bug"}
        ],
    )

    result = run_verify_gate(
        story_key="STORY-HARD",
        stage="verify",
        workspace=str(tmp_path),
        context=gate_ctx_at_max,
        quality_cfg=quality_cfg,
        max_retries=max_retries,
    )

    assert result["decision"] == "fail"
    assert "HIGH findings persist after" in result["reason"]
    assert "repair rounds" in result["reason"]


def test_empty_findings_with_exceeded_rounds_still_fail(
    monkeypatch, tmp_path, quality_cfg, gate_ctx_at_max
):
    """Hard gate: even if no open HIGH findings remain, exceeding max_retries must fail.

    NOTE: This assertion reflects the literal wording of the T1.1 task card
    ("无论 finding 数量/质量如何"). The project owner has not yet confirmed
    whether this is the intended semantics; if the intended semantics is instead
    "no findings => advance", this test should be changed to expect "advance".
    """
    _patch_judge_and_log(monkeypatch)

    max_retries = 2
    for _ in range(max_retries):
        increment_review_round_count(gate_ctx_at_max, "verify")

    monkeypatch.setattr(
        db_mod,
        "get_open_findings",
        lambda story_key, min_severity="high": [],
    )

    result = run_verify_gate(
        story_key="STORY-HARD-EMPTY",
        stage="verify",
        workspace=str(tmp_path),
        context=gate_ctx_at_max,
        quality_cfg=quality_cfg,
        max_retries=max_retries,
    )

    # This asserts the gate is truly hard: retry budget exhaustion is itself a fail condition.
    assert result["decision"] == "fail"
