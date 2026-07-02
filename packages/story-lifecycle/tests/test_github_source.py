# tests/test_github_source.py
"""Tests for GithubSource — data source adapter + dual-write sync."""

from unittest.mock import MagicMock, patch

import pytest

from story_lifecycle.sourcing.sources.github_cli import GithubCli
from story_lifecycle.sourcing.sources.github_source import GithubSource


@pytest.fixture
def mock_cli():
    return MagicMock(spec=GithubCli)


@pytest.fixture
def source(mock_cli):
    with patch(
        "story_lifecycle.sourcing.sources.github_source.GithubCli", return_value=mock_cli
    ):
        src = GithubSource({"repo": "owner/repo", "accept_label": "lifecycle:accepted"})
    src._cli = mock_cli
    return src


class TestFetchPending:
    def test_returns_source_items(self, source, mock_cli):
        mock_cli.list_issues.return_value = [
            {
                "number": 1,
                "title": "Implement login",
                "body": "## Requirements\nUse OAuth2",
                "labels": [{"name": "lifecycle:accepted"}, {"name": "type:bug"}],
                "assignees": [{"login": "alice"}],
                "state": "open",
                "milestone": {"title": "v1.0"},
            }
        ]
        items = source.fetch_pending()
        assert len(items) == 1
        assert items[0].id == "1"
        assert items[0].source == "github"
        assert items[0].item_type == "bug"
        assert items[0].title == "Implement login"
        assert items[0].owner == "alice"

    def test_default_type_is_requirement(self, source, mock_cli):
        mock_cli.list_issues.return_value = [
            {
                "number": 2,
                "title": "Add feature",
                "body": "",
                "labels": [{"name": "lifecycle:accepted"}],
                "assignees": [],
                "state": "open",
                "milestone": None,
            }
        ]
        items = source.fetch_pending()
        assert items[0].item_type == "requirement"

    def test_empty_list_on_no_issues(self, source, mock_cli):
        mock_cli.list_issues.return_value = []
        assert source.fetch_pending() == []

    def test_fetch_failure_returns_empty(self, source, mock_cli):
        from story_lifecycle.sourcing.sources.github_cli import GithubCliError

        mock_cli.list_issues.side_effect = GithubCliError("network error")
        items = source.fetch_pending()
        assert items == []


class TestGetDetail:
    def test_returns_full_item(self, source, mock_cli):
        mock_cli.get_issue.return_value = {
            "number": 42,
            "title": "Detail",
            "body": "Full body",
            "labels": [{"name": "lifecycle:accepted"}],
            "assignees": [],
            "state": "open",
            "milestone": None,
        }
        item = source.get_detail("42")
        assert item is not None
        assert item.id == "42"
        assert item.description == "Full body"

    def test_returns_none_on_failure(self, source, mock_cli):
        from story_lifecycle.sourcing.sources.github_cli import GithubCliError

        mock_cli.get_issue.side_effect = GithubCliError("not found")
        assert source.get_detail("42") is None


class TestSyncStatus:
    def test_completed_closes_and_labels(self, source, mock_cli):
        source.sync_status("1", "completed")
        mock_cli.close_issue.assert_called_once_with(1)
        mock_cli.add_label.assert_called_with(1, "lifecycle:done")

    def test_started_adds_label(self, source, mock_cli):
        source.sync_status("1", "started")
        mock_cli.add_label.assert_called_with(1, "lifecycle:implementing")

    def test_blocked_adds_label(self, source, mock_cli):
        source.sync_status("1", "blocked")
        mock_cli.add_label.assert_called_with(1, "lifecycle:blocked")

    def test_unknown_status_is_noop(self, source, mock_cli):
        source.sync_status("1", "unknown_status")
        mock_cli.add_label.assert_not_called()
        mock_cli.close_issue.assert_not_called()

    def test_removes_existing_lifecycle_labels_first(self, source, mock_cli):
        mock_cli.get_issue.return_value = {
            "number": 1,
            "title": "",
            "body": "",
            "labels": [{"name": "lifecycle:implementing"}, {"name": "other"}],
            "assignees": [],
            "state": "open",
            "milestone": None,
        }
        source.sync_status("1", "completed")
        mock_cli.remove_label.assert_called_with(1, "lifecycle:implementing")


class TestSyncContext:
    def test_posts_plan_and_review_comment(self, source, mock_cli):
        source.sync_context(
            "1",
            "design",
            {
                "plan_summary": "Design plan text",
                "review_summary": "Looks good",
            },
        )
        mock_cli.comment_issue.assert_called_once()
        body = mock_cli.comment_issue.call_args[0][1]
        assert "任务书" in body
        assert "评审" in body

    def test_posts_done_data(self, source, mock_cli):
        source.sync_context("1", "implement", {"done_data": {"result": "ok"}})
        body = mock_cli.comment_issue.call_args[0][1]
        assert "完成信号" in body

    def test_noop_on_empty_context(self, source, mock_cli):
        source.sync_context("1", "design", {})
        mock_cli.comment_issue.assert_not_called()


class TestTestConnection:
    def test_delegates_to_cli(self, source, mock_cli):
        mock_cli.test_auth.return_value = True
        assert source.test_connection() is True

    def test_false_on_failure(self, source, mock_cli):
        mock_cli.test_auth.return_value = False
        assert source.test_connection() is False
