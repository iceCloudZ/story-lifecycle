"""Tests for get_story_workspace_diff project_id scoping (multi-project diff).

Covers: project_id selects the right binding, prefers worktree_path over
repo_path, falls back to repo_path when worktree is unprepared/missing, and
the response carries project_id/repo_path/worktree_path for the frontend.
"""

import subprocess

import pytest

from story_lifecycle.infra.db import models as db
from story_lifecycle.sourcing.workspace_diff import (
    _pick_repo_and_branches,
    get_story_workspace_diff,
)


def _make_git_repo(path):
    """Create a real git repo at path with an initial commit on main."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=str(path), check=True)
    (path / "README.md").write_text("hello", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(path), check=True, capture_output=True)


def _make_worktree(repo, wt_path, branch):
    """Create a git worktree + branch off repo's main."""
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(wt_path)],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


class TestPickRepoAndBranches:
    def test_project_id_prefers_worktree(self, isolated_story_home, tmp_path):
        repo_a = tmp_path / "repo-a"
        repo_b = tmp_path / "repo-b"
        _make_git_repo(repo_a)
        _make_git_repo(repo_b)
        db.create_story(story_key="S", title="t", workspace=str(tmp_path / "ws"))
        db.create_project(name="pa", repo_path=str(repo_a))
        db.create_project(name="pb", repo_path=str(repo_b))
        wt_b = tmp_path / "wt-b"
        _make_worktree(repo_b, wt_b, "feat/b")
        db.bind_story_project(
            "S", 2, branch="feat/b", base_branch="main",
            worktree_path=str(wt_b), worktree_state="available",
        )

        repo, src, base, pid, wt = _pick_repo_and_branches("S", 2)
        assert pid == 2
        assert wt == str(wt_b)          # used the worktree, not main repo
        assert str(repo) == str(wt_b)
        assert src == "feat/b"
        assert base == "main"

    def test_project_id_falls_back_to_repo_when_worktree_missing(
        self, isolated_story_home, tmp_path
    ):
        """worktree_path set but doesn't exist on disk → fallback to repo_path, wt=None."""
        repo_a = tmp_path / "repo-a"
        _make_git_repo(repo_a)
        db.create_story(story_key="S", title="t", workspace=str(tmp_path / "ws"))
        db.create_project(name="pa", repo_path=str(repo_a))
        db.bind_story_project(
            "S", 1, branch="feat/a", base_branch="main",
            worktree_path=str(tmp_path / "gone"), worktree_state="unprepared",
        )

        repo, src, base, pid, wt = _pick_repo_and_branches("S", 1)
        assert pid == 1
        assert wt is None                  # worktree unusable → flagged None
        assert str(repo) == str(repo_a)    # fell back to main repo

    def test_project_id_unknown_binding_raises(self, isolated_story_home, tmp_path):
        repo_a = tmp_path / "repo-a"
        _make_git_repo(repo_a)
        db.create_story(story_key="S", title="t", workspace=str(tmp_path / "ws"))
        db.create_project(name="pa", repo_path=str(repo_a))
        db.bind_story_project("S", 1, branch="feat/a")
        with pytest.raises(ValueError, match="no binding for project_id=999"):
            _pick_repo_and_branches("S", 999)

    def test_no_project_id_legacy_first_viable(
        self, isolated_story_home, tmp_path
    ):
        """project_id=None: use workspace if it's a git repo (legacy behaviour)."""
        ws = tmp_path / "ws"
        _make_git_repo(ws)  # workspace itself is the git repo
        db.create_story(story_key="S", title="t", workspace=str(ws))
        repo, src, base, pid, wt = _pick_repo_and_branches("S", None)
        assert str(repo) == str(ws)
        assert wt is None   # legacy path never reports a worktree


class TestGetStoryWorkspaceDiffFields:
    def test_diff_response_carries_project_fields(
        self, isolated_story_home, tmp_path
    ):
        """The returned dict has project_id/repo_path/worktree_path for the frontend."""
        repo_a = tmp_path / "repo-a"
        _make_git_repo(repo_a)
        wt_a = tmp_path / "wt-a"
        _make_worktree(repo_a, wt_a, "feat/a")
        db.create_story(story_key="S", title="t", workspace=str(tmp_path / "ws"))
        db.create_project(name="pa", repo_path=str(repo_a))
        db.bind_story_project(
            "S", 1, branch="feat/a", base_branch="main",
            worktree_path=str(wt_a), worktree_state="available",
        )

        result = get_story_workspace_diff("S", project_id=1)
        assert result["project_id"] == 1
        assert result["worktree_path"] == str(wt_a)
        assert result["repo_path"] == str(wt_a)
        assert result["source"] == "local"
