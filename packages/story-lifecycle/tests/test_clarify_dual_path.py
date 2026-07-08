"""T3.1 · clarify MCP 阻塞 + 交互式双路径。

覆盖 design 阶段两种 HITL 澄清路径:
1. MCP 路径(headless -p,有 MCP 工具):claude 调 ``mcp__lifecycle__clarify``,
   server 落 clarification_request → 阻塞等人答 → 回 MCP result。
2. 交互式终端路径(claude "query",无 MCP):prompt 文案让 claude "在终端直接问人"。

双路径必须互不串线:interactive=False 时引导用 MCP 工具;interactive=True 时引导
在终端直接问人,且绝不让交互式 claude 去调不存在的 MCP 工具。
"""

import pytest

from story_lifecycle.orchestrator.engine.prompt_sections import (
    build_design_dimensions_section,
)
from story_lifecycle.orchestrator.mcp.clarify_server import handle_clarify_call


class TestClarifyMcpBlockingPath:
    """MCP 阻塞路径:handle_clarify_call 落事件 + 等人答 + 回 result。"""

    def test_handle_clarify_call_logs_request_and_returns_answer(self):
        """fake poll 立即返回答,result.text 含人答。"""
        logged = []

        def fake_log(story_key, stage, event_type, payload):
            logged.append({"story_key": story_key, "event_type": event_type, "payload": payload})

        def fake_await(story_key, request_id, timeout):
            return "hc_config"

        result = handle_clarify_call(
            story_key="S-1",
            question="用户中心存哪?",
            options=["hc_user", "hc_config"],
            header="存储位置",
            log_event_fn=fake_log,
            await_answer_fn=fake_await,
            id_factory=lambda: "rid-mcp-1",
        )

        assert len(logged) == 1
        assert logged[0]["event_type"] == "clarification_request"
        p = logged[0]["payload"]
        assert p["id"] == "rid-mcp-1"
        assert p["question"] == "用户中心存哪?"
        assert p["options"] == ["hc_user", "hc_config"]
        assert p["header"] == "存储位置"

        assert result["isError"] is False
        assert result["content"] == [{"type": "text", "text": "hc_config"}]

    def test_handle_clarify_call_timeout_returns_conservative_fallback(self):
        """超时/无人答 → 回 conservative fallback 文案,isError=False(不无限卡)。"""
        result = handle_clarify_call(
            story_key="S-1",
            question="选 A 还是 B?",
            options=["A", "B"],
            log_event_fn=lambda *a, **k: None,
            await_answer_fn=lambda *a, **k: None,
            id_factory=lambda: "rid-mcp-2",
        )

        assert result["isError"] is False
        text = result["content"][0]["text"]
        assert "conservative" in text.lower() or "最保守" in text or "自行" in text


class TestClarifyInteractiveTerminalPath:
    """交互式终端路径:prompt 文案直接引导在终端问人。"""

    def test_interactive_branch_asks_human_in_terminal(self):
        """interactive=True 时含「在终端直接问人」,不含 MCP 工具调用。"""
        section = build_design_dimensions_section(
            story_key="S-1",
            workspace=".",
            stage="design",
            interactive=True,
        )

        assert "在终端直接问人" in section
        assert "mcp__lifecycle__clarify" not in section
        assert "拿到人答再继续" in section

    def test_non_interactive_branch_uses_mcp_tool(self):
        """interactive=False(默认 headless)时引导调用 mcp__lifecycle__clarify 工具。"""
        section = build_design_dimensions_section(
            story_key="S-1",
            workspace=".",
            stage="design",
            interactive=False,
        )

        assert "mcp__lifecycle__clarify" in section
        assert "在终端直接问人" not in section

    def test_non_design_stage_returns_empty(self):
        """该 section 只在 design stage 注入。"""
        for stage in ("implement", "verify", "deploy"):
            assert build_design_dimensions_section("S-1", ".", stage, interactive=True) == ""
