"""管家 WP2 serve 侧增补的路由测试(DESIGN-story-butler §3.2)。

覆盖:
- GET /api/story/{key} 新增 confirmGates 字段(确认门结构化视图:targetState +
  targetIsTerminal —— 终态判定来自 UPGRADE_STATES,任务要求"终态集合从状态机代码找,
  不硬编码"的服务端执法依据);
- GET /api/story 列表新增 awaitingConfirm 布尔(story_list 的「是否停门」列,免 butler
  N+1 拉详情);
- POST /api/story/{key}/lifecycle/advance 新增可选 body {confirmed_via}(管家账本
  §3.1/§4:确认门续推也要落确认来源)。

工具面(executor)侧的测试在 test_butler_mcp.py(HTTP 全 mock)。
"""

import json

import pytest
from fastapi.testclient import TestClient

from story_lifecycle.infra.db import models as db
from story_lifecycle.orchestrator.evaluation import stage_completion
from story_lifecycle.orchestrator.service.api import app


@pytest.fixture
def client(isolated_story_home):
    return TestClient(app)


def _seed(key: str, ctx: dict, home, **extra):
    db.create_story(key, f"title-{key}", str(home))
    fields = {"context_json": json.dumps(ctx, ensure_ascii=False)}
    fields.update(extra)
    db.update_story(key, **fields)
    return key


def _gates(client, key):
    data = client.get(f"/api/story/{key}").json()
    return data["confirmGates"]


# ---- GET /api/story/{key} 的 confirmGates ----


class TestConfirmGatesField:
    def test_story_state_gate_with_terminal_judgment(self, client, isolated_story_home):
        """ui_button 停门 → kind=story_state;终态判定按 UPGRADE_STATES(上线=True)。"""
        key = _seed(
            "tapd-cg-1",
            {"_story_state_gate": {
                "from": "开发", "to": "测试", "awaiting_confirm": True,
                "label": "进入测试", "final_target": "结项",
            }},
            isolated_story_home,
        )
        gates = _gates(client, key)
        assert len(gates) == 1
        g = gates[0]
        assert g["kind"] == "story_state"
        assert g["targetState"] == "测试"
        assert g["targetIsTerminal"] is False  # 测试 不在 UPGRADE_STATES
        assert g["finalTarget"] == "结项"

    def test_story_state_gate_to_online_is_terminal(self, client, isolated_story_home):
        """to=上线(UPGRADE_STATES 成员)→ targetIsTerminal=True(管家据此拒绝)。"""
        key = _seed(
            "tapd-cg-2",
            {"_story_state_gate": {
                "from": "测试", "to": "上线", "awaiting_confirm": True,
                "final_target": "上线",
            }},
            isolated_story_home,
        )
        (gate,) = _gates(client, key)
        assert gate["targetState"] == "上线"
        assert gate["targetIsTerminal"] is True

    def test_upgrade_gate_always_terminal(self, client, isolated_story_home):
        """428 挂起的 _upgrade_gate → kind=upgrade,恒终态(只能 UI 确认)。"""
        key = _seed(
            "tapd-cg-3",
            {"_upgrade_gate": {"prev": "测试", "target": "结项", "origin": "advance"}},
            isolated_story_home,
        )
        (gate,) = _gates(client, key)
        assert gate["kind"] == "upgrade"
        assert gate["targetState"] == "结项"
        assert gate["targetIsTerminal"] is True

    def test_stage_gate(self, client, isolated_story_home):
        """stage 间确认闸 → kind=stage,targetState=next_stage。"""
        key = _seed(
            "tapd-cg-4",
            {"_stage_gate": {"completed_stage": "build", "next_stage": "verify",
                             "awaiting_confirm": True}},
            isolated_story_home,
        )
        (gate,) = _gates(client, key)
        assert gate["kind"] == "stage"
        assert gate["targetState"] == "verify"
        assert gate["targetIsTerminal"] is False

    def test_plan_gate_target_is_dev(self, client, isolated_story_home):
        """有规划未确认 → plan_confirm 门,targetState=开发(/plan/confirm 语义)。"""
        key = _seed(
            "tapd-cg-5",
            {"_agent_actions": [{"action": "launch", "stage": "design"}]},
            isolated_story_home,
        )
        (gate,) = _gates(client, key)
        assert gate["kind"] == "plan_confirm"
        assert gate["targetState"] == "开发"
        assert gate["targetIsTerminal"] is False

    def test_confirmed_plan_has_no_gate(self, client, isolated_story_home):
        """规划已确认 → 无 plan_confirm 门。"""
        key = _seed(
            "tapd-cg-6",
            {"_agent_actions": [{"action": "launch", "stage": "design"}],
             "_plan_confirmed": True},
            isolated_story_home,
        )
        assert _gates(client, key) == []

    def test_no_gates_and_corrupt_ctx(self, client, isolated_story_home):
        """无闸 → 空列表;context_json 损坏 → 空列表(宁可少报不误报)。"""
        k1 = _seed("tapd-cg-7", {}, isolated_story_home)
        assert _gates(client, k1) == []
        k2 = _seed("tapd-cg-8", {}, isolated_story_home)
        db.update_story(k2, context_json="{broken json")
        assert _gates(client, k2) == []


# ---- GET /api/story 列表的 awaitingConfirm ----


class TestListAwaitingConfirm:
    def test_flag_in_list(self, client, isolated_story_home):
        gated = _seed(
            "tapd-ac-1",
            {"_story_state_gate": {"from": "开发", "to": "测试",
                                   "awaiting_confirm": True}},
            isolated_story_home,
        )
        plain = _seed("tapd-ac-2", {}, isolated_story_home)
        rows = {r["storyKey"]: r for r in client.get("/api/story").json()}
        assert rows[gated]["awaitingConfirm"] is True
        assert rows[plain]["awaitingConfirm"] is False


# ---- POST /lifecycle/advance 的 confirmed_via 透传 ----


def _gate_story_key(home, key="tapd-cv-1"):
    """挂一个 ui_button 确认门(测试→上线)的 paused story。"""
    return _seed(
        key,
        {"_story_state_gate": {
            "from": "测试", "to": "上线", "awaiting_confirm": True,
            "label": "进入上线", "final_target": "上线",
        }},
        home,
        status="paused",
        lifecycle_state="测试",
    )


def _transition_events(key):
    out = []
    for ev in db.get_story_events(key):
        if ev.get("event_type") == "story_state_transition":
            payload = ev.get("payload")
            if isinstance(payload, str):
                payload = json.loads(payload)
            out.append(payload)
    return out


class TestLifecycleAdvanceConfirmedVia:
    def test_invalid_confirmed_via_400(self, client, isolated_story_home):
        key = _gate_story_key(isolated_story_home, "tapd-cv-bad")
        resp = client.post(f"/api/story/{key}/lifecycle/advance",
                           json={"confirmed_via": "hacker"})
        assert resp.status_code == 400
        assert _transition_events(key) == []  # 脏值不落账本

    def test_confirmed_via_passthrough_to_ledger(self, client, isolated_story_home, monkeypatch):
        """管家 wechat 确认 → story_state_transition 事件带 confirmed_via=wechat。"""
        key = _gate_story_key(isolated_story_home, "tapd-cv-wx")
        # 打桩 LLM target 续推(其内部行为已有自己的测试);这里只验账本透传。
        monkeypatch.setattr(
            stage_completion, "advance_lifecycle_to_target",
            lambda **kw: {"new_state": kw["target"], "paused_for_confirm": False},
        )
        resp = client.post(f"/api/story/{key}/lifecycle/advance",
                           json={"confirmed_via": "wechat"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        payloads = _transition_events(key)
        assert payloads and payloads[0]["confirmed_via"] == "wechat"
        assert payloads[0]["from"] == "测试" and payloads[0]["to"] == "上线"

    def test_no_body_defaults_ui(self, client, isolated_story_home, monkeypatch):
        """老调用方无 body(UI 直调)→ 默认 ui,行为不变。"""
        key = _gate_story_key(isolated_story_home, "tapd-cv-ui")
        monkeypatch.setattr(
            stage_completion, "advance_lifecycle_to_target",
            lambda **kw: {"new_state": kw["target"], "paused_for_confirm": False},
        )
        resp = client.post(f"/api/story/{key}/lifecycle/advance")
        assert resp.status_code == 200
        payloads = _transition_events(key)
        assert payloads and payloads[0]["confirmed_via"] == "ui"
