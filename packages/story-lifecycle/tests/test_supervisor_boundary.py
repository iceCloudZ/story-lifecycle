"""T3.2 · supervisor HITL 决策边界测试。

补 supervisor 在边界场景下的行为:
1. mock LLM 返回标准 {choice, reason} → decide_response 正确解析。
2. PTY 输出含提问 marker → handle_pty_output 触发 HITL 决策并应答。
3. LLM 返回非法格式(JSON 损坏 / choice 不在 options / 缺字段) → handle_pty_output
   降级:不崩、不写 PTY、不 log,返回 False,让上层继续监督。

这些边界守护 supervisor 不会因一次 LLM 失常就炸掉整个 PTY session。
"""

from types import SimpleNamespace

import pytest

from story_lifecycle.orchestrator.engine.supervisor import (
    decide_response,
    handle_pty_output,
)


class TestDecideResponseBoundary:
    """Decider 解析边界:标准格式必须能解。"""

    def test_parses_choice_and_reason(self):
        """mock LLM 返回 {choice, reason} → decide_response 正确解析。"""

        def fake_llm(prompt: str) -> str:
            return '{"choice": "option_a", "reason": "blast radius smaller"}'

        result = decide_response(
            question="用方案 A 还是 B?",
            options=["option_a", "option_b"],
            story_facts={"story_key": "FEAT-1", "stage": "implement"},
            llm_invoke=fake_llm,
        )

        assert result == {"choice": "option_a", "reason": "blast radius smaller"}

    def test_raises_on_invalid_json(self):
        """非法 JSON 时 decide_response 显式抛异常(由 Handler 捕获降级)。"""

        def fake_llm(prompt: str) -> str:
            return "not valid json"

        with pytest.raises((ValueError, Exception)):
            decide_response(
                question="q",
                options=["a", "b"],
                story_facts={"story_key": "S-1"},
                llm_invoke=fake_llm,
            )


class TestHandlePtyOutputHitlTrigger:
    """PTY 输出命中提问 marker → 触发 HITL 决策。"""

    def _make_deps(self, llm_response: str):
        writes: list[bytes] = []
        logs: list[dict] = []

        fake_pty = SimpleNamespace(write=lambda d: writes.append(d))

        def fake_log(story_key, *, stage, event_type, payload):
            logs.append({"story_key": story_key, "event_type": event_type, "payload": payload})

        def fake_awaiting(buffer):
            if "请选择" in buffer:
                return ("请选择部署环境?", ["staging", "prod"])
            return None

        def fake_llm(prompt):
            return llm_response

        return fake_pty, fake_log, fake_awaiting, fake_llm, writes, logs

    def test_triggers_hitl_on_question_marker(self):
        """auto_confirm=True:buffer 命中提问信号 → 决策 + pty.write + log。"""
        fake_pty, fake_log, fake_awaiting, fake_llm, writes, logs = self._make_deps(
            '{"choice": "staging", "reason": "safer"}'
        )

        answered = handle_pty_output(
            buffer="系统提示: 请选择部署环境?",
            pty=fake_pty,
            adapter="codex",
            story_facts={"story_key": "S-1", "stage": "deploy", "auto_confirm": True},
            is_awaiting_fn=fake_awaiting,
            llm_invoke=fake_llm,
            log_event_fn=fake_log,
        )

        assert answered is True
        assert writes == [b"staging\r"]
        assert len(logs) == 1
        assert logs[0]["event_type"] == "supervisor_decision"
        assert logs[0]["payload"]["choice"] == "staging"


class TestHandlePtyOutputDegradation:
    """auto_confirm=True 时 LLM 失常,handle_pty_output 降级:不崩、不写 PTY、不 log。

    (降级路径只在自动确认模式触发;人工模式根本不调 LLM,无降级可言。)
    """

    _AUTO_CONFIRM_FACTS = {"story_key": "S-1", "stage": "implement", "auto_confirm": True}

    def _make_deps(self, llm_response: str):
        writes: list[bytes] = []
        logs: list[dict] = []

        fake_pty = SimpleNamespace(write=lambda d: writes.append(d))

        def fake_log(story_key, *, stage, event_type, payload):
            logs.append({"story_key": story_key, "event_type": event_type, "payload": payload})

        def fake_awaiting(buffer):
            return ("用 A 还是 B?", ["A", "B"])

        def fake_llm(prompt):
            return llm_response

        return fake_pty, fake_log, fake_awaiting, fake_llm, writes, logs

    def test_degrades_on_invalid_json(self):
        """LLM 返回非 JSON → handle_pty_output 返回 False,无副作用。"""
        fake_pty, fake_log, fake_awaiting, fake_llm, writes, logs = self._make_deps(
            "I think A is better"
        )

        answered = handle_pty_output(
            buffer="用 A 还是 B?",
            pty=fake_pty,
            adapter="codex",
            story_facts=TestHandlePtyOutputDegradation._AUTO_CONFIRM_FACTS,
            is_awaiting_fn=fake_awaiting,
            llm_invoke=fake_llm,
            log_event_fn=fake_log,
        )

        assert answered is False
        assert writes == []
        assert logs == []

    def test_degrades_on_choice_not_in_options(self):
        """LLM 返回的 choice 不在 options → handle_pty_output 返回 False,无副作用。"""
        fake_pty, fake_log, fake_awaiting, fake_llm, writes, logs = self._make_deps(
            '{"choice": "C", "reason": "invalid"}'
        )

        answered = handle_pty_output(
            buffer="用 A 还是 B?",
            pty=fake_pty,
            adapter="codex",
            story_facts=TestHandlePtyOutputDegradation._AUTO_CONFIRM_FACTS,
            is_awaiting_fn=fake_awaiting,
            llm_invoke=fake_llm,
            log_event_fn=fake_log,
        )

        assert answered is False
        assert writes == []
        assert logs == []

    def test_degrades_on_missing_field(self):
        """LLM 返回 JSON 缺 choice/reason → handle_pty_output 返回 False,无副作用。"""
        fake_pty, fake_log, fake_awaiting, fake_llm, writes, logs = self._make_deps(
            '{"answer": "A"}'
        )

        answered = handle_pty_output(
            buffer="用 A 还是 B?",
            pty=fake_pty,
            adapter="codex",
            story_facts=TestHandlePtyOutputDegradation._AUTO_CONFIRM_FACTS,
            is_awaiting_fn=fake_awaiting,
            llm_invoke=fake_llm,
            log_event_fn=fake_log,
        )

        assert answered is False
        assert writes == []
        assert logs == []
