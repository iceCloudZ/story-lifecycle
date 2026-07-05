"""Tests for replanner(层2 执行反馈→重规划)。

``replan`` 在 decide_transition 选 swap_approach / 一个 stage 的做法整条错时,拿执行反馈
重规划:复用 ``planner.run_orchestrator_agent`` 的 ``invoke_with_tools`` + plan_step 工具。
纯 Decider 部分(build_replan_messages)+ 注入 invoke_with_tools 的循环(可测)。
"""

import json

from story_lifecycle.orchestrator.engine.replanner import (
    build_replan_messages,
    replan,
)


class TestBuildReplanMessages:
    def test_returns_system_and_user_messages(self):
        msgs = build_replan_messages(
            story_facts={"story_key": "S-1", "stage": "implement", "summary": "add notif"},
            feedback={"stage": "implement", "failure_mode": "quality", "reason": "缺错误处理"},
            prior_actions=[{"action": "launch", "stage": "implement", "adapter": "codex"}],
        )
        assert len(msgs) >= 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"

    def test_user_message_contains_feedback_and_prior(self):
        msgs = build_replan_messages(
            story_facts={"story_key": "S-2", "stage": "implement"},
            feedback={"stage": "verify", "failure_mode": "tests", "reason": "test_login fail"},
            prior_actions=[{"action": "launch", "stage": "implement", "adapter": "claude"}],
        )
        user_text = msgs[1]["content"]
        assert "test_login" in user_text  # feedback reason 进 prompt
        assert "verify" in user_text
        assert "implement" in user_text  # prior actions 摘要进 prompt

    def test_system_message_instructs_replan(self):
        msgs = build_replan_messages(
            story_facts={"story_key": "S-3", "stage": "x"},
            feedback={"stage": "x", "reason": "y"},
            prior_actions=[],
        )
        sys_text = msgs[0]["content"]
        assert "重规划" in sys_text or "重新" in sys_text or "plan_step" in sys_text


class TestReplan:
    def test_collects_plan_step_actions_from_tool_calls(self):
        """注入的 invoke_with_tools 返回 plan_step 调用 → replan 收成 actions。"""
        calls = {"n": 0}

        def fake_invoke(messages, tools, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return {
                    "message": {"role": "assistant", "content": ""},
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": "plan_step",
                                "arguments": {"stage": "implement", "adapter": "kimi", "focus": "加错误处理"},
                            },
                        }
                    ],
                    "content": "",
                }
            return {"message": {"role": "assistant", "content": "done"}, "tool_calls": [], "content": "done"}

        actions = replan(
            story_facts={"story_key": "S-4", "stage": "implement"},
            feedback={"stage": "implement", "reason": "缺错误处理"},
            prior_actions=[],
            invoke_with_tools=fake_invoke,
            tools=[],
        )
        assert len(actions) == 1
        assert actions[0]["action"] == "launch"
        assert actions[0]["stage"] == "implement"
        assert actions[0]["adapter"] == "kimi"

    def test_stops_when_no_more_tool_calls(self):
        """LLM 不再调工具 → 停(不死循环)。"""
        calls = {"n": 0}

        def fake_invoke(messages, tools, **kwargs):
            calls["n"] += 1
            # 第 1 次返回一个 plan_step,第 2 次返回空(说完了)
            if calls["n"] == 1:
                return {
                    "message": {"role": "assistant", "content": ""},
                    "tool_calls": [
                        {"id": "c1", "type": "function",
                         "function": {"name": "plan_step",
                                      "arguments": {"stage": "verify", "adapter": "claude"}}},
                    ],
                    "content": "",
                }
            return {"message": {"role": "assistant", "content": "done"}, "tool_calls": [], "content": "done"}

        actions = replan(
            story_facts={"story_key": "S-5", "stage": "verify"},
            feedback={"stage": "verify", "reason": "x"},
            prior_actions=[],
            invoke_with_tools=fake_invoke,
            tools=[],
        )
        assert len(actions) == 1  # 只收第 1 次的 plan_step
        assert calls["n"] == 2  # 第 2 次空 → 停

    def test_skip_stage_tool_call_becomes_skip_action(self):
        def fake_invoke(messages, tools, **kwargs):
            return {
                "message": {"role": "assistant", "content": ""},
                "tool_calls": [
                    {"id": "c1", "type": "function",
                     "function": {"name": "skip_stage",
                                  "arguments": {"stage": "release", "reason": "低价值跳过"}}},
                ],
                "content": "",
            }

        actions = replan(
            story_facts={"story_key": "S-6", "stage": "release"},
            feedback={"stage": "release", "reason": "x"},
            prior_actions=[],
            invoke_with_tools=fake_invoke,
            tools=[],
        )
        assert actions[0]["action"] == "skip"
        assert actions[0]["stage"] == "release"
