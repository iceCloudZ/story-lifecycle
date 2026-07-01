"""Tests for bug resolve endpoint."""

from fastapi.testclient import TestClient

import story_lifecycle.orchestrator.service.api as api_mod
import story_lifecycle.sources.tapd_api as tapi_mod
from story_lifecycle.db import models as db
from story_lifecycle.orchestrator.service.api import app


class _FakeTapdApi:
    def __init__(self, *a, **kw):
        pass

    def update_bug(self, bid, fields):
        self.updated_bug = bid
        self.updated_fields = fields
        return True


def test_resolve_bug_updates_status_and_tapd(
    monkeypatch, isolated_story_home, tmp_path
):
    db.upsert_story_from_source(
        source_type="tapd",
        source_id="bug_1009779",
        title="b",
        tapd_type="bug",
        workspace=str(tmp_path),
    )
    key = db.find_by_source_id("tapd", "bug_1009779")["story_key"]

    fake = _FakeTapdApi()
    monkeypatch.setattr(tapi_mod, "TapdApi", lambda **kw: fake)
    monkeypatch.setattr(
        api_mod, "_load_tapd_config", lambda: {"workspace_id": "44381896"}
    )

    client = TestClient(app)
    r = client.post(f"/api/story/{key}/resolve")
    assert r.status_code == 200
    assert r.json()["has_bugfix_report"] is False
    assert fake.updated_bug == "1009779"
    assert fake.updated_fields == {"status": "resolved"}
    s = db.get_story(key)
    assert s["status"] == "completed" and s["tapd_status"] == "resolved"


def test_resolve_404_nonexistent(isolated_story_home):
    assert TestClient(app).post("/api/story/NOPE/resolve").status_code == 404


def test_resolve_400_not_bug(isolated_story_home, tmp_path):
    db.create_story(story_key="REQ-r", title="r", workspace=str(tmp_path))
    assert TestClient(app).post("/api/story/REQ-r/resolve").status_code == 400
