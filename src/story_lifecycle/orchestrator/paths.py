"""Workspace path registry — single source of truth for .story/ layout.

All runtime code must use these helpers instead of hand-building paths
like ``Path(workspace) / ".story-done" / ...``.  The on-disk layout:

    .story/
      done/          stage handshake files (was .story-done)
      context/       plans, reviews, packets (was .story-context)
      runs/          benchmark run workspaces (was .story-runs)
"""

from __future__ import annotations

from pathlib import Path


def story_dir(workspace: str | Path) -> Path:
    """Top-level ``.story/`` inside a project workspace."""
    return Path(workspace) / ".story"


# ---- done ----


def done_dir(workspace: str | Path) -> Path:
    return story_dir(workspace) / "done"


def stage_done_file(workspace: str | Path, story_key: str, stage: str) -> Path:
    return done_dir(workspace) / story_key / f"{stage}.json"


# ---- context ----


def context_dir(workspace: str | Path, story_key: str) -> Path:
    return story_dir(workspace) / "context" / story_key


def plan_file(workspace: str | Path, story_key: str, stage: str) -> Path:
    return context_dir(workspace, story_key) / f"plan_{stage}.md"


def review_file(workspace: str | Path, story_key: str, stage: str) -> Path:
    return context_dir(workspace, story_key) / f"review_{stage}.md"


def done_snapshot_file(workspace: str | Path, story_key: str, stage: str) -> Path:
    """Consumed done snapshot — written before source deletion."""
    return context_dir(workspace, story_key) / "done" / f"{stage}.json"


def malformed_done_file(workspace: str | Path, story_key: str, stage: str) -> Path:
    """Destination for un-parseable done files."""
    return context_dir(workspace, story_key) / "done" / f"{stage}.malformed"


def gate_report_dir(workspace: str | Path, story_key: str) -> Path:
    return context_dir(workspace, story_key) / "gates"


# ---- runs (benchmark) ----


def runs_dir(workspace_root: str | Path) -> Path:
    return story_dir(workspace_root) / "runs"


def swebench_run_dir(workspace_root: str | Path, run_id: str) -> Path:
    return runs_dir(workspace_root) / "swebench" / run_id
