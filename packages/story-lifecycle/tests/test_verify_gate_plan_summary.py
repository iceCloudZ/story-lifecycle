"""ISS-004 regression: FC verify gate must populate plan_summary in repair packet.

Commit c71ad221 fixed a bug where FC planning wrote ``_agent_actions`` but never
``plan_summary``, so the verify gate's repair packet had an empty Plan section.
The fix: ``run_verify_gate`` now prefers the per-stage ``focus`` from
``_agent_actions`` (filtered by stage + 'launch' action), falling back to the
ctx-level ``plan_summary`` overview.

This test asserts that contract: with ``_agent_actions`` containing a launch
action for the verify stage, the gate passes that stage's focus as
``plan_summary`` to ``build_repair_packet`` (not empty, not the unrelated
ctx-level overview).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def gate_ctx():
    """A context_json shape that mirrors what FC planning produces."""
    return {
        # FC planner writes a stage-aggregated overview.
        "plan_summary": "implement: 全阶段总览; verify: 全阶段总览",
        # Per-stage launch actions with their focus (the ISS-004 field).
        "_agent_actions": [
            {
                "stage": "implement",
                "action": "launch",
                "focus": "落地用户服务与权限校验",
            },
            {
                "stage": "verify",
                "action": "launch",
                "focus": "跑测试并产出验证报告",
            },
        ],
        "last_verify_summary": "verify stage completed",
    }


def _patch_gate_dependencies(monkeypatch, tmp_path):
    """Stub DB + repair-packet writer so the test exercises only the decision
    logic that picks plan_summary, without touching real DB/files."""
    from story_lifecycle.orchestrator.evaluation import gate as gate_mod
    from story_lifecycle.orchestrator.evaluation import evaluator_loop as el_mod
    from story_lifecycle.infra.db import models as db_mod

    # capture: record the plan_summary passed to build_repair_packet
    captured = {"plan_summary": None, "called": False}

    def fake_build_repair_packet(**kwargs):
        captured["plan_summary"] = kwargs.get("plan_summary")
        captured["called"] = True
        # Return a path so the gate records it (write_file branch).
        return str(tmp_path / "repair.md")

    def fake_get_open_findings(story_key, min_severity="high"):
        # Non-empty so the gate proceeds to the retry path (where plan_summary
        # is consumed). One finding is enough.
        return [{"id": 1, "severity": "high", "title": "x"}]

    def fake_log_event(*a, **kw):
        return None

    def fake_write_gate_report(gd, workspace):
        return None

    # All three are imported lazily inside run_verify_gate, so patch their
    # canonical modules (not gate_mod.* attributes).
    monkeypatch.setattr(db_mod, "get_open_findings", fake_get_open_findings)
    monkeypatch.setattr(db_mod, "log_event", fake_log_event)
    monkeypatch.setattr(gate_mod, "write_gate_report", fake_write_gate_report)
    monkeypatch.setattr(el_mod, "build_repair_packet", fake_build_repair_packet)
    return captured


def test_gate_uses_per_stage_focus_for_plan_summary(
    monkeypatch, tmp_path, gate_ctx
):
    """The core ISS-004 fix: per-stage focus is preferred over ctx overview."""
    captured = _patch_gate_dependencies(monkeypatch, tmp_path)
    from story_lifecycle.orchestrator.evaluation.gate import run_verify_gate

    quality_cfg = {"enabled": True, "block_on_open_high_findings": True}
    result = run_verify_gate(
        story_key="STORY-1",
        stage="implement",
        workspace=str(tmp_path),
        context=gate_ctx,
        quality_cfg=quality_cfg,
        max_retries=2,
    )

    # The gate entered retry path (open high finding present).
    assert result["decision"] == "retry"
    assert captured["called"], "build_repair_packet was never invoked"
    # CRITICAL: plan_summary is the implement-stage focus, NOT the ctx overview
    # and NOT empty (the pre-fix bug).
    assert captured["plan_summary"] == "落地用户服务与权限校验"
    assert captured["plan_summary"] != gate_ctx["plan_summary"]


def test_gate_falls_back_to_ctx_overview_when_no_stage_action(
    monkeypatch, tmp_path, gate_ctx
):
    """When _agent_actions has no launch entry for the verify stage, fall back
    to the ctx-level plan_summary (the other half of the ISS-004 fix)."""
    captured = _patch_gate_dependencies(monkeypatch, tmp_path)
    from story_lifecycle.orchestrator.evaluation.gate import run_verify_gate

    # ctx with overview but no matching stage action
    ctx = {
        "plan_summary": "全局总览 fallback",
        "_agent_actions": [],  # no per-stage actions
        "last_verify_summary": "done",
    }
    quality_cfg = {"enabled": True, "block_on_open_high_findings": True}
    run_verify_gate(
        story_key="STORY-2",
        stage="verify",
        workspace=str(tmp_path),
        context=ctx,
        quality_cfg=quality_cfg,
        max_retries=2,
    )

    assert captured["called"]
    assert captured["plan_summary"] == "全局总览 fallback"


def test_gate_plan_summary_never_empty_when_ctx_has_data(
    monkeypatch, tmp_path, gate_ctx
):
    """The ISS-004 user-visible symptom was an empty Plan section in the repair
    packet. With the fix, plan_summary must always carry meaningful content
    when either source is available."""
    captured = _patch_gate_dependencies(monkeypatch, tmp_path)
    from story_lifecycle.orchestrator.evaluation.gate import run_verify_gate

    quality_cfg = {"enabled": True, "block_on_open_high_findings": True}
    run_verify_gate(
        story_key="STORY-3",
        stage="verify",
        workspace=str(tmp_path),
        context=gate_ctx,
        quality_cfg=quality_cfg,
        max_retries=2,
    )

    assert captured["called"]
    assert captured["plan_summary"]  # non-empty
    # Should be the verify-stage focus, which is well-formed content
    assert "验证" in captured["plan_summary"] or "测试" in captured["plan_summary"]
