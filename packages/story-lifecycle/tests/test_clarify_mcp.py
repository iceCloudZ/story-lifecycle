"""Tests for the design-clarify MCP server (runbook HITL 重做,外接 MCP 方案)。

design 阶段「claude 逐问 + 人答」走**外接 stdio MCP server**:claude 调
``mcp__lifecycle__clarify(question,options)`` → server 落 clarification_request 事件 →
阻塞轮询 DB 等 clarification_answer → 回 MCP result → claude 带上下文继续(不重 spawn)。

本测覆盖可单测的纯核心:`handle_clarify_call`(emit+await 注入)、`poll_clarify_answer`
(get_events 注入)。stdio JSONRPC 循环(run_server)是薄 I/O 层,不在此单测。
"""

import pytest

from story_lifecycle.orchestrator.mcp.clarify_server import (
    CLARIFY_TOOL,
    get_pending_clarification,
    handle_clarify_call,
    poll_clarify_answer,
)


class TestHandleClarifyCall:
    def test_emits_request_awaits_answer_returns_mcp_result(self):
        """调 clarify → 落 clarification_request 事件 → 阻塞等人答 → 回 MCP result。"""
        logged = []

        def fake_log(story_key, stage, event_type, payload):
            logged.append({"story_key": story_key, "event_type": event_type, "payload": payload})

        def fake_await(story_key, request_id, timeout):
            assert request_id == "rid-1"  # id 透传到 await
            return "hc_user"

        result = handle_clarify_call(
            story_key="S-1",
            question="存哪?",
            options=["hc_user", "hc_config"],
            header="存储位置",
            log_event_fn=fake_log,
            await_answer_fn=fake_await,
            id_factory=lambda: "rid-1",
        )

        # 落了 clarification_request 事件(id/question/options/header)
        assert len(logged) == 1
        assert logged[0]["event_type"] == "clarification_request"
        p = logged[0]["payload"]
        assert p["id"] == "rid-1"
        assert p["question"] == "存哪?"
        assert p["options"] == ["hc_user", "hc_config"]
        assert p["header"] == "存储位置"
        # 回 MCP result(text 内容 = 人答)
        assert result["isError"] is False
        assert result["content"] == [{"type": "text", "text": "hc_user"}]

    def test_header_defaults_to_question(self):
        """header 缺省取 question。"""
        logged = []
        handle_clarify_call(
            story_key="S-1",
            question="用 A 还是 B?",
            options=["A", "B"],
            log_event_fn=lambda *a, **k: logged.append(a[3]),  # payload 是第 4 个位置参
            await_answer_fn=lambda *a, **k: "A",
            id_factory=lambda: "x",
        )
        assert logged[0]["header"] == "用 A 还是 B?"

    def test_timeout_returns_proceed_hint_not_error(self):
        """超时/无人答 → 回「自行判断」提示,isError=False(绝不无限卡 claude)。"""
        result = handle_clarify_call(
            story_key="S-1",
            question="q?",
            options=["a", "b"],
            log_event_fn=lambda *a, **k: None,
            await_answer_fn=lambda *a, **k: None,  # 超时
            id_factory=lambda: "x",
        )
        assert result["isError"] is False
        text = result["content"][0]["text"]
        assert "判断" in text or "proceed" in text.lower()  # 让 claude 自行决断


class TestPollClarifyAnswer:
    def test_returns_answer_once_event_appears(self):
        """轮询 DB:clarification_answer 事件一出现就返回其 answer。"""
        calls = {"n": 0}

        def fake_get_events(story_key):
            calls["n"] += 1
            if calls["n"] < 2:
                return []  # 第一次没有
            return [
                {"event_type": "clarification_answer", "payload": {"id": "r9", "answer": "hc_config"}}
            ]

        ans = poll_clarify_answer(
            "S-1", "r9", get_events_fn=fake_get_events,
            max_polls=5, sleep_fn=lambda _s: None,
        )
        assert ans == "hc_config"
        assert calls["n"] == 2  # 第二次拿到

    def test_ignores_answer_with_other_id(self):
        """id 不匹配的事件不算(防串答)。"""
        def fake_get_events(story_key):
            return [{"event_type": "clarification_answer", "payload": {"id": "other", "answer": "x"}}]

        ans = poll_clarify_answer(
            "S-1", "r9", get_events_fn=fake_get_events,
            max_polls=2, sleep_fn=lambda _s: None,
        )
        assert ans is None  # 没有 id=r9 的答

    def test_timeout_returns_none(self):
        """一直没人答 → 轮到上限返回 None。"""
        def fake_get_events(story_key):
            return []
        ans = poll_clarify_answer(
            "S-1", "r9", get_events_fn=fake_get_events,
            max_polls=3, sleep_fn=lambda _s: None,
        )
        assert ans is None


class TestClarifyToolSchema:
    def test_tool_name_and_schema_present(self):
        """MCP tools/list 暴露的 clarify 工具定义(name + inputSchema)。"""
        assert CLARIFY_TOOL["name"] == "clarify"
        schema = CLARIFY_TOOL["inputSchema"]
        assert "question" in schema["required"]
        assert "options" in schema["required"]


class TestGetPendingClarification:
    """GET /clarify 的核心:DB 事件里找「最新未答的 clarification_request」。

    事件驱动(MCP 方案):MCP server 落 clarification_request,POST /clarify/answer 落
    clarification_answer。pending = 最新 request 且无匹配 id 的 answer。
    """

    def test_returns_latest_unanswered_request(self):
        events = [
            {"event_type": "clarification_request", "payload": {"id": "r1", "question": "Q1", "options": ["a", "b"], "header": "H1"}},
            {"event_type": "clarification_answer", "payload": {"id": "r1", "answer": "a"}},
            {"event_type": "clarification_request", "payload": {"id": "r2", "question": "Q2", "options": ["c", "d"], "header": "H2"}},
        ]
        pending = get_pending_clarification("S-1", get_events_fn=lambda _k: events)
        assert pending is not None
        assert pending["id"] == "r2"  # 最新且未答
        assert pending["question"] == "Q2"
        assert pending["options"] == ["c", "d"]

    def test_none_when_latest_request_answered(self):
        events = [
            {"event_type": "clarification_request", "payload": {"id": "r1", "question": "Q1", "options": ["a"]}},
            {"event_type": "clarification_answer", "payload": {"id": "r1", "answer": "a"}},
        ]
        assert get_pending_clarification("S-1", get_events_fn=lambda _k: events) is None

    def test_none_when_no_request(self):
        assert get_pending_clarification("S-1", get_events_fn=lambda _k: []) is None
