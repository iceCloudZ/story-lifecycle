"""2.3 — 调度点② 卡住 LLM 诊断测试(摘要先行 + agentic 例外)。

设计依据:DESIGN §4.3。summary 默认纯判定;agentic 例外(规则触发,只读 + ≤5)。
无打字纠偏(评审 C):restart/escalate/wait。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from story_lifecycle.infra.db import models as db
from story_lifecycle.orchestrator.evaluation.stuck_diagnose import (
    AGENTIC_MAX_CALLS,
    diagnose_stuck_agentic,
    diagnose_stuck_summary,
    should_upgrade_agentic,
)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("STORY_HOME", str(tmp_path / ".story-home"))
    db.init_db()
    db.create_story("SD-1", "t", str(tmp_path), profile="minimal", current_stage="design")


def _mock_llm(cause, action, seed="", reason=""):
    from story_lifecycle.orchestrator.evaluation.stuck_diagnose import StuckDiagnosis

    llm = MagicMock()
    llm.api_key = "fake"
    llm.model = "test"
    llm.invoke_structured.return_value = StuckDiagnosis(
        cause=cause, action=action, seed=seed, reason=reason
    )
    return llm


_DETECTION = {"rule": "no_output_timeout", "reason": "idle 600s", "duration": 600}


# ---- should_upgrade_agentic(触发规则)----


def test_no_upgrade_on_first_stuck():
    """第一次卡住(无历史 stuck 决策)→ 不升级 agentic。"""
    assert should_upgrade_agentic("SD-1", "design", _DETECTION) is False


def test_upgrade_on_second_stuck():
    """同 stage 已卡过一次(stuck 决策 >= 1)→ 升级 agentic。"""
    db.log_decision("SD-1", "design", "stuck_summary", "escalate", reason="第一次卡住")
    assert should_upgrade_agentic("SD-1", "design", _DETECTION) is True


def test_upgrade_on_loop_pattern():
    """events 文本 hash 重复 >= LOOP_REPEAT_THRESHOLD → 循环模式 → 升级。"""
    events = [
        {"text": "Error: connection refused, retrying..."},
        {"text": "Error: connection refused, retrying..."},
        {"text": "Error: connection refused, retrying..."},  # 第 3 次重复
    ]
    assert should_upgrade_agentic("SD-1", "design", _DETECTION, events=events) is True


def test_no_upgrade_on_spinner_only():
    """spinner 字符反复出现但内容不同 → 不算循环。"""
    events = [
        {"text": "✻ Garnishing step 1"},
        {"text": "✽ Garnishing step 2"},
        {"text": "✶ Garnishing step 3"},
    ]
    assert should_upgrade_agentic("SD-1", "design", _DETECTION, events=events) is False


def test_no_upgrade_when_other_stage_stuck():
    """其他 stage 卡过 → 不影响当前 stage(不升级)。"""
    db.log_decision("SD-1", "build", "stuck_summary", "escalate", reason="build 卡")
    assert should_upgrade_agentic("SD-1", "design", _DETECTION) is False


# ---- diagnose_stuck_summary(摘要先行纯判定)----


def test_summary_restart_with_seed():
    """summary 判 detoured → restart 带 seed。"""
    llm = _mock_llm("detoured", "restart", seed="卡在 git push,改用 SSH 重试", reason="跑偏到 git")
    result = diagnose_stuck_summary(
        story_key="SD-1", stage="design", detection=_DETECTION,
        events=[{"text": "thinking"}], story_facts={"adapter": "claude"}, llm=llm,
    )
    assert result["action"] == "restart"
    assert "git push" in result["seed"]
    assert result["trigger"] == "stuck_summary"
    # 落了决策
    decisions = db.get_decisions("SD-1", "design")
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "restart"


def test_summary_escalate_for_asking():
    """summary 判 asking(等人答)→ escalate(不 restart)。"""
    llm = _mock_llm("asking", "escalate", reason="在等澄清")
    result = diagnose_stuck_summary(
        story_key="SD-1", stage="design", detection=_DETECTION,
        events=[{"text": "等回答"}], story_facts={"adapter": "claude"}, llm=llm,
    )
    assert result["action"] == "escalate"


def test_summary_wait_for_slow():
    """summary 判 slow → wait(延长超时)。"""
    llm = _mock_llm("slow", "wait", reason="大任务正常慢")
    result = diagnose_stuck_summary(
        story_key="SD-1", stage="design", detection=_DETECTION,
        events=[{"text": "processing"}], story_facts={"adapter": "claude"}, llm=llm,
    )
    assert result["action"] == "wait"


def test_summary_fallback_when_no_llm():
    """LLM 无 api_key → fallback escalate(规则检测兜底)。"""
    llm = MagicMock()
    llm.api_key = ""
    result = diagnose_stuck_summary(
        story_key="SD-1", stage="design", detection=_DETECTION,
        events=[], story_facts={"adapter": "claude"}, llm=llm,
    )
    assert result["action"] == "escalate"
    assert result.get("fallback") is True


def test_summary_fallback_when_llm_raises():
    """LLM 抛异常 → fallback escalate(不崩)。"""
    llm = MagicMock()
    llm.api_key = "fake"
    llm.invoke_structured.side_effect = RuntimeError("超时")
    result = diagnose_stuck_summary(
        story_key="SD-1", stage="design", detection=_DETECTION,
        events=[], story_facts={"adapter": "claude"}, llm=llm,
    )
    assert result["action"] == "escalate"
    assert result.get("fallback") is True


# ---- diagnose_stuck_agentic(例外,只读 + ≤5)----


def test_agentic_reads_events_jsonl(tmp_path):
    """agentic 用 read_file 工具读 events.jsonl(只读)。"""
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        '{"ts":"t1","dir":"output","text":"line1"}\n'
        '{"ts":"t2","dir":"output","text":"Error: connection refused"}\n',
        encoding="utf-8",
    )
    llm = MagicMock()
    llm.api_key = "fake"
    llm.model = "test"
    # 第一轮调 read_file,第二轮无 tool_call 给最终答案
    llm.invoke_with_tools.side_effect = [
        {"tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": {"path": str(events_path), "offset": 0, "limit": 50}}}], "content": ""},
        {"tool_calls": [], "content": '{"cause":"failed","action":"restart","seed":"连接被拒,改 SSH","reason":"connection refused 循环"}'},
    ]
    result = diagnose_stuck_agentic(
        story_key="SD-1", stage="design", detection=_DETECTION,
        events_path=str(events_path), story_facts={"adapter": "claude"}, llm=llm,
    )
    assert result["action"] == "restart"
    assert result["trigger"] == "stuck_agentic"
    # 用了 read_file 工具
    assert llm.invoke_with_tools.call_count == 2


def test_agentic_max_5_calls_then_escalate(tmp_path):
    """agentic 调用满 AGENTIC_MAX_CALLS 仍无答案 → escalate。"""
    events_path = tmp_path / "events.jsonl"
    events_path.write_text('{"text":"x"}', encoding="utf-8")
    llm = MagicMock()
    llm.api_key = "fake"
    llm.model = "test"
    # 每轮都调 read_file(永不收敛)
    llm.invoke_with_tools.return_value = {
        "tool_calls": [{"id": "c", "function": {"name": "read_file", "arguments": {"path": str(events_path)}}}],
        "content": "",
    }
    result = diagnose_stuck_agentic(
        story_key="SD-1", stage="design", detection=_DETECTION,
        events_path=str(events_path), story_facts={"adapter": "claude"}, llm=llm,
    )
    assert result["action"] == "escalate"
    assert "用满" in result["reason"]
    # 调用上限 ≤ AGENTIC_MAX_CALLS
    assert llm.invoke_with_tools.call_count <= AGENTIC_MAX_CALLS


def test_agentic_no_typed_correction(tmp_path):
    """红线:agentic 决策只有 restart/escalate/wait,无打字纠偏(评审 C)。"""
    events_path = tmp_path / "events.jsonl"
    events_path.write_text('{"text":"x"}', encoding="utf-8")
    llm = MagicMock()
    llm.api_key = "fake"
    llm.model = "test"
    llm.invoke_with_tools.return_value = {
        "tool_calls": [],
        "content": '{"cause":"truly_stuck","action":"restart","seed":"重起,从 git add 开始","reason":"死锁"}',
    }
    result = diagnose_stuck_agentic(
        story_key="SD-1", stage="design", detection=_DETECTION,
        events_path=str(events_path), story_facts={"adapter": "claude"}, llm=llm,
    )
    assert result["action"] in {"restart", "escalate", "wait"}
    # seed 是 restart 指引,不是往 PTY 注的文字
    assert "重起" in result["seed"]


def test_agentic_fallback_when_no_llk(tmp_path):
    """agentic 无 LLM → fallback escalate。"""
    llm = MagicMock()
    llm.api_key = ""
    result = diagnose_stuck_agentic(
        story_key="SD-1", stage="design", detection=_DETECTION,
        events_path=str(tmp_path / "e.jsonl"), story_facts={"adapter": "claude"}, llm=llm,
    )
    assert result["action"] == "escalate"
    assert result.get("fallback") is True
