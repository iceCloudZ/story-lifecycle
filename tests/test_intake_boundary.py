"""Test intake_state boundary — candidate/ready guards."""

from story_lifecycle.db import models as db
from story_lifecycle.orchestrator.graph import start_story_async, recover_orphan_stories


class TestCandidateRejection:
    def test_sync_creates_candidate_idle(self, isolated_story_home):
        story, created = db.upsert_story_from_source(
            "tapd",
            "99999",
            title="Test Candidate",
            workspace=str(isolated_story_home),
        )
        assert created
        assert story["intake_state"] == "candidate"
        assert story["status"] == "idle"

    def test_start_story_async_rejects_candidate(
        self, isolated_story_home, monkeypatch
    ):
        key = "tapd-99998"
        db.create_story(key, "Test Reject", str(isolated_story_home))
        db.update_story(
            key, intake_state="candidate", source_type="tapd", source_id="99998"
        )
        monkeypatch.setattr("story_lifecycle.orchestrator.graph._running_stories", {})
        start_story_async(key)
        from story_lifecycle.orchestrator.graph import is_story_running

        assert not is_story_running(key)

    def test_list_active_stories_excludes_candidates(self, isolated_story_home):
        db.create_story("ready-1", "Ready Story", str(isolated_story_home))
        db.update_story("ready-1", intake_state="ready")
        db.create_story("cand-1", "Candidate Story", str(isolated_story_home))
        db.update_story("cand-1", intake_state="candidate")
        active = db.list_active_stories()
        keys = [s["story_key"] for s in active]
        assert "ready-1" in keys
        assert "cand-1" not in keys

    def test_recover_orphan_skips_candidates(self, isolated_story_home, monkeypatch):
        db.create_story("ready-orphan", "Ready Orphan", str(isolated_story_home))
        db.update_story("ready-orphan", intake_state="ready")
        db.create_story("cand-orphan", "Candidate Orphan", str(isolated_story_home))
        db.update_story("cand-orphan", intake_state="candidate")
        resumed = []
        monkeypatch.setattr(
            "story_lifecycle.orchestrator.graph.resume_story_async",
            lambda k: resumed.append(k),
        )
        recover_orphan_stories()
        assert "ready-orphan" in resumed
        assert "cand-orphan" not in resumed
