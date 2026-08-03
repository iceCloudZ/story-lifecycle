"""设计 10 改动 1:verify provider 扩展点 + unified gate 外部验证合并测试。

覆盖:
- load_verify_provider:未配置 → None / duck-type 加载 / 失败容错(R6)
- R8 接线:_agent_actions 合入 done_data
- R2:外部 FAIL → retry + 计 reject budget,超限 force-escalate
- R3:外部 PASS 只合并 findings,不覆盖 decision
- 未配置/异步模式(None) → 零行为变化
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from story_lifecycle.infra.db import models as db
from story_lifecycle.orchestrator.evaluation.unified_gate import (
    VerifyGateDecision,
    run_unified_verify_gate,
)
from story_lifecycle.orchestrator.verify_providers import load_verify_provider
from story_lifecycle.orchestrator.verify_providers.base import VerifyResult


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("STORY_HOME", str(tmp_path / ".story-home"))
    db.init_db()
    db.create_story(
        "EXT-VERIFY", "t", str(tmp_path), profile="minimal", current_stage="verify"
    )


def _llm(decision: VerifyGateDecision | None = None, api_key: str = "fake-key"):
    mock = MagicMock()
    mock.api_key = api_key
    if decision is not None:
        mock.invoke_structured.return_value = decision
    return mock


def _advance_llm():
    return _llm(VerifyGateDecision(verdict="pass", decision="advance", reason="ok"))


# ---- load_verify_provider ----


def test_load_provider_none_when_unconfigured():
    assert load_verify_provider({}) is None
    assert load_verify_provider({"verify_provider": None}) is None


def test_load_provider_duck_type_no_abc(tmp_path, monkeypatch):
    """R6:只要求有 verify() 方法,不强制继承 BaseVerifyProvider(hc 侧无需装包)。"""
    mod = tmp_path / "hc_provider.py"
    mod.write_text(
        "class HcPytestVerifyProvider:\n"
        "    def __init__(self, config): self.config = config\n"
        "    def verify(self, story_key, workspace, stage, done_data):\n"
        "        return None\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    provider = load_verify_provider(
        {"verify_provider": {"module": "hc_provider", "class": "HcPytestVerifyProvider"}}
    )
    assert provider is not None
    assert provider.verify("k", "w", "verify", {}) is None


def test_load_provider_missing_verify_method_returns_none(tmp_path, monkeypatch):
    mod = tmp_path / "bad_provider.py"
    mod.write_text("class BadProvider:\n    pass\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    assert (
        load_verify_provider(
            {"verify_provider": {"module": "bad_provider", "class": "BadProvider"}}
        )
        is None
    )


def test_load_provider_import_error_returns_none():
    """加载失败不阻断,降级到 LLM-only gate。"""
    assert (
        load_verify_provider(
            {"verify_provider": {"module": "no.such.module", "class": "X"}}
        )
        is None
    )


# ---- gate 合并(R2 / R3 / R8) ----


def _run_gate(ext_result, context=None):
    with patch(
        "story_lifecycle.orchestrator.evaluation.unified_gate._run_external_verify",
        return_value=ext_result,
    ):
        with patch(
            "story_lifecycle.infra.db.models.get_open_findings", return_value=[]
        ):
            with patch(
                "story_lifecycle.orchestrator.evaluation.unified_gate.get_llm",
                return_value=_advance_llm(),
            ):
                return run_unified_verify_gate(
                    story_key="EXT-VERIFY",
                    stage="verify",
                    workspace="/tmp",
                    context=context or {"task_type": "fund-flow"},
                    done_data={"summary": "ok"},
                    adapter_name="claude",
                )


def test_external_fail_forces_retry_and_counts_reject_budget():
    """R2:LLM 判 advance + 外部 FAIL → 强制 retry + 计 reject budget。"""
    ext = VerifyResult(passed=False, summary="journey borrow-flow 挂了")
    result = _run_gate(ext)
    assert result["decision"] == "retry"
    assert result["repair_action"]["kind"] == "retry"
    assert "外部测试失败" in result["reason"]
    assert result["external_verify"]["passed"] is False
    # reject 已入 orchestrator_decision(trigger=external_verify)
    rejects = db.get_decisions("EXT-VERIFY", "verify", trigger="external_verify")
    assert any(d["decision"] == "reject" for d in rejects)


def test_external_fail_over_budget_escalates_to_fail():
    """R2:连续外部失败超预算 → force-escalate(fail 转人),不死循环。"""
    for i in range(3):  # 3 次已用满(默认 REJECT_LIMIT=3)
        db.log_decision(
            "EXT-VERIFY",
            "verify",
            "external_verify",
            "reject",
            reason=f"fail {i}",
        )
    ext = VerifyResult(passed=False, summary="journey 又挂了")
    result = _run_gate(ext)
    assert result["decision"] == "fail"
    assert result["repair_action"]["kind"] == "escalate"
    assert "防打回循环" in result["reason"]


def test_external_pass_merges_findings_keeps_decision():
    """R3:外部 PASS 只合并 findings,不跳过/覆盖 decision(advance 仍需人 confirm)。"""
    ext = VerifyResult(
        passed=True,
        summary="3 journeys passed",
        findings=[
            {"scenario": "scenario:borrow-flow", "status": "PASS", "detail": ""},
            {"scenario": "scenario:repay-flow", "status": "FAIL", "detail": "还款超时"},
        ],
    )
    with patch(
        "story_lifecycle.orchestrator.evaluation.quality.record_finding"
    ) as mock_record:
        result = _run_gate(ext)
    assert result["decision"] == "advance"  # LLM 的 decision 原样保留
    assert result["external_verify"]["passed"] is True
    # FAIL 的那条 journey 落了 test_failure finding
    recorded = [c.args for c in mock_record.call_args_list]
    assert any(
        args[2].get("category") == "test_failure"
        and args[2].get("location") == "scenario:repay-flow"
        for args in recorded
    )


def test_external_none_no_behavior_change():
    """异步产物模式(provider 返回 None)→ 本轮 LLM-only,decision 不变。"""
    result = _run_gate(None)
    assert result["decision"] == "advance"
    assert "external_verify" not in result


def test_r8_wires_agent_actions_into_done_data():
    """R8:gate 把 ctx["_agent_actions"] 合入 done_data,provider 读得到 selected_scenarios。"""
    spy = MagicMock()
    spy.verify.return_value = None
    actions = [
        {
            "action": "launch",
            "stage": "verify",
            "selected_scenarios": ["scenario:borrow-flow"],
        }
    ]
    with patch(
        "story_lifecycle.infra.config.get_config",
        return_value={"verify_provider": {"module": "x", "class": "Y"}},
    ):
        with patch(
            "story_lifecycle.orchestrator.verify_providers.load_verify_provider",
            return_value=spy,
        ):
            with patch(
                "story_lifecycle.infra.db.models.get_open_findings", return_value=[]
            ):
                with patch(
                    "story_lifecycle.orchestrator.evaluation.unified_gate.get_llm",
                    return_value=_advance_llm(),
                ):
                    run_unified_verify_gate(
                        story_key="EXT-VERIFY",
                        stage="verify",
                        workspace="/tmp",
                        context={"_agent_actions": actions},
                        done_data={"summary": "ok"},
                        adapter_name="claude",
                    )
    assert spy.verify.called
    passed_done = spy.verify.call_args.args[3]
    assert passed_done["_agent_actions"] == actions
    assert passed_done["_agent_actions"][0]["selected_scenarios"] == [
        "scenario:borrow-flow"
    ]
