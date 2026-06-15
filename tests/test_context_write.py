"""Tests for context write endpoints (agent backfill)."""
from fastapi.testclient import TestClient
from story_lifecycle.orchestrator.api import app
from story_lifecycle.db import models as db


def _seed(key, tmp_path):
    db.create_story(story_key=key, title="t", workspace=str(tmp_path))


def test_add_document(isolated_story_home, tmp_path):
    _seed("W1", tmp_path)
    client = TestClient(app)
    r = client.post("/api/story/W1/context/documents", json={"kind": "prd", "ref": "prd/W1.md", "summary": "s"})
    assert r.status_code == 200
    assert r.json()["kind"] == "prd"
    docs = db.get_story_documents("W1")
    assert len(docs) == 1 and docs[0]["ref"] == "prd/W1.md"
    assert db.get_context_revision("W1") >= 1   # revision bumped


def test_add_document_404(isolated_story_home):
    client = TestClient(app)
    r = client.post("/api/story/NOPE/context/documents", json={"kind": "prd"})
    assert r.status_code == 404