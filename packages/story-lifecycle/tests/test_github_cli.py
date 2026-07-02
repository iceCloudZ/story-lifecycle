"""Tests for GithubCli — gh CLI wrapper with mocked subprocess."""

import json
from unittest.mock import MagicMock, patch

import pytest

from story_lifecycle.sourcing.sources.github_cli import GithubCli, GithubCliError


class TestGithubCliInit:
    def test_stores_repo(self):
        cli = GithubCli("owner/repo")
        assert cli.repo == "owner/repo"


class TestListIssues:
    @patch("story_lifecycle.sourcing.sources.github_cli.subprocess.run")
    def test_returns_list_of_issues(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "number": 1,
                        "title": "Fix bug",
                        "labels": [{"name": "bug"}],
                        "body": "desc",
                        "assignees": [],
                        "state": "open",
                        "milestone": None,
                    }
                ]
            ),
        )
        cli = GithubCli("owner/repo")
        issues = cli.list_issues(state="open", label="lifecycle:accepted")
        assert len(issues) == 1
        assert issues[0]["number"] == 1
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "gh" in cmd
        assert "issue" in cmd
        assert "list" in cmd

    @patch("story_lifecycle.sourcing.sources.github_cli.subprocess.run")
    def test_raises_on_nonzero_exit(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="auth required")
        cli = GithubCli("owner/repo")
        with pytest.raises(GithubCliError, match="gh command failed"):
            cli.list_issues()


class TestGetIssue:
    @patch("story_lifecycle.sourcing.sources.github_cli.subprocess.run")
    def test_returns_single_issue(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"number": 42, "title": "Test", "body": "body"}),
        )
        cli = GithubCli("owner/repo")
        issue = cli.get_issue(42)
        assert issue["number"] == 42


class TestCreateIssue:
    @patch("story_lifecycle.sourcing.sources.github_cli.subprocess.run")
    def test_returns_issue_number(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="https://github.com/owner/repo/issues/7\n"
        )
        cli = GithubCli("owner/repo")
        num = cli.create_issue("Title", "Body", label=["lifecycle:accepted"])
        assert num == 7


class TestCloseIssue:
    @patch("story_lifecycle.sourcing.sources.github_cli.subprocess.run")
    def test_calls_close(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        cli = GithubCli("owner/repo")
        cli.close_issue(7)
        cmd = mock_run.call_args[0][0]
        assert "close" in cmd


class TestLabels:
    @patch("story_lifecycle.sourcing.sources.github_cli.subprocess.run")
    def test_add_label(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        cli = GithubCli("owner/repo")
        cli.add_label(7, "lifecycle:implementing")
        cmd = mock_run.call_args[0][0]
        assert "--add-label" in cmd

    @patch("story_lifecycle.sourcing.sources.github_cli.subprocess.run")
    def test_remove_label(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        cli = GithubCli("owner/repo")
        cli.remove_label(7, "lifecycle:implementing")
        cmd = mock_run.call_args[0][0]
        assert "--remove-label" in cmd


class TestCommentIssue:
    @patch("story_lifecycle.sourcing.sources.github_cli.subprocess.run")
    def test_posts_comment(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        cli = GithubCli("owner/repo")
        cli.comment_issue(7, "Hello world")
        cmd = mock_run.call_args[0][0]
        assert "comment" in cmd


class TestTestAuth:
    @patch("story_lifecycle.sourcing.sources.github_cli.subprocess.run")
    def test_returns_true_on_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        cli = GithubCli("owner/repo")
        assert cli.test_auth() is True

    @patch("story_lifecycle.sourcing.sources.github_cli.subprocess.run")
    def test_returns_false_on_failure(self, mock_run):
        # This test mocks subprocess.run to return a failure
        # In real usage, test_auth() checks the actual gh command
        mock_run.side_effect = Exception("gh command failed")
        cli = GithubCli("owner/repo")
        assert cli.test_auth() is False
