"""Tests for execution mode selection (profile.execution_mode → headless flag)."""

from types import SimpleNamespace

import pytest

from story_lifecycle.orchestrator.engine.execution import (
    ExecutionMode,
    headless_from_profile,
    parse_execution_mode,
)


class TestParseExecutionMode:
    def test_default_is_interactive_pty(self):
        assert parse_execution_mode(None) == ExecutionMode.INTERACTIVE_PTY
        assert parse_execution_mode("") == ExecutionMode.INTERACTIVE_PTY

    def test_headless_parsed(self):
        assert parse_execution_mode("headless") == ExecutionMode.HEADLESS

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            parse_execution_mode("bogus")


class TestHeadlessFromProfile:
    """headless_from_profile:profile 的 execution_mode 决定走 headless(kimi -p wrapper)
    还是 interactive PTY。realtest profile 显式 headless(PTY 路径 kimi idle 未注入,headless 验证可跑通)。"""

    def test_headless_profile_returns_true(self):
        assert headless_from_profile(SimpleNamespace(execution_mode="headless")) is True

    def test_pty_profile_returns_false(self):
        assert headless_from_profile(SimpleNamespace(execution_mode="interactive_pty")) is False

    def test_none_profile_returns_false(self):
        assert headless_from_profile(None) is False

    def test_missing_attr_returns_false(self):
        assert headless_from_profile(SimpleNamespace()) is False
