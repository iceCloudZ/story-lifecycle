"""Shared service layer — single entry point for TUI and server."""

from pathlib import Path

from ..db import models as db
from .nodes import load_profile, get_stage_config


def create_and_start_story(
    story_key: str,
    title: str = "",
    profile: str = "minimal",
    workspace: str = "",
    prd_path: str | None = None,
) -> str:
    """Create a story via service layer. Writes to DB and returns story_key.

    The caller (TUI worker or server) is responsible for starting execution.
    """
    ws = workspace or str(Path.cwd())
    profile_data = load_profile(profile)
    stages = profile_data.get("stages", {})
    first_stage = next(iter(stages)) if stages else "design"

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
