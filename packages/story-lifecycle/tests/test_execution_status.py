"""Tests for sourcing/execution_status.py — 4态 enum + normalize + 集合判断。"""

from story_lifecycle.sourcing.execution_status import (
    ACTIVE_STATUSES,
    ExecutionStatus,
    TERMINAL_STATUSES,
    is_active,
    is_terminal,
    normalize_status,
)


class TestNormalizeStatus:
    def test_core_values_unchanged(self):
        assert normalize_status("active") == "active"
        assert normalize_status("paused") == "paused"
        assert normalize_status("completed") == "completed"
        assert normalize_status("failed") == "failed"

    def test_legacy_implementing_to_active(self):
        assert normalize_status("implementing") == "active"

    def test_legacy_blocked_to_paused(self):
        assert normalize_status("blocked") == "paused"

    def test_legacy_waiting_subtasks_to_paused(self):
        assert normalize_status("waiting_subtasks") == "paused"

    def test_legacy_aborted_to_failed(self):
        assert normalize_status("aborted") == "failed"

    def test_unknown_value_passthrough(self):
        assert normalize_status("future_value") == "future_value"

    def test_none_returns_empty(self):
        assert normalize_status(None) == ""
        assert normalize_status("") == ""


class TestStatusSets:
    def test_terminal_contains_completed_and_failed(self):
        assert "completed" in TERMINAL_STATUSES
        assert "failed" in TERMINAL_STATUSES
        assert "active" not in TERMINAL_STATUSES
        assert "paused" not in TERMINAL_STATUSES

    def test_active_contains_active_and_paused(self):
        assert "active" in ACTIVE_STATUSES
        assert "paused" in ACTIVE_STATUSES
        assert "completed" not in ACTIVE_STATUSES
        assert "failed" not in ACTIVE_STATUSES


class TestPredicates:
    def test_is_terminal(self):
        assert is_terminal("completed") is True
        assert is_terminal("failed") is True
        assert is_terminal("active") is False
        assert is_terminal("paused") is False

    def test_is_terminal_normalizes_legacy(self):
        assert is_terminal("aborted") is True
        assert is_terminal("implementing") is False

    def test_is_active(self):
        assert is_active("active") is True
        assert is_active("paused") is True
        assert is_active("completed") is False
        assert is_active("failed") is False

    def test_is_active_normalizes_legacy(self):
        assert is_active("blocked") is True
        assert is_active("waiting_subtasks") is True
        assert is_active("aborted") is False


class TestEnum:
    def test_enum_values(self):
        assert ExecutionStatus.ACTIVE.value == "active"
        assert ExecutionStatus.PAUSED.value == "paused"
        assert ExecutionStatus.COMPLETED.value == "completed"
        assert ExecutionStatus.FAILED.value == "failed"

    def test_enum_is_str(self):
        assert isinstance(ExecutionStatus.ACTIVE, str)
        assert ExecutionStatus.ACTIVE == "active"
