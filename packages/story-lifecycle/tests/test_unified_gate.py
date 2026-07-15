"""Tests for unified verify-gate (REFACTOR §5.3).

一次 LLM 完成质量判断 + finding 识别 + decision + repair_action。
替原 gate.py(judge + finding) + transition.py(decide_transition)三步。
"""

from unittest.mock import MagicMock, patch

from story_lifecycle.orchestrator.evaluation.unified_gate import (
    run_unified_verify_gate,
    _fallback_gate_decision,
    VerifyGateDecision,
    RepairAction,
)
from story_lifecycle.orchestrator.engine.planner import _repair_spec_to_action


class TestVerifyGateDecisionSchema:
    """Pydantic schema 基本行为。"""

    def test_advance_decision(self):
        d = VerifyGateDecision(verdict="pass", decision="advance", reason="ok")
        assert d.decision == "advance"
        assert d.repair_action is None

    def test_retry_with_repair_action(self):
        d = VerifyGateDecision(
            verdict="rework", decision="retry", reason="test fail",
            repair_action=RepairAction(kind="retry", reason="retry same"),
        )
        assert d.repair_action.kind == "retry"

    def test_swap_with_new_adapter(self):
        d = VerifyGateDecision(
            verdict="rework", decision="retry", reason="codex weak",
            repair_action=RepairAction(
                kind="swap_approach", reason="history says claude works",
                new_adapter="claude",
            ),
        )
        assert d.repair_action.new_adapter == "claude"


class TestFallbackGateDecision:
    """§5.3.3:fallback 区分 HIGH finding 存在 vs LLM 抖动。"""

    def test_high_finding_present_escalates(self):
        """有 HIGH finding → 不盲目 retry,直接 fail/escalate(不掩盖质量问题)。"""
        evidence = {
            "story_key": "S-1",
            "retry_count": 1,
            "max_retries": 3,
            "open_high_findings": [{"severity": "high", "description": "SQL 注入"}],
        }
        result = _fallback_gate_decision(evidence, db=MagicMock(), story_key="S-1")
        assert result["decision"] == "fail"
        assert result["repair_action"]["kind"] == "escalate"

    def test_no_high_finding_llm_jitter_retries(self):
        """无 HIGH finding + 未超限 → 默认 retry(LLM 抖动不打扰人)。"""
        evidence = {
            "story_key": "S-1",
            "retry_count": 1,
            "max_retries": 3,
            "open_high_findings": [],
        }
        result = _fallback_gate_decision(evidence, db=MagicMock(), story_key="S-1")
        assert result["decision"] == "retry"
        assert result["repair_action"]["kind"] == "retry"

    def test_no_high_finding_over_limit_escalates(self):
        """无 HIGH finding 但 retry 超限 → escalate。"""
        evidence = {
            "story_key": "S-1",
            "retry_count": 3,
            "max_retries": 3,
            "open_high_findings": [],
        }
        result = _fallback_gate_decision(evidence, db=MagicMock(), story_key="S-1")
        assert result["decision"] == "fail"
        assert result["repair_action"]["kind"] == "escalate"

    def test_fallback_always_has_required_fields(self):
        """fallback 返回的 dict 必须有 planner 裸下标读的字段。"""
        evidence = {
            "story_key": "S-1",
            "retry_count": 2,
            "max_retries": 3,
            "open_high_findings": [],
        }
        result = _fallback_gate_decision(evidence, db=MagicMock(), story_key="S-1")
        assert "round" in result
        assert "retry_limit" in result
        assert "decision" in result
        assert "reason" in result


class TestRepairSpecToAction:
    """§5.3.4:_repair_spec_to_action 把 repair_action spec 转 action dict(字段映射)。"""

    def test_retry_uses_same_adapter(self):
        result = _repair_spec_to_action(
            repair_spec={"kind": "retry", "reason": "jitter"},
            story_key="S-1", adapter_name="codex", round_n=1, reason="fail",
        )
        assert result["adapter"] == "codex"
        assert result["stage"] == "verify"
        assert "verify-round1.json" in result["done_file"]

    def test_swap_uses_new_adapter_from_spec(self):
        """模型指定 new_adapter(基于 playbook),不是硬编码轮转。"""
        result = _repair_spec_to_action(
            repair_spec={"kind": "swap_approach", "reason": "history", "new_adapter": "claude"},
            story_key="S-1", adapter_name="codex", round_n=2, reason="fail",
        )
        assert result["adapter"] == "claude"

    def test_swap_falls_back_to_rotation_when_no_adapter(self):
        """模型未指定 new_adapter → 兜底轮转(与原 _SWAP_ADAPTER_ORDER 一致)。"""
        result = _repair_spec_to_action(
            repair_spec={"kind": "swap_approach", "reason": "no adapter specified"},
            story_key="S-1", adapter_name="codex", round_n=1, reason="fail",
        )
        assert result["adapter"] == "claude"  # codex → claude (rotation)

    def test_insert_rescue_stage(self):
        result = _repair_spec_to_action(
            repair_spec={"kind": "insert_rescue_stage", "reason": "缺 mock", "rescue_stage": "setup_dependency"},
            story_key="S-1", adapter_name="claude", round_n=1, reason="fail",
        )
        assert result["stage"] == "setup_dependency"
        assert "setup_dependency.json" in result["done_file"]

    def test_escalate_returns_none(self):
        result = _repair_spec_to_action(
            repair_spec={"kind": "escalate", "reason": "give up"},
            story_key="S-1", adapter_name="codex", round_n=1, reason="fail",
        )
        assert result is None


class TestRunUnifiedVerifyGate:
    """run_unified_verify_gate 主函数行为。"""

    def test_no_api_key_falls_back(self):
        """无 api_key → 走 fallback(不调 LLM)。"""
        mock_llm = MagicMock()
        mock_llm.api_key = None
        with patch("story_lifecycle.orchestrator.evaluation.unified_gate.get_llm", return_value=mock_llm):
            with patch("story_lifecycle.infra.db.models.get_open_findings", return_value=[]):
                result = run_unified_verify_gate(
                    story_key="S-1", stage="verify", workspace="/tmp",
                    context={"task_type": "credit-limit"},
                    done_data={"summary": "test"}, adapter_name="codex",
                )
                assert result["decision"] in ("retry", "fail")
                assert "round" in result  # planner 裸下标

    def test_llm_exception_falls_back(self):
        """LLM 调用抛异常 → 走 fallback。"""
        mock_llm = MagicMock()
        mock_llm.api_key = "fake-key"
        mock_llm.invoke_structured.side_effect = RuntimeError("network down")
        with patch("story_lifecycle.orchestrator.evaluation.unified_gate.get_llm", return_value=mock_llm):
            with patch("story_lifecycle.infra.db.models.get_open_findings", return_value=[]):
                result = run_unified_verify_gate(
                    story_key="S-1", stage="verify", workspace="/tmp",
                    context={}, done_data={}, adapter_name="codex",
                )
                assert result["decision"] in ("retry", "fail")
                assert mock_llm.invoke_structured.called  # 确实尝试调了

    def test_llm_returns_advance(self):
        """LLM 返回 advance → 不进 retry/fail 分支。"""
        mock_llm = MagicMock()
        mock_llm.api_key = "fake-key"
        decision = VerifyGateDecision(
            verdict="pass", decision="advance", reason="all good",
        )
        mock_llm.invoke_structured.return_value = decision
        with patch("story_lifecycle.orchestrator.evaluation.unified_gate.get_llm", return_value=mock_llm):
            with patch("story_lifecycle.infra.db.models.get_open_findings", return_value=[]):
                result = run_unified_verify_gate(
                    story_key="S-1", stage="verify", workspace="/tmp",
                    context={}, done_data={}, adapter_name="codex",
                )
                assert result["decision"] == "advance"

    def test_llm_returns_swap_with_playbook_adapter(self):
        """LLM 基于 playbook 返回 swap_approach + new_adapter → action 用新 adapter。"""
        mock_llm = MagicMock()
        mock_llm.api_key = "fake-key"
        decision = VerifyGateDecision(
            verdict="rework", decision="retry", reason="codex weak",
            repair_action=RepairAction(
                kind="swap_approach", reason="history says claude",
                new_adapter="claude",
            ),
        )
        mock_llm.invoke_structured.return_value = decision
        with patch("story_lifecycle.orchestrator.evaluation.unified_gate.get_llm", return_value=mock_llm):
            with patch("story_lifecycle.infra.db.models.get_open_findings", return_value=[]):
                result = run_unified_verify_gate(
                    story_key="S-1", stage="verify", workspace="/tmp",
                    context={}, done_data={}, adapter_name="codex",
                )
                assert result["decision"] == "retry"
                assert result["repair_action"]["new_adapter"] == "claude"
