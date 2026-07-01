"""Tests for worktree resolver, decider, and handler modules."""

import os
import subprocess
from pathlib import Path

from story_lifecycle.orchestrator.workspace.worktree.resolver import (
    resolve_worktrees,
)
from story_lifecycle.orchestrator.workspace.worktree.decider import (
    decide_prepare,
    decide_cleanup,
    PrepareAction,
    CleanupAction,
    RejectReason,
    CleanupRejectReason,
)
from story_lifecycle.orchestrator.workspace.worktree.handler import (
    prepare_worktrees,
)


def _init_git_repo(path: Path) -> None:
    """Initialize a git repo with an initial commit at the given path."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(path), capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(path),
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(path),
        capture_output=True,
        check=True,
    )
    # Create a file and commit so we have a valid HEAD
    (path / "README.md").write_text("# test repo")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=str(path),
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "initial commit"],
        cwd=str(path),
        capture_output=True,
        check=True,
    )


def _make_story_project_binding(**overrides) -> dict:
    """Create a dict that looks like a story_project DB row."""
    defaults = {
        "id": 1,
        "story_key": "test-story",
        "project_id": 1,
        "branch": "codex/test-story",
        "base_branch": "main",
        "base_commit": "",
        "worktree_path": "",
        "workspace_type": "worktree",
        "worktree_state": "unprepared",
        "summary": "",
        "source": "user",
        "evidence_ref": "",
    }
    defaults.update(overrides)
    return defaults


class TestWorktreeResolver:
    def test_resolve_empty_worktrees(self, tmp_path):
        """A fresh repo with no worktrees should return empty dict."""
        repo = tmp_path / "repo"
        _init_git_repo(repo)

        wts = resolve_worktrees(str(repo))
        # Main checkout is NOT listed as a worktree by `git worktree list`
        # A bare init usually lists the main checkout. But for a fresh repo,
        # there is always at least 1 (the main) worktree.
        assert len(wts) >= 1
        # The main repo path should be in the list (normalize for Windows path sep)
        main_path = os.path.normpath(str(repo))
        assert main_path in wts
        # Default branch may be "main" or "master" depending on git config
        assert wts[main_path].branch in ("main", "master")

    def test_two_stories_isolated_worktrees(self, tmp_path, isolated_story_home):
        """Two worktrees for the same project should be isolated."""
        from story_lifecycle.db import models as db

        repo = tmp_path / "repo"
        _init_git_repo(repo)

        # Detect the actual default branch name
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=True,
        )
        default_branch = result.stdout.strip()

        # Register project
        proj = db.create_project(
            name="test-proj", repo_path=str(repo), default_branch=default_branch
        )

        wt_root = tmp_path / "worktrees"
        wt_root.mkdir()

        story_a = "test-story-a"
        story_b = "test-story-b"

        db.create_story(story_a, "Story A", str(repo))
        db.update_story(story_a, intake_state="ready")
        db.create_story(story_b, "Story B", str(repo))
        db.update_story(story_b, intake_state="ready")

        wt_path_a = str(wt_root / story_a / "test-proj")
        wt_path_b = str(wt_root / story_b / "test-proj")

        db.bind_story_project(
            story_key=story_a,
            project_id=proj["id"],
            branch="codex/story-a",
            base_branch=default_branch,
            worktree_path=wt_path_a,
            worktree_state="unprepared",
        )
        db.bind_story_project(
            story_key=story_b,
            project_id=proj["id"],
            branch="codex/story-b",
            base_branch=default_branch,
            worktree_path=wt_path_b,
            worktree_state="unprepared",
        )

        results_a = prepare_worktrees(story_a, worktree_root=str(wt_root))
        results_b = prepare_worktrees(story_b, worktree_root=str(wt_root))

        assert results_a[0]["action"] == "create"
        assert results_b[0]["action"] == "create"
        assert Path(wt_path_a).exists()
        assert Path(wt_path_b).exists()
        assert wt_path_a != wt_path_b


class TestWorktreeDecider:
    def test_branch_conflict_rejected(self, tmp_path):
        """Branch already checked out in another worktree should be rejected."""
        repo = tmp_path / "repo"
        _init_git_repo(repo)

        # Create a worktree first
        wt_other = tmp_path / "other-worktree"
        wt_other.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", str(wt_other), "-b", "feature/existing"],
            cwd=str(repo),
            capture_output=True,
            check=True,
        )

        worktrees = resolve_worktrees(str(repo))

        # Simulate a story_project that wants the same branch but different path
        sp = _make_story_project_binding(
            branch="feature/existing",
            worktree_path=str(tmp_path / "different-path"),
            worktree_state="unprepared",
        )

        result = decide_prepare(sp, worktrees)
        assert result.action == PrepareAction.REJECT
        assert result.reject_reason == RejectReason.BRANCH_CHECKED_OUT_ELSEWHERE

    def test_path_conflict_rejected(self, tmp_path):
        """Path exists but is not a git worktree should be rejected."""
        repo = tmp_path / "repo"
        _init_git_repo(repo)

        # Create a plain directory (not a worktree)
        plain_dir = tmp_path / "plain-dir"
        plain_dir.mkdir()

        worktrees = resolve_worktrees(str(repo))

        sp = _make_story_project_binding(
            branch="feature/test",
            worktree_path=str(plain_dir),
            worktree_state="unprepared",
        )

        result = decide_prepare(sp, worktrees)
        assert result.action == PrepareAction.REJECT
        assert result.reject_reason == RejectReason.PATH_CONFLICT

    def test_stale_branch_rejected(self, tmp_path):
        """Worktree exists but branch doesn't match expected should be rejected."""
        repo = tmp_path / "repo"
        _init_git_repo(repo)

        # Create a worktree with a specific branch
        wt_path = tmp_path / "my-worktree"
        subprocess.run(
            ["git", "worktree", "add", str(wt_path), "-b", "feature/correct"],
            cwd=str(repo),
            capture_output=True,
            check=True,
        )

        worktrees = resolve_worktrees(str(repo))

        # Story expects a different branch at this worktree path
        sp = _make_story_project_binding(
            branch="feature/wrong-branch",
            worktree_path=str(wt_path),
        )

        result = decide_prepare(sp, worktrees)
        assert result.action == PrepareAction.REJECT
        assert result.reject_reason == RejectReason.STALE


class TestWorktreeCleanup:
    def test_dirty_worktree_no_cleanup(self, tmp_path):
        """Dirty worktree should not be allowed to clean up."""
        sp = _make_story_project_binding(
            worktree_path=str(tmp_path / "some-worktree"),
        )

        result = decide_cleanup(
            sp,
            delivery_state="merged",
            is_worktree_clean=False,
            worktree_exists=True,
        )
        assert result.action == CleanupAction.REJECT
        assert result.reject_reason == CleanupRejectReason.WORKTREE_DIRTY

    def test_delivery_not_finalized_no_cleanup(self, tmp_path):
        """Worktree with non-finalized delivery should not be cleaned."""
        sp = _make_story_project_binding(
            worktree_path=str(tmp_path / "some-worktree"),
        )

        result = decide_cleanup(
            sp,
            delivery_state="review_pending",
            is_worktree_clean=True,
            worktree_exists=True,
        )
        assert result.action == CleanupAction.REJECT
        assert result.reject_reason == CleanupRejectReason.DELIVERY_NOT_FINALIZED

    def test_clean_merged_worktree_allowed(self, tmp_path):
        """Clean worktree with merged delivery should be allowed to clean."""
        sp = _make_story_project_binding(
            worktree_path=str(tmp_path / "some-worktree"),
        )

        result = decide_cleanup(
            sp,
            delivery_state="merged",
            is_worktree_clean=True,
            worktree_exists=True,
        )
        assert result.action == CleanupAction.ALLOW

    def test_abandoned_worktree_allowed(self, tmp_path):
        """Abandoned delivery should allow cleanup."""
        sp = _make_story_project_binding(
            worktree_path=str(tmp_path / "some-worktree"),
        )

        result = decide_cleanup(
            sp,
            delivery_state="abandoned",
            is_worktree_clean=True,
            worktree_exists=True,
        )
        assert result.action == CleanupAction.ALLOW
