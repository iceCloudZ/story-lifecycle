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


class TestRunServerStdioChain:
    """集成:mock-claude 经 stdio 驱动真 clarify_server.run_server 跑通全链路。

    覆盖 ``run_server`` 的 stdio JSONRPC 循环(单测难覆盖的 I/O 层):MCP 握手 →
    tools/call clarify → server 落 clarification_request + 阻塞轮询 DB → answer 落 →
    返回人答。真 claude 调用 MCP 的那半(网关侧)另由 real-claude 探针验证(memory)。
    """

    def test_handshake_call_block_answer_respond(self, isolated_story_home, monkeypatch):
        import json as _json
        import os as _os
        import subprocess as _sp
        import sys as _sys
        import threading as _th
        import time as _time

        from story_lifecycle.infra.db import models as _db

        # isolated_story_home(经 _isolated_db autouse)已把 STORY_HOME 指向带 schema 的
        # 隔离 DB(event_log 在)。server 子进程继承同一 STORY_HOME → 同一 DB。
        _home = _os.environ["STORY_HOME"]
        key = "MOCK-SRV-1"
        _db.upsert_story(key, title="m", workspace=_home, profile="minimal", status="active")
        monkeypatch.setenv("STORY_KEY", key)

        # PYTHONPATH 让子进程 `python -m story_lifecycle...` 能 import。
        import story_lifecycle as _sl
        _src_root = _os.path.dirname(_os.path.dirname(_os.path.dirname(_sl.__file__)))
        _env = {**_os.environ, "PYTHONPATH": _src_root + _os.pathsep + _os.environ.get("PYTHONPATH", "")}

        srv = _sp.Popen(
            [_sys.executable, "-m", "story_lifecycle.orchestrator.mcp.clarify_server"],
            stdin=_sp.PIPE, stdout=_sp.PIPE, stderr=_sp.PIPE,
            env=_env, text=True, encoding="utf-8", errors="replace",
        )
        try:
            def send(o):
                srv.stdin.write(_json.dumps(o, ensure_ascii=False) + "\n")
                srv.stdin.flush()

            def recv():
                line = srv.stdout.readline()
                if not line:
                    err = srv.stderr.read() if srv.stderr else ""
                    raise AssertionError(
                        f"server stdout EOF (poll={srv.poll()}): stderr={err[:2000]}"
                    )
                return _json.loads(line)

            send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            assert "result" in recv()
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            assert "clarify" in [t["name"] for t in recv()["result"]["tools"]]

            # 后台:轮询 DB,一见 clarification_request 就回 answer
            def answerer():
                for _ in range(60):
                    for ev in _db.get_story_events(key):
                        if ev.get("event_type") == "clarification_request":
                            p = _db.parse_event_payload(ev)
                            _db.log_event(key, "design", "clarification_answer",
                                          {"id": p.get("id"), "answer": "hc_user"})
                            return
                    _time.sleep(0.3)

            _th.Thread(target=answerer, daemon=True).start()

            send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "clarify",
                "arguments": {"question": "hc_user or hc_config?", "options": ["hc_user", "hc_config"]},
            }})
            r = recv()
            assert r["result"]["content"][0]["text"] == "hc_user"
            assert r["result"]["isError"] is False

            types = [e.get("event_type") for e in _db.get_story_events(key)]
            assert types == ["clarification_request", "clarification_answer"]
        finally:
            try:
                srv.stdin.close()
                srv.wait(timeout=5)
            except Exception:
                srv.kill()
