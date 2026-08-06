"""routers/projects — projects domain API（设计15 阶段C 拆自 api.py）。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ....infra.db import models as db
from pydantic import BaseModel

router = APIRouter(tags=["projects"])


class CreateProjectRequest(BaseModel):
    name: str
    repo_path: str
    default_branch: str = "main"
    remote_url: str = ""


class UpdateProjectRequest(BaseModel):
    name: str | None = None
    repo_path: str | None = None
    default_branch: str | None = None
    remote_url: str | None = None


@router.get("/api/projects")
def api_list_projects():
    """List all registered projects with fresh availability."""
    from ...workspace.project_registry import check_project_availability

    projects = db.list_projects()
    # 刷新每个项目的 availability（轻量 git rev-parse）
    for p in projects:
        check_project_availability(p["id"])
    return {"projects": db.list_projects()}


@router.post("/api/projects")
def api_create_project(req: CreateProjectRequest):
    """Register a new project."""
    from ...workspace.project_registry import register_project

    proj = register_project(
        name=req.name,
        repo_path=req.repo_path,
        default_branch=req.default_branch,
        remote_url=req.remote_url,
    )
    return proj


@router.put("/api/projects/{project_id}")
def api_update_project(project_id: int, req: UpdateProjectRequest):
    """Update a project."""
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="no fields to update")
    from ...workspace.project_registry import update_project

    update_project(project_id, **updates)
    return db.get_project(project_id)
