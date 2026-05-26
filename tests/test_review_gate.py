"""Tests for review gate observability and control (P0)."""

import json
import os
from unittest.mock import patch


# ---------------------------------------------------------------------------
# GateDecision tests
# ---------------------------------------------------------------------------


class TestGateDecision:
    def test_roundtrip_to_from_dict(self):
        from story_lifecycle.orchestrator.gate import GateDecision

        gd = GateDecision(
            story_key="TEST-001",
            stage="design",
            gate_name="adversarial_review",
            decision="wait_confirm",
            reason_code="review_retry_limit",
            human_message="Review retry limit reached (3 rounds).",
            executor_attempt_count=9,
            review_round_count=3,
            retry_limit=3,
            reviewer={"kind": "llm_api", "model": "deepseek-chat"},
            evidence={"done_consumed": True},
        )
        d = gd.to_dict()
        restored = GateDecision.from_dict(d)
        assert restored.story_key == "TEST-001"
        assert restored.decision == "wait_confirm"
        assert restored.reason_code == "review_retry_limit"
        assert restored.executor_attempt_count == 9
        assert restored.review_round_count == 3
        assert restored.decision_id == gd.decision_id

    def test_auto_generates_decision_id_and_timestamp(self):
        from story_lifecycle.orchestrator.gate import GateDecision

        gd = GateDecision(story_key="K", stage="design")
        assert gd.decision_id
        assert gd.decision_id.startswith("design-gate-")
        assert gd.created_at

    def test_auto_human_message(self):
        from story_lifecycle.orchestrator.gate import GateDecision

        gd = GateDecision(story_key="K", stage="design")
        assert "design" in gd.human_message.lower()
        assert "manual" in gd.human_message.lower()

    def test_default_allowed_actions(self):
        from story_lifecycle.orchestrator.gate import GateDecision

        gd = GateDecision(story_key="K", stage="design")
        assert "retry_review" in gd.allowed_actions
        assert "accept_risk_advance" in gd.allowed_actions


# ---------------------------------------------------------------------------
# Review round count helpers
# ---------------------------------------------------------------------------


class TestReviewRoundCount:
    def test_get_returns_zero_when_never_set(self):
        from story_lifecycle.orchestrator.gate import get_review_round_count

        assert get_review_round_count({}, "design") == 0

    def test_get_reads_correct_key(self):
        from story_lifecycle.orchestrator.gate import get_review_round_count

        ctx = {"review_round_count_design": 5, "review_round_count_implement": 2}
        assert get_review_round_count(ctx, "design") == 5
        assert get_review_round_count(ctx, "implement") == 2

    def test_get_handles_non_int_value(self):
        from story_lifecycle.orchestrator.gate import get_review_round_count

        assert (
            get_review_round_count({"review_round_count_design": "abc"}, "design") == 0
        )

    def test_increment_increases_and_returns_new_count(self):
        from story_lifecycle.orchestrator.gate import increment_review_round_count

        ctx = {}
        assert increment_review_round_count(ctx, "design") == 1
        assert ctx["review_round_count_design"] == 1
        assert increment_review_round_count(ctx, "design") == 2
        assert ctx["review_round_count_design"] == 2


# ---------------------------------------------------------------------------
# Gate report writer
# ---------------------------------------------------------------------------


class TestWriteGateReport:
    def test_writes_markdown_file(self, tmp_path):
        from story_lifecycle.orchestrator.gate import GateDecision, write_gate_report

        gd = GateDecision(
            story_key="TEST-001",
            stage="design",
            decision="wait_confirm",
            reason_code="review_retry_limit",
            human_message="Review retry limit reached.",
            executor_attempt_count=3,
            review_round_count=3,
            retry_limit=3,
            reviewer={"kind": "llm_api", "model": "deepseek-chat"},
        )
        path = write_gate_report(gd, str(tmp_path))
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert "# Review Gate: design" in content
        assert "wait_confirm" in content
        assert "Review retry limit reached." in content
        assert "deepseek-chat" in content

    def test_creates_gates_directory(self, tmp_path):
        from story_lifecycle.orchestrator.gate import GateDecision, write_gate_report

        gd = GateDecision(story_key="K", stage="implement")
        path = write_gate_report(gd, str(tmp_path))
        assert path.parent.name == "gates"
        assert path.parent.exists()

    def test_report_includes_findings(self, tmp_path):
        from story_lifecycle.orchestrator.gate import GateDecision, write_gate_report

        gd = GateDecision(
            story_key="K",
            stage="design",
            evidence={
                "done_consumed": True,
                "open_findings": [
                    {
                        "severity": "high",
                        "description": "Missing rollback plan",
                        "location": "docs/design.md",
                    }
                ],
            },
        )
        path = write_gate_report(gd, str(tmp_path))
        content = path.read_text(encoding="utf-8")
        assert "HIGH" in content
        assert "Missing rollback plan" in content


# ---------------------------------------------------------------------------
# gate_decision_from_state factory
# ---------------------------------------------------------------------------


class TestGateDecisionFromState:
    def test_reads_execution_count_and_stage(self):
        from story_lifecycle.orchestrator.gate import gate_decision_from_state

        state = {
            "story_key": "S-1",
            "current_stage": "implement",
            "execution_count": 7,
            "context": {"review_round_count_implement": 2},
        }
        gd = gate_decision_from_state(state, decision="wait_confirm")
        assert gd.story_key == "S-1"
        assert gd.stage == "implement"
        assert gd.executor_attempt_count == 7
        assert gd.review_round_count == 2

    def test_uses_provided_reviewer(self):
        from story_lifecycle.orchestrator.gate import gate_decision_from_state

        state = {
            "story_key": "S-1",
            "current_stage": "design",
            "execution_count": 0,
            "context": {},
        }
        reviewer = {"kind": "cli", "adapter": "claude", "model": "sonnet"}
        gd = gate_decision_from_state(state, reviewer=reviewer)
        assert gd.reviewer == reviewer


# ---------------------------------------------------------------------------
# review_stage_node fatigue logic
# ---------------------------------------------------------------------------


class TestReviewStageFatigue:
    def _make_state(self, **overrides):
        base = {
            "story_key": "FATIGUE-01",
            "title": "Fatigue Test",
            "workspace": os.getcwd(),
            "profile": "minimal",
            "current_stage": "design",
            "status": "active",
            "context": {},
            "execution_count": 0,
            "last_error": None,
            "trajectory_score": None,
            "review_summary": None,
        }
        base.update(overrides)
        return base

    def test_gate1_review_round_count_fatigue(self, isolated_story_home, monkeypatch):
        """When review_round_count >= retry_limit, should produce GateDecision."""
        from story_lifecycle.orchestrator import nodes
        from story_lifecycle.db import models as db

        db.upsert_story("FATIGUE-01", workspace=os.getcwd(), profile="minimal")

        state = self._make_state(
            context={"review_round_count_design": 3},
            execution_count=10,
        )

        result = nodes.review_stage_node(state)
        assert result.get("last_error")
        assert "review retry limit" in result["last_error"].lower()
        assert result.get("_pre_routed_action") == "wait_confirm"
        assert result.get("_gate_decision") is not None
        gd = result["_gate_decision"]
        assert gd["reason_code"] == "review_retry_limit"
        assert gd["review_round_count"] == 3

        # Verify event_log
        events = db.get_story_events("FATIGUE-01")
        gate_events = [e for e in events if e["event_type"] == "gate_decision"]
        assert len(gate_events) >= 1

    def test_gate2_stale_executor_no_review(self, isolated_story_home, monkeypatch):
        """execution_count >= retry_limit but review_round_count == 0."""
        from story_lifecycle.orchestrator import nodes
        from story_lifecycle.db import models as db

        db.upsert_story("FATIGUE-02", workspace=os.getcwd(), profile="minimal")

        state = self._make_state(
            context={},
            execution_count=9,
        )

        result = nodes.review_stage_node(state)
        assert result.get("last_error")
        assert "review did not run" in result["last_error"].lower()
        assert result.get("_pre_routed_action") == "wait_confirm"
        gd = result["_gate_decision"]
        assert gd["reason_code"] == "review_not_run_due_to_stale_executor_attempt_count"
        assert gd["review_round_count"] == 0

    @patch("story_lifecycle.orchestrator.nodes.planner")
    def test_no_fatigue_when_counts_below_limit(
        self, mock_planner, isolated_story_home, monkeypatch
    ):
        """Should not block when both counts are below limit."""
        from story_lifecycle.orchestrator import nodes
        from story_lifecycle.db import models as db

        mock_planner.compress_context.return_value = None
        mock_planner.review_stage.return_value = {
            "quality": "pass",
            "summary": "test",
            "issues": [],
            "suggestions": [],
            "trajectory_score": 0.9,
            "context_updates": {},
            "reasoning": "test",
        }

        db.upsert_story("FATIGUE-03", workspace=os.getcwd(), profile="minimal")

        state = self._make_state(
            context={"review_round_count_design": 1},
            execution_count=2,
        )

        result = nodes.review_stage_node(state)
        assert result.get("_pre_routed_action") != "wait_confirm"
        assert result.get("_gate_decision") is None

    def test_circuit_breaker_skips_review(self, isolated_story_home):
        """When last_error is already set, skip review entirely."""
        from story_lifecycle.orchestrator import nodes

        state = self._make_state(last_error="Some prior error")
        result = nodes.review_stage_node(state)
        assert result.get("last_error") == "Some prior error"
        assert result.get("_gate_decision") is None


# ---------------------------------------------------------------------------
# wait_confirm_node tests
# ---------------------------------------------------------------------------


class TestWaitConfirmNode:
    def _make_state(self, **overrides):
        base = {
            "story_key": "WAIT-01",
            "title": "Wait Test",
            "workspace": os.getcwd(),
            "profile": "minimal",
            "current_stage": "design",
            "status": "active",
            "context": {},
            "execution_count": 0,
        }
        base.update(overrides)
        return base

    def test_writes_last_error_and_gate_decision(
        self, isolated_story_home, monkeypatch
    ):
        """wait_confirm_node should write last_error and gate_decision event."""
        from story_lifecycle.orchestrator import nodes
        from story_lifecycle.orchestrator.gate import GateDecision
        from story_lifecycle.db import models as db
        from story_lifecycle.orchestrator import graph as graph_mod

        db.upsert_story("WAIT-01", workspace=os.getcwd(), profile="minimal")

        gd = GateDecision(
            story_key="WAIT-01",
            stage="design",
            decision="wait_confirm",
            reason_code="review_retry_limit",
            human_message="Review retry limit reached (3 rounds).",
        )
        state = self._make_state(_gate_decision=gd.to_dict())

        # Mock the interrupt to avoid actually yielding
        interrupted = []

        def _fake_interrupt(payload):
            interrupted.append(payload)

        monkeypatch.setattr(nodes, "interrupt", _fake_interrupt)
        monkeypatch.setattr(graph_mod, "is_story_running", lambda k: False)

        result = nodes.wait_confirm_node(state)

        # interrupt was called
        assert len(interrupted) == 1
        assert result["status"] == "paused"
        assert result["last_error"] == gd.human_message
        assert result["context"].get("last_gate_decision_id") == gd.decision_id

        # Check DB state
        s = db.get_story("WAIT-01")
        assert s["status"] == "paused"
        assert s["last_error"] == gd.human_message

        # Check event_log
        events = db.get_story_events("WAIT-01")
        gate_events = [e for e in events if e["event_type"] == "gate_decision"]
        assert len(gate_events) >= 1

    def test_falls_back_to_constructing_from_state(
        self, isolated_story_home, monkeypatch
    ):
        """When _gate_decision is not in state, construct one from state fields."""
        from story_lifecycle.orchestrator import nodes
        from story_lifecycle.db import models as db
        from story_lifecycle.orchestrator import graph as graph_mod

        db.upsert_story("WAIT-02", workspace=os.getcwd(), profile="minimal")

        state = self._make_state(
            execution_count=5,
            context={"review_round_count_design": 0},
        )

        interrupted = []
        monkeypatch.setattr(nodes, "interrupt", lambda p: interrupted.append(p))
        monkeypatch.setattr(graph_mod, "is_story_running", lambda k: False)

        result = nodes.wait_confirm_node(state)
        assert result["last_error"]
        assert "manual" in result["last_error"].lower()
        assert result["context"].get("last_gate_decision_id")

    def test_writes_gate_report(self, isolated_story_home, tmp_path, monkeypatch):
        """Gate report should be written to .story/context/{key}/gages/."""
        from story_lifecycle.orchestrator import nodes
        from story_lifecycle.orchestrator.gate import GateDecision
        from story_lifecycle.db import models as db
        from story_lifecycle.orchestrator import graph as graph_mod

        ws = str(tmp_path)
        db.upsert_story("WAIT-03", workspace=ws, profile="minimal")

        gd = GateDecision(
            story_key="WAIT-03",
            stage="design",
            decision="wait_confirm",
            reason_code="no_progress",
            human_message="No progress on high findings.",
        )
        state = {
            "story_key": "WAIT-03",
            "title": "Report Test",
            "workspace": ws,
            "profile": "minimal",
            "current_stage": "design",
            "status": "active",
            "context": {},
            "execution_count": 2,
            "_gate_decision": gd.to_dict(),
        }

        interrupted = []
        monkeypatch.setattr(nodes, "interrupt", lambda p: interrupted.append(p))
        monkeypatch.setattr(graph_mod, "is_story_running", lambda k: False)

        result = nodes.wait_confirm_node(state)
        report_rel = result["context"].get("last_gate_report_path", "")
        assert report_rel
        report_path = tmp_path / report_rel
        assert report_path.exists()
        content = report_path.read_text(encoding="utf-8")
        assert "No progress on high findings" in content


# ---------------------------------------------------------------------------
# entry.py GATE_WAIT_CONFIRM state
# ---------------------------------------------------------------------------


class TestEntryGateState:
    def test_is_in_gate_wait_positive(self):
        from story_lifecycle.orchestrator.entry import _is_in_gate_wait

        s = {
            "status": "paused",
            "context_json": json.dumps(
                {"last_gate_decision_id": "design-gate-abc12345"}
            ),
        }
        assert _is_in_gate_wait(s) is True

    def test_is_in_gate_wait_wrong_status(self):
        from story_lifecycle.orchestrator.entry import _is_in_gate_wait

        s = {
            "status": "active",
            "context_json": json.dumps(
                {"last_gate_decision_id": "design-gate-abc12345"}
            ),
        }
        assert _is_in_gate_wait(s) is False

    def test_is_in_gate_wait_no_gate_decision_id(self):
        from story_lifecycle.orchestrator.entry import _is_in_gate_wait

        s = {"status": "paused", "context_json": "{}"}
        assert _is_in_gate_wait(s) is False

    def test_is_in_gate_wait_invalid_json(self):
        from story_lifecycle.orchestrator.entry import _is_in_gate_wait

        s = {"status": "paused", "context_json": "not-json"}
        assert _is_in_gate_wait(s) is False

    def test_resolve_stage_state_returns_gate_wait(self, tmp_path):
        from story_lifecycle.orchestrator.entry import (
            resolve_stage_state,
            StageEntryState,
            WorkspaceState,
        )

        class FakeBackend:
            def is_healthy(self, sid):
                return False

            def resolve_session_state(self, sid):
                return "missing"

            def attach_foreground(self, sid):
                return ["echo", "attach"]

            def launch_independent_terminal(self, *a, **kw):
                pass

        s = {
            "story_key": "GATE-01",
            "current_stage": "design",
            "workspace": str(tmp_path),
            "status": "paused",
            "context_json": json.dumps(
                {"last_gate_decision_id": "design-gate-abc12345"}
            ),
        }
        state = resolve_stage_state(
            s,
            FakeBackend(),
            is_running=False,
            workspace_state=WorkspaceState.FREE,
        )
        assert state == StageEntryState.GATE_WAIT_CONFIRM

    def test_gate_state_maps_r_to_retry_review(self):
        from story_lifecycle.orchestrator.entry import (
            StageEntryState,
            StageEntryAction,
            decide_action,
        )

        action = decide_action(StageEntryState.GATE_WAIT_CONFIRM, "r")
        assert action == StageEntryAction.RETRY_REVIEW

    def test_gate_state_maps_e_to_show_gate_status(self):
        from story_lifecycle.orchestrator.entry import (
            StageEntryState,
            StageEntryAction,
            decide_action,
        )

        action = decide_action(StageEntryState.GATE_WAIT_CONFIRM, "e")
        assert action == StageEntryAction.SHOW_GATE_STATUS


# ---------------------------------------------------------------------------
# record_gate_result
# ---------------------------------------------------------------------------


class TestRecordGateResult:
    def test_writes_to_gate_result_table(self, isolated_story_home):
        from story_lifecycle.db import models as db

        db.upsert_story("GR-01", workspace=os.getcwd(), profile="minimal")
        db.record_gate_result(
            "GR-01",
            "design",
            "adversarial_review",
            "wait_confirm",
            json.dumps({"reason_code": "review_retry_limit"}),
        )

        conn = db.get_conn()
        try:
            row = conn.execute(
                "SELECT g.* FROM gate_result g JOIN story s ON g.story_id = s.id "
                "WHERE s.story_key = ?",
                ("GR-01",),
            ).fetchone()
            assert row is not None
            assert row["gate_name"] == "adversarial_review"
            assert row["result"] == "wait_confirm"
        finally:
            conn.close()

    def test_noop_when_story_not_found(self, isolated_story_home):
        from story_lifecycle.db import models as db

        # Should not raise
        db.record_gate_result("NONEXISTENT", "design", "g", "r", "")
