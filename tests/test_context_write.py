"""Tests for context write endpoints (agent backfill)."""

from fastapi.testclient import TestClient
from story_lifecycle.orchestrator.api import app
from story_lifecycle.db import models as db


def _seed(key, tmp_path):
    db.create_story(story_key=key, title="t", workspace=str(tmp_path))


def test_add_document(isolated_story_home, tmp_path):
    _seed("W1", tmp_path)
    client = TestClient(app)
    r = client.post(
        "/api/story/W1/context/documents",
        json={"kind": "prd", "ref": "prd/W1.md", "summary": "s"},
    )
    assert r.status_code == 200
    assert r.json()["kind"] == "prd"
    docs = db.get_story_documents("W1")
    assert len(docs) == 1 and docs[0]["ref"] == "prd/W1.md"
    assert db.get_context_revision("W1") >= 1  # revision bumped


def test_add_document_404(isolated_story_home):
    client = TestClient(app)
    r = client.post("/api/story/NOPE/context/documents", json={"kind": "prd"})
    assert r.status_code == 404


def test_add_change_item_nacos(isolated_story_home, tmp_path):
    _seed("W2", tmp_path)
    client = TestClient(app)
    r = client.post(
        "/api/story/W2/context/change-items",
        json={
            "kind": "nacos",
            "ref": "hc-order.yaml",
            "summary": "改超时",
            "evidence_ref": "timeout: 30 -> 60",
        },
    )
    assert r.status_code == 200
    cis = db.get_story_change_items("W2")
    assert len(cis) == 1 and cis[0]["evidence_ref"] == "timeout: 30 -> 60"


def test_set_branch_creates_binding(isolated_story_home, tmp_path):
    _seed("W3", tmp_path)
    db.create_project(name="p3", repo_path=str(tmp_path))
    client = TestClient(app)
    r = client.put(
        "/api/story/W3/context/branch", json={"project_id": 1, "branch": "feature/W3"}
    )
    assert r.status_code == 200
    assert r.json()["branch"] == "feature/W3"


def test_set_branch_updates_existing(isolated_story_home, tmp_path):
    _seed("W4", tmp_path)
    db.create_project(name="p4", repo_path=str(tmp_path))
    db.bind_story_project("W4", 1, branch="old-branch")
    client = TestClient(app)
    r = client.put(
        "/api/story/W4/context/branch", json={"project_id": 1, "branch": "new-branch"}
    )
    assert r.status_code == 200
    assert r.json()["branch"] == "new-branch"
