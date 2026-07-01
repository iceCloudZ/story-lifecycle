"""Tests for project_registry module."""

import subprocess

from story_lifecycle.orchestrator.workspace.project_registry import (
    register_project,
    check_project_availability,
    add_runtime_fact,
)


class TestProjectRegistry:
    def test_register_project_normalizes_path(self, isolated_story_home, tmp_path):
        """repo_path should be resolved to absolute path via Path.resolve()."""
        subdir = tmp_path / "my-project"
        subdir.mkdir()

        # Use a relative path like "." or "subdir"
        project = register_project(
            name="test-proj",
            repo_path=str(subdir / ".." / "my-project"),
        )

        assert project["name"] == "test-proj"
        assert project["repo_path"] == str(subdir.resolve())
        assert project["availability"] == "unknown"

    def test_register_project_missing_path_sets_availability(
        self, isolated_story_home, tmp_path
    ):
        """When the path does not exist, availability should be 'missing'."""
        nonexistent = tmp_path / "does-not-exist"
        assert not nonexistent.exists()

        project = register_project(
            name="ghost-proj",
            repo_path=str(nonexistent),
        )

        assert project["availability"] == "missing"
        assert project["availability_reason"] is not None
        assert "does not exist" in project["availability_reason"].lower()

    def test_check_availability_valid_git_repo(self, isolated_story_home, tmp_path):
        """A valid git repo should report availability='available'."""
        repo_dir = tmp_path / "test-repo"
        repo_dir.mkdir()
        subprocess.run(["git", "init"], cwd=str(repo_dir), capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(repo_dir),
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(repo_dir),
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=str(repo_dir),
            capture_output=True,
        )

        project = register_project(
            name="git-proj",
            repo_path=str(repo_dir),
        )

        updated = check_project_availability(project["id"])
        assert updated is not None
        assert updated["availability"] == "available"

    def test_check_availability_not_git(self, isolated_story_home, tmp_path):
        """A directory that exists but is not a git repo should report unavailable."""
        plain_dir = tmp_path / "plain-dir"
        plain_dir.mkdir()

        project = register_project(
            name="plain-proj",
            repo_path=str(plain_dir),
        )

        updated = check_project_availability(project["id"])
        assert updated is not None
        assert updated["availability"] == "unavailable"

    def test_add_runtime_fact(self, isolated_story_home, tmp_path):
        """add_runtime_fact should create a runtime fact row."""
        subdir = tmp_path / "runtime-proj"
        subdir.mkdir()

        project = register_project(
            name="runtime-proj",
            repo_path=str(subdir),
        )

        fact = add_runtime_fact(
            project_id=project["id"],
            runtime_type="python",
            runtime_version="3.12.0",
            check_command="python --version",
            availability="available",
        )

        assert fact is not None
        assert fact["project_id"] == project["id"]
        assert fact["runtime_type"] == "python"
        assert fact["runtime_version"] == "3.12.0"
        assert fact["availability"] == "available"

        # Upserting the same (project_id, runtime_type) should update, not create
        fact2 = add_runtime_fact(
            project_id=project["id"],
            runtime_type="python",
            runtime_version="3.13.0",
        )

        assert fact2["id"] == fact["id"]
        assert fact2["runtime_version"] == "3.13.0"
