"""routers/stories — stories domain API（设计15 阶段C 拆自 api.py）。"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ....infra.db import models as db
from ....sourcing.workspace_diff import get_story_workspace_diff
from ...engine.graph import start_story_async
from .sessions import _story_headless
from .._shared import (
    _serialize_story_summary,
)

log = logging.getLogger("story-lifecycle.api.stories")

router = APIRouter(tags=["stories"])


class CreateStoryRequest(BaseModel):
    key: str
    title: str = ""
    content: str = ""
    profile: str = "minimal"
    workspace: str = ""
    autostart: bool = True


@router.get("/api/story")
def list_stories(
    status: str = "",
    overdue: bool = False,
    show_all: bool = False,
    tapd_type: str = "",
    show_completed: bool = False,
    show_test: bool = False,
):
    """List stories with optional filters.

    Query params:
        status: Filter by status (active, paused, completed, failed)
        overdue: Only show stories past their deadline
        show_all: Include completed/failed stories
        tapd_type: Filter by type (story/bug/subtask)
        show_completed: Show completed TAPD stories (default hides resolved/rejected/closed)
        show_test: Show is_test=1 stories (default hides test/demo data)
    """
    stories = db.list_visible_stories(
        show_all=show_all,
        status=status,
        item_type=tapd_type,
        show_completed=show_completed,
        overdue=overdue,
        show_test=show_test,
    )

    return JSONResponse([_serialize_story_summary(s) for s in stories])


@router.get("/api/bugs")
def list_bugs(status: str = "", show_all: bool = False):
    """List bug stories. Defaults to open bugs; pass show_all to include resolved/closed."""
    stories = db.list_visible_stories(
        show_all=show_all,
        status=status,
        item_type="bug",
        show_completed=show_all,
    )
    # TAPD closed/resolved/rejected bugs are considered done unless show_all.
    if not show_all:
        done_tapd = {"closed", "resolved", "rejected"}
        stories = [
            s for s in stories if (s.get("tapd_status") or "").lower() not in done_tapd
        ]
    return JSONResponse([_serialize_story_summary(s) for s in stories])


@router.get("/api/story/{story_key}")
def get_story(story_key: str):
    # 设计13：GET /story 去掉副作用（consume_orphan_artifacts 已被全局编排线程
    # 的 poll-artifacts 替代 —— 编排线程每轮检查所有 active story 的成果物，
    # 打开详情页不再需要触发认领）。
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")

    # 迭代 2（P4-UI）：显式暴露 plan 确认态（数据源唯一：context_json._plan_confirmed），
    # 前端不再从 status 字符串推断（status 无 'planning' 值——'planning' 从不在
    # status 语义里，前端三处 status==='planning' 条件恒 false 是「确认规划」按钮
    # 永不显示的根因）。
    try:
        import json as _json

        _ctx = _json.loads(s.get("context_json") or "{}")
    except (ValueError, TypeError):
        _ctx = {}
    plan_confirmed = bool(_ctx.get("_plan_confirmed"))
    has_plan = bool(_ctx.get("_agent_actions"))

    subs = db.get_sub_stories(story_key)
    sub_list = (
        [
            {
                "storyKey": sub["story_key"],
                "subType": sub.get("sub_type"),
                "status": sub["status"],
                "currentStage": sub["current_stage"],
            }
            for sub in subs
        ]
        if subs
        else []
    )

    return JSONResponse(
        {
            "storyKey": s["story_key"],
            "title": s["title"],
            "currentStage": s["current_stage"],
            "status": s["status"],
            "complexity": s["complexity"],
            "workspace": s["workspace"],
            "profile": s["profile"],
            "contextJson": s["context_json"],
            "executionCount": s["execution_count"],
            "lastError": s["last_error"],
            "updatedAt": s["updated_at"],
            "parentKey": s.get("parent_key"),
            "subType": s.get("sub_type"),
            "deadline": s.get("deadline"),
            "priority": s.get("priority"),
            "owner": s.get("owner"),
            "branchesJson": s.get("branches_json", "[]"),
            "tapdStatus": s.get("tapd_status"),
            "tapdUrl": s.get("tapd_url"),
            "sourceType": s.get("source_type"),
            "sourceId": s.get("source_id"),
            "subs": sub_list,
            "lifecycleState": s.get("lifecycle_state"),
            "releaseTrain": s.get("release_train"),
            "isTest": bool(s.get("is_test")),
            # BUG #9:暴露 headless 让前端 ClarifyDialog 据此决定显隐
            # (headless 路径走 MCP clarify→卡片;交互式路径走"终端问人"→不显示卡片)。
            "headless": _story_headless(s),
            # 迭代 2（P4-UI）：plan 确认态显式字段（前端三条件点唯一数据源）。
            "planConfirmed": plan_confirmed,
            "hasPlan": has_plan,
        }
    )


@router.get("/api/story/{story_key}/stats")
def get_story_stats(story_key: str):
    """Aggregate quality/progress stats for the detail-page overview cards.

    Returns:
        code_changes: delivery artifacts (PRs/MRs) — units of code change.
        loop_rounds: adversarial plan↔review / code↔review iterations logged.
        findings_open: unresolved findings (status == 'open').
        tokens: aggregated LLM token usage and estimated cost (CNY).
    """
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")

    code_changes = len(db.get_story_delivery_artifacts(story_key))

    findings_open = sum(
        1 for f in db.get_findings_by_story(story_key) if f.get("status") == "open"
    )

    loop_rounds = sum(
        1 for ev in db.get_story_events(story_key) if db.is_adversarial_loop_event(ev)
    )

    tokens = db.get_story_token_usage(story_key)

    return {
        "code_changes": code_changes,
        "loop_rounds": loop_rounds,
        "findings_open": findings_open,
        "tokens": tokens,
    }


@router.get("/api/story/{story_key}/llm-calls")
def get_story_llm_calls(story_key: str):
    """Prompt/response/reasoning bodies for every LLM call in a story.

    Audit endpoint: JOINs llm_call (正文) ↔ llm_trace (指标) by trace_id, ordered
    by call id. Use this to inspect what was asked/answered/thought across an
    orchestration run. Bodies are stored unconditionally (no config switch).
    """
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")

    calls = db.get_story_llm_calls(story_key)
    return {"story_key": story_key, "calls": calls}


@router.get("/api/story/{story_key}/diff")
def get_story_diff(story_key: str, project_id: int | None = None):
    """Return git diff for the story's workspace branch vs its base branch.

    Query params:
        project_id: scope the diff to a single bound project — prefers its
            worktree_path (the agent's checkout), falling back to repo_path.
            Omit to diff the story workspace / first viable project (legacy).

    Returns:
        current_branch, base_branch, diff_range, files[], total_additions,
        total_deletions, total_changes, diff (raw unified diff text),
        project_id, repo_path, worktree_path.
    """
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")

    try:
        result = get_story_workspace_diff(story_key, project_id=project_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        log.exception("failed to get workspace diff for %s", story_key)
        raise HTTPException(500, f"diff failed: {e}")

    return result


@router.post("/api/story")
def create_story(req: CreateStoryRequest):
    from ..story_service import create_and_start_story

    workspace = req.workspace.strip()
    if not workspace:
        raise HTTPException(status_code=400, detail="workspace required")
    if not Path(workspace).is_absolute():
        raise HTTPException(
            status_code=400, detail="workspace must be an absolute path"
        )

    story_key = create_and_start_story(
        story_key=req.key,
        title=req.title,
        profile=req.profile,
        workspace=workspace,
        prd_path=None,
    )

    if req.autostart:
        start_story_async(story_key)

    s = db.get_story(story_key)
    return JSONResponse(
        {
            "id": s["id"],
            "storyKey": s["story_key"],
            "title": s["title"],
            "currentStage": s["current_stage"],
            "status": s["status"],
            "workspace": s["workspace"],
        }
    )
