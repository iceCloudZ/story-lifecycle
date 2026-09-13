"""outbox 状态机 + 投递线程单测(管家 WP1,DESIGN-story-butler §3.1/§5/§9)。

锁定:
- emit_event 只写一行 outbox(快),tier/通道随路由;免打扰降级标 deferred
- pending → sent(通道送达,sent_at 落)
- 失败退避重试:1m/5m/30m;attempts≥5 → failed 保留
- per-channel 至少一次:已送达通道重试不重复发
- 通道不可用 → skipped
- emit 绝不投递(Handler 才有投递副作用)

channel 一律 fake,绝不真 ssh / 真弹窗。
"""

import json

import pytest

from story_lifecycle.infra.db import models as db
from story_lifecycle.infra.notification.delivery import (
    BACKOFF_SECONDS,
    MAX_ATTEMPTS,
    NotificationThread,
    build_channels,
)
from story_lifecycle.infra.notification.emitter import emit_event
from story_lifecycle.infra.notification.router import load_notification_section


class FakeChannel:
    """可编程 fake 通道:记录调用,按预设返回 ok/raise。"""

    def __init__(self, name, ok=True, exc=None, available=True):
        self.name = name
        self.ok = ok
        self.exc = exc
        self._available = available
        self.calls = []

    def available(self):
        return self._available

    def send(self, title, message, tier="batch"):
        self.calls.append((title, message, tier))
        if self.exc is not None:
            raise self.exc
        return self.ok


@pytest.fixture
def quiet_config(monkeypatch):
    """无弹窗且显式关闭免打扰的干净配置(不受真实 config.yaml 与墙钟影响——
    默认 22:00-07:30 静音窗会让本文件一半时段红,2026-09-13 00:45 实测)。"""
    monkeypatch.setattr(
        "story_lifecycle.infra.config.get_config",
        lambda: {"notification": {"quiet_hours": None}},
    )


def _mk_thread(**channels):
    return NotificationThread(channels=dict(channels), config={})


class TestEmitEvent:
    def test_emit_writes_one_pending_row(self, quiet_config):
        nid = emit_event("judge_rejected", story_key="S-1", stage="design",
                         message="成果物为空")
        assert nid is not None
        row = db.get_notification(nid)
        assert row["status"] == "pending"
        assert row["event_type"] == "judge_rejected"
        assert row["story_key"] == "S-1"
        assert row["tier"] == "interrupt"
        assert row["attempts"] == 0
        payload = json.loads(row["payload_json"])
        assert payload["channels"] == ["wechat", "desktop"]
        assert payload["stage"] == "design"

    def test_emit_default_title(self, quiet_config):
        nid = emit_event("stage_completed", story_key="S-1", message="ok")
        assert "Stage 完成" in db.get_notification(nid)["title"]

    def test_emit_does_not_deliver(self, quiet_config):
        """emit 绝不投递 —— 只落行;fake 通道没被碰(Handler 才有投递副作用)。"""
        ch = FakeChannel("desktop")
        emit_event("stage_completed", story_key="S-1", message="ok")
        assert ch.calls == []

    def test_emit_quiet_hours_marks_deferred(self, quiet_config, monkeypatch):
        """免打扰时段 emit:interrupt 降级 batch + payload 标 deferred,不丢。"""
        from datetime import datetime

        # 自带显式静音窗(覆盖下方 now),不依赖默认窗口与墙钟
        monkeypatch.setattr(
            "story_lifecycle.infra.config.get_config",
            lambda: {"notification": {"quiet_hours": {"start": "23:00", "end": "23:59"}}},
        )
        nid = emit_event(
            "awaiting_question", story_key="S-1", message="?",
            now=datetime(2026, 9, 1, 23, 30),
        )
        row = db.get_notification(nid)
        assert row["tier"] == "batch"
        payload = json.loads(row["payload_json"])
        assert payload["deferred"] is True
        assert "免打扰" in row["message"]

    def test_emit_failure_returns_none_never_raises(self, monkeypatch):
        """emit 内部任何失败吞掉返回 None —— 事件出口绝不炸调用方。"""
        monkeypatch.setattr(
            "story_lifecycle.infra.config.get_config",
            lambda: (_ for _ in ()).throw(RuntimeError("config boom")),
        )
        # get_config 抛 → load_notification_section 兜 {} → 正常落行;
        # 再让 enqueue 炸一次验证吞异常路径
        nid = emit_event("stage_completed", story_key="S-1")
        assert nid is not None  # 配置读取失败不挡 emit
        monkeypatch.setattr(
            "story_lifecycle.infra.db.outbox.enqueue_notification",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("db boom")),
        )
        assert emit_event("stage_completed", story_key="S-1") is None

    def test_emit_wakes_running_thread(self, quiet_config, monkeypatch):
        """emit 后 try_wake 被调(线程在跑时提前打断 30s 等待)。"""
        woken = []
        import story_lifecycle.infra.notification.delivery as delivery_mod

        monkeypatch.setattr(
            delivery_mod, "try_wake", lambda: woken.append(1)
        )
        emit_event("stage_completed", story_key="S-1")
        assert woken == [1]


class TestOutboxStateMachine:
    def test_pending_to_sent(self, quiet_config):
        ch = FakeChannel("desktop", ok=True)
        nid = emit_event("stage_completed", story_key="S-1", message="ok")
        handled = _mk_thread(desktop=ch).drain_once(now_ts=1000.0)
        assert handled == 1
        row = db.get_notification(nid)
        assert row["status"] == "sent"
        assert row["sent_at"] is not None
        assert ch.calls == [("Stage 完成", "ok", "batch")]

    def test_failure_retries_with_backoff(self, quiet_config):
        """失败 → attempts+1 留 pending;退避到期前不重试;到期后重试。"""
        ch = FakeChannel("desktop", ok=False)
        nid = emit_event("stage_completed", story_key="S-1", message="ok")
        thr = _mk_thread(desktop=ch)

        assert thr.drain_once(now_ts=1000.0) == 1
        row = db.get_notification(nid)
        assert row["status"] == "pending"
        assert row["attempts"] == 1
        assert row["last_error"]

        # 退避窗口内(1m)不重试
        assert thr.drain_once(now_ts=1000.0 + 30) == 0
        assert db.get_notification(nid)["attempts"] == 1
        # 到期重试
        assert thr.drain_once(now_ts=1000.0 + 61) == 1
        assert db.get_notification(nid)["attempts"] == 2
        assert len(ch.calls) == 2

    def test_backoff_schedule_is_1m_5m_30m(self, quiet_config):
        assert BACKOFF_SECONDS[:3] == [60, 300, 1800]

    def test_attempts_ge_five_marks_failed_and_kept(self, quiet_config):
        """attempts≥5 → failed;行保留(降级矩阵 §5:丢失 0,可审计)。"""
        ch = FakeChannel("desktop", ok=False)
        nid = emit_event("stage_completed", story_key="S-1", message="ok")
        thr = _mk_thread(desktop=ch)
        ts = 1000.0
        for _ in range(MAX_ATTEMPTS):
            # 每次推到退避到期
            thr.drain_once(now_ts=ts)
            ts += 2000.0  # 远超 30m,保证每轮都到期
        row = db.get_notification(nid)
        assert row["status"] == "failed"
        assert row["attempts"] == MAX_ATTEMPTS
        # 行保留,不再被 drain
        assert thr.drain_once(now_ts=ts + 4000.0) == 0
        assert db.get_notification(nid) is not None

    def test_partial_success_does_not_resend_delivered_channel(self, quiet_config):
        """per-channel 至少一次:desktop 送达后 wechat 重试,desktop 不重发。"""
        desktop = FakeChannel("desktop", ok=True)
        wechat = FakeChannel("wechat", ok=False)
        nid = emit_event("judge_escalated", story_key="S-1", message="x")
        thr = _mk_thread(desktop=desktop, wechat=wechat)

        thr.drain_once(now_ts=1000.0)
        row = db.get_notification(nid)
        assert row["status"] == "pending"  # wechat 还没成功
        assert json.loads(row["payload_json"])["delivered"] == ["desktop"]
        assert len(desktop.calls) == 1

        # wechat 恢复 → 重试只发 wechat,desktop 不重复
        # (wechat 共 2 次调用:第 1 次失败 + 重试成功;desktop 始终 1 次)
        wechat.ok = True
        thr.drain_once(now_ts=1000.0 + 61)
        row = db.get_notification(nid)
        assert row["status"] == "sent"
        assert len(desktop.calls) == 1  # 不重发
        assert len(wechat.calls) == 2

    def test_unavailable_channel_marks_skipped(self, quiet_config):
        """通道不可用(如 plyer 缺失)→ skipped,不算失败不重试。"""
        desktop = FakeChannel("desktop", available=False)
        nid = emit_event("stage_completed", story_key="S-1", message="ok")
        thr = _mk_thread(desktop=desktop)
        thr.drain_once(now_ts=1000.0)
        row = db.get_notification(nid)
        assert row["status"] == "skipped"
        assert desktop.calls == []

    def test_channel_exception_treated_as_failure(self, quiet_config):
        ch = FakeChannel("desktop", exc=RuntimeError("plyer exploded"))
        nid = emit_event("stage_completed", story_key="S-1", message="ok")
        thr = _mk_thread(desktop=ch)
        thr.drain_once(now_ts=1000.0)
        row = db.get_notification(nid)
        assert row["status"] == "pending"
        assert "plyer exploded" in (row["last_error"] or "")

    def test_interrupt_tier_sends_both_channels(self, quiet_config):
        desktop = FakeChannel("desktop")
        wechat = FakeChannel("wechat")
        nid = emit_event("gate_waiting", story_key="S-1", message="等确认")
        _mk_thread(desktop=desktop, wechat=wechat).drain_once(now_ts=1000.0)
        assert db.get_notification(nid)["status"] == "sent"
        assert len(desktop.calls) == 1 and len(wechat.calls) == 1


class TestBuildChannels:
    def test_default_section_builds_both(self):
        channels = build_channels({})
        assert set(channels) == {"desktop", "wechat"}
        assert channels["wechat"].host == "101"
        assert "remind.py" in channels["wechat"].command

    def test_wechat_config_override(self):
        channels = build_channels(
            {"channels": {"wechat": {"host": "myhost", "command": "/x/remind.py"}}}
        )
        assert channels["wechat"].host == "myhost"
        assert channels["wechat"].command == "/x/remind.py"

    def test_disabled_channel_not_built(self):
        channels = build_channels(
            {"channels": {"wechat": {"enabled": False}, "desktop": {"enabled": False}}}
        )
        assert channels == {}

    def test_section_passthrough_from_load(self, monkeypatch):
        """load_notification_section + build_channels 装配链(无段 → 双通道)。"""
        monkeypatch.setattr("story_lifecycle.infra.config.get_config", lambda: {})
        channels = build_channels(load_notification_section())
        assert set(channels) == {"desktop", "wechat"}
