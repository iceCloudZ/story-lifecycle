"""Stage 完成裁判「一次 LLM 三个决定」测试（设计 14 §2.3）。

设计 12 契约：artifacts 落地后一次 LLM 调用输出三决定 —— quality /
lifecycle_target / summary。本文件锁定：
- 三字段解析正确（approve 正常路径）
- reject → 不推进 lifecycle，插 retry action（全自动 handler）
- escalate → paused 等人
- lifecycle_target 跨多状态 → advance_lifecycle_to_target 迭代推进，遇
  ui_button 停住等人确认
- LLM 非法 JSON / 调用失败 → 降级不崩
"""

import json

import pytest

from story_lifecycle.infra.db import models as db
from story_lifecycle.orchestrator.evaluation.stage_completion import (
    StageCompletionDecision,
    advance_lifecycle_to_target,
    judge_stage_completion,
)
from story_lifecycle.orchestrator.handlers import make_decision_handler
from story_lifecycle.orchestrator.engine import graph


class _FakeLLM:
    """模拟 invoke_structured：返回预置的 StageCompletionDecision。"""

    api_key = "fake-key"
    model = "test-model"

    def __init__(self, decision):
        self._decision = decision
        self.invoked = False
        self.last_prompt = ""

    def invoke_structured(self, prompt, schema, **kwargs):
        self.invoked = True
        self.last_prompt = prompt
        return self._decision


def _decision(quality="approve", target=None, summary="完成", reason="ok"):
    return StageCompletionDecision(
        quality=quality, lifecycle_target=target, summary=summary, reason=reason
    )


def _judge(**kw) -> dict:
    """构造 JudgeRequest 调 judge_stage_completion（设计 14 F2 后单参数）。"""
    from story_lifecycle.orchestrator.evaluation.stage_completion import JudgeRequest

    base = dict(
        story_key="JUDGE-1",
        stage="design",
        workspace=db.get_story("JUDGE-1")["workspace"],
        ctx={},
        lifecycle_state="待启动",
        done_data={"summary": "x", "files_changed": []},
    )
    base.update(kw)
    return judge_stage_completion(JudgeRequest(**base))


@pytest.fixture
def judge_story(tmp_path, isolated_story_home):
    """建一条 active story + workspace + 最小 judge 输入。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    db.create_story("JUDGE-1", "裁判测试", str(ws), profile="minimal")
    db.update_story(
        "JUDGE-1",
        lifecycle_state="待启动",
        context_json=json.dumps(
            {
                "_plan_confirmed": True,
                "_agent_actions": [
                    {"action": "launch", "stage": "design", "adapter": "claude"}
                ],
            },
            ensure_ascii=False,
        ),
    )
    return "JUDGE-1"


class TestThreeDecisionsParse:
    def test_approve_parses_three_fields(self, judge_story, monkeypatch):
        """mock LLM 返回三字段 → 返回值三字段都对。"""
        llm = _FakeLLM(
            _decision(quality="approve", target="开发", summary="设计完成")
        )
        monkeypatch.setattr(
            "story_lifecycle.infra.llm_client.get_llm",
            lambda: llm,
        )
        out = _judge(done_data={"summary": "spec 落地", "files_changed": ["story/spec.md"]})
        assert llm.invoked
        assert out["quality"] == "approve"
        assert out["lifecycle_target"] == "开发"
        assert out["summary"] == "设计完成"

    def test_reject_has_no_lifecycle_target(self, judge_story, monkeypatch):
        """quality=reject → lifecycle_target 恒为 None（不合格不算产出）。"""
        llm = _FakeLLM(
            _decision(quality="reject", target="开发", reason="成果物为空")
        )
        monkeypatch.setattr(
            "story_lifecycle.infra.llm_client.get_llm",
            lambda: llm,
        )
        out = _judge(done_data={"summary": "空", "files_changed": []})
        assert out["quality"] == "reject"
        assert out["lifecycle_target"] is None

    def test_invalid_target_treated_as_none(self, judge_story, monkeypatch):
        """LLM 返回不在 LIFECYCLE_ORDER 里的 target → 视为不推进。"""
        llm = _FakeLLM(_decision(quality="approve", target="乱写的状态"))
        monkeypatch.setattr(
            "story_lifecycle.infra.llm_client.get_llm",
            lambda: llm,
        )
        out = _judge(done_data={"summary": "x"})
        assert out["lifecycle_target"] is None
        assert out["quality"] == "approve"

    def test_llm_exception_falls_back_without_crash(self, judge_story, monkeypatch):
        """LLM 抛异常 → 降级（fallback approve 不崩）。"""
        def _boom(*a, **kw):
            raise RuntimeError("llm down")

        monkeypatch.setattr(
            "story_lifecycle.infra.llm_client.get_llm",
            lambda: type("L", (), {"api_key": "k", "invoke_structured": _boom})(),
        )
        out = _judge(done_data={"summary": "x"})
        assert out["quality"] in ("approve", "escalate")
        assert out["fallback"] is True


class TestRejectInsertsRetry:
    def test_reject_does_not_advance_and_inserts_retry(
        self, judge_story, tmp_path, monkeypatch
    ):
        """reject → 不调 advance_lifecycle_to_target，插 retry action（全自动）。"""
        monkeypatch.setattr(
            "story_lifecycle.infra.llm_client.get_llm",
            lambda: _FakeLLM(_decision(quality="reject", reason="质量不达标")),
        )
        out = _judge(done_data={"summary": "差", "files_changed": []})
        assert out["quality"] == "reject"

        # 全自动 handler 处理 reject → 插 retry action
        story = db.get_story("JUDGE-1")
        db.update_story("JUDGE-1", profile="single-pass")  # 全自动
        story = db.get_story("JUDGE-1")
        ctx = json.loads(story["context_json"])
        handler = make_decision_handler(story, ctx)
        advanced = []
        monkeypatch.setattr(
            "story_lifecycle.orchestrator.evaluation.stage_completion.advance_lifecycle_to_target",
            lambda **kw: advanced.append(kw) or {"new_state": "待启动", "paused_for_confirm": False},
        )
        handler.handle_reject("JUDGE-1", "design", out, ctx, ctx["_agent_actions"])
        ctx_after = json.loads(db.get_story("JUDGE-1")["context_json"])
        assert len(ctx_after["_agent_actions"]) == 2, "reject 应插入 retry action"
        assert "reject" in ctx_after["_agent_actions"][1]["focus"]
        assert advanced == []  # reject 不推进 lifecycle


class TestEscalatePauses:
    def test_escalate_pauses_for_human(self, judge_story):
        """escalate → paused 等人（交互式 handler）。"""
        story = db.get_story("JUDGE-1")
        ctx = json.loads(story["context_json"])
        handler = make_decision_handler(story, ctx)
        handler.handle_escalate(
            "JUDGE-1",
            "design",
            {"quality": "escalate", "reason": "质量问题超限", "summary": ""},
            ctx,
        )
        assert db.get_story("JUDGE-1")["status"] == "paused"


class TestAdvanceLifecycleToTarget:
    def test_multi_state_advance_hits_ui_button_pause(
        self, tmp_path, isolated_story_home
    ):
        """跨多状态推进：遇 ui_button 的转换停住，写 _story_state_gate。"""
        db.create_story("ADV-1", "推进测试", str(tmp_path / "ws2"), profile="minimal")
        db.update_story(
            "ADV-1",
            lifecycle_state="待启动",
            context_json=json.dumps({}),
        )
        ctx = json.loads(db.get_story("ADV-1")["context_json"])
        # 开发→测试 是 ui_button（人等确认），其余自动
        story_states = {
            "待启动": {"confirm": {"type": "none"}},
            "开发": {"confirm": {"type": "ui_button", "label": "进入测试"}},
            "测试": {"confirm": {"type": "none"}},
        }
        out = advance_lifecycle_to_target(
            story_key="ADV-1",
            ctx=ctx,
            current="待启动",
            target="结项",
            story_states=story_states,
            db_module=db,
        )
        assert out["paused_for_confirm"] is True
        assert out["new_state"] == "开发"  # 停在上一个已自动推进的状态
        # _story_state_gate 被写，final_target 记住终点
        ctx_after = json.loads(db.get_story("ADV-1")["context_json"])
        gate = ctx_after.get("_story_state_gate") or {}
        assert gate.get("awaiting_confirm") is True
        assert gate.get("final_target") == "结项"
        assert db.get_story("ADV-1")["status"] == "paused"

    def test_multi_state_advance_auto_when_no_gate(
        self, tmp_path, isolated_story_home
    ):
        """没有 ui_button 的跨状态 → 一路自动推进到 target。"""
        db.create_story("ADV-2", "推进测试2", str(tmp_path / "ws3"), profile="minimal")
        db.update_story(
            "ADV-2",
            lifecycle_state="待启动",
            context_json=json.dumps({}),
        )
        ctx = json.loads(db.get_story("ADV-2")["context_json"])
        out = advance_lifecycle_to_target(
            story_key="ADV-2",
            ctx=ctx,
            current="待启动",
            target="结项",
            story_states={},  # 无状态机定义 → 全自动
            db_module=db,
        )
        assert out["paused_for_confirm"] is False
        assert out["new_state"] == "结项"
        assert db.get_story("ADV-2")["lifecycle_state"] == "结项"

    def test_target_equals_current_noop(self, tmp_path, isolated_story_home):
        """target == current → 不推进不暂停。"""
        db.create_story("ADV-3", "推进测试3", str(tmp_path / "ws4"), profile="minimal")
        db.update_story("ADV-3", lifecycle_state="开发", context_json=json.dumps({}))
        ctx = json.loads(db.get_story("ADV-3")["context_json"])
        out = advance_lifecycle_to_target(
            story_key="ADV-3",
            ctx=ctx,
            current="开发",
            target="开发",
            story_states={},
            db_module=db,
        )
        assert out["new_state"] == "开发"
        assert out["paused_for_confirm"] is False


class TestExternalVerifyEvidence:
    """F2b: verify stage 外部测试 provider 证据进 judge 上下文。

    回归(2026-08-06 real-run 1068018):design 12 收敛后 run_unified_verify_gate
    无调用方,provider 成孤儿 —— journey 执行证据永远进不了 judge 上下文,
    judge 只能看 agent 自述(静态核对→连续 reject/escalate)。
    迭代 3 G5:_run_external_verify 从 unified_gate 迁入 stage_completion(孤儿模块删除)。
    """

    def _run_judge_verify(self, monkeypatch, judge_story, ext, llm):
        from story_lifecycle.orchestrator.evaluation import stage_completion as sc
        from story_lifecycle.orchestrator.verify_providers.base import VerifyResult

        monkeypatch.setattr(
            "story_lifecycle.infra.llm_client.get_llm",
            lambda: llm,
        )
        if ext is not None:
            monkeypatch.setattr(
                sc, "_run_external_verify", lambda *a, **k: ext
            )
        else:
            monkeypatch.setattr(
                sc, "_run_external_verify", lambda *a, **k: None
            )
        return _judge(
            stage="verify",
            done_data={
                "summary": "测试报告",
                "files_changed": ["story/test-report.md"],
            },
            ctx={"_agent_actions": [{"stage": "verify", "adapter": "claude"}]},
        )

    def test_verify_provider_evidence_in_prompt(self, judge_story, monkeypatch):
        """provider PASS 证据必须出现在 judge prompt(LLM 才能基于真实执行判)。"""
        from story_lifecycle.orchestrator.verify_providers.base import VerifyResult

        llm = _FakeLLM(_decision(quality="approve", summary="ok"))
        ext = VerifyResult(
            passed=True,
            summary="1 passed, 7 deselected in 94.44s",
            evidence_ref="D:/hc-all/hc-pytest/reports/old_user_repeat_borrow.html",
            evidence={"runs": [{"scenario": "borrow-flow", "status": "PASS"}]},
        )
        self._run_judge_verify(monkeypatch, judge_story, ext, llm)
        assert llm.invoked
        assert "## 外部测试证据" in llm.last_prompt
        assert "PASS" in llm.last_prompt
        assert "old_user_repeat_borrow" in llm.last_prompt

    def test_verify_provider_none_prompt_has_empty_section(self, judge_story, monkeypatch):
        """provider 未配置/返回 None → 段落为空,judge 维持 LLM-only 不崩。"""
        llm = _FakeLLM(_decision(quality="approve", summary="ok"))
        self._run_judge_verify(monkeypatch, judge_story, None, llm)
        assert llm.invoked
        assert "## 外部测试证据" in llm.last_prompt
        assert "（无外部测试参与）" in llm.last_prompt or "外部测试" in llm.last_prompt

    def test_verify_provider_fail_records_finding(self, judge_story, monkeypatch):
        """provider FAIL → 记 finding(source=test_failure)进 open_findings。"""
        from story_lifecycle.orchestrator.evaluation import stage_completion as sc
        from story_lifecycle.orchestrator.verify_providers.base import VerifyResult

        recorded = {}

        def _fake_record_finding(story_key, stage, finding):
            recorded["finding"] = finding

        monkeypatch.setattr(
            "story_lifecycle.orchestrator.evaluation.quality.record_finding",
            _fake_record_finding,
        )
        llm = _FakeLLM(_decision(quality="reject", reason="测试失败"))
        ext = VerifyResult(
            passed=False,
            summary="journey 失败",
            findings=[{"scenario": "borrow-flow", "status": "FAIL", "detail": "断言超时"}],
        )
        self._run_judge_verify(monkeypatch, judge_story, ext, llm)
        assert recorded.get("finding", {}).get("source") == "test_failure"
        assert recorded["finding"]["location"] == "borrow-flow"
