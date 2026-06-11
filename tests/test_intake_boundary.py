"""Test intake_state boundary — candidate/ready guards."""

from story_lifecycle.db import models as db
from story_lifecycle.orchestrator.graph import start_story_async, recover_orphan_stories


class TestCandidateRejection:
    def test_sync_creates_candidate_idle(self, tmp_path):
        db.init_db()
        story, created = db.upsert_story_from_source(
            "tapd",
            "99999",
            title="Test",
            workspace=str(tmp_path),
            intake_state="candidate",
            status="idle",
        )
        assert created
        assert story["intake_state"] == "candidate"
        assert story["status"] == "idle"

    def test_start_story_async_rejects_candidate(self, tmp_path, monkeypatch):
        db.init_db()
        key = "tapd-99998"
        db.create_story(key, "Test", str(tmp_path))
        db.update_story(key, intake_state="candidate", status="idle")
        monkeypatch.setattr("story_lifecycle.orchestrator.graph._running_stories", {})
        start_story_async(key)
        # Should not be in running stories after rejection
        from story_lifecycle.orchestrator.graph import is_story_running

        assert not is_story_running(key)

    def test_list_active_stories_excludes_candidates(self, tmp_path):
        db.init_db()
        db.create_story("ready-1", "Ready", str(tmp_path))
        db.update_story("ready-1", intake_state="ready", status="active")
        db.create_story("cand-1", "Candidate", str(tmp_path))
        db.update_story("cand-1", intake_state="candidate", status="idle")
        active = db.list_active_stories()
        keys = [s["story_key"] for s in active]
        assert "ready-1" in keys
        assert "cand-1" not in keys

    def test_recover_orphan_skips_candidates(self, tmp_path, monkeypatch):
        db.init_db()
        db.create_story("ready-orphan", "Ready", str(tmp_path))
        db.update_story("ready-orphan", intake_state="ready", status="active")
        db.create_story("cand-orphan", "Candidate", str(tmp_path))
        db.update_story("cand-orphan", intake_state="candidate", status="idle")
        resumed = []
        monkeypatch.setattr(
            "story_lifecycle.orchestrator.graph.resume_story_async",
            lambda k: resumed.append(k),
        )
        recover_orphan_stories()
        assert "ready-orphan" in resumed
        assert "cand-orphan" not in resumed
