"""Tests for supervisor Decider — LLM-driven response to code-agent questions.

Supervisor 监督交互式 code agent (claude/codex/kimi),当 agent 提问/要选择时,
``decide_response`` 用注入的 LLM 决策返回 {choice, reason}。纯 Decider,零副作用。
两轨(Claude stream-json / codex-kimi PTY)共用此决策大脑。
"""

import pytest

from story_lifecycle.orchestrator.engine.supervisor import (
    decide_response,
    handle_pty_output,
    log_decision,
)


class TestDecideResponse:
    def test_parses_llm_json_into_choice_and_reason(self):
        """LLM 返回 JSON 字符串 → decide_response 解析成 {choice, reason}。"""

        def fake_llm(prompt: str) -> str:
            return '{"choice": "option_a", "reason": "blast radius smaller"}'

        result = decide_response(
            question="用方案 A 还是 B?",
            options=["option_a", "option_b"],
            story_facts={"story_key": "FEAT-1", "stage": "implement"},
            llm_invoke=fake_llm,
        )

        assert result == {"choice": "option_a", "reason": "blast radius smaller"}

    def test_strips_markdown_code_fence_around_json(self):
        """LLM 常把 JSON 包在 ```json ... ``` 里,decide_response 要剥离再解析。"""

        def fake_llm(prompt: str) -> str:
            return '```json\n{"choice": "option_b", "reason": "faster"}\n```'

        result = decide_response(
            question="q",
            options=["option_a", "option_b"],
            story_facts={"story_key": "S-2", "stage": "implement"},
            llm_invoke=fake_llm,
        )

        assert result == {"choice": "option_b", "reason": "faster"}

    def test_raises_when_choice_not_in_options(self):
        """LLM 返回的 choice 不在 options → raise(Decider 不替 LLM 圆场,Handler 接)。"""

        def fake_llm(prompt: str) -> str:
            return '{"choice": "option_c", "reason": "unsupported"}'

        with pytest.raises(ValueError):
            decide_response(
                question="q",
                options=["option_a", "option_b"],
                story_facts={"story_key": "S-3", "stage": "implement"},
                llm_invoke=fake_llm,
            )


class TestLogDecision:
    def test_logs_supervisor_decision_event_with_payload(self):
        """log_decision 把决策落 log_event(event_type=supervisor_decision)。"""
        recorded = []

        def fake_log_event(story_key, *, stage, event_type, payload):
            recorded.append(
                {
                    "story_key": story_key,
                    "stage": stage,
                    "event_type": event_type,
                    "payload": payload,
                }
            )

        log_decision(
            story_key="FEAT-1",
            stage="implement",
            adapter="claude",
            question="用 A 还是 B?",
            options=["option_a", "option_b"],
            decision={"choice": "option_a", "reason": "safer"},
            log_event_fn=fake_log_event,
        )

        assert len(recorded) == 1
        entry = recorded[0]
        assert entry["story_key"] == "FEAT-1"
        assert entry["stage"] == "implement"
        assert entry["event_type"] == "supervisor_decision"
        assert entry["payload"]["adapter"] == "claude"
        assert entry["payload"]["choice"] == "option_a"
        assert entry["payload"]["reason"] == "safer"


class TestHandlePtyOutput:
    def test_answers_and_logs_on_awaiting_hit(self):
        """is_awaiting 命中 → 决策 + pty.write 应答 + log。"""
        from types import SimpleNamespace

        writes: list[bytes] = []
        fake_pty = SimpleNamespace(write=lambda d: writes.append(d))
        logs: list[dict] = []

        def fake_log(story_key, *, stage, event_type, payload):
            logs.append({"story_key": story_key, "event_type": event_type, "payload": payload})

        def fake_awaiting(buffer):
            if "A 还是 B" in buffer:
                return ("用 A 还是 B?", ["option_a", "option_b"])
            return None

        def fake_llm(prompt):
            return '{"choice": "option_a", "reason": "safer"}'

        answered = handle_pty_output(
            buffer="提问: 用 A 还是 B?",
            pty=fake_pty,
            adapter="codex",
            story_facts={"story_key": "S-1", "stage": "implement"},
            is_awaiting_fn=fake_awaiting,
            llm_invoke=fake_llm,
            log_event_fn=fake_log,
        )

        assert answered is True
        assert writes == [b"option_a\r"]
        assert len(logs) == 1
        assert logs[0]["event_type"] == "supervisor_decision"
        assert logs[0]["payload"]["choice"] == "option_a"
