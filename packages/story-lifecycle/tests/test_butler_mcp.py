"""管家 WP2 MCP 工具面测试(DESIGN-story-butler §3.2/§9 WP2)。

覆盖 butler_server 的纯核心(HTTP 经 fetch/send 注入,**绝不真起 serve**):
- 工具 schema/注册(tools/list 的六个 method);
- confirm_token 执法:错 token 拒绝(说明正确拼法)/ token 对但终态拒绝 /
  正确非终态通过(断言 POST body 带 confirmed_via);
- serve 未启动的友好错误路径(monkeypatch urlopen 抛 URLError,不抛异常);
- story_detail 的 gate target_state 字段存在性;
- stdio JSONRPC 循环(in-process,monkeypatch dispatch_tool)。

serve 侧 confirmGates/confirmed_via 增补的路由测试在 test_butler_confirm_gates_api.py。
"""

import io
import json
import urllib.error

import pytest

from story_lifecycle.orchestrator.mcp import butler_server as bs
from story_lifecycle.orchestrator.mcp.butler_server import (
    BUTLER_TOOLS,
    build_confirm_token,
    check_confirm_token,
    dispatch_tool,
    make_fetch,
    make_send,
    tool_knowledge_search,
    tool_patrol_summary,
    tool_plan_confirm,
    tool_session_register,
    tool_story_advance,
    tool_story_detail,
    tool_story_list,
    write_butler_mcp_config,
)

KEY = "tapd-butler-1"


def _gate(**over):
    """story_state 门的故事详情(confirmGates 形状 = serve 端 _pending_confirm_gates)。"""
    g = {
        "kind": "story_state",
        "fromState": "开发",
        "targetState": "测试",
        "finalTarget": "结项",
        "label": "进入测试",
        "targetIsTerminal": False,
    }
    g.update(over)
    return g


def _story_payload(gates):
    return {
        "storyKey": KEY,
        "title": "管家测试 story",
        "status": "paused",
        "lifecycleState": "开发",
        "currentStage": "build",
        "lastError": "",
        "planConfirmed": True,
        "hasPlan": True,
        "workspace": "D:/worktrees/demo",
        "confirmGates": gates,
    }


class _FakeApi:
    """记录调用的 fake serve:GET 路径分发,变更调用记 (method, path, body)。"""

    def __init__(self, gets=None):
        self.gets = gets or {}
        self.calls = []

    def fetch(self, path):
        if path in self.gets:
            return self.gets[path]
        raise AssertionError(f"unexpected GET {path}")

    def send(self, method, path, body=None):
        self.calls.append({"method": method, "path": path, "body": body})
        return 200, {"ok": True}


def test_build_confirm_token_format():
    """confirm_token 契约:精确等于 f"{story_key}:{target_state}"。"""
    assert build_confirm_token(KEY, "测试") == f"{KEY}:测试"


# ---- 工具 schema / 注册 ----


class TestToolSchemas:
    def test_seven_tools_with_expected_names(self):
        """tools/list 暴露七个下划线风格 method(WP-F §8.2 加 knowledge_search)。"""
        assert [t["name"] for t in BUTLER_TOOLS] == [
            "story_list",
            "story_detail",
            "patrol_summary",
            "story_advance",
            "plan_confirm",
            "session_register",
            "knowledge_search",
        ]

    def test_schemas_have_required_args(self):
        by_name = {t["name"]: t for t in BUTLER_TOOLS}
        assert by_name["story_detail"]["inputSchema"]["required"] == ["key"]
        assert by_name["story_advance"]["inputSchema"]["required"] == [
            "key",
            "confirm_token",
        ]
        assert by_name["plan_confirm"]["inputSchema"]["required"] == [
            "key",
            "confirm_token",
        ]
        assert by_name["session_register"]["inputSchema"]["required"] == [
            "key",
            "session_id",
        ]
        # 每个工具都有给 LLM 看的 description
        assert all(t["description"] for t in BUTLER_TOOLS)


# ---- story_list / story_detail ----


class TestStoryList:
    def test_rows_and_gate_flag(self):
        api = _FakeApi({
            "/api/story": [
                {
                    "storyKey": KEY,
                    "title": "A",
                    "lifecycleState": "开发",
                    "currentStage": "build",
                    "status": "paused",
                    "awaitingConfirm": True,
                },
                {
                    "storyKey": "tapd-butler-2",
                    "title": "B",
                    "lifecycleState": "待启动",
                    "currentStage": "",
                    "status": "active",
                    "awaitingConfirm": False,
                },
            ]
        })
        res = tool_story_list(api.fetch)
        assert res["ok"] is True
        assert res["count"] == 2
        assert res["stories"][0]["atConfirmGate"] is True
        assert res["stories"][1]["atConfirmGate"] is False
        assert "1 个停在确认门" in res["summary"]

    def test_status_filter_appended_as_query(self):
        api = _FakeApi({"/api/story?status=paused": []})
        res = tool_story_list(api.fetch, status="paused")
        assert res["count"] == 0
        assert "列表为空" in res["summary"]


class TestStoryDetail:
    def test_gate_target_state_present(self):
        """story_detail 的确认门详情必须含 targetState(LLM 拼 confirm_token 的材料)。"""
        api = _FakeApi({
            f"/api/story/{KEY}": _story_payload([_gate()]),
            f"/api/story/{KEY}/plan": {"stages": [{"name": "build", "done": False}]},
            f"/api/story/{KEY}/timeline": {"decisions": [
                {"stage": "build", "decision": "approve", "human_message": "成果物齐", "created_at": "2026-09-01"}
            ]},
            f"/api/story/{KEY}/deliverables": {"deliverables": [], "gate": None},
            f"/api/story/{KEY}/docs": {"docs": []},
            f"/api/story/{KEY}/sessions": {"sessions": []},
        })
        res = tool_story_detail(api.fetch, KEY)
        assert res["ok"] is True
        gate = res["confirmGates"][0]
        assert "targetState" in gate
        assert gate["targetState"] == "测试"
        # 给 LLM 的拼法提示也在(targetState 可直接用)
        assert gate["confirmTokenFormat"] == f"{KEY}:测试"
        assert "确认门" in res["summary"] and "测试" in res["summary"]
        assert res["nextStep"] and "story_advance" in res["nextStep"]

    def test_best_effort_endpoints_missing_ok(self):
        """辅助端点(plan/timeline/deliverables)缺失时详情主数据仍返回。"""
        api = _FakeApi({f"/api/story/{KEY}": _story_payload([])})
        res = tool_story_detail(api.fetch, KEY)
        assert res["ok"] is True
        assert res["confirmGates"] == []
        assert res["stageProgress"] == []
        assert "无挂起确认门" in res["summary"]

    def test_terminal_gate_suggests_desktop_review(self):
        """终态门在详情里就明确指向桌面评审/UI,不引导走 MCP。"""
        api = _FakeApi({
            f"/api/story/{KEY}": _story_payload([
                _gate(targetState="上线", targetIsTerminal=True)
            ]),
        })
        res = tool_story_detail(api.fetch, KEY)
        assert "桌面评审" in res["summary"]


# ---- confirm_token 执法 ----


class TestConfirmTokenEnforcement:
    def _api_with_gate(self, gates):
        """单门(dict)或门列表(list)都收;空列表 = 无门。"""
        if isinstance(gates, dict):
            gates = [gates]
        return _FakeApi({f"/api/story/{KEY}": _story_payload(gates)})

    def test_wrong_token_refused_with_recipe(self):
        """错 token → 拒绝并说明正确拼法。"""
        api = self._api_with_gate(_gate())
        res = tool_story_advance(api.fetch, api.send, KEY, f"{KEY}:上线")
        assert res["ok"] is False
        assert res["refused"] == "token_mismatch"
        assert res["expectedToken"] == f"{KEY}:测试"
        assert "正确拼法" in res["summary"]
        assert api.calls == []  # 执法挡在调用 serve 之前

    def test_correct_token_but_terminal_refused(self):
        """token 正确但 target 终态/发布类 → 一律拒绝(高危只在桌面评审)。"""
        api = self._api_with_gate(_gate(targetState="上线", targetIsTerminal=True))
        res = tool_story_advance(api.fetch, api.send, KEY, f"{KEY}:上线")
        assert res["ok"] is False
        assert res["refused"] == "terminal_target"
        assert "桌面评审" in res["summary"]
        assert api.calls == []

    def test_correct_non_terminal_passes_with_confirmed_via(self):
        """正确非终态 token → POST /lifecycle/advance 且 body 带 confirmed_via。"""
        api = self._api_with_gate(_gate())
        res = tool_story_advance(api.fetch, api.send, KEY, f"{KEY}:测试", "desktop")
        assert res["ok"] is True
        assert len(api.calls) == 1
        call = api.calls[0]
        assert call["method"] == "POST"
        assert call["path"] == f"/api/story/{KEY}/lifecycle/advance"
        assert call["body"] == {"confirmed_via": "desktop"}
        assert "confirmed_via=desktop" in res["summary"]

    def test_stage_gate_advance_uses_put_advance(self):
        """stage 间确认门 → PUT /api/story/{key}/advance(透传 confirmed_via)。"""
        api = self._api_with_gate(_gate(kind="stage", completedStage="build",
                                        targetState="verify", targetIsTerminal=False))
        res = tool_story_advance(api.fetch, api.send, KEY, f"{KEY}:verify", "wechat")
        assert res["ok"] is True
        call = api.calls[0]
        assert call["method"] == "PUT"
        assert call["path"] == f"/api/story/{KEY}/advance"
        assert call["body"] == {"confirmed_via": "wechat"}

    def test_invalid_confirmed_via_refused(self):
        """confirmed_via 越界值拒绝(账本字段防脏,§3.1)。"""
        api = self._api_with_gate(_gate())
        res = tool_story_advance(api.fetch, api.send, KEY, f"{KEY}:测试", "hacker")
        assert res["ok"] is False
        assert api.calls == []

    def test_no_gate_refused_friendly(self):
        """无确认门 → 友好拒绝,不瞎推进。"""
        api = self._api_with_gate([])
        res = tool_story_advance(api.fetch, api.send, KEY, f"{KEY}:测试")
        assert res["ok"] is False
        assert res["refused"] == "no_gate"
        assert api.calls == []

    def test_unknown_story_404_friendly(self, monkeypatch):
        """story_advance 打不存在的 key → 「查无」提示而非后端未启动。"""
        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(
                json.dumps({"detail": "Story not found"}).encode("utf-8")
            ))

        monkeypatch.setattr(bs.urllib.request, "urlopen", fake_urlopen)
        res = tool_story_advance(make_fetch(), make_send(), "tapd-nope", "tapd-nope:测试")
        assert res["ok"] is False
        assert "查无 story tapd-nope" in res["summary"]

    def test_upgrade_gate_points_to_ui(self):
        """428 挂起的升级门 → 拒绝并指向 UI(/lifecycle/ui-upgrade)。"""
        api = self._api_with_gate([
            _gate(kind="upgrade", targetState="结项", targetIsTerminal=True)
        ])
        res = tool_story_advance(api.fetch, api.send, KEY, f"{KEY}:结项")
        assert res["ok"] is False
        assert res["refused"] == "terminal_target"

    def test_backend_rejected_4xx_surfaced(self):
        """serve 返回 4xx(如成果物 gate 未满足 409)→ 明确转述,不误报成功。"""
        api = self._api_with_gate(_gate())
        api.send = lambda m, p, b=None: (409, {"detail": "成果物 gate 未满足,还差: 测试报告"})
        res = tool_story_advance(api.fetch, api.send, KEY, f"{KEY}:测试")
        assert res["ok"] is False
        assert res["refused"] == "backend_rejected"
        assert "409" in res["summary"]


class TestCheckConfirmToken:
    """执法纯函数的直接单测(规则顺序:终态优先于 token)。"""

    def test_pass_returns_none(self):
        assert check_confirm_token(KEY, _gate(), f"{KEY}:测试") is None

    def test_terminal_beats_token(self):
        """token 错 + 终态 → 报终态(高危理由优先露出)。"""
        res = check_confirm_token(KEY, _gate(targetState="结项", targetIsTerminal=True), "x")
        assert res["refused"] == "terminal_target"


# ---- plan_confirm ----


class TestPlanConfirm:
    def _payload(self, gates, **over):
        p = _story_payload(gates)
        p.update(over)
        return p

    def test_no_plan_refused_with_hint(self):
        api = _FakeApi({f"/api/story/{KEY}": self._payload([], hasPlan=False, planConfirmed=False)})
        res = tool_plan_confirm(api.fetch, api.send, KEY, f"{KEY}:开发")
        assert res["ok"] is False
        assert res["refused"] == "no_plan"
        assert api.calls == []

    def test_already_confirmed_refused(self):
        api = _FakeApi({f"/api/story/{KEY}": self._payload([], planConfirmed=True)})
        res = tool_plan_confirm(api.fetch, api.send, KEY, f"{KEY}:开发")
        assert res["ok"] is False
        assert res["refused"] == "already_confirmed"

    def test_wrong_token_refused(self):
        api = _FakeApi({f"/api/story/{KEY}": self._payload([
            {"kind": "plan_confirm", "targetState": "开发", "targetIsTerminal": False},
        ])})
        res = tool_plan_confirm(api.fetch, api.send, KEY, f"{KEY}:测试")
        assert res["ok"] is False
        assert res["expectedToken"] == f"{KEY}:开发"

    def test_correct_token_posts_plan_confirm(self):
        api = _FakeApi({f"/api/story/{KEY}": self._payload([
            {"kind": "plan_confirm", "targetState": "开发", "targetIsTerminal": False},
        ])})
        res = tool_plan_confirm(api.fetch, api.send, KEY, f"{KEY}:开发")
        assert res["ok"] is True
        assert api.calls[0]["path"] == f"/api/story/{KEY}/plan/confirm"


# ---- session_register ----


class TestSessionRegister:
    def test_registers_zcode_session(self):
        """zcode 的 sess id 登记 → POST /api/story/{key}/session。"""
        api = _FakeApi()
        res = tool_session_register(api.send, KEY, "build", "zcode", "sess_abc123")
        assert res["ok"] is True
        call = api.calls[0]
        assert call["method"] == "POST"
        assert call["path"] == f"/api/story/{KEY}/session"
        assert call["body"] == {
            "session_id": "sess_abc123",
            "stage": "build",
            "adapter": "zcode",
        }

    def test_missing_session_id_refused(self):
        api = _FakeApi()
        res = tool_session_register(api.send, KEY, "build", "zcode", "")
        assert res["ok"] is False
        assert api.calls == []

    def test_unknown_story_404_friendly(self):
        api = _FakeApi()
        api.send = lambda m, p, b=None: (404, {"detail": "Story not found"})
        res = tool_session_register(api.send, "tapd-nope", "build", "zcode", "sess_x")
        assert res["ok"] is False
        assert "tapd-nope" in res["summary"]


# ---- patrol_summary ----


class TestPatrolSummary:
    def test_fail_findings_stuck_and_reject_budget(self):
        list_payload = [
            {
                "storyKey": KEY,
                "status": "paused",
                "currentStage": "build",
                "awaitingConfirm": True,
                "patrolSummary": {"itemsCount": 3, "latestRunAt": "t1", "latestResult": "FAIL"},
            },
            {
                "storyKey": "tapd-butler-2",
                "status": "active",
                "currentStage": "design",
                "awaitingConfirm": False,
                "patrolSummary": {"itemsCount": 2, "latestRunAt": "t2", "latestResult": "PASS"},
            },
            {
                "storyKey": "tapd-butler-3",
                "status": "active",
                "currentStage": "design",
                "awaitingConfirm": False,
            },
        ]
        api = _FakeApi({
            "/api/story": list_payload,
            f"/api/story/{KEY}/patrol/runs?limit=1": {"runs": [{
                "summary": "ES错误超标",
                "items": [
                    {"name": "ES错误面", "result": "FAIL", "observed": "17条"},
                    {"name": "灰度读数", "result": "PASS", "observed": ""},
                ],
            }]},
            f"/api/story/{KEY}/timeline": {"decisions": [
                {"stage": "build", "decision": "reject", "reason_code": "boundary_judge",
                 "human_message": "缺测试", "created_at": "t1"},
                {"stage": "build", "decision": "reject", "reason_code": "boundary_judge",
                 "human_message": "还缺", "created_at": "t2"},
                {"stage": "build", "decision": "escalate", "reason_code": "stuck_diagnose",
                 "human_message": "stuck: 30min 无输出", "created_at": "t3"},
            ]},
            "/api/story/tapd-butler-2/timeline": {"decisions": []},
        })
        res = tool_patrol_summary(api.fetch, reject_budget=3)
        assert res["ok"] is True
        assert res["patrol"]["failCount"] == 1
        assert res["patrol"]["neverPatrolled"] == 1
        assert res["patrol"]["failFindings"][0]["failItems"][0]["name"] == "ES错误面"
        row = res["perStory"][0]
        assert row["stuck"] is True
        assert row["rejectBudget"] == [{"stage": "build", "rejected": 2, "remaining": 1}]
        assert res["stuckCount"] == 1

    def test_per_story_detail_bounded(self):
        """逐 story 拉时间线有界:超过 detail_limit 的活跃 story 截断并说明。"""
        stories = [
            {"storyKey": f"k{i}", "status": "active", "currentStage": "s", "awaitingConfirm": False}
            for i in range(5)
        ]
        gets = {"/api/story": stories}
        for i in range(3):
            gets[f"/api/story/k{i}/timeline"] = {"decisions": []}
        res = tool_patrol_summary(_FakeApi(gets).fetch, detail_limit=3)
        assert len(res["perStory"]) == 3
        assert "只逐个巡了前 3 个" in res["summary"]


# ---- knowledge_search(WP-F §8.2 第 7 工具,只读走 REST) ----


class TestKnowledgeSearch:
    def test_schema_requires_q_only(self):
        by_name = {t["name"]: t for t in BUTLER_TOOLS}
        assert by_name["knowledge_search"]["inputSchema"]["required"] == ["q"]
        props = by_name["knowledge_search"]["inputSchema"]["properties"]
        assert set(props) == {"q", "story_key", "top_k"}

    def test_hit_renders_results_with_source_refs(self):
        import urllib.parse

        path = "/api/knowledge/search?" + urllib.parse.urlencode({"q": "联系人落表", "top_k": "5"})
        api = _FakeApi({
            path: {
                "count": 1,
                "results": [{
                    "title": "联系人落表坑",
                    "type": "failure",
                    "category": "run-pitfall",
                    "detail": "三方返回要落表,别只落日志",
                    "tags": ["tapd-1069389", "run-pitfall"],
                    "source_refs": ["D:/proj/docs/test-runs/RUN-tapd-1069389-20260908.md"],
                }],
            }
        })
        res = tool_knowledge_search(api.fetch, q="联系人落表")
        assert res["ok"] is True
        assert res["count"] == 1
        row = res["results"][0]
        assert row["source_refs"] == ["D:/proj/docs/test-runs/RUN-tapd-1069389-20260908.md"]
        assert "联系人落表坑" in res["summary"]
        assert "RUN-tapd-1069389" in res["summary"]

    def test_story_key_and_top_k_forwarded_as_query(self):
        api = _FakeApi({
            "/api/knowledge/search?q=x&top_k=2&story_key=tapd-1": {"results": []}
        })
        res = tool_knowledge_search(api.fetch, q="x", story_key="tapd-1", top_k=2)
        assert res["ok"] is True
        assert res["count"] == 0
        assert "无命中" in res["summary"]

    def test_missing_q_refused_without_http(self):
        api = _FakeApi()
        res = tool_knowledge_search(api.fetch, q="  ")
        assert res["ok"] is False
        assert "缺少检索词" in res["summary"]

    def test_serve_warning_surfaced(self):
        """serve 端降级(warning 字段)→ 工具结果里透传,不装作正常命中。"""
        api = _FakeApi({
            "/api/knowledge/search?q=x&top_k=5": {
                "results": [], "warning": "知识检索不可用:root 坏了"
            }
        })
        res = tool_knowledge_search(api.fetch, q="x")
        assert res["ok"] is True
        assert res["warning"] == "知识检索不可用:root 坏了"
        assert "降级提示" in res["summary"]

    def test_dispatch_degrades_friendly_when_serve_down(self, monkeypatch):
        """serve 连不上 → 友好中文提示,不炸宿主会话(§5 降级矩阵)。"""
        monkeypatch.setattr(
            bs.urllib.request, "urlopen",
            lambda req, timeout=None: (_ for _ in ()).throw(urllib.error.URLError("refused")),
        )
        res = dispatch_tool("knowledge_search", {"q": "联系人"})
        assert res["ok"] is False
        assert "管家后端未启动" in res["summary"]

    def test_dispatch_unknown_tool_still_lists_knowledge_search(self):
        res = dispatch_tool("nope", {})
        assert "knowledge_search" in res["summary"]


# ---- serve 未启动的友好错误路径(monkeypatch HTTP 层,绝不真连) ----


class TestBackendDownFriendly:
    def _raise_conn_error(self, req, timeout=None):
        raise urllib.error.URLError("Connection refused")

    def test_http_json_raises_butler_error(self, monkeypatch):
        monkeypatch.setattr(bs.urllib.request, "urlopen", self._raise_conn_error)
        with pytest.raises(bs.ButlerServeError):
            bs.http_json("GET", "/api/story")

    def test_dispatch_returns_friendly_chinese_error(self, monkeypatch):
        """serve 连不上 → 工具返回友好中文提示,不抛异常(§5 降级矩阵)。"""
        monkeypatch.setattr(bs.urllib.request, "urlopen", self._raise_conn_error)
        for name, args in [
            ("story_list", {}),
            ("story_detail", {"key": KEY}),
            ("patrol_summary", {}),
            ("story_advance", {"key": KEY, "confirm_token": "x"}),
            ("plan_confirm", {"key": KEY, "confirm_token": "x"}),
        ]:
            res = dispatch_tool(name, args)
            assert res["ok"] is False
            assert "管家后端未启动" in res["summary"]
            assert "story serve" in res["summary"]

    def test_unknown_tool_friendly(self):
        res = dispatch_tool("nope", {})
        assert res["ok"] is False
        assert "未知工具" in res["summary"]

    def test_http_404_is_not_reported_as_backend_down(self, monkeypatch):
        """查无 story(HTTP 404)≠ 后端未启动 —— 转述 404,不误导去 story serve。"""
        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(
                json.dumps({"detail": "Story not found"}).encode("utf-8")
            ))

        monkeypatch.setattr(bs.urllib.request, "urlopen", fake_urlopen)
        res = dispatch_tool("story_detail", {"key": "tapd-nope"})
        assert res["ok"] is False
        assert "查无 story tapd-nope" in res["summary"]
        assert "管家后端未启动" not in res["summary"]

    def test_tool_crash_never_raises(self, monkeypatch):
        """工具内部炸了也只回友好错误(绝不炸宿主会话)。"""
        monkeypatch.setattr(bs, "tool_story_list", lambda *a, **k: 1 / 0)
        res = dispatch_tool("story_list", {})
        assert res["ok"] is False
        assert "管家工具执行失败" in res["summary"]

    def test_story_env_override_base_url(self, monkeypatch):
        monkeypatch.setenv("STORY_SERVE_URL", "http://127.0.0.1:9999/")

        def fake_urlopen(req, timeout=None):
            assert req.full_url == "http://127.0.0.1:9999/api/story"
            raise urllib.error.URLError("stop here")

        monkeypatch.setattr(bs.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(bs.ButlerServeError):
            bs.http_json("GET", "/api/story")


# ---- stdio JSONRPC 循环(in-process;dispatch 打桩避免网络) ----


class TestRunServerStdio:
    def test_handshake_tools_list_and_call(self, monkeypatch):
        def fake_dispatch(name, args):
            return {"ok": True, "summary": f"called {name} {args}", "echo": args}

        monkeypatch.setattr(bs, "dispatch_tool", fake_dispatch)
        stdin_lines = "\n".join([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, ensure_ascii=False),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, ensure_ascii=False),
            json.dumps({
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "story_detail", "arguments": {"key": KEY}},
            }, ensure_ascii=False),
            "not-json-line",  # 坏行静默跳过
            "",
        ])
        monkeypatch.setattr(bs.sys, "stdin", io.StringIO(stdin_lines))
        out = io.StringIO()
        monkeypatch.setattr(bs.sys, "stdout", out)

        bs.run_server()

        replies = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
        assert [r.get("id") for r in replies] == [1, 2, 3]
        assert replies[0]["result"]["serverInfo"]["name"] == "butler"
        assert [t["name"] for t in replies[1]["result"]["tools"]] == [
            t["name"] for t in BUTLER_TOOLS
        ]
        res = replies[2]["result"]
        assert res["isError"] is False
        assert KEY in res["content"][0]["text"]  # 摘要 + 结构化 JSON 都在 text 里


# ---- write_butler_mcp_config(merge 语义,不覆盖 clarify) ----


class TestWriteButlerMcpConfig:
    def test_merges_preserving_clarify_entry(self, tmp_path):
        cfg = tmp_path / ".mcp.json"
        cfg.write_text(
            json.dumps({"mcpServers": {
                "lifecycle": {"command": "py", "args": ["-m", "story_lifecycle.orchestrator.mcp.clarify_server"]}
            }}, ensure_ascii=False),
            encoding="utf-8",
        )
        out = write_butler_mcp_config(cfg, "python-bin")
        assert out == str(cfg)
        data = json.loads(cfg.read_text(encoding="utf-8"))
        # clarify 的条目原样保留,butler 新增
        assert data["mcpServers"]["lifecycle"]["args"] == [
            "-m", "story_lifecycle.orchestrator.mcp.clarify_server",
        ]
        assert data["mcpServers"]["butler"] == {
            "command": "python-bin",
            "args": ["-m", "story_lifecycle.orchestrator.mcp.butler_server"],
        }

    def test_creates_file_and_env_override(self, tmp_path):
        cfg = tmp_path / "sub" / ".mcp.json"
        write_butler_mcp_config(cfg, "py", serve_url="http://127.0.0.1:9000")
        data = json.loads(cfg.read_text(encoding="utf-8"))
        assert data["mcpServers"]["butler"]["env"] == {"STORY_SERVE_URL": "http://127.0.0.1:9000"}

    def test_corrupt_existing_file_recovered(self, tmp_path):
        cfg = tmp_path / ".mcp.json"
        cfg.write_text("{not json", encoding="utf-8")
        write_butler_mcp_config(cfg, "py")
        data = json.loads(cfg.read_text(encoding="utf-8"))
        assert "butler" in data["mcpServers"]


# ---- make_fetch 的 >=400 语义 ----


class TestMakeFetch:
    def test_404_raises_with_detail(self, monkeypatch):
        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(
                json.dumps({"detail": "Story not found"}).encode("utf-8")
            ))

        monkeypatch.setattr(bs.urllib.request, "urlopen", fake_urlopen)
        fetch = make_fetch()
        with pytest.raises(bs.ButlerServeError) as ei:
            fetch("/api/story/nope")
        assert "404" in str(ei.value)

    def test_2xx_returns_parsed(self, monkeypatch):
        class _Resp(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(
            bs.urllib.request, "urlopen",
            lambda req, timeout=None: _Resp(json.dumps([{"storyKey": "x"}]).encode("utf-8")),
        )
        assert make_fetch()("/api/story") == [{"storyKey": "x"}]
