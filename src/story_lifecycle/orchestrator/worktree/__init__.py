"""Worktree lifecycle — resolve, decide, handle.

Resolver: read-only inspection of git worktree state.
Decider: pure functions, no side effects.
Handler: execute git operations with locking.
"""

from .resolver import resolve_worktrees, resolve_story_worktree, WorktreeState
from .decider import (
    DecidePrepareResult,
    DecideCleanupResult,
    decide_prepare,
    decide_cleanup,
)
from .handler import prepare_worktrees, cleanup_worktree

__all__ = [
    "resolve_worktrees",
    "resolve_story_worktree",
    "WorktreeState",
    "DecidePrepareResult",
    "DecideCleanupResult",
    "decide_prepare",
    "decide_cleanup",
    "prepare_worktrees",
    "cleanup_worktree",
]
