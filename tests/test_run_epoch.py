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
    poll_completion_node,
    advance_node,
    fail_node,
    route_after_plan,
    route_after_poll,
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
        """Old run holds lock (epoch 1) → force_stop bumps epoch to 2 (NO release) →
        old finally releases with epoch 1 (succeeds, it's the owner) →
        new run (epoch 2) acquires and sets owner_epoch=2 →
        old stale release with epoch 1 is a no-op (wrong epoch).

        The key: force_stop no longer call release_workspace from a non-owner thread.
        The old worker's finally handles release naturally. If a stale thread tries
        to release after ownership transferred, the epoch guard blocks it.
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

        # Phase 1: Old run (epoch 1) acquires workspace lock
        assert acquire_workspace(ws, "RACE-001") is True
        _set_workspace_owner(ws, 1)
        assert _workspace_locks[ws]["lock"].locked() is True

        # Phase 2: force_stop bumps epoch to 2. Does NOT call release_workspace.
        # (In the fixed code, force_stop_story only bumps epoch + removes
        # running guard. The workspace lock is left for the owning thread.)
        with _running_lock:
            _story_epochs["RACE-001"] = 2

        # Lock still held — force_stop didn't touch it
        assert _workspace_locks[ws]["lock"].locked() is True

        # Phase 3: New run (epoch 2) cannot acquire while old thread holds lock
        assert acquire_workspace(ws, "RACE-001") is False

        # Phase 4: Old run's finally block calls release_workspace(ws, epoch=1).
        # This SUCCEEDS because epoch 1 matches owner_epoch 1.
        release_workspace(ws, epoch=1)
        assert _workspace_locks[ws]["lock"].locked() is False

        # Phase 5: New run (epoch 2) now acquires and sets ownership
        assert acquire_workspace(ws, "RACE-001") is True
        _set_workspace_owner(ws, 2)

        # Phase 6: Stale old thread tries to release again with epoch 1.
        # This is a NO-OP — owner_epoch is 2, caller epoch is 1.
        release_workspace(ws, epoch=1)
        assert _workspace_locks[ws]["lock"].locked() is True  # still held by epoch 2

        # Phase 7: New run's finally releases correctly
        release_workspace(ws, epoch=2)
        assert _workspace_locks[ws]["lock"].locked() is False

    def test_release_workspace_honors_matching_epoch(self, tmp_path):
        """release_workspace with correct epoch releases the lock."""
        from story_lifecycle.orchestrator.graph import (
            _workspace_locks,
            _set_workspace_owner,
            release_workspace,
            acquire_workspace,
        )

        ws = str(tmp_path)
        _workspace_locks.clear()

        assert acquire_workspace(ws, "T") is True
        _set_workspace_owner(ws, 5)
        release_workspace(ws, epoch=5)
        assert _workspace_locks[ws]["lock"].locked() is False

    def test_release_workspace_with_epoch_zero_always_releases(self, tmp_path):
        """epoch=0 means no ownership tracking — backward compat release."""
        from story_lifecycle.orchestrator.graph import (
            _workspace_locks,
            release_workspace,
            acquire_workspace,
        )

        ws = str(tmp_path)
        _workspace_locks.clear()

        assert acquire_workspace(ws, "T") is True
        # No owner set → owner_epoch stays 0
        release_workspace(ws, epoch=0)  # epoch 0 matches
        assert _workspace_locks[ws]["lock"].locked() is False


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


class TestPollCompletionCancelled:
    def test_returns_early_when_cancelled(self):
        state = _make_state(_cancelled=True)
        result = poll_completion_node(state)
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
        result = fail_node(state)
        # No DB write — status stays "active", not "blocked"
        story = db.get_story("EPOCH-001")
        assert story["status"] == "active"
        assert result.get("status") == "active"


# -------- cancelled routing --------


class TestCancelledRouting:
    def test_route_after_plan_goes_to_end(self):
        state = _make_state(_cancelled=True)
        assert route_after_plan(state) == "__end__"

    def test_route_after_poll_goes_to_end(self):
        state = _make_state(_cancelled=True)
        assert route_after_poll(state) == "__end__"

    def test_route_from_router_goes_to_end(self):
        state = _make_state(_cancelled=True, _next_action="advance")
        assert route_from_router(state) == "__end__"

    def test_route_after_advance_goes_to_end(self):
        state = _make_state(_cancelled=True)
        assert route_after_advance(state) == "__end__"

    def test_normal_routing_unaffected(self):
        state = _make_state()
        assert route_after_plan(state) == "execute_stage"
        assert route_after_poll(state) == "review_stage"
        assert route_after_advance(state) == "plan_stage"
