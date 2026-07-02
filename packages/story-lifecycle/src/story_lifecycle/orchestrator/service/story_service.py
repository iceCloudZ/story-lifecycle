"""Shared service layer — single entry point for TUI and server."""

import json as _json
import shutil
from dataclasses import dataclass
from pathlib import Path

from ...infra.db import models as db
from ...infra.story_paths import story_prd_path
from ..nodes import load_profile, get_stage_config

MAX_CONTEXT_SIZE = 1 * 1024 * 1024  # 1MB
MAX_SUB_DEPTH = 1


class WorkspaceError(Exception):
    """Raised when workspace validation fails."""


def _validate_workspace(workspace: str) -> None:
    """Check basic workspace requirements before story creation."""
    ws = Path(workspace)

    # A relative workspace (e.g. ".") resolves against the server's CWD and
    # historically caused evidence artifacts to land inside the tool's own
    # package directory. Require an absolute path so the workspace is always
    # an explicit, user-chosen business directory.
    if not ws.is_absolute():
        raise WorkspaceError(
            f"Workspace must be an absolute path, got: {workspace!r}"
        )

    if not ws.exists():
        raise WorkspaceError(f"Workspace directory does not exist: {ws}")

    if not ws.is_dir():
        raise WorkspaceError(f"Workspace path is not a directory: {ws}")

    # Check write permission
    test_file = ws / ".story" / ".write_test"
    try:
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink(missing_ok=True)
    except PermissionError:
        raise WorkspaceError(f"No write permission in workspace: {ws}")
    except OSError as e:
        raise WorkspaceError(f"Cannot write to workspace: {ws} ({e})")

    # Detect legacy .story-done directory
    legacy = ws / ".story-done"
    if legacy.exists():
        import logging

        logging.getLogger(__name__).warning(
            "Legacy .story-done/ directory detected. Run 'story doctor paths' to migrate."
        )


def _save_prd_task(item, workspace: str, story_key: str = ""):
    """Write prd-task-{story_key}.json for AI-enhanced PRD generation."""
    ws = Path(workspace) if workspace else Path.cwd()
    task_dir = ws / ".story"
    task_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"-{story_key}" if story_key else ""
    task_file = task_dir / f"prd-task{suffix}.json"
    task_file.write_text(
        _json.dumps(
            {
                "source": item.source,
                "source_id": item.id,
                "title": item.title,
                "description": item.description,
                "item_type": item.item_type,
                "priority": item.priority,
                "owner": item.owner,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def create_and_start_story(
    story_key: str,
    title: str = "",
    profile: str = "minimal",
    workspace: str = "",
    prd_path: str | None = None,
    parent_key: str | None = None,
    subtask_index: int = 0,
    description: str = "",
) -> str:
    """Create a story via service layer. Writes to DB and returns story_key.

    The caller (TUI worker or server) is responsible for starting execution.
    """
    ws = workspace or str(Path.cwd())
    _validate_workspace(ws)
    profile_data = load_profile(profile)
    stages = profile_data.get("stages", {})
    first_stage = next(iter(stages)) if stages else "design"

    # Clean stale done files from previous runs
    done_dir = Path(ws) / ".story" / "done" / story_key
    if done_dir.exists():
        shutil.rmtree(done_dir, ignore_errors=True)

    # Handle PRD content
    if prd_path:
        p = Path(prd_path)
        if p.exists():
            prd_content = p.read_text(encoding="utf-8")
            prd_file = story_prd_path(ws, story_key, title)
            prd_file.parent.mkdir(parents=True, exist_ok=True)
            prd_file.write_text(prd_content, encoding="utf-8")
            prd_path = str(prd_file)

    # Upsert business DB (for board quick-read)
    db.upsert_story(
        story_key,
        title=title,
        workspace=ws,
        profile=profile,
        current_stage=first_stage,
        status="active",
        parent_key=parent_key,
        subtask_index=subtask_index,
    )

    # Auto-tag task_type via pure keyword classifier so brand-new stories get a
    # non-None task_type — otherwise knowledge injection returns None (no-op).
    # Pure keywords, no LLM: must stay fast/cheap at creation time.
    try:
        from ..engine.prompt_sections import classify_task_type

        task_type = classify_task_type(title, description)
        if task_type:
            db.update_context(story_key, "task_type", task_type)
    except Exception:  # noqa: BLE001 — tagging must never block story creation
        pass

    if prd_path:
        db.update_context(story_key, "prd_path", prd_path)
        db.create_document(
            story_key,
            "prd",
            ref=prd_path,
            summary="Intake PRD",
            source="system",
            verification_state="verified",
        )

    return story_key


def get_story_cli_model(story_key: str) -> dict:
    """Get CLI tool and model for a story's current stage."""
    s = db.get_story(story_key)
    if not s:
        return {"cli": "claude", "model": "sonnet"}

    profile = s.get("profile", "minimal")
    stage = s.get("current_stage", "design")
    try:
        cfg = get_stage_config(profile, stage)
        profile_data = load_profile(profile)
        return {
            "cli": cfg.get("cli", profile_data.get("cli", "claude")),
            "model": cfg.get("model", "sonnet"),
        }
    except FileNotFoundError:
        return {"cli": "claude", "model": "sonnet"}


def fail_story(story_key: str, reason: str = "Manual fail"):
    """Mark a story as blocked."""
    db.update_story(story_key, status="blocked", last_error=reason)
    db.log_stage(story_key, "", "fail", reason)


def skip_stage(story_key: str, stage: str, reason: str = "Manual skip"):
    """Skip a story's current stage."""
    db.log_stage(story_key, stage, "skip", reason)
    db.update_story(story_key, status="active")


def delete_story(story_key: str):
    """Delete a story and clean up."""
    from ...infra.terminal import ttyd

    db.delete_story(story_key)
    ttyd.stop_ttyd(story_key)


def create_sub_story(
    parent_key: str,
    sub_type: str | None = None,
    start_stage: str | None = None,
    description: str = "",
) -> str:
    """Create a sub-story that inherits parent context. Returns sub story_key."""
    parent = db.get_story(parent_key)
    if not parent:
        raise ValueError(f"Parent story not found: {parent_key}")

    # Nesting check
    if parent.get("parent_key"):
        raise ValueError("子故事不能嵌套创建")

    # Generate sub story key
    siblings = db.get_sub_stories(parent_key)
    index = len(siblings)
    story_key = f"{parent_key}-sub-{index + 1}"

    # Derive start_stage
    if not start_stage:
        profile_data = load_profile(parent.get("profile", "minimal"))
        stages = list(profile_data.get("stages", {}).keys())
        start_stage = stages[0] if stages else "design"

    # Inherit context with size control
    parent_ctx_str = parent.get("context_json") or "{}"
    if len(parent_ctx_str) > MAX_CONTEXT_SIZE:
        parent_ctx = _json.loads(parent_ctx_str)
        child_ctx = {
            "parent_ref": parent_key,
            "sub_description": description,
            "_skipped_fields": [
                k
                for k, v in parent_ctx.items()
                if isinstance(v, str) and len(v) > 10_000
            ],
        }
        for k, v in parent_ctx.items():
            if not (isinstance(v, str) and len(v) > 10_000):
                child_ctx[k] = v
    else:
        child_ctx = _json.loads(_json.dumps(_json.loads(parent_ctx_str)))
        child_ctx["sub_description"] = description

    # Create sub-story
    db.upsert_story(
        story_key,
        title=description,
        workspace=parent["workspace"],
        profile=parent.get("profile", "minimal"),
        current_stage=start_stage,
        status="active",
        parent_key=parent_key,
        subtask_index=index,
    )
    db.update_story(story_key, context_json=_json.dumps(child_ctx, ensure_ascii=False))
    if sub_type:
        db.update_story(story_key, sub_type=sub_type)
    db.log_stage(story_key, "", "create_sub", f"type={sub_type}, from={parent_key}")

    # Parent status transition
    if parent["status"] == "active":
        db.update_story(parent_key, status="waiting_subtasks")

    return story_key


def abort_story(story_key: str, reason: str = "User abort"):
    """Abort a story. Aborted stories don't count as 'completed'."""
    s = db.get_story(story_key)
    if not s:
        raise ValueError(f"Story not found: {story_key}")

    db.update_story(story_key, status="aborted", last_error=reason)
    db.log_stage(story_key, "", "abort", reason)

    # If this is a sub-story, check if parent can resume
    if s.get("parent_key"):
        _check_parent_auto_resume(s["parent_key"])


def resume_parent(parent_key: str, strategy: str = "pause_subs"):
    """Resume a parent from waiting_subtasks. Handles unfinished subs."""
    parent = db.get_story(parent_key)
    if not parent:
        raise ValueError(f"Parent story not found: {parent_key}")
    if parent["status"] != "waiting_subtasks":
        raise ValueError("父故事不在等待子故事状态")

    subs = db.get_sub_stories(parent_key)
    active_subs = [s for s in subs if s["status"] in ("active", "paused", "blocked")]

    if strategy == "pause_subs":
        for sub in active_subs:
            db.update_story(sub["story_key"], status="paused")
            db.log_stage(sub["story_key"], "", "pause", "父故事恢复，子故事被暂停")
    elif strategy == "abort_subs":
        for sub in active_subs:
            abort_story(sub["story_key"], "父故事恢复，子故事被中止")

    db.update_story(parent_key, status="active")
    db.log_stage(parent_key, "", "resume", "手动恢复")


def _check_parent_auto_resume(parent_key: str):
    """Check if all subs are done; if so, resume parent automatically."""
    subs = db.get_sub_stories(parent_key)
    terminal = {"completed", "aborted", "blocked"}
    unfinished = [s for s in subs if s["status"] not in terminal]

    if not unfinished:
        import json as _json

        db.update_story(parent_key, status="active")
        summary = {
            "total": len(subs),
            "completed": [
                {"story_key": s["story_key"], "type": s.get("sub_type")}
                for s in subs
                if s["status"] == "completed"
            ],
            "aborted": [
                {"story_key": s["story_key"], "type": s.get("sub_type")}
                for s in subs
                if s["status"] == "aborted"
            ],
        }
        db.update_context(
            parent_key, "sub_story_results", _json.dumps(summary, ensure_ascii=False)
        )
        db.log_event(parent_key, "", "subtasks_completed", summary)


@dataclass
class CreateFromSourceResult:
    status: str  # "created" | "need_manual_select" | "failed"
    story_key: str | None = None
    bug_item: object | None = None
    error: str | None = None


def create_story_from_source(
    item,
    profile: str = "minimal",
    workspace: str = "",
    generate_prd: bool = True,
    generate_ai_prd: bool = False,
    auto_start: bool = True,
    force_standalone: bool = False,
) -> CreateFromSourceResult:
    from ...sourcing.sources.base import resolve_bug_parent
    from ...sourcing.sources import get_source
    from ...sourcing.sources.prd_providers import fetch_prd_content, save_prd
    from ...sourcing.sources.bug_providers import fetch_bug_content, format_bug_context

    story_key = _derive_story_key(item)
    prd_path = None

    # Requirement -> PrdProvider chain (or AI-enhanced PRD)
    if generate_ai_prd and item.item_type == "requirement":
        _save_prd_task(item, workspace, story_key)
    elif generate_prd and item.item_type == "requirement":
        prd_content = fetch_prd_content(item)
        if prd_content and prd_content.markdown:
            prd_path = save_prd(story_key, prd_content, workspace, title=item.title)

    # Bug -> resolve parent (skip when force_standalone to avoid infinite loop)
    if item.item_type == "bug" and not force_standalone:
        active_stories = _get_all_stories()
        result = resolve_bug_parent(item, active_stories)

        # Auto-import parent if needed
        if result.need_import_parent and result.parent_source_id:
            source = get_source(item.source)
            parent_item = source.get_detail(result.parent_source_id) if source else None
            if not parent_item:
                return CreateFromSourceResult(
                    status="failed",
                    error=f"无法导入父需求: {item.source}/{result.parent_source_id}",
                )
            parent_result = create_story_from_source(
                parent_item,
                profile=profile,
                workspace=workspace,
                generate_prd=True,
                auto_start=False,
            )
            if parent_result.status != "created" or not parent_result.story_key:
                return CreateFromSourceResult(
                    status="failed",
                    error=f"父需求导入失败: {parent_result.error or parent_result.status}",
                )
            # Mark auto-imported parent as paused with import marker
            db.update_story(parent_result.story_key, status="paused")
            db.update_context(parent_result.story_key, "source_import_only", "true")
            result.parent_key = parent_result.story_key

        if result.need_manual_select:
            return CreateFromSourceResult(status="need_manual_select", bug_item=item)
        if result.parent_key:
            bug_ctx = fetch_bug_content(item)
            bug_desc = format_bug_context(bug_ctx)
            sub_key = create_sub_story(
                parent_key=result.parent_key,
                sub_type="bug-fix",
                description=bug_desc,
            )
            db.update_story(sub_key, source_type=item.source, source_id=item.id)
            if auto_start:
                from ..engine.graph import start_story_async

                start_story_async(sub_key)
            return CreateFromSourceResult(status="created", story_key=sub_key)

    # Create normal story (standalone bug or requirement)
    bug_ctx = None
    if item.item_type == "bug":
        bug_ctx = fetch_bug_content(item)
    title = item.title
    if bug_ctx:
        title = bug_ctx.description or item.title
    key = create_and_start_story(
        story_key=story_key,
        title=title,
        profile=profile,
        workspace=workspace,
        prd_path=prd_path,
        description=item.description or "",
    )
    db.update_story(key, source_type=item.source, source_id=item.id)

    # Record story_intake event for quality flywheel
    try:
        from ..evaluation.quality import record_story_intake

        record_story_intake(
            story_key=key,
            source=item.source,
            source_id=item.id,
            metadata={"has_prd": bool(prd_path), "item_type": item.item_type},
        )
    except Exception:
        pass

    if auto_start:
        from ..engine.graph import start_story_async

        start_story_async(key)

    return CreateFromSourceResult(status="created", story_key=key)


def _derive_story_key(item) -> str:
    if item.source == "github":
        return f"GH-{item.id}"
    return (
        f"TAPD-{item.id[-7:]}"
        if item.source == "tapd"
        else f"{item.source.upper()}-{item.id[-6:]}"
    )


def _get_all_stories() -> list[dict]:
    """Get all stories for parent matching."""
    try:
        return db.list_active_stories()
    except Exception:
        return []
