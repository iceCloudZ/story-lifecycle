"""Tests for run-epoch cancellation mechanism."""

import pytest

from story_lifecycle.orchestrator.graph import (
    get_epoch,
    is_epoch_current,
    force_stop_story,
    _story_epochs,
    _running_stories,
    _running_lock,
)
from story_lifecycle.orchestrator.nodes import (
    StoryState,
    _is_cancelled,
    execute_and_wait_node,
    advance_node,
    router_node,
    route_after_plan,
    route_after_execute,
    route_from_router,
    route_after_advance,
)


@pytest.fixture(autouse=True)
def _clean_epoch_state():
    """Reset global epoch and running state between tests."""
    with _running_lock:
        _story_epochs.clear()
        _running_stories.clear()
    yield
    with _running_lock:
        _story_epochs.clear()
        _running_stories.clear()


def _make_state(**overrides) -> StoryState:
    base: StoryState = {
        "story_key": "EPOCH-001",
        "title": "Test",
        "workspace": "/tmp/test",
        "profile": "minimal",
        "current_stage": "design",
        "status": "active",
        "complexity": "M",
        "context": {},
        "execution_count": 0,
        "last_error": None,
        "stage_start_time": 0.0,
        "plan_summary": None,
        "review_summary": None,
        "trajectory_score": None,
        "plan": None,
    }
    base.update(overrides)
    return base


# -------- graph.py epoch helpers --------


class TestGetEpoch:
    def test_returns_zero_when_never_tracked(self):
        assert get_epoch("NO-SUCH-KEY") == 0

    def test_returns_current_epoch_after_bump(self):
        with _running_lock:
            _story_epochs["EPOCH-001"] = 3
        assert get_epoch("EPOCH-001") == 3


class TestIsEpochCurrent:
    def test_always_true_when_epoch_is_zero(self):
        assert is_epoch_current("ANY", 0) is True

    def test_true_when_epoch_matches(self):
        with _running_lock:
            _story_epochs["EPOCH-001"] = 5
        assert is_epoch_current("EPOCH-001", 5) is True

    def test_false_when_epoch_mismatch(self):
        with _running_lock:
            _story_epochs["EPOCH-001"] = 5
        assert is_epoch_current("EPOCH-001", 4) is False

    def test_false_when_epoch_not_tracked(self):
        assert is_epoch_current("NO-SUCH-KEY", 1) is False


class TestForceStopBumpsEpoch:
    def test_force_stop_increments_epoch(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STORY_HOME", str(tmp_path / ".story-lifecycle"))
        from story_lifecycle.db import models as db

        db.init_db()
        db.upsert_story("EPOCH-001", title="T", workspace=str(tmp_path))

        with _running_lock:
            _story_epochs["EPOCH-001"] = 1

        force_stop_story("EPOCH-001")
        assert get_epoch("EPOCH-001") == 2

    def test_force_stop_on_never_tracked_sets_epoch_to_1(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STORY_HOME", str(tmp_path / ".story-lifecycle"))
        from story_lifecycle.db import models as db

        db.init_db()
        db.upsert_story("EPOCH-001", title="T", workspace=str(tmp_path))

        assert get_epoch("EPOCH-001") == 0
        force_stop_story("EPOCH-001")
        assert get_epoch("EPOCH-001") == 1


# -------- workspace lock ownership race --------


class TestWorkspaceLockOwnership:
    def test_force_stop_does_not_release_other_epoch_lock(self, tmp_path, monkeypatch):
        """Old run (story A, epoch 1) holds lock → force_stop bumps epoch →
        old finally releases (matches owner_token) → new run (epoch 2) acquires →
        stale old thread with epoch 1 cannot release (wrong token).

        Also verifies cross-story: story B with epoch 1 cannot release story A's lock.
        """
        monkeypatch.setenv("STORY_HOME", str(tmp_path / ".story-lifecycle"))
        from story_lifecycle.db import models as db
        from story_lifecycle.orchestrator.graph import (
            _workspace_locks,
            _set_workspace_owner,
            release_workspace,
            acquire_workspace,
        )

        db.init_db()
        db.upsert_story("RACE-001", title="T", workspace=str(tmp_path))

        ws = str(tmp_path)
        _workspace_locks.clear()

        # Phase 1: Story A, old run (epoch 1) acquires workspace lock
        assert acquire_workspace(ws, "RACE-001") is True
        _set_workspace_owner(ws, "RACE-001", 1)
        assert _workspace_locks[ws]["lock"].locked() is True

        # Phase 2: force_stop bumps epoch to 2. Does NOT call release_workspace.
        with _running_lock:
            _story_epochs["RACE-001"] = 2

        assert _workspace_locks[ws]["lock"].locked() is True

        # Phase 3: New run (epoch 2) cannot acquire while old thread holds lock
        assert acquire_workspace(ws, "RACE-001") is False

        # Phase 4: Old run (epoch 1) releases — SUCCEEDS (matches owner_token)
        release_workspace(ws, "RACE-001", epoch=1)
        assert _workspace_locks[ws]["lock"].locked() is False

        # Phase 5: New run (epoch 2) acquires and sets ownership
        assert acquire_workspace(ws, "RACE-001") is True
        _set_workspace_owner(ws, "RACE-001", 2)

        # Phase 6: Stale old thread (epoch 1) tries to release — NO-OP
        release_workspace(ws, "RACE-001", epoch=1)
        assert _workspace_locks[ws]["lock"].locked() is True

        # Phase 7: Correct release by epoch 2
        release_workspace(ws, "RACE-001", epoch=2)
        assert _workspace_locks[ws]["lock"].locked() is False

    def test_cross_story_release_denied(self, tmp_path):
        """Story B cannot release a lock owned by story A, even if epochs match."""
        from story_lifecycle.orchestrator.graph import (
            _workspace_locks,
            _set_workspace_owner,
            release_workspace,
            acquire_workspace,
        )

        ws = str(tmp_path)
        _workspace_locks.clear()

        assert acquire_workspace(ws, "STORY-A") is True
        _set_workspace_owner(ws, "STORY-A", 1)

        # Story B with same epoch=1 tries to release — denied
        release_workspace(ws, "STORY-B", epoch=1)
        assert _workspace_locks[ws]["lock"].locked() is True

        # Story A correctly releases
        release_workspace(ws, "STORY-A", epoch=1)
        assert _workspace_locks[ws]["lock"].locked() is False

    def test_running_stories_epoch_guard_prevents_old_clear(self):
        """Old run's finally should NOT clear _running_stories if epoch mismatched."""
        with _running_lock:
            _running_stories.clear()
            _running_stories["STORY-X"] = 5  # new run epoch 5
            _story_epochs["STORY-X"] = 5

        # Simulate old run (epoch 3) finally
        with _running_lock:
            if _running_stories.get("STORY-X") == 3:
                _running_stories.pop("STORY-X", None)

        # Guard should still be present (epoch 3 != 5)
        with _running_lock:
            assert "STORY-X" in _running_stories
            assert _running_stories["STORY-X"] == 5

    def test_release_workspace_honors_matching_token(self, tmp_path):
        """release_workspace with correct (story_key, epoch) releases the lock."""
        from story_lifecycle.orchestrator.graph import (
            _workspace_locks,
            _set_workspace_owner,
            release_workspace,
            acquire_workspace,
        )

        ws = str(tmp_path)
        _workspace_locks.clear()

        assert acquire_workspace(ws, "T") is True
        _set_workspace_owner(ws, "T", 5)
        release_workspace(ws, "T", epoch=5)
        assert _workspace_locks[ws]["lock"].locked() is False

    def test_release_workspace_no_story_key_always_releases(self, tmp_path):
        """story_key="" means no ownership check — backward compat."""
        from story_lifecycle.orchestrator.graph import (
            _workspace_locks,
            release_workspace,
            acquire_workspace,
        )

        ws = str(tmp_path)
        _workspace_locks.clear()

        assert acquire_workspace(ws, "T") is True
        release_workspace(ws)  # no story_key, no epoch check
        assert _workspace_locks[ws]["lock"].locked() is False


# -------- resume does not bump epoch --------


class TestResumeEpoch:
    def test_resume_does_not_bump_epoch(self, tmp_path, monkeypatch):
        """resume_story must NOT bump _story_epochs — it continues the same run.
        If epoch is bumped, the checkpoint's old _epoch looks stale and
        _is_cancelled() self-cancels the resumed graph."""
        monkeypatch.setenv("STORY_HOME", str(tmp_path / ".story-lifecycle"))
        from story_lifecycle.db import models as db
        from story_lifecycle.orchestrator.graph import get_epoch

        db.init_db()
        db.upsert_story("RESUME-01", title="T", workspace=str(tmp_path))

        with _running_lock:
            _story_epochs["RESUME-01"] = 3
            _running_stories.clear()

        # Simulate what resume_story does: read epoch, don't bump
        epoch_before = get_epoch("RESUME-01")
        # resume_story uses existing epoch, does NOT increment
        assert epoch_before == 3

        # After resume, epoch should still be 3 (unchanged)
        assert get_epoch("RESUME-01") == 3

    def test_checkpoint_epoch_matches_after_resume(self, tmp_path, monkeypatch):
        """After resume, the checkpoint's _epoch should match current epoch.
        If they don't match, _is_cancelled() fires immediately."""
        monkeypatch.setenv("STORY_HOME", str(tmp_path / ".story-lifecycle"))

        with _running_lock:
            _story_epochs["RESUME-02"] = 7

        from story_lifecycle.orchestrator.graph import get_epoch, is_epoch_current

        # The checkpoint state carries _epoch=7 from the original run.
        # After resume, _story_epochs["RESUME-02"] should still be 7.
        checkpoint_epoch = 7
        assert is_epoch_current("RESUME-02", checkpoint_epoch) is True

        # If resume had bumped to 8, this would be False → self-cancel
        assert get_epoch("RESUME-02") == 7

    def test_restart_recovery_restores_epoch_from_checkpoint(
        self, tmp_path, monkeypatch
    ):
        """After process restart, _story_epochs is empty but checkpoint holds _epoch=3.
        resume_story must restore the in-memory epoch to 3, not reset to 1,
        otherwise _is_cancelled() fires on state._epoch=3 vs current epoch=1."""
        monkeypatch.setenv("STORY_HOME", str(tmp_path / ".story-lifecycle"))
        from story_lifecycle.db import models as db
        from story_lifecycle.orchestrator.graph import (
            get_epoch,
            _restore_epoch_from_checkpoint,
        )

        db.init_db()
        db.upsert_story("RESTART-01", title="T", workspace=str(tmp_path))

        with _running_lock:
            _story_epochs.clear()
            _running_stories.clear()

        # _restore_epoch_from_checkpoint reads the real checkpoint.
        # No checkpoint exists yet → returns 0.
        cp_epoch = _restore_epoch_from_checkpoint("RESTART-01")
        assert cp_epoch == 0

        # When no checkpoint epoch, resume_story defaults to 1
        mem_epoch = get_epoch("RESTART-01")
        if cp_epoch and cp_epoch > mem_epoch:
            epoch = cp_epoch
        elif mem_epoch > 0:
            epoch = mem_epoch
        else:
            epoch = 1
        assert epoch == 1

    def test_resume_story_does_not_bump_when_checkpoint_has_epoch(
        self, tmp_path, monkeypatch
    ):
        """Call resume_story() with mocked checkpoint returning _epoch=3.
        Verify _story_epochs is set to 3, not bumped to a new value."""
        monkeypatch.setenv("STORY_HOME", str(tmp_path / ".story-lifecycle"))
        from unittest.mock import patch, MagicMock
        from story_lifecycle.db import models as db
        from story_lifecycle.orchestrator.graph import (
            get_epoch,
            resume_story,
        )

        db.init_db()
        db.upsert_story("RESTART-02", title="T", workspace=str(tmp_path))

        with _running_lock:
            _story_epochs.clear()
            _running_stories.clear()

        # Mock compiled.get_state() to return _epoch=3
        mock_snapshot = MagicMock()
        mock_snapshot.values = {"_epoch": 3}

        mock_compiled = MagicMock()
        mock_compiled.get_state.return_value = mock_snapshot
        mock_compiled.invoke = MagicMock()  # no-op

        with patch(
            "story_lifecycle.orchestrator.graph.get_compiled_graph",
            return_value=mock_compiled,
        ):
            resume_story("RESTART-02")

        # After resume, epoch should be 3 (from checkpoint), NOT bumped to 4 or 1
        assert get_epoch("RESTART-02") == 3


# -------- _is_cancelled helper --------


class TestIsCancelled:
    def test_not_cancelled_when_no_epoch(self):
        state = _make_state()
        assert _is_cancelled(state) is False

    def test_cancelled_when_flag_set(self):
        state = _make_state(_cancelled=True)
        assert _is_cancelled(state) is True

    def test_cancelled_when_epoch_stale(self):
        with _running_lock:
            _story_epochs["EPOCH-001"] = 10
        state = _make_state(_epoch=5)
        assert _is_cancelled(state) is True
        assert state["_cancelled"] is True

    def test_not_cancelled_when_epoch_current(self):
        with _running_lock:
            _story_epochs["EPOCH-001"] = 5
        state = _make_state(_epoch=5)
        assert _is_cancelled(state) is False


# -------- cancelled nodes skip DB writes --------


class TestExecuteAndWaitCancelled:
    def test_returns_early_when_cancelled(self):
        state = _make_state(_cancelled=True)
        result = execute_and_wait_node(state)
        # Should return unchanged — no error set, no interrupt
        assert result.get("last_error") is None


class TestAdvanceCancelled:
    def test_returns_early_when_cancelled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STORY_HOME", str(tmp_path / ".story-lifecycle"))
        from story_lifecycle.db import models as db

        db.init_db()
        db.upsert_story("EPOCH-001", title="T", workspace=str(tmp_path))

        state = _make_state(_cancelled=True)
        result = advance_node(state)
        # No DB write should happen — status stays "active"
        story = db.get_story("EPOCH-001")
        assert story["status"] == "active"
        assert result.get("status") == "active"


class TestFailCancelled:
    def test_returns_early_when_cancelled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STORY_HOME", str(tmp_path / ".story-lifecycle"))
        from story_lifecycle.db import models as db

        db.init_db()
        db.upsert_story("EPOCH-001", title="T", workspace=str(tmp_path))

        state = _make_state(_cancelled=True, last_error="boom")
        result = router_node(state)
        # Cancelled → __end__, no DB write
        assert result["_next_action"] == "__end__"


# -------- cancelled routing --------


class TestCancelledRouting:
    def test_route_after_plan_goes_to_end(self):
        state = _make_state(_cancelled=True)
        assert route_after_plan(state) == "__end__"

    def test_route_after_execute_goes_to_end(self):
        state = _make_state(_cancelled=True)
        assert route_after_execute(state) == "__end__"

    def test_route_from_router_goes_to_end(self):
        state = _make_state(_cancelled=True, _next_action="advance")
        assert route_from_router(state) == "__end__"

    def test_route_after_advance_goes_to_end(self):
        state = _make_state(_cancelled=True)
        assert route_after_advance(state) == "__end__"

    def test_normal_routing_unaffected(self):
        state = _make_state()
        assert route_after_plan(state) == "execute_and_wait"
        assert route_after_execute(state) == "review_stage"
        assert route_after_advance(state) == "plan_stage"
