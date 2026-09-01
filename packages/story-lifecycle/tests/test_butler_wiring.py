"""管家 WP1 接线回归(DESIGN-story-butler §3.1/§9)。

5 个 emit 接线点 + confirmed_via 账本:
- supervisor 两处发声点(_notify_awaiting / escalate_stuck)→ 事件出口;
  notify_fn 注入兼容(存量测试行为不破)+ 新增 emit 断言
- gate_waiting:lifecycle 推进停在 ui_button 确认门(advance_lifecycle_to_target)
- judge_rejected / judge_escalated / stage_completed:judge 决策落
  orchestrator_decision 处按 quality 分流
- PUT /api/story/{key}/advance 的 confirmed_via 透传进 log_event payload
- 事件出口只观察:emit 不改 story 状态

channel 全 mock;时间固定白天(免打扰不触发,tier 断言与时钟无关)。
"""

import json
from datetime import datetime

import pytest

import story_lifecycle.infra.notification.router as router_mod
import story_lifecycle.orchestrator.service.routers.lifecycle as lc
from story_lifecycle.infra.db import models as db
from story_lifecycle.orchestrator.engine.supervisor import (
    _notify_awaiting,
    escalate_stuck,
)
from story_lifecycle.orchestrator.evaluation.stage_completion import (
    StageCompletionDecision,
    advance_lifecycle_to_target,
    judge_stage_completion,
)


class _FixedDatetime(datetime):
    """route() 的 now=None 分支固定在白天 12:00 —— interrupt 档不被免打扰降级。"""

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 9, 1, 12, 0, tzinfo=tz) if tz else datetime(
            2026, 9, 1, 12, 0
        )


@pytest.fixture(autouse=True)
def _daytime_and_clean_config(monkeypatch):
    """接线测试统一环境:白天 + 无 notification: 段(不受真实 config.yaml 影响)。"""
    monkeypatch.setattr(router_mod, "datetime", _FixedDatetime)
    monkeypatch.setattr("story_lifecycle.infra.config.get_config", lambda: {})


def _outbox_rows(event_type=None):
    rows = db.list_notifications(limit=100)
    if event_type:
        rows = [r for r in rows if r["event_type"] == event_type]
    return rows


# ---- 接线点 1/2:supervisor 两处发声点 ----


class TestSupervisorWiring:
    def test_notify_awaiting_emits_outbox_row(self):
        """接线点①:agent 提问 → awaiting_question 事件写 outbox(默认打断档)。"""
        _notify_awaiting("FEAT-9", "design", "claude", "用方案 A 还是 B?")
        rows = _outbox_rows("awaiting_question")
        assert len(rows) == 1
        row = rows[0]
        assert row["story_key"] == "FEAT-9"
        assert row["tier"] == "interrupt"
        assert "claude" in row["title"]
        assert "用方案 A 还是 B?" in row["message"]

    def test_notify_awaiting_does_not_touch_story(self, tmp_path):
        """事件出口只观察:emit 前后 story 行不变。"""
        db.create_story("OBS-1", "观察", str(tmp_path / "ws"), profile="minimal")
        before = db.get_story("OBS-1")
        _notify_awaiting("OBS-1", "design", "kimi", "?")
        assert db.get_story("OBS-1") == before

    def test_escalate_stuck_emits_and_keeps_notify_fn_compat(self):
        """接线点②:stuck → stuck_detected 事件 + 注入 notify_fn 仍被调(兼容)。"""
        notified = []
        escalate_stuck(
            story_key="STUCK-W1",
            stage="build",
            adapter="kimi",
            detection={"rule": "no_output_timeout", "reason": "idle 600s", "duration": 600},
            log_event_fn=lambda *a, **k: None,
            notify_fn=lambda t, m: notified.append((t, m)),
        )
        # 存量契约:注入的 notify_fn 照常被调
        assert len(notified) == 1 and "STUCK-W1" in notified[0][0]
        # 新增:事件出口落行
        rows = _outbox_rows("stuck_detected")
        assert len(rows) == 1
        assert rows[0]["tier"] == "interrupt"
        assert "idle 600s" in rows[0]["message"]
        payload = json.loads(rows[0]["payload_json"])
        assert payload["rule"] == "no_output_timeout"

    def test_escalate_stuck_default_path_emits_without_direct_plyer(self, monkeypatch):
        """默认路径(notify_fn=None)不再直连 plyer,只走事件出口。"""
        from story_lifecycle.orchestrator.engine import notify as engine_notify

        plyer_called = []

        class _RecordingChannel:
            def __init__(self):
                self.sink = plyer_called

            def send(self, t, m, tier="batch"):
                self.sink.append((t, m))
                return True

        monkeypatch.setattr(
            "story_lifecycle.infra.notification.desktop_plyer.DesktopPlyerChannel",
            _RecordingChannel,
        )

        escalate_stuck(
            story_key="STUCK-W2",
            stage="design",
            adapter="claude",
            detection={"rule": "repeated_errors", "reason": "连续报错"},
            log_event_fn=lambda *a, **k: None,
        )
        assert plyer_called == []  # 默认路径不直连 plyer
        assert len(_outbox_rows("stuck_detected")) == 1
        # 兼容 re-export 本身仍可用(投递线程用它)
        engine_notify.send("t", "m")

    def test_escalate_stuck_notify_fn_failure_non_fatal(self):
        """notify_fn 抛异常不炸(事件已落)。"""
        def boom(t, m):
            raise RuntimeError("plyer dead")

        escalate_stuck(
            story_key="STUCK-W3",
            stage="verify",
            adapter="opencode",
            detection={"rule": "no_output_timeout", "reason": "idle", "duration": 1},
            log_event_fn=lambda *a, **k: None,
            notify_fn=boom,
        )
        assert len(_outbox_rows("stuck_detected")) == 1


# ---- 接线点 3:gate_waiting(停在 ui_button 确认门) ----


class TestGateWaitingWiring:
    def test_ui_button_gate_emits_gate_waiting(self, tmp_path, isolated_story_home):
        db.create_story("GATE-1", "停门", str(tmp_path / "ws"), profile="minimal")
        db.update_story("GATE-1", lifecycle_state="待启动", context_json="{}")
        ctx = {}
        story_states = {
            "待启动": {"confirm": {"type": "none"}},
            "开发": {"confirm": {"type": "ui_button", "label": "进入测试"}},
        }
        out = advance_lifecycle_to_target(
            story_key="GATE-1",
            ctx=ctx,
            current="待启动",
            target="测试",
            story_states=story_states,
            db_module=db,
        )
        assert out["paused_for_confirm"] is True
        rows = _outbox_rows("gate_waiting")
        assert len(rows) == 1
        assert rows[0]["story_key"] == "GATE-1"
        assert rows[0]["tier"] == "interrupt"
        payload = json.loads(rows[0]["payload_json"])
        assert payload["from"] == "开发"
        assert payload["to"] == "测试"
        assert payload["final_target"] == "测试"

    def test_auto_advance_no_gate_no_emit(self, tmp_path, isolated_story_home):
        """无 ui_button 的推进不发声(只有停门才通知)。"""
        db.create_story("GATE-2", "自动", str(tmp_path / "ws2"), profile="minimal")
        advance_lifecycle_to_target(
            story_key="GATE-2",
            ctx={},
            current="待启动",
            target="结项",
            story_states={},
            db_module=db,
        )
        assert _outbox_rows("gate_waiting") == []


# ---- 接线点 4/5:judge 决策分流 ----


class _FakeLLM:
    api_key = "fake-key"
    model = "test-model"

    def __init__(self, decision):
        self._decision = decision
        self.invoked = False

    def invoke_structured(self, prompt, schema, **kwargs):
        self.invoked = True
        return self._decision


@pytest.fixture
def judge_story(tmp_path, isolated_story_home):
    ws = tmp_path / "ws"
    ws.mkdir()
    db.create_story("JW-1", "judge接线", str(ws), profile="minimal")
    db.update_story(
        "JW-1",
        lifecycle_state="待启动",
        context_json=json.dumps(
            {
                "_plan_confirmed": True,
                "_agent_actions": [
                    {"action": "launch", "stage": "design", "adapter": "claude"}
                ],
            },
            ensure_ascii=False,
        ),
    )
    return "JW-1"


def _run_judge(monkeypatch, judge_story, decision):
    from story_lifecycle.orchestrator.evaluation.stage_completion import JudgeRequest

    llm = _FakeLLM(decision)
    monkeypatch.setattr("story_lifecycle.infra.llm_client.get_llm", lambda: llm)
    return judge_stage_completion(
        JudgeRequest(
            story_key=judge_story,
            stage="design",
            workspace=db.get_story(judge_story)["workspace"],
            ctx={},
            lifecycle_state="待启动",
            done_data={"summary": "x", "files_changed": []},
        )
    )


class TestJudgeWiring:
    def test_approve_emits_stage_completed(self, judge_story, monkeypatch):
        _run_judge(
            monkeypatch,
            judge_story,
            StageCompletionDecision(
                quality="approve", lifecycle_target="开发", summary="设计完成"
            ),
        )
        rows = _outbox_rows("stage_completed")
        assert len(rows) == 1
        assert rows[0]["story_key"] == judge_story
        assert rows[0]["tier"] == "batch"  # 攒批档
        assert "设计完成" in json.loads(rows[0]["payload_json"])["summary"]

    def test_reject_emits_judge_rejected(self, judge_story, monkeypatch):
        _run_judge(
            monkeypatch,
            judge_story,
            StageCompletionDecision(quality="reject", reason="成果物为空"),
        )
        rows = _outbox_rows("judge_rejected")
        assert len(rows) == 1
        assert rows[0]["tier"] == "interrupt"
        assert "成果物为空" in rows[0]["message"]

    def test_escalate_emits_judge_escalated(self, judge_story, monkeypatch):
        _run_judge(
            monkeypatch,
            judge_story,
            StageCompletionDecision(quality="escalate", reason="超限"),
        )
        rows = _outbox_rows("judge_escalated")
        assert len(rows) == 1
        assert rows[0]["tier"] == "interrupt"

    def test_llm_down_fallback_emits_judge_escalated(self, judge_story, monkeypatch):
        """LLM 挂了 → fail-closed escalate 也走事件出口(裁判瞎了人要知道)。"""

        def boom(prompt, schema, **kwargs):
            raise RuntimeError("llm down")

        monkeypatch.setattr(
            "story_lifecycle.infra.llm_client.get_llm",
            lambda: type("L", (), {"api_key": "k", "model": "", "invoke_structured": boom})(),
        )
        from story_lifecycle.orchestrator.evaluation.stage_completion import JudgeRequest

        out = judge_stage_completion(
            JudgeRequest(
                story_key=judge_story,
                stage="design",
                workspace=db.get_story(judge_story)["workspace"],
                ctx={},
                lifecycle_state="待启动",
            )
        )
        assert out["fallback"] is True
        assert len(_outbox_rows("judge_escalated")) == 1


# ---- confirmed_via:PUT /api/story/{key}/advance ----


class TestConfirmedVia:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient

        from story_lifecycle.orchestrator.service.api import app

        return TestClient(app)

    def _paused_story(self):
        return {
            "story_key": "S-CV",
            "status": "paused",
            "current_stage": "design",
            "context_json": "{}",
            "lifecycle_state": "开发",
        }

    def test_confirmed_via_wechat_logged(self, client, monkeypatch):
        monkeypatch.setattr(lc.db, "get_story", lambda k: self._paused_story())
        monkeypatch.setattr(lc, "sm_activate", lambda *a, **k: None)
        monkeypatch.setattr(lc, "start_story_async", lambda *a, **k: None)
        events = []
        monkeypatch.setattr(
            lc.db,
            "log_event",
            lambda sk, st, et, payload=None: events.append((sk, st, et, payload)),
        )
        r = client.put(
            "/api/story/S-CV/advance", json={"description": "", "confirmed_via": "wechat"}
        )
        assert r.status_code == 200
        assert r.json()["status"] == "resumed"
        advances = [e for e in events if e[2] == "manual_advance"]
        assert len(advances) == 1
        payload = advances[0][3]
        assert payload["confirmed_via"] == "wechat"
        assert payload["action"] == "resumed"
        assert payload["from_status"] == "paused"

    def test_confirmed_via_defaults_to_ui(self, client, monkeypatch):
        """不传 confirmed_via → 默认 ui(UI 按钮是现存调用方)。"""
        monkeypatch.setattr(lc.db, "get_story", lambda k: self._paused_story())
        monkeypatch.setattr(lc, "sm_activate", lambda *a, **k: None)
        monkeypatch.setattr(lc, "start_story_async", lambda *a, **k: None)
        events = []
        monkeypatch.setattr(
            lc.db,
            "log_event",
            lambda sk, st, et, payload=None: events.append((sk, st, et, payload)),
        )
        r = client.put("/api/story/S-CV/advance", json={})
        assert r.status_code == 200
        assert events[-1][3]["confirmed_via"] == "ui"

    def test_invalid_confirmed_via_rejected(self, client, monkeypatch):
        """账本枚举外值 → 400(防脏账本)。"""
        monkeypatch.setattr(lc.db, "get_story", lambda k: self._paused_story())
        r = client.put("/api/story/S-CV/advance", json={"confirmed_via": "carrier-pigeon"})
        assert r.status_code == 400

    def test_story_not_found_still_404(self, client, monkeypatch):
        monkeypatch.setattr(lc.db, "get_story", lambda k: None)
        r = client.put("/api/story/NOPE/advance", json={"confirmed_via": "wechat"})
        assert r.status_code == 404
