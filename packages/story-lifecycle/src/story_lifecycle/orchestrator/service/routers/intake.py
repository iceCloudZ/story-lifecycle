"""routers/intake — story intake domain（设计15 阶段C 拆自 plan.py）。

intake 与 start 端点:从外部源(tapd)拉取 story 详情 → 生成 PRD → 绑定项目。
"""

from __future__ import annotations

import logging
from pathlib import Path

import tempfile

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ....infra.db import models as db
from ....sourcing.state_machine import activate as sm_activate
from ....sourcing.sources import tapd_source
from ....sourcing.sources.tapd_source import TapdSource
from ....knowledge.adapters import get_adapter
from ...engine import planner
from ...engine.graph import start_story_async
from .._shared import _load_tapd_config, _workspace_root_for_project
from .. import prd_generator

log = logging.getLogger("story-lifecycle.api.intake")

router = APIRouter(tags=["intake"])

class IntakePreviewRequest(BaseModel):
    source_type: str = "tapd"
    source_id: str


class StartStoryRequest(BaseModel):
    project_ids: list[int] = []
    content: str = ""  # PRD / 需求正文，开始开发时必填，design 阶段注入给 CLI
    seed_context: str = ""  # 接手中途需求:已有工作说明,写入 context_json.seed_context
    branch: str = ""  # 预生成的分支名（由 intake preview 产出），保存时直接复用


def _bind_story_projects_for_start(
    story_key: str, story: dict, project_ids: list[int], branch: str = ""
):
    # 覆盖语义：用户本次提交的 project_ids 代表完整期望绑定集合。
    # 先清除旧绑定（story key 复用/intake 重走时避免残留）。
    # intake 阶段绑定均为 worktree_state="unprepared"，无 worktree 副作用，可直接删。
    for sp in db.get_story_projects(story_key):
        try:
            db.unbind_story_project(story_key, sp["project_id"])
        except Exception:
            log.debug(
                "[%s] unbind stale project %s failed", story_key, sp.get("project_id")
            )

    if not project_ids:
        return

    all_projects = {p["id"]: p for p in db.list_projects()}
    bound_repo = None
    for pid in project_ids:
        proj = all_projects.get(pid)
        if not proj:
            continue

        # 优先复用 preview 阶段预生成的分支名，避免保存时重复调 LLM。
        # 若未传入或 profile 规则需要按项目区分，则现场生成。
        if branch:
            per_project_branch = branch
        else:
            from ...engine.profile_loader import load_profile
            from ...workspace.branch_naming import generate_branch_for_story

            profile_raw = load_profile(story.get("profile") or "minimal")
            per_project_branch = (
                generate_branch_for_story(
                    story_key=story_key,
                    title=story.get("title", ""),
                    profile_raw=profile_raw,
                    project_name=proj["name"],
                )
                or f"codex/{story_key}-{proj['name']}"
            )

        repo_path = proj.get("repo_path", "")

        db.bind_story_project(
            story_key=story_key,
            project_id=proj["id"],
            branch=per_project_branch,
            base_branch=proj.get("default_branch", "main"),
            worktree_state="unprepared",
            source="user",
        )
        if not bound_repo and repo_path:
            bound_repo = repo_path
    if bound_repo:
        workspace_root = _workspace_root_for_project(bound_repo)
        db.update_story(story_key, workspace=str(workspace_root))


def _prepare_intake_prd_content(story_key: str, story: dict, content: str):
    """Return (content, error_response) for the start endpoint.

    If the user supplied content, treat it as the PRD/intake material directly.
    Otherwise, ask the built-in PRD generator to prepare PRD from the story source.
    """
    if (content or "").strip():
        return content, None

    source_type = story.get("source_type") or ""
    source_id = story.get("source_id") or ""
    if not source_type or not source_id:
        return "", JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "reasonCode": "content_required",
                "message": "请填写 story 内容 / PRD",
            },
        )

    try:
        source_snapshot = _load_story_source_snapshot(story_key, story)
    except Exception as exc:
        return "", JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "reasonCode": "source_detail_unavailable",
                "message": f"无法读取 story 来源详情: {exc}",
            },
        )
    if not source_snapshot:
        return "", JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "reasonCode": "content_required",
                "message": "请填写 story 内容 / PRD",
            },
        )

    from .. import prd_generator

    try:
        result = prd_generator.generate_prd_from_source(source_snapshot)
    except Exception as exc:
        return "", JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "reasonCode": "prd_generation_failed",
                "message": f"PRD 生成失败: {exc}",
            },
        )

    if result.action == "generated" and result.markdown.strip():
        return result.markdown, None

    if result.action == "manual_download_required":
        return "", JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "reasonCode": "dingtalk_download_required",
                "message": result.summary or "请先打开外部文档并下载/复制 PRD 内容",
                "dingtalk_links": result.dingtalk_links,
            },
        )

    if result.action == "needs_clarification":
        return "", JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "reasonCode": "intake_clarification_required",
                "message": result.summary or "PRD 生成前需要补充需求信息",
                "questions": result.questions,
            },
        )

    return "", JSONResponse(
        status_code=409,
        content={
            "ok": False,
            "reasonCode": "prd_generation_failed",
            "message": result.summary or "PRD 生成失败",
        },
    )


@router.post("/api/intake/preview")
def api_intake_preview(
    source_type: str = Form("tapd"),
    source_id: str = Form(""),
    files: list[UploadFile] = File(default_factory=list),
):
    """Fetch source detail and ask the built-in PRD generator to prefill Intake.

    Accepts optional image uploads so users can supply screenshots that the
    source system cannot fetch automatically (e.g. TAPD images behind login).
    """
    source_id = (source_id or "").strip()
    if not source_id:
        raise HTTPException(status_code=400, detail="source_id required")
    source_type = (source_type or "tapd").strip().lower()

    if source_type != "tapd":
        raise HTTPException(
            status_code=400, detail=f"unsupported source: {source_type}"
        )

    source_id = source_id.removeprefix("tapd-")
    from ....sourcing.sources import tapd_source
    from .. import prd_generator

    source = tapd_source.TapdSource(_load_tapd_config())
    item = source.get_detail(source_id)
    if not item:
        raise HTTPException(status_code=404, detail="source story not found")

    local_image_paths: list[str] = []
    if files:
        tmp_dir = Path(tempfile.gettempdir()) / "story-intake-images"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        for upload in files:
            if not upload.filename:
                continue
            # Sanitize upload filename: HTTP multipart filename is fully
            # attacker-controlled. Take basename only and reject traversal.
            safe_name = Path(upload.filename).name
            if not safe_name or safe_name in {".", ".."} or ".." in safe_name:
                continue  # drop suspicious upload silently
            tmp_path = tmp_dir / f"{item.source}-{item.id}_{safe_name}"
            # Blast shield: tmp_path must stay inside tmp_dir.
            try:
                tmp_path.resolve().relative_to(tmp_dir.resolve())
            except ValueError:
                continue
            with tmp_path.open("wb") as f:
                f.write(upload.file.read())
            local_image_paths.append(str(tmp_path))

    snapshot = prd_generator.StorySourceSnapshot(
        story_key=f"{item.source}-{item.id}",
        source_type=item.source,
        source_id=item.id,
        title=item.title,
        description=item.description or "",
        url=item.extra.get("url", ""),
        priority=item.priority,
        owner=item.owner,
        status=item.status,
        local_image_paths=local_image_paths,
    )
    try:
        result = prd_generator.generate_prd_from_source(snapshot)
    except Exception as exc:
        log.exception("prd_generator failed for %s", snapshot.story_key)
        raise HTTPException(
            status_code=502,
            detail=f"PRD 生成失败: {exc}",
        )

    # 预生成分支名，让保存阶段直接复用，避免每次点击保存都调 LLM。
    # 仅当 profile 的 branch_rule 不含 {project} 时才能前置；含 {project} 时
    # 让 start 阶段按项目名动态生成。
    branch = ""
    try:
        from ...engine.profile_loader import load_profile
        from ...workspace.branch_naming import generate_branch_for_story

        profile_raw = load_profile("minimal")
        rule = profile_raw.get("branch_rule", "")
        if rule and "{project}" not in rule:
            branch = (
                generate_branch_for_story(
                    story_key=snapshot.story_key,
                    title=snapshot.title,
                    profile_raw=profile_raw,
                )
                or ""
            )
    except Exception:
        log.exception("branch pre-generation failed for %s", snapshot.story_key)

    return {
        "storyKey": snapshot.story_key,
        "sourceType": snapshot.source_type,
        "sourceId": snapshot.source_id,
        "title": snapshot.title,
        "sourceUrl": snapshot.url,
        "action": result.action,
        "markdown": result.markdown,
        "summary": result.summary,
        "dingtalkLinks": result.dingtalk_links,
        "questions": result.questions,
        "branch": branch,
    }


@router.post("/api/story/{story_key}/start")
def api_start_story(story_key: str, req: StartStoryRequest | None = None):
    """Start a story. Binds projects, promotes to ready, triggers LLM planning."""
    story = db.get_story(story_key)
    if not story:
        raise HTTPException(status_code=404, detail="story not found")

    intake_state = story.get("intake_state", "ready")
    req = req or StartStoryRequest()

    # 接手中途需求:用户可能只填了 seed_context(已有工作说明)没填 PRD content。
    # 用 seed_context 兜底当 PRD 正文——接手说明本身描述了需求,写成 PRD 文件后
    # design/verify 阶段的 prd_path 注入仍能工作。用户填了 content 则 content 优先。
    effective_content = req.content or req.seed_context

    # Intake: user-provided PRD wins; otherwise source-backed stories can ask the
    # built-in PRD generator LLM to prepare or route PRD creation.
    prd_content, intake_error = _prepare_intake_prd_content(
        story_key, story, effective_content
    )
    if intake_error:
        return intake_error

    try:
        if intake_state == "candidate":
            # Promote candidate to ready. planning 移出 status(归 lifecycle_state=待启动);
            # /start 后引擎开始跑规划,算 active。
            db.update_story(story_key, intake_state="ready", status="active")

        # Project binding is optional during Intake. In monorepos, the selected
        # implementation modules (for example hc-order or hc-limit under hc-all) are
        # discovered later by Design/Build, not modeled as separate repo projects.
        _bind_story_projects_for_start(story_key, story, req.project_ids, req.branch)

        # 保存 PRD 到 story evidence 目录，供 design 阶段注入。
        # 不写入被绑定服务仓库的 prd/，避免污染业务代码仓库。
        story = db.get_story(story_key)
        workspace = (story or {}).get("workspace", "") or ""
        if not workspace:
            return JSONResponse(
                status_code=409,
                content={
                    "ok": False,
                    "reasonCode": "workspace_required",
                    "message": "无法确定工作区，请先选择工作区或注册项目",
                },
            )

        from ....infra.story_paths import story_prd_path

        prd_file = story_prd_path(workspace, story_key, (story or {}).get("title", ""))
        prd_file.parent.mkdir(parents=True, exist_ok=True)
        prd_file.write_text(prd_content, encoding="utf-8")
        db.update_context(story_key, "prd_path", str(prd_file))
        # 接手中途需求:把 seed_context 写进 context_json,供规划 LLM
        # (run_orchestrator_agent)和执行 prompt(prompts.py 的
        # "### 已有工作(接手)" section)读取。
        if req.seed_context.strip():
            db.update_context(story_key, "seed_context", req.seed_context.strip())
        existing_prd = [
            d for d in db.get_story_documents(story_key) if d.get("kind") == "prd"
        ]
        if existing_prd:
            db.update_document(
                existing_prd[0]["id"],
                ref=str(prd_file),
                summary="Intake PRD",
                source="system",
                verification_state="verified",
            )
        else:
            db.create_document(
                story_key,
                "prd",
                ref=str(prd_file),
                summary="Intake PRD",
                source="system",
                verification_state="verified",
            )
        # Dual-write: also version the PRD body into story_doc so the docs UI
        # (version history / diff / search) sees intake-created PRDs. Goes
        # through the shared helper to keep both tables in sync — same path
        # the AI stage outputs and the web editor use.
        try:
            from ....infra.doc_sync import register_doc_dual_write

            register_doc_dual_write(
                story_key,
                "prd",
                str(prd_file),
                content=prd_content,
                change_reason="Intake PRD 初始导入",
                author="system",
                workspace=workspace,
                summary="Intake PRD",
                source="system",
                verification_state="verified",
            )
        except Exception as exc:  # noqa: BLE001 — versioning is best-effort
            log.debug("[%s] PRD dual-write skipped: %s", story_key, exc)
        db.bump_context_revision(story_key)

        db.update_story(story_key, intake_state="ready", status="active")

        # Intake 后触发 LLM 规划（兑现 docstring "triggers LLM planning"）。
        # BUG FIX: 原本 /start 只改 status=active 却不调规划，而 run_story 无条件调
        # continue_orchestrator_agent(它要求 ctx["_agent_actions"] 已存在)→ 规划永远
        # 不跑 → 用户点「开始执行」必命中 "No actions to execute" false-failed。
        # 这里同步跑规划（用户在 intake 弹窗等「处理中...」，阻塞可接受），
        # 规划失败不阻断 /start（用户可在详情页 regenerate 重试）。
        try:
            planner.run_orchestrator_agent(story_key)
        except Exception as plan_exc:  # noqa: BLE001 — 规划失败不阻断 intake
            log.warning(
                "[%s] intake planning failed (user can regenerate): %s",
                story_key,
                plan_exc,
            )
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("Failed to start story %s", story_key)
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "reasonCode": "start_failed",
                "message": f"启动 Story 失败: {exc}",
            },
        )

    return {"ok": True, "story_key": story_key}


def _load_story_source_snapshot(story_key: str, story: dict):
    from .. import prd_generator

    source_type = story.get("source_type") or ""
    source_id = story.get("source_id") or ""

    if source_type == "tapd":
        from ....sourcing.sources import tapd_source

        source = tapd_source.TapdSource(_load_tapd_config())
        item = source.get_detail(source_id)
        if not item:
            return None
        return prd_generator.StorySourceSnapshot(
            story_key=story_key,
            source_type=item.source,
            source_id=item.id,
            title=item.title or story.get("title", ""),
            description=item.description or "",
            url=item.extra.get("url", "") or story.get("tapd_url", ""),
            priority=item.priority,
            owner=item.owner,
            status=item.status,
        )

    return prd_generator.StorySourceSnapshot(
        story_key=story_key,
        source_type=source_type,
        source_id=source_id,
        title=story.get("title", ""),
        description="",
        url=story.get("tapd_url", ""),
        priority=story.get("priority", ""),
        owner=story.get("owner", ""),
        status=story.get("tapd_status", ""),
    )

