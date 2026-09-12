"""路由器表驱动单测(管家 WP1,DESIGN-story-butler §3.1/§9)。

route() 是纯 Decider:事件 × 配置 → 通道动作。锁定:
- 默认表:5 种事件(v1)默认档位;未知事件默认 batch
- config.yaml ``notification:`` 覆盖:routes / quiet_hours
- 免打扰时段:interrupt 降级 batch(跨午夜区间 + 边界值)
- 配置缺失/坏值:全走默认,不炸
"""

from datetime import datetime

import pytest

from story_lifecycle.infra.notification.router import (
    DEFAULT_ROUTES,
    ChannelAction,
    in_quiet_hours,
    load_notification_section,
    resolve_tier,
    route,
)

# 固定白天时刻(避开默认免打扰 22:00-07:30,保证 tier 断言与时钟无关)
_NOON = datetime(2026, 9, 1, 12, 0)


def _channels(actions):
    return [a.channel for a in actions]


class TestDefaultRoutes:
    """默认配置(无 notification: 段)下 5 种事件的档位与通道展开。"""

    @pytest.mark.parametrize(
        "event_type,tier",
        [
            ("awaiting_question", "interrupt"),
            ("stuck_detected", "interrupt"),
            ("gate_waiting", "interrupt"),
            ("judge_rejected", "interrupt"),
            ("judge_escalated", "interrupt"),
            ("stage_completed", "batch"),
        ],
    )
    def test_default_table(self, event_type, tier):
        actions = route(event_type, config={}, now=_NOON)
        assert actions[0].tier == tier
        if tier == "interrupt":
            assert _channels(actions) == ["wechat", "desktop"]
        else:
            assert _channels(actions) == ["desktop"]

    def test_unknown_event_defaults_to_batch(self):
        """未知事件(不在默认表)→ batch(宁攒勿扰,打断省着用)。"""
        actions = route("totally_new_event", config={}, now=_NOON)
        assert actions[0].tier == "batch"
        assert _channels(actions) == ["desktop"]

    def test_project_param_accepted_no_effect(self):
        """project 参数收下但默认策略不分项目(预留)。"""
        assert route("stage_completed", project="mgm", config={}, now=_NOON) == route(
            "stage_completed", config={}, now=_NOON
        )

    def test_config_none_loads_from_get_config(self, monkeypatch):
        """config=None → 读 config.yaml(经 infra.config.get_config)。"""
        monkeypatch.setattr(
            "story_lifecycle.infra.config.get_config",
            lambda: {},
        )
        actions = route("stage_completed", now=_NOON)
        assert actions[0].tier == "batch"


class TestConfigOverride:
    """config.yaml ``notification:`` 段覆盖默认表。"""

    def test_routes_override(self):
        cfg = {"notification": {"routes": {"stage_completed": "interrupt"}}}
        actions = route("stage_completed", config=cfg, now=_NOON)
        assert actions[0].tier == "interrupt"
        assert _channels(actions) == ["wechat", "desktop"]

    def test_routes_demote_interrupt(self):
        cfg = {"notification": {"routes": {"awaiting_question": "digest"}}}
        actions = route("awaiting_question", config=cfg, now=_NOON)
        assert actions[0].tier == "digest"
        # WP-C:digest 档通道 desktop-only → (wechat, desktop)(日清简报推微信)
        assert _channels(actions) == ["wechat", "desktop"]

    def test_invalid_tier_value_falls_back_to_batch(self):
        """routes 值打错字(非合法档位)→ 回 batch,不炸。"""
        cfg = {"notification": {"routes": {"judge_rejected": "urgent!!"}}}
        actions = route("judge_rejected", config=cfg, now=_NOON)
        assert actions[0].tier == "batch"

    def test_section_missing_defaults(self, monkeypatch):
        """config 里完全没有 notification 段 → 全走代码内默认。"""
        monkeypatch.setattr(
            "story_lifecycle.infra.config.get_config",
            lambda: {"worktrees_root": "D:/worktrees"},
        )
        assert resolve_tier("gate_waiting", load_notification_section()) == "interrupt"
        assert resolve_tier("whatever", load_notification_section()) == "batch"

    def test_default_table_constant_shape(self):
        """默认表契约:v1 五事件 + WP-D patrol_failed / WP-C bug_aging、
        story_overdue = interrupt;daily_digest = digest;stage_completed = batch。"""
        assert DEFAULT_ROUTES["stage_completed"] == "batch"
        assert all(
            DEFAULT_ROUTES[e] == "interrupt"
            for e in (
                "awaiting_question",
                "stuck_detected",
                "gate_waiting",
                "judge_rejected",
                "judge_escalated",
                "patrol_failed",
                "bug_aging",
                "story_overdue",
            )
        )
        assert DEFAULT_ROUTES["daily_digest"] == "digest"
        assert ChannelAction("wechat", "interrupt") == ChannelAction(
            "wechat", "interrupt"
        )


class TestQuietHours:
    """免打扰时段(默认 22:00-07:30,可配):interrupt 降级 batch。"""

    def test_interrupt_downgraded_inside_quiet_window(self):
        cfg = {"notification": {}}
        actions = route("judge_escalated", config=cfg, now=datetime(2026, 9, 1, 23, 0))
        assert actions[0].tier == "batch"
        assert _channels(actions) == ["desktop"]

    @pytest.mark.parametrize(
        "hh,mm,quiet",
        [
            (21, 59, False),  # 窗口前 1 分钟
            (22, 0, True),  # 起点含
            (23, 59, True),
            (0, 0, True),  # 跨午夜
            (7, 29, True),  # 终点前 1 分钟
            (7, 30, False),  # 终点不含
            (12, 0, False),
        ],
    )
    def test_default_window_boundaries(self, hh, mm, quiet):
        assert (
            in_quiet_hours(datetime(2026, 9, 1, hh, mm), {}) is quiet
        ), f"{hh:02d}:{mm:02d} 应为 quiet={quiet}"

    def test_custom_window(self):
        cfg = {"notification": {"quiet_hours": {"start": "12:00", "end": "14:00"}}}
        assert in_quiet_hours(datetime(2026, 9, 1, 13, 0), cfg["notification"])
        assert not in_quiet_hours(datetime(2026, 9, 1, 15, 0), cfg["notification"])
        actions = route("stuck_detected", config=cfg, now=datetime(2026, 9, 1, 13, 0))
        assert actions[0].tier == "batch"

    def test_explicit_null_disables_quiet_hours(self):
        """``quiet_hours: null`` → 显式关闭,interrupt 深夜也不降级。"""
        section = {"quiet_hours": None}
        assert not in_quiet_hours(datetime(2026, 9, 1, 23, 30), section)
        actions = route("gate_waiting", config={"notification": section},
                        now=datetime(2026, 9, 1, 23, 30))
        assert actions[0].tier == "interrupt"

    def test_bad_values_no_crash_no_quiet(self):
        """坏配置(start 解析失败)→ 不降级(通知宁多勿丢)。"""
        section = {"quiet_hours": {"start": "abc", "end": "07:30"}}
        assert not in_quiet_hours(datetime(2026, 9, 1, 23, 0), section)

    def test_batch_events_unaffected_by_quiet_hours(self):
        """batch 事件免打扰时段维持 batch(无降级概念)。"""
        actions = route("stage_completed", config={}, now=datetime(2026, 9, 1, 23, 0))
        assert actions[0].tier == "batch"

    def test_router_is_pure_no_writes(self, tmp_path):
        """纯 Decider 回归:route 不写库不发消息(同参数重复调用结果一致)。"""
        cfg = {"notification": {"routes": {"x": "interrupt"}}}
        a1 = route("x", config=cfg, now=_NOON)
        a2 = route("x", config=cfg, now=_NOON)
        assert a1 == a2
