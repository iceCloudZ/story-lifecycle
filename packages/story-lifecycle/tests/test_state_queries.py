"""Tests for sourcing/state_queries.py — CQRS 读侧纯函数。"""

from story_lifecycle.sourcing.state_queries import (
    CONFIRM_CONFIG,
    CONFIRM_NONE,
    CONFIRM_UI_BUTTON,
    current_lifecycle_state,
    get_confirm_type,
    get_next_state,
    is_at_terminal_state,
    is_story_state_complete,
    should_auto_advance,
    should_pause_for_gate,
)
from story_lifecycle.sourcing.lifecycle_state import LifecycleState, resolve_lifecycle_state


# 测试用拓扑(与 default.yaml 一致)
TOPOLOGY = {
    "开发": {"stages": ["design", "build"], "next": "测试", "confirm": {"type": "ui_button"}},
    "测试": {"stages": ["verify"], "next": "上线", "confirm": {"type": "config", "key": "auto_advance_test"}},
    "上线": {"stages": [], "next": "结项", "confirm": {"type": "none"}},
    "结项": {"stages": [], "next": None, "confirm": {"type": "none"}},
}


class TestIsStoryStateComplete:
    def test_all_stages_done(self):
        assert is_story_state_complete("开发", ["design", "build"], TOPOLOGY) is True

    def test_partial_stages(self):
        assert is_story_state_complete("开发", ["design"], TOPOLOGY) is False

    def test_no_stages_done(self):
        assert is_story_state_complete("开发", [], TOPOLOGY) is False

    def test_state_not_in_topology(self):
        assert is_story_state_complete("未知", ["design"], TOPOLOGY) is False

    def test_empty_topology(self):
        assert is_story_state_complete("开发", ["design"], {}) is False

    def test_state_with_no_stages(self):
        # 上线状态无 stages → 不算"完成"(无阶段可完成)
        assert is_story_state_complete("上线", [], TOPOLOGY) is False


class TestGetNextState:
    def test_dev_to_test(self):
        assert get_next_state("开发", TOPOLOGY) == "测试"

    def test_terminal_state_returns_none(self):
        assert get_next_state("结项", TOPOLOGY) is None

    def test_unknown_state(self):
        assert get_next_state("未知", TOPOLOGY) is None


class TestGetConfirmType:
    def test_ui_button(self):
        assert get_confirm_type("开发", TOPOLOGY) == CONFIRM_UI_BUTTON

    def test_config(self):
        assert get_confirm_type("测试", TOPOLOGY) == CONFIRM_CONFIG

    def test_none(self):
        assert get_confirm_type("上线", TOPOLOGY) == CONFIRM_NONE

    def test_unknown_defaults_none(self):
        assert get_confirm_type("未知", TOPOLOGY) == CONFIRM_NONE


class TestShouldAutoAdvance:
    def test_none_type_auto_advances(self):
        # 上线 stages 为空 → is_story_state_complete=False → 不会 auto
        # 用一个有 stages + confirm=none 的状态测(构造临时拓扑)
        topo = {"X": {"stages": ["a"], "next": "Y", "confirm": {"type": "none"}}}
        assert should_auto_advance("X", ["a"], topo) is True

    def test_ui_button_does_not_auto(self):
        assert should_auto_advance("开发", ["design", "build"], TOPOLOGY) is False

    def test_incomplete_does_not_advance(self):
        assert should_auto_advance("开发", ["design"], TOPOLOGY) is False


class TestShouldPauseForGate:
    def test_ui_button_pauses(self):
        assert should_pause_for_gate("开发", ["design", "build"], TOPOLOGY) is True

    def test_none_does_not_pause(self):
        topo = {"X": {"stages": ["a"], "next": "Y", "confirm": {"type": "none"}}}
        assert should_pause_for_gate("X", ["a"], topo) is False

    def test_incomplete_does_not_pause(self):
        assert should_pause_for_gate("开发", ["design"], TOPOLOGY) is False


class TestIsAtTerminalState:
    def test_terminal_with_done_stages(self):
        # 结项 next=None,但 stages 也空 → is_story_state_complete=False → 非终态判定
        # 构造一个有 stages + next=None 的终态
        topo = {"终": {"stages": ["last"], "next": None, "confirm": {"type": "none"}}}
        assert is_at_terminal_state("终", ["last"], topo) is True

    def test_non_terminal(self):
        assert is_at_terminal_state("开发", ["design", "build"], TOPOLOGY) is False


class TestResolveLifecycleState:
    def test_ctx_wins(self):
        assert resolve_lifecycle_state("测试", "开发") == "测试"

    def test_db_fallback(self):
        assert resolve_lifecycle_state(None, "开发") == "开发"

    def test_default_pending(self):
        assert resolve_lifecycle_state(None, None) == LifecycleState.PENDING.value


class TestCurrentLifecycleState:
    def test_ctx_priority(self):
        ctx = {"_lifecycle_state": "测试"}
        story = {"lifecycle_state": "开发"}
        assert current_lifecycle_state(ctx, story) == "测试"

    def test_db_fallback(self):
        assert current_lifecycle_state({}, {"lifecycle_state": "上线"}) == "上线"

    def test_default(self):
        assert current_lifecycle_state({}, {}) == LifecycleState.PENDING.value
