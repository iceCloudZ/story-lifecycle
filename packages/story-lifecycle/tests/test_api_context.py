"""Tests for new API endpoints — context, projects, worktrees, delivery."""

import pytest
from fastapi.testclient import TestClient

from story_lifecycle.db import models as db
from story_lifecycle.orchestrator.service.api import app


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

    def test_workspaces_are_inferred_from_registered_projects(
        self, client, isolated_story_home
    ):
        """Workspace picker should show monorepo roots, not every module as a workspace."""
        monorepo = isolated_story_home / "hc-all"
        (monorepo / ".story").mkdir(parents=True)
        service = monorepo / "hc-order"
        frontend = monorepo / "frontends" / "hc-admin"
        service.mkdir(parents=True)
        frontend.mkdir(parents=True)
        db.create_project("hc-order", str(service), "master")
        db.create_project("hc-admin", str(frontend), "master")

        resp = client.get("/api/workspaces")

        assert resp.status_code == 200
        workspaces = resp.json()["workspaces"]
        assert workspaces == [
            {
                "path": str(monorepo.resolve()),
                "name": "hc-all",
                "projectCount": 2,
                "projects": ["hc-admin", "hc-order"],
            }
        ]

    def test_create_story_requires_workspace(self, client, isolated_story_home):
        """The UI/API should not silently create stories in the server cwd."""
        resp = client.post(
            "/api/story",
            json={"key": "tapd-1", "title": "No Workspace", "autostart": False},
        )

        assert resp.status_code == 400
        assert resp.json()["detail"] == "workspace required"

    def test_candidate_start_allows_no_project_for_intake_only(
        self, client, isolated_story_home
    ):
        """Intake/PRD preparation should not require selecting a project/module."""
        key = "tapd-candidate-no-proj"
        db.create_story(key, "No Project", str(isolated_story_home))
        db.update_story(
            key,
            intake_state="candidate",
            source_type="tapd",
            source_id="999",
        )

        resp = client.post(f"/api/story/{key}/start", json={"content": "# PRD"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        story = db.get_story(key)
        assert story["status"] == "planning"

    def test_intake_preview_prefills_story_from_source(
        self, client, isolated_story_home, monkeypatch
    ):
        """Entering a story id should fetch source detail and run PRD generator."""
        from story_lifecycle.sources.base import SourceItem
        from story_lifecycle.sources import tapd_source as tapd_source_mod
        from story_lifecycle.orchestrator.service import prd_generator

        class FakeTapdSource:
            def __init__(self, config):
                pass

            def get_detail(self, item_id):
                return SourceItem(
                    id="1144381896001065618",
                    source="tapd",
                    item_type="requirement",
                    title="授信提现展示拒绝原因",
                    description="提现被拒绝时展示拒绝原因",
                    extra={
                        "url": "https://www.tapd.cn/114438189600/prong/stories/view/1144381896001065618"
                    },
                )

        monkeypatch.setattr(tapd_source_mod, "TapdSource", FakeTapdSource)
        monkeypatch.setattr(
            prd_generator,
            "generate_prd_from_source",
            lambda source: prd_generator.PrdGenerationResult(
                action="generated",
                markdown="# 授信提现展示拒绝原因\n\n## 安全审查\n\n无前端可控核心参数。",
                summary="已生成 PRD",
            ),
        )

        resp = client.post(
            "/api/intake/preview",
            data={"source_type": "tapd", "source_id": "1065618"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["storyKey"] == "tapd-1144381896001065618"
        assert data["title"] == "授信提现展示拒绝原因"
        assert data["action"] == "generated"
        assert "安全审查" in data["markdown"]

    def test_start_tapd_candidate_returns_dingtalk_link_when_prd_source_is_external(
        self, client, isolated_story_home, monkeypatch
    ):
        """A TAPD intake should stop when the PRD generator asks for DingTalk download."""
        from story_lifecycle.sources.base import SourceItem
        from story_lifecycle.sources import tapd_source as tapd_source_mod
        from story_lifecycle.orchestrator.service import prd_generator

        class FakeTapdSource:
            def __init__(self, config):
                pass

            def get_detail(self, item_id):
                return SourceItem(
                    id=item_id,
                    source="tapd",
                    item_type="requirement",
                    title="钉钉文档需求",
                    description='请看 <a href="https://alidocs.dingtalk.com/i/nodes/abc">钉钉文档</a>',
                )

        monkeypatch.setattr(tapd_source_mod, "TapdSource", FakeTapdSource)
        monkeypatch.setattr(
            prd_generator,
            "generate_prd_from_source",
            lambda source: prd_generator.PrdGenerationResult(
                action="manual_download_required",
                dingtalk_links=["https://alidocs.dingtalk.com/i/nodes/abc"],
                markdown="",
                summary="需要人工下载钉钉文档",
            ),
        )
        proj = db.create_project("ding-proj", str(isolated_story_home), "main")
        db.create_story("tapd-123", "钉钉文档需求", str(isolated_story_home))
        db.update_story(
            "tapd-123",
            intake_state="candidate",
            source_type="tapd",
            source_id="123",
        )

        resp = client.post(
            "/api/story/tapd-123/start",
            json={"project_ids": [proj["id"]]},
        )

        assert resp.status_code == 409
        data = resp.json()
        assert data["reasonCode"] == "dingtalk_download_required"
        assert data["dingtalk_links"] == ["https://alidocs.dingtalk.com/i/nodes/abc"]

    def test_start_tapd_candidate_generates_prd_from_tapd_body_when_no_dingtalk(
        self, client, isolated_story_home, monkeypatch, tmp_path
    ):
        """A TAPD intake should save PRD.md when the generator returns markdown."""
        from story_lifecycle.sources.base import SourceItem
        from story_lifecycle.sources import tapd_source as tapd_source_mod
        from story_lifecycle.orchestrator.service import prd_generator

        class FakeTapdSource:
            def __init__(self, config):
                pass

            def get_detail(self, item_id):
                return SourceItem(
                    id=item_id,
                    source="tapd",
                    item_type="requirement",
                    title="授信提现展示拒绝原因",
                    description="<p>用户提现被拒绝时，需要展示拒绝原因。</p>",
                    priority="高",
                    owner="赵子豪",
                    status="status_3",
                    extra={"url": "https://www.tapd.cn/ws/prong/stories/view/123"},
                )

        monkeypatch.setattr(tapd_source_mod, "TapdSource", FakeTapdSource)
        monkeypatch.setattr(
            prd_generator,
            "generate_prd_from_source",
            lambda source: prd_generator.PrdGenerationResult(
                action="generated",
                dingtalk_links=[],
                markdown=(
                    "# 授信提现展示拒绝原因\n\n"
                    "## 需求描述\n\n用户提现被拒绝时，需要展示拒绝原因。\n\n"
                    "## 安全审查\n\n无前端可控核心参数。\n"
                ),
                summary="已生成 PRD",
            ),
        )
        repo = tmp_path / "repo"
        repo.mkdir()
        proj = db.create_project("auto-prd-proj", str(repo), "main")
        db.create_story("tapd-1234", "授信提现展示拒绝原因", str(isolated_story_home))
        db.update_story(
            "tapd-1234",
            intake_state="candidate",
            source_type="tapd",
            source_id="1234",
        )

        resp = client.post(
            "/api/story/tapd-1234/start",
            json={"project_ids": [proj["id"]]},
        )

        assert resp.status_code == 200
        story = db.get_story("tapd-1234")
        assert story["intake_state"] == "ready"
        assert story["status"] == "planning"
        import json as _json

        ctx = _json.loads(story["context_json"] or "{}")
        prd_path = ctx["prd_path"]
        from pathlib import Path

        prd = Path(prd_path)
        assert prd.name == "PRD.md"
        content = prd.read_text(encoding="utf-8")
        assert "授信提现展示拒绝原因" in content
        assert "用户提现被拒绝时，需要展示拒绝原因" in content
        assert "安全审查" in content

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

    def test_gate_result_backfill_visible_in_gate_history(
        self, client, isolated_story_home
    ):
        """Manual evidence backfill should record gate results with evidence."""
        key = "test-gate-backfill"
        db.create_story(key, "Gate Backfill", str(isolated_story_home))
        db.update_story(key, intake_state="ready")

        resp = client.post(
            f"/api/story/{key}/gate-results",
            json={
                "stage": "build-check",
                "gate_name": "backend_compile_ci",
                "result": "PASS",
                "summary": "hc-config CI compile success",
                "evidence_ref": "Skyladder build #33085",
                "evidence": {"build_no": 33085, "commit": "abc123"},
            },
        )
        assert resp.status_code == 200
        created = resp.json()
        assert created["ok"] is True

        resp = client.get(f"/api/story/{key}/gate-history")
        assert resp.status_code == 200
        decisions = resp.json()["decisions"]
        assert len(decisions) == 1
        decision = decisions[0]
        assert decision["stage"] == "build-check"
        assert decision["decision"] == "PASS"
        assert decision["reason_code"] == "backend_compile_ci"
        assert decision["human_message"] == "hc-config CI compile success"
        assert decision["evidence"]["evidence_ref"] == "Skyladder build #33085"
        assert decision["evidence"]["build_no"] == 33085
