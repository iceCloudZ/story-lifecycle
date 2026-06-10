"""API integration tests — test FastAPI endpoints added in Phase 3.

Covers: Timeline, Gate History, Loop Trace, Findings, Dependency Graph,
Patterns, and boundary conditions.
"""

import pytest

from story_lifecycle.db import models as db


@pytest.fixture
def api_client(isolated_story_home):
    """Create a FastAPI TestClient with isolated DB."""
    from story_lifecycle.orchestrator.api import app
    from fastapi.testclient import TestClient

    return TestClient(app)


@pytest.fixture
def seeded_story(isolated_story_home):
    """Create a story with events for API testing."""
    db.upsert_story(
        "API-TEST-001",
        title="API Test Story",
        workspace="/tmp/test-ws",
        profile="minimal",
        current_stage="implement",
        status="active",
    )
    # Add some events
    db.log_event("API-TEST-001", "design", "execute", {"adapter": "claude"})
    db.log_event("API-TEST-001", "design", "complete", {"summary": "Design done"})
    db.log_event("API-TEST-001", "implement", "execute", {"adapter": "claude"})
    return "API-TEST-001"


class TestTimelineAPI:
    def test_timeline_returns_stages(self, api_client, seeded_story):
        resp = api_client.get(f"/api/story/{seeded_story}/timeline")
        assert resp.status_code == 200
        data = resp.json()
        assert data["story_key"] == seeded_story
        assert isinstance(data["stages"], list)

    def test_timeline_nonexistent_story(self, api_client, isolated_story_home):
        resp = api_client.get("/api/story/NONEXIST/timeline")
        assert resp.status_code == 404


class TestGateHistoryAPI:
    def test_gate_history_returns_decisions(self, api_client, seeded_story):
        resp = api_client.get(f"/api/story/{seeded_story}/gate-history")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data.get("decisions", []), list)

    def test_gate_history_empty(self, api_client, isolated_story_home):
        db.upsert_story("EMPTY-001", title="Empty", workspace="/tmp", profile="minimal")
        resp = api_client.get("/api/story/EMPTY-001/gate-history")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("decisions", []) == []

    def test_gate_history_nonexistent(self, api_client, isolated_story_home):
        resp = api_client.get("/api/story/NONEXIST/gate-history")
        assert resp.status_code == 404


class TestLoopTraceAPI:
    def test_loop_trace_returns_rounds(self, api_client, seeded_story):
        resp = api_client.get(f"/api/story/{seeded_story}/loop-trace")
        assert resp.status_code == 200
        data = resp.json()
        assert "plan_loop" in data or "code_loop" in data

    def test_loop_trace_nonexistent(self, api_client, isolated_story_home):
        resp = api_client.get("/api/story/NONEXIST/loop-trace")
        assert resp.status_code == 404


class TestFindingsAPI:
    def test_findings_returns_dict(self, api_client, seeded_story, isolated_story_home):
        db.create_finding(
            story_key=seeded_story,
            stage="implement",
            source="code_review",
            severity="high",
            category="security",
            description="SQL injection risk",
        )
        resp = api_client.get(f"/api/story/{seeded_story}/findings")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)
        assert "findings" in data

    def test_findings_with_filter(self, api_client, seeded_story, isolated_story_home):
        db.create_finding(
            story_key=seeded_story,
            stage="implement",
            source="code_review",
            severity="low",
            category="style",
            description="Code style issue",
        )
        resp = api_client.get(f"/api/story/{seeded_story}/findings?min_severity=high")
        assert resp.status_code == 200

    def test_findings_empty(self, api_client, isolated_story_home):
        db.upsert_story(
            "NO-FIND-001", title="No Findings", workspace="/tmp", profile="minimal"
        )
        resp = api_client.get("/api/story/NO-FIND-001/findings")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)
        assert data.get("findings", []) == []


class TestDependencyGraphAPI:
    def test_dependency_graph_returns_structure(self, api_client, seeded_story):
        resp = api_client.get(f"/api/story/{seeded_story}/dependency-graph")
        assert resp.status_code == 200
        data = resp.json()
        assert "nodes" in data
        assert "edges" in data

    def test_dependency_graph_nonexistent(self, api_client, isolated_story_home):
        resp = api_client.get("/api/story/NONEXIST/dependency-graph")
        assert resp.status_code == 404


class TestPatternsAPI:
    def test_patterns_list(self, api_client, isolated_story_home):
        resp = api_client.get("/api/patterns")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)
        assert "patterns" in data

    def test_patterns_approve(self, api_client, isolated_story_home):
        pid = db.create_learned_pattern(
            pattern="Always validate inputs",
            applies_to=["api"],
            rule="Add input validation to all API endpoints",
            confidence="medium",
        )
        resp = api_client.post(f"/api/patterns/{pid}/approve")
        assert resp.status_code == 200

    def test_patterns_reject(self, api_client, isolated_story_home):
        pid = db.create_learned_pattern(
            pattern="Bad pattern",
            applies_to=["none"],
            rule="Do nothing",
            confidence="low",
        )
        resp = api_client.post(f"/api/patterns/{pid}/reject")
        assert resp.status_code == 200


class TestStoriesAPI:
    def test_list_stories(self, api_client, seeded_story):
        resp = api_client.get("/api/story")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert any(s.get("storyKey") == seeded_story for s in data)

    def test_get_single_story(self, api_client, seeded_story):
        resp = api_client.get(f"/api/story/{seeded_story}")
        assert resp.status_code == 200
        data = resp.json()
        # API returns snake_case or camelCase depending on serialization
        key = data.get("story_key") or data.get("storyKey")
        assert key == seeded_story

    def test_get_nonexistent_story(self, api_client, isolated_story_home):
        resp = api_client.get("/api/story/NONEXIST")
        assert resp.status_code == 404


class TestDiagnosticsAPI:
    def test_debug_endpoint_returns_debug_info(self, api_client, seeded_story):
        resp = api_client.get(f"/api/story/{seeded_story}/debug")
        assert resp.status_code == 200
        data = resp.json()
        assert "story" in data or "recentEvents" in data


class TestSyncAPI:
    def test_sync_status_unconfigured(self, api_client, isolated_story_home):
        resp = api_client.get("/api/sync/tapd/status")
        assert resp.status_code == 200
        assert resp.json()["configured"] is False

    def test_sync_tapd_unconfigured_returns_400(self, api_client, isolated_story_home):
        resp = api_client.post("/api/sync/tapd", json={})
        assert resp.status_code == 400


class TestStoryListWithFilters:
    def test_list_with_overdue_filter(self, api_client, isolated_story_home):
        db.upsert_story_from_source(
            source_type="tapd",
            source_id="1001",
            title="逾期需求",
            deadline="2020-01-01",
        )
        db.upsert_story_from_source(
            source_type="tapd",
            source_id="1002",
            title="未来需求",
            deadline="2099-12-31",
        )

        resp = api_client.get("/api/story?overdue=true")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["title"] == "逾期需求"

    def test_list_returns_new_fields(self, api_client, isolated_story_home):
        db.upsert_story_from_source(
            source_type="tapd",
            source_id="1001",
            title="带字段",
            deadline="2026-06-15",
            priority="高",
            tapd_status="open",
        )

        resp = api_client.get("/api/story")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        item = data[0]
        assert item["deadline"] == "2026-06-15"
        assert item["priority"] == "高"
        assert item["tapdStatus"] == "open"

    def test_story_detail_returns_new_fields(self, api_client, isolated_story_home):
        db.upsert_story_from_source(
            source_type="tapd",
            source_id="1001",
            title="详情测试",
            tapd_status="progressing",
        )

        resp = api_client.get("/api/story/tapd-1001")
        assert resp.status_code == 200
        data = resp.json()
        assert data["tapdStatus"] == "progressing"
        assert data["sourceType"] == "tapd"
