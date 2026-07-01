"""Worktree Resolver — read-only inspection of git worktree state.

- resolve_worktrees: parse `git worktree list --porcelain -z`
- resolve_story_worktree: map story_project bindings to WorktreeState
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


def _normpath(p: str) -> str:
    """Normalize a path for cross-platform comparison.

    Git porcelain output uses forward slashes on Windows;
    Python Path uses backslashes. This normalizes both to os-native form.
    """
    return os.path.normpath(p)


class WorktreeState(str, Enum):
    """Possible states for a story worktree."""

    UNPREPARED = "unprepared"  # not yet created
    AVAILABLE = "available"  # worktree exists, branch matches, not occupied
    MISSING = "missing"  # worktree path exists in DB but not on disk
    STALE = "stale"  # worktree exists but branch doesn't match
    CONFLICT = "conflict"  # branch checked out elsewhere
    UNKNOWN = "unknown"  # cannot determine (git error, etc.)


@dataclass
class GitWorktree:
    """Parsed representation of a single git worktree entry."""

    path: str
    head: str = ""
    branch: str = ""
    locked: bool = False
    prunable: str = ""


def resolve_worktrees(project_path: str | Path) -> dict[str, GitWorktree]:
    """Parse `git worktree list --porcelain -z` for a project's main repository.

    Returns a dict mapping worktree path -> GitWorktree.
    Keys are normalized with os.path.normpath for cross-platform matching.
    An empty dict means no worktrees (bare repo or no worktrees created).
    """
    project_path = str(project_path)
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain", "-z"],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return {}
    except subprocess.TimeoutExpired:
        return {}
    except OSError:
        return {}

    if result.returncode != 0:
        return {}

    return _parse_porcelain_z(result.stdout)


def _parse_porcelain_z(output: str) -> dict[str, GitWorktree]:
    """Parse git worktree list --porcelain -z output into a dict.

    With -z, EVERY field is NUL-terminated, and records are separated
    by an extra NUL (i.e., double-NUL between records).
    """
    worktrees: dict[str, GitWorktree] = {}

    # Split on double-NUL to get records
    for record in output.split("\0\0"):
        record = record.strip("\0").strip()
        if not record:
            continue
        wt = _parse_single_worktree_z(record)
        if wt:
            worktrees[_normpath(wt.path)] = wt

    return worktrees


def _parse_single_worktree_z(record: str) -> GitWorktree | None:
    """Parse a single worktree record from -z output.

    Each attribute line is NUL-terminated within the record.
    """
    wt = GitWorktree(path="")
    for line in record.split("\0"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("worktree "):
            wt.path = line[len("worktree ") :]
        elif line.startswith("HEAD "):
            wt.head = line[len("HEAD ") :]
        elif line.startswith("branch "):
            wt.branch = line[len("branch ") :]
        elif line.strip() == "locked":
            wt.locked = True
        elif line.startswith("prunable "):
            wt.prunable = line[len("prunable ") :]

    if not wt.path:
        return None
    # Normalize path for cross-platform matching
    wt.path = _normpath(wt.path)
    # Strip refs/heads/ prefix from branch to get the short name
    if wt.branch.startswith("refs/heads/"):
        wt.branch = wt.branch[len("refs/heads/") :]
    return wt


def resolve_story_worktree(story_key: str) -> list[dict]:
    """Resolve worktree state for all project bindings of a story.

    Returns a list of dicts with keys:
        story_project: the DB row
        state: WorktreeState value
        worktree: GitWorktree if found, else None
        reason: human-readable explanation
    """
    from ....infra.db import models as db

    bindings = db.get_story_projects(story_key)
    results: list[dict] = []

    for sp in bindings:
        project = db.get_project(sp["project_id"])
        if not project:
            results.append(
                {
                    "story_project": sp,
                    "state": WorktreeState.UNKNOWN,
                    "worktree": None,
                    "reason": f"project {sp['project_id']} not found",
                }
            )
            continue

        repo_path = project["repo_path"]
        worktrees = resolve_worktrees(repo_path)
        wt_path = sp.get("worktree_path", "")
        expected_branch = sp.get("branch", "")

        if not wt_path:
            results.append(
                {
                    "story_project": sp,
                    "state": WorktreeState.UNPREPARED,
                    "worktree": None,
                    "reason": "no worktree_path set",
                }
            )
            continue

        # Check if the worktree is registered in git (normalize path for comparison)
        wt_path_norm = _normpath(wt_path)
        if wt_path_norm in worktrees:
            wt = worktrees[wt_path_norm]
            if wt.branch == expected_branch:
                # Check if branch is also checked out elsewhere
                other_bindings = _find_other_worktrees_for_branch(
                    worktrees, expected_branch, wt_path
                )
                if other_bindings:
                    results.append(
                        {
                            "story_project": sp,
                            "state": WorktreeState.CONFLICT,
                            "worktree": wt,
                            "reason": f"branch {expected_branch} also checked out at {other_bindings}",
                        }
                    )
                else:
                    results.append(
                        {
                            "story_project": sp,
                            "state": WorktreeState.AVAILABLE,
                            "worktree": wt,
                            "reason": "worktree ready",
                        }
                    )
            else:
                results.append(
                    {
                        "story_project": sp,
                        "state": WorktreeState.STALE,
                        "worktree": wt,
                        "reason": f"expected branch {expected_branch}, got {wt.branch}",
                    }
                )
        else:
            # Path not in worktree list — might not exist or is a normal directory
            if Path(wt_path).exists():
                results.append(
                    {
                        "story_project": sp,
                        "state": WorktreeState.CONFLICT,
                        "worktree": None,
                        "reason": f"path {wt_path} exists but is not a registered worktree",
                    }
                )
            else:
                results.append(
                    {
                        "story_project": sp,
                        "state": WorktreeState.MISSING,
                        "worktree": None,
                        "reason": f"worktree path {wt_path} does not exist",
                    }
                )

    return results


def _find_other_worktrees_for_branch(
    worktrees: dict[str, GitWorktree], branch: str, exclude_path: str
) -> list[str]:
    """Find worktree paths (other than exclude_path) that have the given branch."""
    conflicts = []
    exclude_norm = _normpath(exclude_path)
    for path, wt in worktrees.items():
        if path != exclude_norm and wt.branch == branch:
            conflicts.append(path)
    return conflicts
