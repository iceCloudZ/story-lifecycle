"""routers/workspaces — workspaces domain API（设计15 阶段C 拆自 api.py）。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from pathlib import Path
import json

from ....infra.db import models as db
from .._shared import _resolve_workspace_or_404, _workspace_root_for_project
from pydantic import BaseModel

router = APIRouter(tags=["workspaces"])


class CreateWorkspaceRequest(BaseModel):
    name: str
    slug: str | None = None
    knowledge_root: str | None = None


class WorkspaceInitRequest(BaseModel):
    step: str | None = None
    repos: list[str] = []
    integrations_json: dict | None = None


class TestEnvRequest(BaseModel):
    test_env: dict


def _workspace_options_from_projects(projects: list[dict]) -> list[dict]:
    """Return unique selectable workspaces derived from registered projects."""
    options: dict[str, dict] = {}
    for project in projects:
        repo_path = project.get("repo_path") or ""
        if not repo_path:
            continue
        root = _workspace_root_for_project(repo_path)
        key = str(root)
        option = options.setdefault(
            key,
            {
                "path": key,
                "name": root.name or key,
                "projectCount": 0,
                "projects": [],
            },
        )
        option["projectCount"] += 1
        option["projects"].append(project.get("name", ""))
    return sorted(options.values(), key=lambda item: item["name"])


def _workspace_scenarios(ws: dict) -> list[dict]:
    """旅程 tab 数据源(D6):scenario 条目投影,不存 wiki 页。

    best-effort:知识根不存在/INDEX.json 缺失 → 返回 [] 不阻断。
    """
    try:
        from ...workspace.workspace_registry import _knowledge_root_for

        kroot = _knowledge_root_for(ws)
        if not kroot:
            return []
        index_path = Path(kroot) / "INDEX.json"
        if not index_path.exists():
            return []
        payload = json.loads(index_path.read_text(encoding="utf-8"))
        return [
            {
                "id": e.get("id", ""),
                "title": e.get("title", ""),
                "domain": e.get("domain", ""),
                "status": e.get("status", ""),
                "tags": e.get("tags", []),
                "updated_at": e.get("updated_at", ""),
                "apis": e.get("apis", []),
                "test_ref": e.get("test_ref", ""),
            }
            for e in payload.get("entries", [])
            if e.get("type") == "scenario"
        ]
    except Exception:
        return []


@router.get("/api/workspaces")
def api_list_workspaces():
    """List selectable story workspaces inferred from registered projects."""
    return {"workspaces": _workspace_options_from_projects(db.list_projects())}


@router.get("/api/workspace-entities")
def api_list_workspace_entities():
    """List workspace entities with repo/story counts."""
    from ...workspace.workspace_registry import list_workspaces

    workspaces = list_workspaces()
    return {
        "workspaces": [
            {
                "id": ws["id"],
                "name": ws["name"],
                "slug": ws["slug"],
                "knowledge_root": ws.get("knowledge_root") or "",
                "integrations": json.loads(ws.get("integrations_json") or "{}"),
                "init_state": json.loads(ws.get("init_state") or "{}"),
                "repo_count": len(db.list_projects_by_workspace(ws["id"])),
                "story_count": len(db.list_stories_by_workspace(ws["id"])),
                "updated_at": ws.get("updated_at", ""),
            }
            for ws in workspaces
        ]
    }


@router.post("/api/workspace-entities")
def api_create_workspace_entity(req: CreateWorkspaceRequest):
    from ...workspace.workspace_registry import create_workspace

    try:
        ws = create_workspace(
            req.name,
            slug=req.slug,
            knowledge_root=req.knowledge_root,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return ws


@router.get("/api/workspace-entities/{slug}")
def api_get_workspace_entity(slug: str):
    """WorkspacePage 详情:概览(repos+runtime facts+集成)/Stories/旅程(scenario 投影)/Wiki。"""
    ws = _resolve_workspace_or_404(slug)
    repos = db.list_projects_by_workspace(ws["id"])
    for repo in repos:
        repo["runtime_facts"] = db.get_runtime_facts(repo["id"])
    wiki: list[dict] = []
    try:
        from ...workspace.workspace_registry import _knowledge_root_for
        from ....knowledge.wiki_pipeline import list_wiki_entries

        kroot = _knowledge_root_for(ws)
        if kroot:
            wiki = list_wiki_entries(kroot)
    except Exception:
        pass
    return {
        "workspace": {
            "id": ws["id"],
            "name": ws["name"],
            "slug": ws["slug"],
            "knowledge_root": ws.get("knowledge_root") or "",
            "integrations": json.loads(ws.get("integrations_json") or "{}"),
            "init_state": json.loads(ws.get("init_state") or "{}"),
            "created_at": ws.get("created_at", ""),
            "updated_at": ws.get("updated_at", ""),
        },
        "repos": repos,
        "stories": db.list_stories_by_workspace(ws["id"]),
        "scenarios": _workspace_scenarios(ws),
        "wiki": wiki,
    }


@router.post("/api/workspace-entities/{slug}/init")
def api_workspace_init(slug: str, req: WorkspaceInitRequest):
    """Run the init pipeline(或单步重跑)。同 `story workspace init`。"""
    from ...workspace.workspace_registry import run_init_pipeline

    ws = _resolve_workspace_or_404(slug)
    repos = []
    for spec in req.repos:
        if "=" not in spec:
            raise HTTPException(status_code=400, detail=f"repo 应为 name=path: {spec}")
        name, path = spec.split("=", 1)
        repos.append((name.strip(), path.strip()))
    results = run_init_pipeline(
        ws["id"],
        step=req.step,
        repos=repos,
        integrations_json=req.integrations_json,
    )
    return {"results": results}


@router.get("/api/workspace-entities/{slug}/test-env")
def api_get_test_env(slug: str):
    """读 workspace 的测试环境配置(integrations_json.test_env)。"""
    ws = _resolve_workspace_or_404(slug)
    integrations = json.loads(ws.get("integrations_json") or "{}")
    return {"test_env": integrations.get("test_env", {})}


@router.put("/api/workspace-entities/{slug}/test-env")
def api_put_test_env(slug: str, req: TestEnvRequest):
    """写/确认测试环境配置 → _scan_status=confirmed。"""
    from ...workspace.workspace_registry import confirm_test_env

    ws = _resolve_workspace_or_404(slug)
    result = confirm_test_env(ws["id"], req.test_env)
    return {"test_env": result}


@router.get("/api/workspace-entities/{slug}/test-suites")
def api_get_test_suites(slug: str):
    """读 hc-pytest journey 列表(扫 workspace 下 hc-pytest/journeys/*.yaml)。"""
    from ...workspace.workspace_registry import _infer_workspace_root

    ws = _resolve_workspace_or_404(slug)
    repos = db.list_projects_by_workspace(ws["id"])
    ws_root = _infer_workspace_root(repos)
    if ws_root is None:
        return {"suites": []}

    journeys_dir = ws_root / "hc-pytest" / "journeys"
    suites: list[dict] = []
    if journeys_dir.exists():
        import yaml

        # knowledge scenario → journey 映射(反查 test_ref)
        scenario_map: dict[str, str] = {}
        try:
            from ....knowledge.context_providers.knowledge_provider import (
                _KNOWLEDGE_ROOT,
            )
            from knowledge import KnowledgeIndex

            idx = KnowledgeIndex(str(_KNOWLEDGE_ROOT))
            for e in idx.all():
                if e.type == "scenario" and getattr(e, "test_ref", ""):
                    scenario_map[e.test_ref] = e.id
        except Exception:
            pass

        for yml in sorted(journeys_dir.glob("*.y*ml")):
            suite: dict = {"name": yml.stem, "file": str(yml)}
            try:
                data = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
                suite["title"] = data.get("title", yml.stem)
                suite["description"] = str(data.get("description", ""))[:120]
                # 反查关联 scenario
                scenarios = [
                    sid for ref, sid in scenario_map.items() if ref == yml.stem
                ]
                suite["scenarios"] = scenarios
            except Exception:
                pass
            suites.append(suite)
    return {"suites": suites}
