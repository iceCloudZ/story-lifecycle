"""Tests for sync-related-bugs (TapdApi + endpoint)."""

from fastapi.testclient import TestClient

import story_lifecycle.orchestrator.service.api as api_mod
import story_lifecycle.sources.tapd_api as tapi_mod
from story_lifecycle.infra.db import models as db
from story_lifecycle.orchestrator.service.api import app
from story_lifecycle.sources.tapd_api import TapdApi


def test_get_related_bugs_calls_cli_with_story_id(monkeypatch):
    api = TapdApi(workspace_id="44381896")
    calls = []

    def fake_call(cmd, params):
        calls.append((cmd, params))
        return {"data": [{"bug_id": "B1", "story_id": "S1"}]}

    monkeypatch.setattr(api, "_call", fake_call)
    result = api.get_related_bugs("S1")
    assert result == [{"bug_id": "B1", "story_id": "S1"}]
    assert calls == [("get_related_bugs", {"story_id": "S1"})]


def test_upsert_bug_sets_parent_key(isolated_story_home, tmp_path):
    db.create_story(story_key="REQ-1", title="需求", workspace=str(tmp_path))
    story, _ = db.upsert_story_from_source(
        source_type="tapd",
        source_id="bug_1009779",
        title="客户UID千分位",
        tapd_type="bug",
        parent_key="REQ-1",
    )
    assert story["parent_key"] == "REQ-1"
    # update path also sets parent_key
    db.upsert_story_from_source(
        source_type="tapd", source_id="bug_1009779", parent_key="REQ-1"
    )
    assert db.get_story(story["story_key"])["parent_key"] == "REQ-1"


class _FakeTapdApi:
    def __init__(self, *a, **kw):
        pass

    def get_related_bugs(self, sid):
        return [{"bug_id": "1144381896001009779", "story_id": sid}]

    def get_bug_detail(self, bid):
        return {
            "Bug": {
                "title": "客户UID千分位",
                "status": "new",
                "current_owner": "赵子豪;",
            }
        }


def test_sync_related_bugs_upserts_with_parent(
    monkeypatch, isolated_story_home, tmp_path
):
    db.upsert_story_from_source(
        source_type="tapd",
        source_id="1144381896001065460",
        title="删除联系人",
        tapd_type="story",
        workspace=str(tmp_path),
    )
    key = db.find_by_source_id("tapd", "1144381896001065460")["story_key"]

    monkeypatch.setattr(tapi_mod, "TapdApi", _FakeTapdApi)
    monkeypatch.setattr(
        api_mod, "_load_tapd_config", lambda: {"workspace_id": "44381896"}
    )

    client = TestClient(app)
    r = client.post(f"/api/story/{key}/sync-related-bugs")
    assert r.status_code == 200
    assert r.json()["synced"] == 1
    bug = db.find_by_source_id("tapd", "bug_1144381896001009779")
    assert bug["parent_key"] == key
    assert bug["tapd_type"] == "bug"


def test_sync_related_bugs_404_unknown(isolated_story_home):
    client = TestClient(app)
    assert client.post("/api/story/NOPE/sync-related-bugs").status_code == 404
