"""Tests for stuck reason detection — RL-04.

Covers all 10 deterministic stuck_reason codes in debug_packet._explain_stuck_reason.
"""

import json
from unittest.mock import patch

from story_lifecycle.orchestrator.observability.debug_packet import _explain_stuck_reason


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _story(**overrides):
    """Build a minimal story dict for testing."""
    base = {
        "status": "active",
        "current_stage": "design",
        "story_key": "TEST-001",
        "context_json": "{}",
    }
    base.update(overrides)
    return base


def _no_exit():
    """Simulate no CLI exit."""
    from story_lifecycle.orchestrator.entry import CliExitState

    return CliExitState.NONE


def _exited_without_done():
    """Simulate CLI exited without done."""
    from story_lifecycle.orchestrator.entry import CliExitState

    return CliExitState.EXITED_WITHOUT_DONE


# ---------------------------------------------------------------------------
# Tests for each stuck reason code
# ---------------------------------------------------------------------------


class TestMissingConfig:
    def test_no_llm_config(self):
        with patch(
            "story_lifecycle.orchestrator.observability.debug_packet._check_llm_configured",
            return_value=False,
        ):
            result = _explain_stuck_reason(_story(), False, None, _no_exit(), True)
        assert result["code"] == "missing_config"
        assert result["severity"] == "error"

    def test_with_config_skips(self):
        with patch(
            "story_lifecycle.orchestrator.observability.debug_packet._check_llm_configured",
            return_value=True,
        ):
            result = _explain_stuck_reason(_story(), False, None, _no_exit(), True)
        assert result["code"] != "missing_config"


class TestStoryBlocked:
    def test_blocked_status(self):
        with patch(
            "story_lifecycle.orchestrator.observability.debug_packet._check_llm_configured",
            return_value=True,
        ):
            result = _explain_stuck_reason(
                _story(status="blocked"), False, None, _no_exit(), True
            )
        assert result["code"] == "story_blocked"
        assert result["severity"] == "error"


class TestWaitingSubtasks:
    def test_waiting_subtasks_status(self):
        with patch(
            "story_lifecycle.orchestrator.observability.debug_packet._check_llm_configured",
            return_value=True,
        ):
            result = _explain_stuck_reason(
                _story(status="waiting_subtasks"), False, None, _no_exit(), True
            )
        assert result["code"] == "waiting_subtasks"
        assert result["severity"] == "info"


class TestGateBlocked:
    def test_paused_with_gate(self):
        ctx = json.dumps({"last_gate_decision_id": "gate-123"})
        with patch(
            "story_lifecycle.orchestrator.observability.debug_packet._check_llm_configured",
            return_value=True,
        ):
            result = _explain_stuck_reason(
                _story(status="paused", context_json=ctx), False, None, _no_exit(), True
            )
        assert result["code"] == "gate_blocked"
        assert result["severity"] == "warning"

    def test_paused_without_gate(self):
        with patch(
            "story_lifecycle.orchestrator.observability.debug_packet._check_llm_configured",
            return_value=True,
        ):
            result = _explain_stuck_reason(
                _story(status="paused", context_json="{}"),
                False,
                None,
                _no_exit(),
                True,
            )
        assert result["code"] != "gate_blocked"


class TestDoneMalformed:
    def test_done_exists_but_invalid(self):
        with patch(
            "story_lifecycle.orchestrator.observability.debug_packet._check_llm_configured",
            return_value=True,
        ):
            result = _explain_stuck_reason(
                _story(),
                done_exists=True,
                done_valid=False,
                cli_exit=_no_exit(),
                session_alive=True,
            )
        assert result["code"] == "done_malformed"
        assert result["severity"] == "error"

    def test_done_exists_and_valid(self):
        with patch(
            "story_lifecycle.orchestrator.observability.debug_packet._check_llm_configured",
            return_value=True,
        ):
            result = _explain_stuck_reason(
                _story(),
                done_exists=True,
                done_valid=True,
                cli_exit=_no_exit(),
                session_alive=True,
            )
        assert result["code"] != "done_malformed"


class TestStageTimeout:
    def test_long_running_stage(self):
        with patch(
            "story_lifecycle.orchestrator.observability.debug_packet._check_llm_configured",
            return_value=True,
        ):
            result = _explain_stuck_reason(
                _story(), False, None, _no_exit(), True, stage_elapsed_seconds=1000
            )
        assert result["code"] == "stage_timeout"
        assert result["severity"] == "warning"

    def test_short_running_stage_not_timeout(self):
        with patch(
            "story_lifecycle.orchestrator.observability.debug_packet._check_llm_configured",
            return_value=True,
        ):
            result = _explain_stuck_reason(
                _story(), False, None, _no_exit(), True, stage_elapsed_seconds=100
            )
        assert result["code"] != "stage_timeout"

    def test_session_dead_not_timeout(self):
        with patch(
            "story_lifecycle.orchestrator.observability.debug_packet._check_llm_configured",
            return_value=True,
        ):
            result = _explain_stuck_reason(
                _story(), False, None, _no_exit(), False, stage_elapsed_seconds=1000
            )
        # session dead → not stage_timeout (agent isn't running)
        assert result["code"] != "stage_timeout"


class TestCliExitedWithoutDone:
    def test_exited_without_done(self):
        with patch(
            "story_lifecycle.orchestrator.observability.debug_packet._check_llm_configured",
            return_value=True,
        ):
            result = _explain_stuck_reason(
                _story(), False, None, _exited_without_done(), True
            )
        assert result["code"] == "cli_exited_without_done"
        assert result["severity"] == "warning"


class TestDoneWaiting:
    def test_session_alive_no_done(self):
        with patch(
            "story_lifecycle.orchestrator.observability.debug_packet._check_llm_configured",
            return_value=True,
        ):
            result = _explain_stuck_reason(_story(), False, None, _no_exit(), True)
        assert result["code"] == "done_waiting"
        assert result["severity"] == "info"


class TestLoopExhausted:
    def test_loop_exhausted(self):
        with patch(
            "story_lifecycle.orchestrator.observability.debug_packet._check_llm_configured",
            return_value=True,
        ):
            with patch(
                "story_lifecycle.orchestrator.observability.debug_packet._has_loop_exhausted",
                return_value=True,
            ):
                result = _explain_stuck_reason(_story(), True, True, _no_exit(), False)
        assert result["code"] == "loop_exhausted"
        assert result["severity"] == "warning"

    def test_not_exhausted(self):
        with patch(
            "story_lifecycle.orchestrator.observability.debug_packet._check_llm_configured",
            return_value=True,
        ):
            with patch(
                "story_lifecycle.orchestrator.observability.debug_packet._has_loop_exhausted",
                return_value=False,
            ):
                result = _explain_stuck_reason(_story(), True, True, _no_exit(), False)
        assert result["code"] == "none"


class TestNoneReason:
    def test_happy_path(self):
        """Happy path: done exists, valid, no issues → 'none'."""
        with patch(
            "story_lifecycle.orchestrator.observability.debug_packet._check_llm_configured",
            return_value=True,
        ):
            with patch(
                "story_lifecycle.orchestrator.observability.debug_packet._has_loop_exhausted",
                return_value=False,
            ):
                result = _explain_stuck_reason(_story(), True, True, _no_exit(), False)
        assert result["code"] == "none"
        assert result["severity"] == "info"


class TestPriorityOrder:
    """Verify that earlier reasons take priority over later ones."""

    def test_blocked_over_gate(self):
        """blocked status takes priority over paused checks."""
        with patch(
            "story_lifecycle.orchestrator.observability.debug_packet._check_llm_configured",
            return_value=True,
        ):
            result = _explain_stuck_reason(
                _story(status="blocked"), False, None, _no_exit(), True
            )
        assert result["code"] == "story_blocked"

    def test_config_over_all(self):
        """missing_config is checked first regardless of other state."""
        with patch(
            "story_lifecycle.orchestrator.observability.debug_packet._check_llm_configured",
            return_value=False,
        ):
            result = _explain_stuck_reason(
                _story(status="blocked"), True, False, _exited_without_done(), True
            )
        assert result["code"] == "missing_config"
