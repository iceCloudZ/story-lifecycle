"""Workspace entity tests — 11-workspace-entity-design.md Phase 2.

Covers: workspace CRUD, project.workspace_id attach(零配置兼容), init pipeline
5 步(幂等/单步重跑/init_state 推进), API endpoints。
"""

import json
from pathlib import Path

import pytest

from story_lifecycle.infra.db import models as db
from story_lifecycle.orchestrator.workspace import workspace_registry as wr
from story_lifecycle.orchestrator.workspace.project_registry import register_project


@pytest.fixture
def repo_dirs(tmp_path):
    """两个带 git 标记的伪仓库目录(空 .git 目录即可让探测走缺失降级)。"""
    dirs = []
    for name in ("repo-a", "repo-b"):
        repo = tmp_path / name
        repo.mkdir()
        (repo / ".git").mkdir(parents=True)
        (repo / "pom.xml").write_text("<project/>", encoding="utf-8")
        (repo / "AGENTS.md").write_text("x", encoding="utf-8")
        dirs.append(repo)
    return dirs


class TestWorkspaceCRUD:
    def test_create_get_list_update_delete(self):
        ws = wr.create_workspace("HappyCash 授信域", slug="hc-credit-domain")
        assert ws["name"] == "HappyCash 授信域"
        assert ws["slug"] == "hc-credit-domain"
        assert ws["knowledge_root"] is None
        assert json.loads(ws["integrations_json"]) == {}
        assert json.loads(ws["init_state"]) == {}

        assert db.get_workspace(ws["id"])["name"] == "HappyCash 授信域"
        assert db.get_workspace_by_slug("hc-credit-domain")["id"] == ws["id"]
        assert db.get_workspace_by_name("HappyCash 授信域")["id"] == ws["id"]
        assert [w["slug"] for w in db.list_workspaces()] == ["hc-credit-domain"]

        db.update_workspace(ws["id"], knowledge_root="D:/k/.story/knowledge")
        assert db.get_workspace(ws["id"])["knowledge_root"] == "D:/k/.story/knowledge"

        wr.delete_workspace(ws["id"])
        assert db.get_workspace(ws["id"]) is None

    def test_slug_default_from_name(self):
        ws = wr.create_workspace("MGM 活动域")
        assert db.get_workspace_by_slug(ws["slug"])["id"] == ws["id"]
        assert ws["slug"] == wr.slugify("MGM 活动域")

    def test_invalid_slug_rejected(self):
        with pytest.raises(ValueError):
            wr.create_workspace("Bad", slug="Bad Slug!")

    def test_duplicate_slug_rejected(self):
        wr.create_workspace("唯一名", slug="uniq-a")
        with pytest.raises(Exception):
            wr.create_workspace("另一个名", slug="uniq-a")

    def test_update_workspace_rejects_unknown_columns(self):
        ws = wr.create_workspace("X", slug="x")
        with pytest.raises(ValueError):
            db.update_workspace(ws["id"], bogus=1)

    def test_get_workspace_resolves_int_and_slug_and_name(self):
        ws = wr.create_workspace("解析", slug="resolve-me")
        assert wr.get_workspace(ws["id"])["id"] == ws["id"]
        assert wr.get_workspace("resolve-me")["id"] == ws["id"]
        assert wr.get_workspace("解析")["id"] == ws["id"]
        assert wr.get_workspace("nope") is None


class TestWorkspaceAttach:
    def test_project_workspace_attach_and_orphan_zero_config(self, repo_dirs):
        """零配置路径:不建 Workspace 的散仓库 workspace_id 保持 NULL,行为不变。"""
        orphan = register_project(name="orphan", repo_path=str(repo_dirs[0]))
        assert orphan["workspace_id"] is None

        ws = wr.create_workspace("域", slug="domain-x")
        attached = register_project(name="attached", repo_path=str(repo_dirs[1]))
        db.update_project(attached["id"], workspace_id=ws["id"])
        assert db.get_project(attached["id"])["workspace_id"] == ws["id"]

        by_ws = db.list_projects_by_workspace(ws["id"])
        assert [p["name"] for p in by_ws] == ["attached"]

    def test_workspace_delete_detaches_projects(self, repo_dirs):
        ws = wr.create_workspace("域", slug="domain-y")
        proj = register_project(name="p1", repo_path=str(repo_dirs[0]))
        db.update_project(proj["id"], workspace_id=ws["id"])
        wr.delete_workspace(ws["id"])
        assert db.get_project(proj["id"]) is not None
        assert db.get_project(proj["id"])["workspace_id"] is None


@pytest.fixture
def seeded_workspace(repo_dirs):
    ws = wr.create_workspace("测试域", slug="test-domain")
    for i, repo in enumerate(repo_dirs):
        proj = register_project(name=f"svc-{i}", repo_path=str(repo))
        db.update_project(proj["id"], workspace_id=ws["id"])
    return ws


class TestInitPipeline:
    def test_pipeline_runs_all_steps_and_state_advances(self, seeded_workspace):
        results = wr.run_init_pipeline(seeded_workspace["id"])
        by_step = {r["step"]: r for r in results}
        assert set(by_step) == set(db.WORKSPACE_INIT_STEPS)
        assert all(r["status"] == "done" for r in results), results

        ws = db.get_workspace(seeded_workspace["id"])
        state = json.loads(ws["init_state"])
        assert all(state[s] == "done" for s in db.WORKSPACE_INIT_STEPS)
        # 知识根被推断并持久化
        assert ws["knowledge_root"]
        assert Path(ws["knowledge_root"]).name == "knowledge"
        assert Path(ws["knowledge_root"]).parent.name == ".story"
        # wiki 骨架就位(Phase 2 占位)
        assert (Path(ws["knowledge_root"]) / "wiki" / "README.md").exists()

    def test_pipeline_is_idempotent_on_rerun(self, seeded_workspace):
        wr.run_init_pipeline(seeded_workspace["id"])
        results = wr.run_init_pipeline(seeded_workspace["id"])
        assert all(r["status"] == "done" for r in results)

    def test_single_step_rerun_only_touches_that_step(self, seeded_workspace):
        results = wr.run_init_pipeline(seeded_workspace["id"], step="detect_runtime")
        assert [r["step"] for r in results] == ["detect_runtime"]
        assert results[0]["status"] == "done"

    def test_unknown_step_rejected(self, seeded_workspace):
        with pytest.raises(ValueError):
            wr.run_init_pipeline(seeded_workspace["id"], step="nope")

    def test_register_repos_requires_at_least_one_repo(self):
        ws = wr.create_workspace("空域", slug="empty-domain")
        results = wr.run_init_pipeline(ws["id"], step="register_repos", repos=[])
        assert results[0]["status"] == "failed"
        assert "至少一个" in results[0]["reason"]

    def test_failed_step_recorded_with_reason(self):
        ws = wr.create_workspace("空域2", slug="empty-domain-2")
        wr.run_init_pipeline(ws["id"])
        state = json.loads(db.get_workspace(ws["id"])["init_state"])
        assert state["register_repos"]["status"] == "failed"
        assert "reason" in state["register_repos"]

    def test_integrations_step_stores_json(self, seeded_workspace):
        results = wr.run_init_pipeline(
            seeded_workspace["id"],
            step="register_integrations",
            integrations_json={"gitlab": {"url": "http://git.example"}},
        )
        assert results[0]["status"] == "done"
        ws = db.get_workspace(seeded_workspace["id"])
        integrations = json.loads(ws["integrations_json"])
        assert integrations["gitlab"]["url"] == "http://git.example"

    def test_detect_runtime_records_facts(self, seeded_workspace):
        wr.run_init_pipeline(seeded_workspace["id"], step="detect_runtime")
        for project in db.list_projects_by_workspace(seeded_workspace["id"]):
            facts = db.get_runtime_facts(project["id"])
            assert any(f["runtime_type"] == "maven" for f in facts)


@pytest.fixture
def api_client(isolated_story_home):
    from story_lifecycle.orchestrator.service.api import app
    from fastapi.testclient import TestClient

    return TestClient(app)


class TestWorkspaceAPI:
    def test_list_create_get(self, api_client):
        resp = api_client.post(
            "/api/workspace-entities",
            json={"name": "API 域", "slug": "api-domain"},
        )
        assert resp.status_code == 200
        ws = resp.json()
        assert ws["slug"] == "api-domain"

        resp = api_client.get("/api/workspace-entities")
        assert resp.status_code == 200
        slugs = [w["slug"] for w in resp.json()["workspaces"]]
        assert "api-domain" in slugs

        resp = api_client.get("/api/workspace-entities/api-domain")
        assert resp.status_code == 200
        data = resp.json()
        assert data["workspace"]["name"] == "API 域"
        assert data["repos"] == []
        assert data["stories"] == []
        assert data["scenarios"] == []

    def test_get_missing_returns_404(self, api_client):
        resp = api_client.get("/api/workspace-entities/nope")
        assert resp.status_code == 404

    def test_create_duplicate_slug_returns_400(self, api_client):
        api_client.post("/api/workspace-entities", json={"name": "重复", "slug": "dup"})
        resp = api_client.post(
            "/api/workspace-entities", json={"name": "重复2", "slug": "dup"}
        )
        assert resp.status_code == 400

    def test_detail_includes_bound_stories(self, api_client, repo_dirs):
        ws = wr.create_workspace("有 story 的域", slug="with-stories")
        proj = register_project(name="svc", repo_path=str(repo_dirs[0]))
        db.update_project(proj["id"], workspace_id=ws["id"])
        db.create_story(
            "WS-STORY-1", "Bound story", str(repo_dirs[0]), current_stage="design"
        )
        db.bind_story_project("WS-STORY-1", proj["id"])

        resp = api_client.get("/api/workspace-entities/with-stories")
        assert resp.status_code == 200
        stories = resp.json()["stories"]
        assert [s["story_key"] for s in stories] == ["WS-STORY-1"]
        assert [r["name"] for r in resp.json()["repos"]] == ["svc"]

    def test_init_endpoint_runs_pipeline(self, api_client, repo_dirs):
        api_client.post(
            "/api/workspace-entities",
            json={"name": "init 域", "slug": "init-domain"},
        )
        resp = api_client.post(
            "/api/workspace-entities/init-domain/init",
            json={
                "repos": [
                    f"svc={repo_dirs[0]}",
                ]
            },
        )
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert all(r["status"] == "done" for r in results), results
