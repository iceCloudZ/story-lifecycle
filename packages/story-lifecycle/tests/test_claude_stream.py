"""Tests for claude_stream —— Claude 轨 stream-json 解析(0b-1)。

Claude 走 ``claude -p --output-format stream-json``(非 PTY)。本模块解析事件流,
识别 Claude "在等人" 的信号(permission MCP 工具调用 / permission_request / elicitation),
产出统一 ``(question, options)`` 喂同一个 ``decide_response``。

fixtures 取自真跑的 stream-json(system/init、assistant tool_use Write、result/success),
保证解析器对真实事件不误判。
"""

import json
from pathlib import Path

from story_lifecycle.orchestrator.engine.claude_stream import (
    ALLOW,
    DENY,
    decide_permission,
    extract_awaiting,
    parse_line,
)

_FIX = Path(__file__).parent / "fixtures" / "claude_stream_samples.json"
SAMPLES = {s["name"]: s["line"] for s in json.loads(_FIX.read_text(encoding="utf-8"))}


class TestParseLine:
    def test_parses_real_init_event(self):
        e = parse_line(SAMPLES["system_init"])
        assert e["type"] == "system"
        assert e["subtype"] == "init"
        assert e["permissionMode"] == "default"

    def test_returns_none_for_non_json(self):
        assert parse_line("not json at all") is None

    def test_returns_none_for_blank(self):
        assert parse_line("") is None
        assert parse_line("   ") is None


class TestExtractAwaiting:
    def test_real_init_not_awaiting(self):
        assert extract_awaiting(SAMPLES["system_init"]) is None

    def test_real_write_tool_use_not_awaiting(self):
        """真实 assistant/tool_use(Write)是正常工具调用,不是 permission MCP 工具 → None。"""
        assert extract_awaiting(SAMPLES["assistant_tool_use_bash"]) is None

    def test_real_result_not_awaiting(self):
        assert extract_awaiting(SAMPLES["result_success"]) is None

    def test_permission_mcp_tool_call_is_awaiting(self):
        """Claude 调 lifecycle 的 permission MCP 工具 → 在等人(要 allow/deny 决策)。"""
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "c1",
                            "name": "mcp__lifecycle__permission",
                            "input": {
                                "tool_name": "Bash",
                                "input": {"command": "rm -rf /tmp/x"},
                            },
                        }
                    ]
                },
            }
        )
        r = extract_awaiting(line)
        assert r is not None
        question, options = r
        assert "Bash" in question
        assert options == [ALLOW, DENY]

    def test_permission_request_event_is_awaiting(self):
        """裸 permission_request 事件 → (question, [allow, deny])。"""
        line = json.dumps(
            {
                "type": "permission_request",
                "tool_name": "Write",
                "input": {"file_path": "/etc/passwd"},
            }
        )
        r = extract_awaiting(line)
        assert r is not None
        assert "Write" in r[0]
        assert r[1] == [ALLOW, DENY]

    def test_elicitation_with_options_is_awaiting(self):
        line = json.dumps(
            {"type": "elicitation", "message": "用方案 A 还是 B?", "options": ["A", "B"]}
        )
        r = extract_awaiting(line)
        assert r is not None
        assert "用方案" in r[0]
        assert r[1] == ["A", "B"]

    def test_custom_permission_tool_name(self):
        """可配置 permission 工具名(不同部署可能用不同 MCP 名)。"""
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "c2",
                            "name": "mcp__myorg__gate",
                            "input": {"tool_name": "Edit"},
                        }
                    ]
                },
            }
        )
        assert extract_awaiting(line, permission_tool="mcp__myorg__gate") is not None
        # 默认工具名不匹配 → None
        assert extract_awaiting(line) is None


class TestDecidePermission:
    def test_returns_deny_when_llm_chooses_deny(self):
        def fake_llm(prompt: str) -> str:
            return '{"choice": "deny", "reason": "destructive command"}'

        d = decide_permission(
            tool_name="Bash",
            tool_input={"command": "rm -rf /"},
            story_facts={"story_key": "S-1", "stage": "implement"},
            llm_invoke=fake_llm,
        )
        assert d["behavior"] == DENY
        assert "destructive" in d["reason"]

    def test_returns_allow_when_llm_chooses_allow(self):
        def fake_llm(prompt: str) -> str:
            return '{"choice": "allow", "reason": "safe read"}'

        d = decide_permission(
            tool_name="Read",
            tool_input={"file_path": "README.md"},
            story_facts={"story_key": "S-2", "stage": "implement"},
            llm_invoke=fake_llm,
        )
        assert d["behavior"] == ALLOW
