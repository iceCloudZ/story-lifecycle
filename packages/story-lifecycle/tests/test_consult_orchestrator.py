"""Tests for consult_orchestrator(§5.6 / 实施步骤 3)。

纯 Decider + 注入式 FC loop 测试 —— fake invoke_with_tools 返回预设 tool_calls,
fake spawn_fn 不真 spawn,所有 terminated_by 路径覆盖(DESIGN §8.1):
finalize / text / max_rounds / hard_timeout / llm_failed / empty_text。

并验证三个关键设计约束:
- decorrelation 硬校验(同 adapter → decorrelation_violation,不真 spawn)
- 失败路径全走 _fallback_advice(advice 永远非空,不阻塞 code agent)
- terminated_by 是开集诊断字段(测试不断言具体取值集合)
"""

from __future__ import annotations


import pytest

from story_lifecycle.orchestrator.engine.consult_orchestrator import (
    CONSULT_TOOLS,
    SPAWN_REVIEWER_TOOL,
    FINALIZE_ADVICE_TOOL,
    build_consult_messages,
    run_consult_orchestrator,
)


# ── fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def consult_request():
    return {
        "request_id": "test1234abcd",
        "question": "should I use approach A or B for the retry loop?",
        "context": "tried A, hit deadlock on Windows",
        "urgency": "high",
        "adapter_of_caller": "claude",
    }


@pytest.fixture
def story_facts():
    return {"story_key": "STORY-1", "stage": "implement"}


@pytest.fixture
def workspace(tmp_path):
    return str(tmp_path)


def _tc(name: str, args: dict, tc_id: str = "c1") -> dict:
    """Build a normalized OpenAI tool_call(同 LLMClient.invoke_with_tools 的归一化形态)。"""
    return {
        "id": tc_id,
        "type": "function",
        "function": {"name": name, "arguments": args},
    }


def _resp(content: str = "", tool_calls: list[dict] | None = None) -> dict:
    """Build a normalized invoke_with_tools response."""
    tcs = tool_calls or []
    return {
        "message": {
            "role": "assistant",
            "content": content,
            "tool_calls": tcs or None,
        },
        "tool_calls": tcs,
        "content": content,
    }


def _fake_spawn_ok(**kw):
    """fake spawn_fn that always succeeds with summary=ok."""
    return {
        "status": "ok",
        "findings": {"summary": "look fine", "recommendation": "go with A"},
        "error": "",
    }


def _fake_spawn_failed(**kw):
    return {"status": "timeout", "findings": {}, "error": "180s elapsed"}


# ── TestBuildConsultMessages ─────────────────────────────────────────


class TestBuildConsultMessages:
    def test_returns_system_and_user_messages(self, consult_request, story_facts):
        msgs = build_consult_messages(
            consult_request=consult_request, story_facts=story_facts
        )
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"

    def test_system_message_mentions_caller_adapter_for_decorrelation(
        self, consult_request, story_facts
    ):
        msgs = build_consult_messages(
            consult_request=consult_request, story_facts=story_facts
        )
        sys_text = msgs[0]["content"]
        assert "claude" in sys_text
        assert "不同" in sys_text  # decorrelation 约束

    def test_user_message_contains_question_and_context(
        self, consult_request, story_facts
    ):
        msgs = build_consult_messages(
            consult_request=consult_request, story_facts=story_facts
        )
        user_text = msgs[1]["content"]
        assert "approach A or B" in user_text
        assert "deadlock on Windows" in user_text

    def test_story_facts_serialized_into_system(self, consult_request, story_facts):
        msgs = build_consult_messages(
            consult_request=consult_request, story_facts=story_facts
        )
        sys_text = msgs[0]["content"]
        assert "STORY-1" in sys_text
        assert "implement" in sys_text


# ── TestConsultOrchestratorTerminatedByPaths ────────────────────────


class TestTerminatedByPaths:
    def test_finalize_terminates_immediately(
        self, consult_request, story_facts, workspace
    ):
        """LLM 第一轮调 finalize_advice → 直接返,terminated_by=finalize。"""
        calls = {"n": 0}

        def fake_invoke(messages, tools, **kw):
            calls["n"] += 1
            return _resp(
                tool_calls=[_tc("finalize_advice", {"advice": "use A", "confidence": "high"})]
            )

        result = run_consult_orchestrator(
            consult_request=consult_request,
            story_facts=story_facts,
            workspace=workspace,
            invoke_with_tools=fake_invoke,
            spawn_fn=_fake_spawn_ok,
        )
        assert result["advice"] == "use A"
        assert result["confidence"] == "high"
        assert result["terminated_by"] == "finalize"
        assert result["followed_up"] is False
        assert result["rounds"] == 1
        assert result["spawn_results"] == []
        assert calls["n"] == 1

    def test_text_only_terminates_immediately(
        self, consult_request, story_facts, workspace
    ):
        """LLM 第一轮纯文本(没调工具)→ 把文本当 advisory,terminated_by=text。"""
        calls = {"n": 0}

        def fake_invoke(messages, tools, **kw):
            calls["n"] += 1
            return _resp(content="just go with A, it's simpler")

        result = run_consult_orchestrator(
            consult_request=consult_request,
            story_facts=story_facts,
            workspace=workspace,
            invoke_with_tools=fake_invoke,
            spawn_fn=_fake_spawn_ok,
        )
        assert result["advice"] == "just go with A, it's simpler"
        assert result["terminated_by"] == "text"
        assert result["confidence"] == "medium"
        assert result["followed_up"] is False

    def test_empty_text_with_no_tool_calls_falls_back(
        self, consult_request, story_facts, workspace
    ):
        """LLM 返回空 content 且无 tool_calls → terminated_by=empty_text,advice 非空。"""
        def fake_invoke(messages, tools, **kw):
            return _resp(content="")

        result = run_consult_orchestrator(
            consult_request=consult_request,
            story_facts=story_facts,
            workspace=workspace,
            invoke_with_tools=fake_invoke,
            spawn_fn=_fake_spawn_ok,
        )
        assert result["terminated_by"] == "empty_text"
        assert result["advice"]  # 非空
        assert "降级" in result["advice"]
        assert result["confidence"] == "low"

    def test_llm_raises_falls_back(
        self, consult_request, story_facts, workspace
    ):
        """invoke_with_tools 抛异常 → terminated_by=llm_failed,fallback advisory。"""
        def fake_invoke(messages, tools, **kw):
            raise RuntimeError("network blip")

        result = run_consult_orchestrator(
            consult_request=consult_request,
            story_facts=story_facts,
            workspace=workspace,
            invoke_with_tools=fake_invoke,
            spawn_fn=_fake_spawn_ok,
        )
        assert result["terminated_by"] == "llm_failed"
        assert "network blip" in result["advice"]
        assert result["confidence"] == "low"
        assert result["followed_up"] is False

    def test_hard_timeout_falls_back(
        self, consult_request, story_facts, workspace
    ):
        """clock_fn 模拟时间走完 → terminated_by=hard_timeout。"""
        state = {"t": 0.0}

        def fake_clock():
            state["t"] += 1000  # 每次跳 1000s
            return state["t"]

        def fake_invoke(messages, tools, **kw):
            return _resp(content="never reached")

        result = run_consult_orchestrator(
            consult_request=consult_request,
            story_facts=story_facts,
            workspace=workspace,
            invoke_with_tools=fake_invoke,
            spawn_fn=_fake_spawn_ok,
            hard_timeout_s=10,  # 第一次 clock 跳 1000s,第一轮就触发
            clock_fn=fake_clock,
        )
        assert result["terminated_by"] == "hard_timeout"
        assert "降级" in result["advice"]

    def test_max_rounds_synthesizes_spawn_results(
        self, consult_request, story_facts, workspace
    ):
        """LLM 连续 spawn 不 finalize → 达 max_rounds → 拼 spawn_results 综合。"""
        spawn_calls = []

        def fake_spawn(**kw):
            spawn_calls.append(kw)
            return {
                "status": "ok",
                "findings": {
                    "summary": f"finding from {kw.get('adapter_name')}",
                    "recommendation": "do X",
                },
                "error": "",
            }

        def fake_invoke(messages, tools, **kw):
            # 永远调 spawn_reviewer,不调 finalize
            return _resp(
                tool_calls=[_tc("spawn_reviewer", {"adapter": "kimi", "focus": "check"})]
            )

        result = run_consult_orchestrator(
            consult_request=consult_request,
            story_facts=story_facts,
            workspace=workspace,
            invoke_with_tools=fake_invoke,
            spawn_fn=fake_spawn,
            max_rounds=3,
        )
        assert result["terminated_by"] == "max_rounds"
        assert result["confidence"] == "low"
        assert result["followed_up"] is True
        assert len(result["spawn_results"]) == 3
        assert "finding from kimi" in result["advice"]


# ── TestDecorrelationHardGuard ──────────────────────────────────────


class TestDecorrelationHardGuard:
    def test_same_adapter_does_not_spawn(
        self, consult_request, story_facts, workspace
    ):
        """caller=claude,LLM spawn claude → 不真 spawn,塞 decorrelation_violation。"""
        spawn_calls = []
        invoke_calls = {"n": 0}

        def fake_spawn(**kw):
            spawn_calls.append(kw)
            return {"status": "ok", "findings": {}, "error": ""}

        def fake_invoke(messages, tools, **kw):
            invoke_calls["n"] += 1
            if invoke_calls["n"] == 1:
                # caller 是 claude,LLM 选了 claude → 违规
                return _resp(
                    tool_calls=[_tc("spawn_reviewer", {"adapter": "claude", "focus": "x"})]
                )
            # 第二轮 LLM 看到违规提示,改选 kimi
            return _resp(
                tool_calls=[_tc("finalize_advice", {"advice": "ok", "confidence": "medium"})]
            )

        result = run_consult_orchestrator(
            consult_request=consult_request,
            story_facts=story_facts,
            workspace=workspace,
            invoke_with_tools=fake_invoke,
            spawn_fn=fake_spawn,
        )

        # 关键:违规那次没真 spawn(spawn_calls 为空 —— 因为第二轮直接 finalize)
        assert spawn_calls == [], (
            "decorrelation violation must NOT trigger real spawn"
        )
        # 第二轮 finalize 成功
        assert result["terminated_by"] == "finalize"

    def test_decorrelation_violation_message_reaches_llm(
        self, consult_request, story_facts, workspace
    ):
        """违规提示必须以 role=tool 塞回 messages,让 LLM 看到后换 adapter。"""
        spawn_calls = []
        invoke_n = {"n": 0}
        captured_messages = []

        def fake_spawn(**kw):
            spawn_calls.append(kw)
            return {"status": "ok", "findings": {"summary": "ok"}, "error": ""}

        def fake_invoke(messages, tools, **kw):
            invoke_n["n"] += 1
            captured_messages.append([dict(m) for m in messages])
            if invoke_n["n"] == 1:
                # 违规选同 adapter
                return _resp(
                    tool_calls=[_tc("spawn_reviewer", {"adapter": "claude", "focus": "x"})]
                )
            # 第二轮:违规提示已在 messages 里,LLM 改选 kimi → 真 spawn
            if invoke_n["n"] == 2:
                return _resp(
                    tool_calls=[_tc("spawn_reviewer", {"adapter": "kimi", "focus": "x"})]
                )
            # 第三轮:finalize
            return _resp(
                tool_calls=[_tc("finalize_advice", {"advice": "ok", "confidence": "high"})]
            )

        run_consult_orchestrator(
            consult_request=consult_request,
            story_facts=story_facts,
            workspace=workspace,
            invoke_with_tools=fake_invoke,
            spawn_fn=fake_spawn,
            max_rounds=3,
        )

        # 第二轮 LLM 应看到塞回的违规提示(role=tool)
        second_round_msgs = captured_messages[1]
        tool_msgs = [m for m in second_round_msgs if m.get("role") == "tool"]
        assert any(
            "decorrelation_violation" in (m.get("content") or "")
            for m in tool_msgs
        ), "violation must be塞回 messages as role=tool"
        # 只真 spawn 了 1 次(kimi),违规那次没 spawn
        assert len(spawn_calls) == 1
        assert spawn_calls[0]["adapter_name"] == "kimi"


# ── TestToolsSchema ─────────────────────────────────────────────────


class TestConsultToolsSchema:
    def test_spawn_reviewer_has_decorrelation_description(self):
        desc = SPAWN_REVIEWER_TOOL["function"]["description"]
        assert "decorrelation" in desc.lower() or "MUST differ" in desc

    def test_spawn_reviewer_adapter_enum_excludes_codex(self):
        """codex 无 headless,enum 不含(DESIGN §3.6)。"""
        enum = SPAWN_REVIEWER_TOOL["function"]["parameters"]["properties"]["adapter"]["enum"]
        assert "claude" in enum
        assert "kimi" in enum
        assert "codex" not in enum

    def test_finalize_advice_has_required_fields(self):
        required = SPAWN_REVIEWER_TOOL["function"]["parameters"]["required"]
        assert "adapter" in required
        assert "focus" in required
        finalize_required = FINALIZE_ADVICE_TOOL["function"]["parameters"]["required"]
        assert "advice" in finalize_required
        assert "confidence" in finalize_required

    def test_consult_tools_default_used_when_none_passed(
        self, consult_request, story_facts, workspace
    ):
        """不传 tools → 用 CONSULT_TOOLS 默认表(参数化注入测试)。"""
        seen_tools = []

        def fake_invoke(messages, tools, **kw):
            seen_tools.append(tools)
            return _resp(content="ok")

        run_consult_orchestrator(
            consult_request=consult_request,
            story_facts=story_facts,
            workspace=workspace,
            invoke_with_tools=fake_invoke,
            spawn_fn=_fake_spawn_ok,
        )
        assert seen_tools[0] is CONSULT_TOOLS


# ── TestResultContractShape ─────────────────────────────────────────


class TestResultContractShape:
    """所有路径返回的 dict 必须含 5 个字段(契约测试,DESIGN §8.3)。"""

    @pytest.mark.parametrize(
        "scenario_invoke,scenario_spawn",
        [
            (  # finalize
                lambda: _resp(
                    tool_calls=[_tc("finalize_advice", {"advice": "x", "confidence": "low"})]
                ),
                _fake_spawn_ok,
            ),
            (  # text
                lambda: _resp(content="text"),
                _fake_spawn_ok,
            ),
            (  # llm_failed
                (lambda: (_ for _ in ()).throw(RuntimeError("x"))),
                _fake_spawn_ok,
            ),
            (  # empty_text
                lambda: _resp(content=""),
                _fake_spawn_ok,
            ),
            (  # hard_timeout
                None,  # special-cased below
                _fake_spawn_ok,
            ),
        ],
    )
    def test_result_dict_has_required_fields(
        self,
        consult_request,
        story_facts,
        workspace,
        scenario_invoke,
        scenario_spawn,
    ):
        REQUIRED = {"advice", "confidence", "followed_up", "rounds", "terminated_by"}

        if scenario_invoke is None:  # hard_timeout special case
            state = {"t": 0.0}
            result = run_consult_orchestrator(
                consult_request=consult_request,
                story_facts=story_facts,
                workspace=workspace,
                invoke_with_tools=lambda m, t, **k: _resp("never"),
                spawn_fn=scenario_spawn,
                clock_fn=lambda: (state.__setitem__("t", state["t"] + 1000) or state["t"]),
                hard_timeout_s=10,
            )
        else:
            result = run_consult_orchestrator(
                consult_request=consult_request,
                story_facts=story_facts,
                workspace=workspace,
                invoke_with_tools=lambda m, t, **k: scenario_invoke(),
                spawn_fn=scenario_spawn,
            )
        assert REQUIRED.issubset(result.keys())
        assert isinstance(result["advice"], str)
        assert result["advice"], "advice must never be empty (不阻塞 code agent)"
        assert isinstance(result["followed_up"], bool)
        assert isinstance(result["rounds"], int)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
