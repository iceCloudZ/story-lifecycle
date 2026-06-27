"""Worktree Decider — pure functions, no side effects.

Decision tables for worktree prepare and cleanup.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


def _normpath(p: str) -> str:
    return os.path.normpath(p)


class PrepareAction(str, Enum):
    CREATE = "create"  # create branch + worktree
    REUSE = "reuse"  # existing worktree, branch matches
    REJECT = "reject"  # cannot proceed


class RejectReason(str, Enum):
    PATH_CONFLICT = "path_conflict"  # path exists but not a worktree
    BRANCH_CHECKED_OUT_ELSEWHERE = "branch_checked_out_elsewhere"
    STALE = "stale"  # worktree branch doesn't match
    PROJECT_NOT_FOUND = "project_not_found"
    BRANCH_EXISTS = "branch_exists"  # branch exists but worktree doesn't
    NO_BRANCH_NAME = "no_branch_name"  # story_project has no branch set


class CleanupAction(str, Enum):
    ALLOW = "allow_cleanup"
    REJECT = "reject"


class CleanupRejectReason(str, Enum):
    DELIVERY_NOT_FINALIZED = "delivery_not_finalized"
    WORKTREE_DIRTY = "worktree_dirty"
    WORKTREE_NOT_FOUND = "worktree_not_found"


@dataclass
class DecidePrepareResult:
    action: PrepareAction
    reason: str = ""
    reject_reason: RejectReason | None = None


@dataclass
class DecideCleanupResult:
    action: CleanupAction
    reason: str = ""
    reject_reason: CleanupRejectReason | None = None


def decide_prepare(
    story_project: dict,
    worktree_map: dict,
) -> DecidePrepareResult:
    """Pure function: decide whether to create, reuse, or reject a worktree prepare.

    Args:
        story_project: DB row from story_project table.
        worktree_map: dict from resolve_worktrees() — path -> GitWorktree.

    Decision table:
        - worktree不存在, 分支不存在 → create
        - worktree存在, 分支匹配, 未被占用 → reuse
        - 路径存在但不是git worktree → reject (path_conflict)
        - 分支已被其他worktree checkout → reject (branch_checked_out_elsewhere)
        - worktree分支不匹配 → reject (stale)
    """
    wt_path = story_project.get("worktree_path", "")
    expected_branch = story_project.get("branch", "")

    if not expected_branch:
        return DecidePrepareResult(
            action=PrepareAction.REJECT,
            reason="story_project has no branch name set",
            reject_reason=RejectReason.NO_BRANCH_NAME,
        )

    # Worktree path not set — need to create (but still check branch conflicts)
    if not wt_path:
        conflicts = [p for p, w in worktree_map.items() if w.branch == expected_branch]
        if conflicts:
            return DecidePrepareResult(
                action=PrepareAction.REJECT,
                reason=f"branch '{expected_branch}' already checked out at: {conflicts}",
                reject_reason=RejectReason.BRANCH_CHECKED_OUT_ELSEWHERE,
            )
        return DecidePrepareResult(
            action=PrepareAction.CREATE,
            reason="no worktree path set, will create",
        )

    wt_path_norm = _normpath(wt_path)

    # Check if worktree is registered (normalized path comparison)
    if wt_path_norm in worktree_map:
        wt = worktree_map[wt_path_norm]
        if wt.branch == expected_branch:
            # Check branch conflict — same branch checked out elsewhere
            conflicts = [
                p
                for p, w in worktree_map.items()
                if p != wt_path_norm and w.branch == expected_branch
            ]
            if conflicts:
                return DecidePrepareResult(
                    action=PrepareAction.REJECT,
                    reason=f"branch '{expected_branch}' already checked out at: {conflicts}",
                    reject_reason=RejectReason.BRANCH_CHECKED_OUT_ELSEWHERE,
                )
            return DecidePrepareResult(
                action=PrepareAction.REUSE,
                reason=f"worktree at {wt_path} with branch {expected_branch} is ready",
            )
        else:
            return DecidePrepareResult(
                action=PrepareAction.REJECT,
                reason=f"worktree branch mismatch: expected '{expected_branch}', got '{wt.branch}'",
                reject_reason=RejectReason.STALE,
            )

    # Path not in worktree map — still check for branch conflicts
    conflicts = [p for p, w in worktree_map.items() if w.branch == expected_branch]
    if conflicts:
        return DecidePrepareResult(
            action=PrepareAction.REJECT,
            reason=f"branch '{expected_branch}' already checked out at: {conflicts}",
            reject_reason=RejectReason.BRANCH_CHECKED_OUT_ELSEWHERE,
        )

    # Check if a non-worktree path exists at the target location
    if Path(wt_path).exists():
        return DecidePrepareResult(
            action=PrepareAction.REJECT,
            reason=f"path {wt_path} exists but is not a git worktree",
            reject_reason=RejectReason.PATH_CONFLICT,
        )

    # Path doesn't exist — need to create
    return DecidePrepareResult(
        action=PrepareAction.CREATE,
        reason=f"worktree path {wt_path} not found, will create",
    )


def decide_cleanup(
    story_project: dict,
    delivery_state: str,
    is_worktree_clean: bool = False,
    worktree_exists: bool = True,
) -> DecideCleanupResult:
    """Pure function: decide whether a worktree can be cleaned up.

    Args:
        story_project: DB row from story_project table.
        delivery_state: aggregate delivery state for the story (merged/abandoned/etc.).
        is_worktree_clean: whether `git status --porcelain` is empty in the worktree.
        worktree_exists: whether the worktree path exists on disk.

    Decision table:
        - delivery_state in (merged, abandoned) AND clean → allow
        - delivery_state in (merged, abandoned) AND dirty → reject (worktree_dirty)
        - 其他 delivery_state → reject (delivery_not_finalized)
        - worktree不存在 → reject (worktree_not_found)
    """
    if not worktree_exists:
        return DecideCleanupResult(
            action=CleanupAction.REJECT,
            reason="worktree does not exist on disk",
            reject_reason=CleanupRejectReason.WORKTREE_NOT_FOUND,
        )

    finalized_states = {"merged", "abandoned"}
    if delivery_state not in finalized_states:
        return DecideCleanupResult(
            action=CleanupAction.REJECT,
            reason=f"delivery state '{delivery_state}' is not finalized (need merged or abandoned)",
            reject_reason=CleanupRejectReason.DELIVERY_NOT_FINALIZED,
        )

    if not is_worktree_clean:
        return DecideCleanupResult(
            action=CleanupAction.REJECT,
            reason="worktree has uncommitted changes",
            reject_reason=CleanupRejectReason.WORKTREE_DIRTY,
        )

    return DecideCleanupResult(
        action=CleanupAction.ALLOW,
        reason="worktree can be cleaned up",
    )
