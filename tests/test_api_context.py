"""Tests for new API endpoints — context, projects, worktrees, delivery."""

import pytest
from fastapi.testclient import TestClient

from story_lifecycle.db import models as db
from story_lifecycle.orchestrator.api import app


@pytest.fixture
def client(isolated_story_home):
    """Create a test client with isolated DB."""
    return TestClient(app)


class TestContextAPI:
    def test_get_context(self, client, isolated_story_home):
        """GET /api/story/{key}/context should return context bundle."""
        key = "test-ctx-api"
        db.create_story(key, "Context API Test", str(isolated_story_home))
        db.update_story(key, intake_state="ready")

        resp = client.get(f"/api/story/{key}/context")
        assert resp.status_code == 200
        data = resp.json()
        assert data["story"]["story_key"] == key
        assert "revision" in data
        assert "validation_errors" in data

    def test_put_context_revision_conflict(self, client, isolated_story_home):
        """PUT with wrong revision should return 409."""
        key = "test-rev-conflict"
        db.create_story(key, "Revision Conflict", str(isolated_story_home))
        db.update_story(key, intake_state="ready")

        resp = client.put(
            f"/api/story/{key}/context",
            json={"revision": 999, "projects": []},
        )
        assert resp.status_code == 409
        data = resp.json()
        assert data["ok"] is False
        assert data["reasonCode"] == "context_revision_conflict"

    def test_refresh_context_no_ai_launch(self, client, isolated_story_home):
        """POST /refresh should not launch AI."""
        key = "test-refresh"
        db.create_story(key, "Refresh Test", str(isolated_story_home))
        db.update_story(key, intake_state="ready")

        resp = client.post(f"/api/story/{key}/context/refresh")
        assert resp.status_code == 200
        data = resp.json()
        assert "results" in data

    def test_get_snapshot(self, client, isolated_story_home):
        """GET /snapshot should return snapshot content."""
        key = "test-snapshot-api"
        db.create_story(key, "Snapshot API", str(isolated_story_home))
        db.update_story(key, intake_state="ready", profile="minimal")

        resp = client.get(f"/api/story/{key}/context/snapshot")
        assert resp.status_code == 200
        data = resp.json()
        assert "revision" in data
        assert "content" in data

    def test_project_crud(self, client, isolated_story_home):
        """Project CRUD endpoints should work."""
        # Create
        resp = client.post(
            "/api/projects",
            json={
                "name": "api-test-proj",
                "repo_path": str(isolated_story_home),
                "default_branch": "main",
            },
        )
        assert resp.status_code == 200
        proj = resp.json()
        assert proj["name"] == "api-test-proj"

        # List
        resp = client.get("/api/projects")
        assert resp.status_code == 200
        assert len(resp.json()["projects"]) >= 1

        # Update
        resp = client.put(
            f"/api/projects/{proj['id']}",
            json={"default_branch": "develop"},
        )
        assert resp.status_code == 200

    def test_candidate_start_rejected_no_project(self, client, isolated_story_home):
        """Starting a TAPD candidate without projects should be rejected."""
        key = "tapd-candidate-no-proj"
        db.create_story(key, "No Project", str(isolated_story_home))
        db.update_story(
            key,
            intake_state="candidate",
            source_type="tapd",
            source_id="999",
        )

        resp = client.post(f"/api/story/{key}/start")
        assert resp.status_code == 409
        data = resp.json()
        assert data["reasonCode"] == "project_not_selected"

    def test_prepare_worktrees_endpoint(self, client, isolated_story_home):
        """POST /worktrees/prepare should return results."""
        key = "test-wt-prepare"
        db.create_story(key, "WT Prepare", str(isolated_story_home))
        db.update_story(key, intake_state="ready")

        resp = client.post(
            f"/api/story/{key}/worktrees/prepare",
            json={"worktree_root": str(isolated_story_home / "wts")},
        )
        assert resp.status_code == 200
        assert "results" in resp.json()

    def test_delivery_crud(self, client, isolated_story_home):
        """Delivery artifact CRUD should work."""
        key = "test-delivery-api"
        db.create_story(key, "Delivery API", str(isolated_story_home))
        db.update_story(key, intake_state="ready")

        # Create
        resp = client.post(
            f"/api/story/{key}/delivery-artifacts",
            json={
                "kind": "github_pr",
                "provider": "github",
                "external_id": "42",
                "delivery_state": "review_pending",
                "source": "user",
            },
        )
        assert resp.status_code == 200
        artifact = resp.json()
        assert artifact["kind"] == "github_pr"

        # List
        resp = client.get(f"/api/story/{key}/delivery-artifacts")
        assert resp.status_code == 200
        assert len(resp.json()["artifacts"]) >= 1

        # Update
        resp = client.put(
            f"/api/story/{key}/delivery-artifacts/{artifact['id']}",
            json={"delivery_state": "approved", "source": "user"},
        )
        assert resp.status_code == 200
