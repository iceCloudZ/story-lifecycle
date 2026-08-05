"""Cross-process driver mutual exclusion via DB CAS (driver_claim column).

Root cause (real-run 2026-07-06, runbook §7.1): the in-process ``_running_stories``
dict (now only the CLI fallback in ``start_story_async``; the serve path routes
through ``scheduler.drive_story_sync``) cannot see other processes — each python
process has its own — so two drivers (e.g. ``tmp_drive_minimal`` + a ``serve``
worker) both passed the guard and double-drove the same story (event_log
``completed``/``judge_verdict`` events appeared ×2, interleaved per stage). Fix:
optimistic CAS on a *shared* DB column ``driver_claim`` — only one caller's
``UPDATE … WHERE driver_claim IS NULL`` wins; the loser bails. This supplements
(not replaces) the in-process dict, which still guards same-process re-entry.
"""

import story_lifecycle.orchestrator.engine.graph as graph
from story_lifecycle.infra.db import models as db


class TestClaimReleaseHelpers:
    def test_claim_wins_when_free(self, isolated_story_home):
        db.create_story("K1", "t", str(isolated_story_home))
        assert db.claim_story_driver("K1", "tok1") is True
        assert db.get_story("K1")["driver_claim"] == "tok1"

    def test_claim_loses_when_held_by_another(self, isolated_story_home):
        db.create_story("K2", "t", str(isolated_story_home))
        assert db.claim_story_driver("K2", "tok1") is True
        # A second caller (other process) races in — CAS fails, claim unchanged.
        assert db.claim_story_driver("K2", "tok2") is False
        assert db.get_story("K2")["driver_claim"] == "tok1"

    def test_release_only_drops_if_still_mine(self, isolated_story_home):
        db.create_story("K3", "t", str(isolated_story_home))
        db.claim_story_driver("K3", "tok1")
        db.release_story_driver("K3", "tok2")  # not mine → no-op
        assert db.get_story("K3")["driver_claim"] == "tok1"
        db.release_story_driver("K3", "tok1")  # mine → released
        assert db.get_story("K3")["driver_claim"] is None


class TestStartStoryAsyncCAS:
    def test_bails_when_already_claimed_by_other_process(
        self, isolated_story_home, monkeypatch
    ):
        db.create_story("K4", "t", str(isolated_story_home))
        db.update_story("K4", intake_state="ready")
        # Simulate another process already holding the claim.
        db.claim_story_driver("K4", "other-pid:1")
        monkeypatch.setattr(graph, "_running_stories", {})
        submitted = []
        monkeypatch.setattr(graph._executor, "submit", lambda *a, **k: submitted.append(a))

        graph.start_story_async("K4")

        # CAS lost → no submit, no in-process run, other process's claim untouched.
        assert submitted == []
        assert "K4" not in graph._running_stories
        assert db.get_story("K4")["driver_claim"] == "other-pid:1"

    def test_claims_and_drives_when_free(self, isolated_story_home, monkeypatch):
        db.create_story("K5", "t", str(isolated_story_home))
        db.update_story("K5", intake_state="ready")
        monkeypatch.setattr(graph, "_running_stories", {})
        submitted = []
        monkeypatch.setattr(graph._executor, "submit", lambda *a, **k: submitted.append(a))

        graph.start_story_async("K5")

        assert len(submitted) == 1
        # submit args: (run_story, story_key, epoch, claim_token)
        assert submitted[0][1] == "K5"
        claim_token = submitted[0][3]
        assert claim_token, "claim_token must be passed to run_story"
        assert "K5" in graph._running_stories
        assert db.get_story("K5")["driver_claim"] == claim_token

    def test_candidate_still_rejected_before_claim(self, isolated_story_home, monkeypatch):
        """Candidate guard runs before the CAS claim (don't claim a candidate)."""
        db.create_story("K7", "t", str(isolated_story_home))
        db.update_story("K7", intake_state="candidate", source_type="tapd", source_id="7")
        monkeypatch.setattr(graph, "_running_stories", {})
        submitted = []
        monkeypatch.setattr(graph._executor, "submit", lambda *a, **k: submitted.append(a))

        graph.start_story_async("K7")

        assert submitted == []
        # Candidate must NOT have acquired a driver claim.
        assert db.get_story("K7")["driver_claim"] is None


class TestRunStoryReleasesClaim:
    def test_finally_releases_my_claim(self, isolated_story_home, monkeypatch):
        db.create_story("K6", "t", str(isolated_story_home))
        db.update_story("K6", intake_state="ready", current_stage="design")
        monkeypatch.setattr(graph, "_running_stories", {})
        # Don't actually drive the orchestrator (design13:run_story → drive_story_sync)。
        monkeypatch.setattr(
            "story_lifecycle.orchestrator.scheduler.drive_story_sync", lambda key: None
        )

        token = "test-pid:1"
        db.claim_story_driver("K6", token)
        graph._running_stories["K6"] = 1

        graph.run_story("K6", epoch=1, claim_token=token)

        assert db.get_story("K6")["driver_claim"] is None
        assert "K6" not in graph._running_stories


class TestDeadPidRecovery:
    """A driver that crashed (PID gone) must not lock the story forever.

    Real-run 2026-07-20: emergency_stop killed the driver, but driver_claim
    stayed → next confirm's CAS failed ("already driven by another process")
    → story stuck in planning forever. Fix: claim_story_driver checks if the
    holding PID is still alive; dead PID → seize.
    """

    def test_seizes_when_holding_pid_is_dead(self, isolated_story_home):
        db.create_story("DP1", "t", str(isolated_story_home))
        # A PID that definitely doesn't exist (very high, never assigned).
        db.claim_story_driver("DP1", "99999999:1")
        # New caller should be able to seize.
        assert db.claim_story_driver("DP1", "12345:2") is True
        assert db.get_story("DP1")["driver_claim"] == "12345:2"

    def test_does_not_seize_when_holding_pid_alive(self, isolated_story_home):
        db.create_story("DP2", "t", str(isolated_story_home))
        # Use our own PID — it's definitely alive (we're running).
        import os

        live_token = f"{os.getpid()}:1"
        db.claim_story_driver("DP2", live_token)
        # Another caller must NOT seize from a live PID.
        assert db.claim_story_driver("DP2", "99999:2") is False
        assert db.get_story("DP2")["driver_claim"] == live_token

    def test_pid_alive_helper(self):
        # malformed token → don't seize (safer)
        assert db._driver_pid_alive("garbage") is True
        # missing PID → don't seize
        assert db._driver_pid_alive("0:1") is True
        # definitely-dead high PID → seize
        assert db._driver_pid_alive("99999999:1") is False
        # our own PID → alive
        import os

        assert db._driver_pid_alive(f"{os.getpid()}:1") is True


