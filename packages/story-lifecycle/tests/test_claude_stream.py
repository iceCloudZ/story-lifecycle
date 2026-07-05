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
    build_resume_command,
    decide_permission,
    extract_awaiting,
    parse_line,
    permission_tool_response,
    supervise_claude_stream,
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


class TestPermissionToolResponse:
    """permission_tool_response:把 decide_permission 包成 MCP --permission-prompt-tool 的返回形。

    MCP 契约:返回 ``{behavior: allow|deny, updatedInput, message}``。
    """

    def test_allow_response_shape(self):
        def fake_llm(p):
            return '{"choice":"allow","reason":"safe"}'

        r = permission_tool_response(
            tool_name="Read",
            tool_input={"file_path": "README.md"},
            story_facts={"story_key": "M-1", "stage": "implement"},
            llm_invoke=fake_llm,
        )
        assert r["behavior"] == "allow"
        assert r["updatedInput"] == {"file_path": "README.md"}  # 原样回传
        assert "safe" in r["message"]

    def test_deny_response_shape(self):
        def fake_llm(p):
            return '{"choice":"deny","reason":"destructive"}'

        r = permission_tool_response(
            tool_name="Bash",
            tool_input={"command": "rm -rf /"},
            story_facts={"story_key": "M-2", "stage": "implement"},
            llm_invoke=fake_llm,
        )
        assert r["behavior"] == "deny"
        assert r["updatedInput"] == {"command": "rm -rf /"}

    def test_logs_supervisor_decision_when_log_fn_given(self):
        def fake_llm(p):
            return '{"choice":"deny","reason":"no"}'

        logged = []

        def fake_log(story_key, stage, event_type, payload):
            logged.append({"story_key": story_key, "event_type": event_type, "payload": payload})

        permission_tool_response(
            tool_name="Write",
            tool_input={"file_path": "/etc/x"},
            story_facts={"story_key": "M-3", "stage": "verify"},
            llm_invoke=fake_llm,
            log_event_fn=fake_log,
        )
        assert len(logged) == 1
        assert logged[0]["event_type"] == "supervisor_decision"
        assert logged[0]["payload"]["choice"] == "deny"
        assert logged[0]["payload"]["adapter"] == "claude"

    def test_no_log_fn_does_not_crash(self):
        def fake_llm(p):
            return '{"choice":"allow","reason":"ok"}'

        r = permission_tool_response(
            tool_name="Read",
            tool_input={},
            story_facts={"story_key": "M-4", "stage": "implement"},
            llm_invoke=fake_llm,
        )
        assert r["behavior"] == "allow"


class TestSuperviseClaudeStream:
    """supervise_claude_stream:Claude 轨决策循环(消费 stream-json 行 → decide → log)。

    defer/resume 路径(0b-2 选项 b,不走 MCP):lifecycle 跑 ``claude -p --output-format stream-json``、
    本函数消费其行流,命中 awaiting(permission_request / elicitation)→ decide_response → 落
    supervisor_decision;Handler 再用决策 ``claude --resume`` 回填。决策循环与 PTY 轨共用 decide_response。
    """

    @staticmethod
    def _lines(*names):
        return [SAMPLES[n] for n in names]

    def test_non_awaiting_lines_yield_no_decisions(self):
        """真 init / Write tool_use / result 行都不触发决策。"""
        calls = {"n": 0, "logs": 0}

        def fake_llm(p):
            calls["n"] += 1
            return '{"choice":"allow","reason":"x"}'

        def fake_log(*a, **k):
            calls["logs"] += 1

        decisions = supervise_claude_stream(
            lines=self._lines("system_init", "assistant_tool_use_bash", "result_success"),
            story_facts={"story_key": "C-1", "stage": "implement"},
            llm_invoke=fake_llm,
            log_event_fn=fake_log,
        )
        assert decisions == []
        assert calls["n"] == 0
        assert calls["logs"] == 0

    def test_permission_request_line_yields_decision_and_logs(self):
        logged = []

        def fake_llm(p):
            return '{"choice":"deny","reason":"destructive"}'

        def fake_log(story_key, stage, event_type, payload):
            logged.append({"story_key": story_key, "event_type": event_type, "payload": payload})

        perm_line = json.dumps(
            {"type": "permission_request", "tool_name": "Bash", "input": {"command": "rm -rf /"}}
        )
        decisions = supervise_claude_stream(
            lines=[SAMPLES["system_init"], perm_line],
            story_facts={"story_key": "C-2", "stage": "implement"},
            llm_invoke=fake_llm,
            log_event_fn=fake_log,
        )
        assert len(decisions) == 1
        assert decisions[0]["choice"] == "deny"
        assert decisions[0]["options"] == [ALLOW, DENY]
        assert len(logged) == 1
        assert logged[0]["event_type"] == "supervisor_decision"
        assert logged[0]["payload"]["adapter"] == "claude"
        assert logged[0]["payload"]["choice"] == "deny"

    def test_elicitation_line_yields_decision_with_its_options(self):
        def fake_llm(p):
            return '{"choice":"A","reason":"faster"}'

        eli_line = json.dumps(
            {"type": "elicitation", "message": "用 A 还是 B?", "options": ["A", "B"]}
        )
        decisions = supervise_claude_stream(
            lines=[eli_line],
            story_facts={"story_key": "C-3", "stage": "design"},
            llm_invoke=fake_llm,
            log_event_fn=lambda *a, **k: None,
        )
        assert decisions[0]["choice"] == "A"
        assert decisions[0]["options"] == ["A", "B"]

    def test_multiple_awaiting_lines_yield_multiple_decisions(self):
        n = {"c": 0}

        def fake_llm(p):
            n["c"] += 1
            return '{"choice":"allow","reason":"ok"}'

        lines = [
            json.dumps({"type": "permission_request", "tool_name": "Read", "input": {}}),
            SAMPLES["result_success"],
            json.dumps({"type": "permission_request", "tool_name": "Glob", "input": {}}),
        ]
        decisions = supervise_claude_stream(
            lines=lines,
            story_facts={"story_key": "C-4", "stage": "implement"},
            llm_invoke=fake_llm,
            log_event_fn=lambda *a, **k: None,
        )
        assert len(decisions) == 2  # 中间 result 行不算
        assert n["c"] == 2


class TestBuildResumeCommand:
    """build_resume_command:把 supervisor 决策包成 claude -p --resume argv(0b-3 回填半)。

    注:真回填(答案怎么注入 Claude)是 Claude 版本相关 + 本机全 allow 无法触发验证;
    本函数只构造文档化的 resume 基命令,决策本身已由 supervise_claude_stream 落日志。
    """

    def test_returns_resume_argv_with_stream_json(self):
        cmd = build_resume_command(
            session_id="sess-123", decision={"choice": "deny", "reason": "x"}
        )
        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "--resume" in cmd
        assert "sess-123" in cmd
        assert "--output-format" in cmd  # 继续接 stream-json(supervisor 持续监督)

    def test_custom_claude_binary(self):
        cmd = build_resume_command(
            session_id="s1", decision={"behavior": "allow"}, claude_bin="/usr/local/bin/claude"
        )
        assert cmd[0] == "/usr/local/bin/claude"

    def test_empty_session_id_raises(self):
        import pytest

        with pytest.raises(ValueError):
            build_resume_command(session_id="", decision={"choice": "allow"})


from story_lifecycle.orchestrator.engine.claude_stream import supervise_headless_stdout


class TestSuperviseHeadlessStdout:
    """supervise_headless_stdout:同步消费 headless proc stdout(drain + 检测提问 + 决策/日志)。

    双重价值:(a) 观察层1 提问(permission_request/elicitation)→ decide_response + 落 supervisor_decision;
    (b) drain stdout 防 PIPE 缓冲满致 headless proc 阻塞(主循环只轮询 done file,从不读 stdout)。
    headless stdin 已关 → observe-only(检测+决策+日志,不回写 agent)。
    """

    def test_drains_stdout_and_detects_permission_request(self):
        import io

        sample_lines = [
            b'{"type":"system","subtype":"init","session_id":"x"}\n',
            b'{"type":"permission_request","tool_name":"Bash","input":{"command":"rm -rf /"}}\n',
            b'{"type":"result","subtype":"success"}\n',
        ]
        fake_proc = type("P", (), {"stdout": io.BytesIO(b"".join(sample_lines))})()

        def fake_llm(p):
            return '{"choice":"deny","reason":"destructive"}'

        logged = []

        def fake_log(story_key, stage, event_type, payload):
            logged.append(event_type)

        decisions = supervise_headless_stdout(
            proc=fake_proc,
            adapter="claude",
            story_facts={"story_key": "H-1", "stage": "design"},
            llm_invoke=fake_llm,
            log_event_fn=fake_log,
        )
        assert len(decisions) == 1
        assert decisions[0]["choice"] == "deny"
        assert "supervisor_decision" in logged

    def test_kimi_text_path_uses_awaiting_detector(self):
        import io

        # kimi 输出是文本(非 stream-json),含中文选择提问
        fake_proc = type("P", (), {"stdout": io.BytesIO("请选择: A) 重试 B) 跳过\n".encode("utf-8"))})()

        def fake_llm(p):
            return '{"choice":"A","reason":"retry"}'

        decisions = supervise_headless_stdout(
            proc=fake_proc,
            adapter="kimi",
            story_facts={"story_key": "H-2", "stage": "implement"},
            llm_invoke=fake_llm,
            log_event_fn=lambda *a, **k: None,
        )
        assert len(decisions) == 1
        assert decisions[0]["choice"] in ("A", "B")

    def test_no_question_no_decision(self):
        import io

        fake_proc = type("P", (), {"stdout": io.BytesIO(b'{"type":"system","subtype":"init"}\n')})()
        calls = {"n": 0}

        def fake_llm(p):
            calls["n"] += 1
            return '{"choice":"allow","reason":"x"}'

        decisions = supervise_headless_stdout(
            proc=fake_proc,
            adapter="claude",
            story_facts={"story_key": "H-3", "stage": "design"},
            llm_invoke=fake_llm,
            log_event_fn=lambda *a, **k: None,
        )
        assert decisions == []
        assert calls["n"] == 0  # 没提问不调 LLM




