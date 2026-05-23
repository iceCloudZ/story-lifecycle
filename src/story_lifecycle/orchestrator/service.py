"""Shared service layer — single entry point for TUI and server."""

import json as _json
import shutil
from pathlib import Path

from ..db import models as db
from .nodes import load_profile, get_stage_config

MAX_CONTEXT_SIZE = 1 * 1024 * 1024  # 1MB
MAX_SUB_DEPTH = 1


def create_and_start_story(
    story_key: str,
    title: str = "",
    profile: str = "minimal",
    workspace: str = "",
    prd_path: str | None = None,
    parent_key: str | None = None,
    subtask_index: int = 0,
) -> str:
    """Create a story via service layer. Writes to DB and returns story_key.

    The caller (TUI worker or server) is responsible for starting execution.
    """
    ws = workspace or str(Path.cwd())
    profile_data = load_profile(profile)
    stages = profile_data.get("stages", {})
    first_stage = next(iter(stages)) if stages else "design"

    # Clean stale done files from previous runs
    done_dir = Path(ws) / ".story-done" / story_key
    if done_dir.exists():
        shutil.rmtree(done_dir, ignore_errors=True)

    # Handle PRD content
    if prd_path:
        p = Path(prd_path)
        if p.exists():
            prd_content = p.read_text(encoding="utf-8")
            prd_dir = Path(ws) / "prd"
            prd_dir.mkdir(exist_ok=True)
            prd_file = prd_dir / f"{story_key}.md"
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

    if prd_path:
        db.update_context(story_key, "prd_path", prd_path)

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


def pause_story(story_key: str):
    """Pause an active story."""
    db.update_story(story_key, status="paused")


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
    from ..terminal import ttyd

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
                k for k, v in parent_ctx.items()
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
