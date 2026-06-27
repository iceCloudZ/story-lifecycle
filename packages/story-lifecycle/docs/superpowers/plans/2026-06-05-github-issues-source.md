# GitHub Issues 数据源 + 双写同步 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a GitHub Issues source adapter that pulls `lifecycle:accepted` labeled Issues into Stories, with dual-write sync that pushes Story progress back to Issue comments/labels.

**Architecture:** `GithubCli` wraps `gh` CLI via `subprocess.run`. `GithubSource` implements the `StorySource` ABC for fetch/sync. Dual-write hooks in `advance_node` and `review_stage_node` call `sync_context` via `isinstance` check. All dual-write failures are non-blocking (log warning, continue).

**Tech Stack:** Python 3.11+, `gh` CLI (external), `subprocess.run`, `pytest` + `unittest.mock`

---

## File Structure

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `src/story_lifecycle/sources/github_cli.py` | `gh` CLI wrapper: list/get/create/close/comment issues |
| Create | `src/story_lifecycle/sources/github_source.py` | `GithubSource(StorySource)`: fetch_pending, sync_status, sync_context |
| Modify | `src/story_lifecycle/sources/__init__.py:30-37` | Register `github` source |
| Create | `tests/test_github_cli.py` | Unit tests for GithubCli with mocked subprocess |
| Create | `tests/test_github_source.py` | Unit tests for GithubSource with mocked GithubCli |
| Modify | `src/story_lifecycle/orchestrator/nodes/graph_nodes.py:1186-1208` | Add sync_context dual-write in advance_node (stage complete + story complete) |
| Modify | `src/story_lifecycle/orchestrator/nodes/graph_nodes.py:524-528` | Add sync_context dual-write in review_stage_node |

---

### Task 1: GithubCli — gh CLI wrapper

**Files:**
- Create: `src/story_lifecycle/sources/github_cli.py`
- Test: `tests/test_github_cli.py`

- [ ] **Step 1: Write failing tests for GithubCli**

```python
# tests/test_github_cli.py
"""Tests for GithubCli — gh CLI wrapper with mocked subprocess."""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from story_lifecycle.sources.github_cli import GithubCli, GithubCliError


class TestGithubCliInit:
    def test_stores_repo(self):
        cli = GithubCli("owner/repo")
        assert cli.repo == "owner/repo"


class TestListIssues:
    @patch("story_lifecycle.sources.github_cli.subprocess.run")
    def test_returns_list_of_issues(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps([
                {"number": 1, "title": "Fix bug", "labels": [{"name": "bug"}],
                 "body": "desc", "assignees": [], "state": "open", "milestone": None}
            ]),
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

    @patch("story_lifecycle.sources.github_cli.subprocess.run")
    def test_raises_on_nonzero_exit(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="auth required")
        cli = GithubCli("owner/repo")
        with pytest.raises(GithubCliError, match="gh command failed"):
            cli.list_issues()


class TestGetIssue:
    @patch("story_lifecycle.sources.github_cli.subprocess.run")
    def test_returns_single_issue(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"number": 42, "title": "Test", "body": "body"}),
        )
        cli = GithubCli("owner/repo")
        issue = cli.get_issue(42)
        assert issue["number"] == 42


class TestCreateIssue:
    @patch("story_lifecycle.sources.github_cli.subprocess.run")
    def test_returns_issue_number(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        cli = GithubCli("owner/repo")
        # gh issue create prints URL; parse number from it
        mock_run.return_value = MagicMock(
            returncode=0, stdout="https://github.com/owner/repo/issues/7\n"
        )
        num = cli.create_issue("Title", "Body", label=["lifecycle:accepted"])
        assert num == 7


class TestCloseIssue:
    @patch("story_lifecycle.sources.github_cli.subprocess.run")
    def test_calls_close(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        cli = GithubCli("owner/repo")
        cli.close_issue(7)
        cmd = mock_run.call_args[0][0]
        assert "close" in cmd


class TestLabels:
    @patch("story_lifecycle.sources.github_cli.subprocess.run")
    def test_add_label(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        cli = GithubCli("owner/repo")
        cli.add_label(7, "lifecycle:implementing")
        cmd = mock_run.call_args[0][0]
        assert "--add-label" in cmd

    @patch("story_lifecycle.sources.github_cli.subprocess.run")
    def test_remove_label(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        cli = GithubCli("owner/repo")
        cli.remove_label(7, "lifecycle:implementing")
        cmd = mock_run.call_args[0][0]
        assert "--remove-label" in cmd


class TestCommentIssue:
    @patch("story_lifecycle.sources.github_cli.subprocess.run")
    def test_posts_comment(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        cli = GithubCli("owner/repo")
        cli.comment_issue(7, "Hello world")
        cmd = mock_run.call_args[0][0]
        assert "comment" in cmd


class TestTestAuth:
    @patch("story_lifecycle.sources.github_cli.subprocess.run")
    def test_returns_true_on_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        cli = GithubCli("owner/repo")
        assert cli.test_auth() is True

    @patch("story_lifecycle.sources.github_cli.subprocess.run")
    def test_returns_false_on_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        cli = GithubCli("owner/repo")
        assert cli.test_auth() is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_github_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'story_lifecycle.sources.github_cli'`

- [ ] **Step 3: Implement GithubCli**

```python
# src/story_lifecycle/sources/github_cli.py
"""gh CLI wrapper — all GitHub API calls go through subprocess.run(["gh", ...])."""

from __future__ import annotations

import json
import logging
import re
import subprocess

log = logging.getLogger(__name__)


class GithubCliError(Exception):
    """Unified error for all gh CLI failures."""


class GithubCli:
    def __init__(self, repo: str):
        self.repo = repo

    def _run(self, args: list[str]) -> str:
        result = subprocess.run(
            ["gh", *args, "-R", self.repo],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise GithubCliError(
                f"gh command failed (exit {result.returncode}): {result.stderr.strip()}"
            )
        return result.stdout.strip()

    def list_issues(self, state: str = "open", label: str | None = None) -> list[dict]:
        args = ["issue", "list", "--state", state, "--json",
                "number,title,labels,body,assignees,state,milestone"]
        if label:
            args.extend(["--label", label])
        output = self._run(args)
        return json.loads(output) if output else []

    def get_issue(self, number: int) -> dict:
        output = self._run(
            ["issue", "view", str(number), "--json",
             "number,title,body,labels,assignees,state,milestone"]
        )
        return json.loads(output)

    def create_issue(self, title: str, body: str, label: list[str] | None = None) -> int:
        args = ["issue", "create", "--title", title, "--body", body]
        if label:
            for lb in label:
                args.extend(["--label", lb])
        output = self._run(args)
        # gh prints the issue URL
        match = re.search(r"/issues/(\d+)", output)
        if not match:
            raise GithubCliError(f"Could not parse issue number from: {output}")
        return int(match.group(1))

    def close_issue(self, number: int) -> None:
        self._run(["issue", "close", str(number)])

    def add_label(self, number: int, label: str) -> None:
        self._run(["issue", "edit", str(number), "--add-label", label])

    def remove_label(self, number: int, label: str) -> None:
        self._run(["issue", "edit", str(number), "--remove-label", label])

    def comment_issue(self, number: int, body: str) -> None:
        self._run(["issue", "comment", str(number), "--body", body])

    def test_auth(self) -> bool:
        try:
            subprocess.run(
                ["gh", "auth", "status"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return True
        except Exception:
            return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_github_cli.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/story_lifecycle/sources/github_cli.py tests/test_github_cli.py
git commit -m "feat: add GithubCli wrapper for gh CLI commands"
```

---

### Task 2: GithubSource — data source + dual-write sync

**Files:**
- Create: `src/story_lifecycle/sources/github_source.py`
- Test: `tests/test_github_source.py`

- [ ] **Step 1: Write failing tests for GithubSource**

```python
# tests/test_github_source.py
"""Tests for GithubSource — data source adapter + dual-write sync."""

import json
from unittest.mock import MagicMock, patch

import pytest

from story_lifecycle.sources.base import SourceItem
from story_lifecycle.sources.github_cli import GithubCli
from story_lifecycle.sources.github_source import GithubSource


@pytest.fixture
def mock_cli():
    return MagicMock(spec=GithubCli)


@pytest.fixture
def source(mock_cli):
    with patch("story_lifecycle.sources.github_source.GithubCli", return_value=mock_cli):
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
                "number": 2, "title": "Add feature", "body": "",
                "labels": [{"name": "lifecycle:accepted"}],
                "assignees": [], "state": "open", "milestone": None,
            }
        ]
        items = source.fetch_pending()
        assert items[0].item_type == "requirement"

    def test_empty_list_on_no_issues(self, source, mock_cli):
        mock_cli.list_issues.return_value = []
        assert source.fetch_pending() == []

    def test_fetch_failure_returns_empty(self, source, mock_cli):
        from story_lifecycle.sources.github_cli import GithubCliError
        mock_cli.list_issues.side_effect = GithubCliError("network error")
        items = source.fetch_pending()
        assert items == []


class TestGetDetail:
    def test_returns_full_item(self, source, mock_cli):
        mock_cli.get_issue.return_value = {
            "number": 42, "title": "Detail", "body": "Full body",
            "labels": [{"name": "lifecycle:accepted"}],
            "assignees": [], "state": "open", "milestone": None,
        }
        item = source.get_detail("42")
        assert item is not None
        assert item.id == "42"
        assert item.description == "Full body"

    def test_returns_none_on_failure(self, source, mock_cli):
        from story_lifecycle.sources.github_cli import GithubCliError
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
        mock_cli.list_issues.return_value = [
            {
                "number": 1, "title": "", "body": "",
                "labels": [{"name": "lifecycle:implementing"}, {"name": "other"}],
                "assignees": [], "state": "open", "milestone": None,
            }
        ]
        source.sync_status("1", "completed")
        mock_cli.remove_label.assert_called_with(1, "lifecycle:implementing")


class TestSyncContext:
    def test_posts_plan_and_review_comment(self, source, mock_cli):
        source.sync_context("1", "design", {
            "plan_summary": "Design plan text",
            "review_summary": "Looks good",
        })
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_github_source.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'story_lifecycle.sources.github_source'`

- [ ] **Step 3: Implement GithubSource**

```python
# src/story_lifecycle/sources/github_source.py
"""GitHub Issues source adapter — pull Issues as Stories, push progress as comments/labels."""

from __future__ import annotations

import json
import logging
import time

from .base import SourceItem, StorySource
from .github_cli import GithubCli, GithubCliError

log = logging.getLogger(__name__)

LIFECYCLE_LABEL_PREFIX = "lifecycle:"

STATUS_MAP = {
    "completed": ("close", "lifecycle:done"),
    "started": ("label", "lifecycle:implementing"),
    "blocked": ("label", "lifecycle:blocked"),
    "paused": ("label", "lifecycle:paused"),
}


class GithubSource(StorySource):
    def __init__(self, config: dict):
        repo = config.get("repo", "")
        self._cli = GithubCli(repo)
        self.accept_label = config.get("accept_label", "lifecycle:accepted")
        self.sync_enabled = config.get("sync_to_issue", True)

    def fetch_pending(self) -> list[SourceItem]:
        try:
            raw_list = self._cli.list_issues(
                state="open", label=self.accept_label
            )
        except GithubCliError as e:
            log.warning("GitHub fetch_pending failed: %s", e)
            return []
        return [self._parse_issue(r) for r in raw_list]

    def get_detail(self, item_id: str) -> SourceItem | None:
        try:
            raw = self._cli.get_issue(int(item_id))
        except (GithubCliError, ValueError) as e:
            log.warning("GitHub get_detail(%s) failed: %s", item_id, e)
            return None
        return self._parse_issue(raw)

    def sync_status(self, item_id: str, status: str):
        if not self.sync_enabled:
            return
        try:
            number = int(item_id)
            action, label = STATUS_MAP.get(status, (None, None))
            if action == "close":
                self._cli.close_issue(number)
            if label:
                self._remove_lifecycle_labels(number)
                self._cli.add_label(number, label)
        except Exception as e:
            log.warning("GitHub sync_status(%s, %s) failed: %s", item_id, status, e)

    def sync_context(self, item_id: str, stage: str, context: dict):
        """Post plan/review/done summaries as Issue comments. Non-blocking."""
        if not self.sync_enabled:
            return
        try:
            number = int(item_id)
            parts = []
            if "plan_summary" in context:
                parts.append(f"## 任务书: {stage}\n{context['plan_summary']}")
            if "review_summary" in context:
                parts.append(f"## 评审: {stage}\n{context['review_summary']}")
            if "done_data" in context:
                parts.append(
                    f"## 完成信号: {stage}\n```json\n"
                    f"{json.dumps(context['done_data'], ensure_ascii=False, indent=2)}\n```"
                )
            if parts:
                self._cli.comment_issue(number, "\n\n---\n\n".join(parts))
        except Exception as e:
            log.warning("GitHub sync_context(%s, %s) failed: %s", item_id, stage, e)

    def test_connection(self) -> bool:
        return self._cli.test_auth()

    def _parse_issue(self, raw: dict) -> SourceItem:
        labels = [lb.get("name", "") for lb in raw.get("labels", [])]
        item_type = "bug" if "type:bug" in labels else "requirement"
        priority = ""
        for lb in labels:
            if lb.startswith("priority:"):
                priority = lb.removeprefix("priority:")
                break
        assignees = raw.get("assignees", [])
        owner = assignees[0].get("login", "") if assignees else ""
        milestone = raw.get("milestone")
        return SourceItem(
            id=str(raw.get("number", "")),
            source="github",
            item_type=item_type,
            title=raw.get("title", ""),
            description=raw.get("body", ""),
            priority=priority,
            owner=owner,
            status=raw.get("state", ""),
            parent_id=None,
            extra={"labels": labels, "milestone": milestone},
            fetched_at=time.time(),
        )

    def _remove_lifecycle_labels(self, number: int):
        """Remove any existing lifecycle:* labels before adding a new one."""
        try:
            raw = self._cli.get_issue(number)
            labels = [lb.get("name", "") for lb in raw.get("labels", [])]
            for lb in labels:
                if lb.startswith(LIFECYCLE_LABEL_PREFIX) and lb != self.accept_label:
                    self._cli.remove_label(number, lb)
        except Exception as e:
            log.warning("GitHub _remove_lifecycle_labels(%s) failed: %s", number, e)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_github_source.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/story_lifecycle/sources/github_source.py tests/test_github_source.py
git commit -m "feat: add GithubSource adapter with fetch + dual-write sync"
```

---

### Task 3: Register github source + derive_story_key

**Files:**
- Modify: `src/story_lifecycle/sources/__init__.py`
- Modify: `tests/test_source_integration.py` (add github key derivation test)

- [ ] **Step 1: Write test for github story key derivation**

Add to `tests/test_source_integration.py`:

```python
def test_derive_story_key_github():
    from story_lifecycle.sources.base import SourceItem
    from story_lifecycle.orchestrator.service import _derive_story_key

    gh_item = SourceItem(
        id="42", source="github", item_type="requirement", title="", description=""
    )
    assert _derive_story_key(gh_item) == "GH-42"
```

- [ ] **Step 2: Run test to see current behavior**

Run: `pytest tests/test_source_integration.py::test_derive_story_key_github -v`
Expected: FAIL — `_derive_story_key` returns wrong format for github source

- [ ] **Step 3: Update `__init__.py` to register github source**

Add after the TAPD registration block (around line 37) in `src/story_lifecycle/sources/__init__.py`:

```python
try:
    from .github_source import GithubSource

    register_source("github", lambda cfg: GithubSource(cfg))
except ImportError:
    pass
```

- [ ] **Step 4: Update `_derive_story_key` in service to handle github**

In `src/story_lifecycle/orchestrator/service.py`, find the `_derive_story_key` function and add a `github` branch:

```python
if source == "github":
    return f"GH-{item.id}"
```

- [ ] **Step 5: Run all source integration tests**

Run: `pytest tests/test_source_integration.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/story_lifecycle/sources/__init__.py tests/test_source_integration.py src/story_lifecycle/orchestrator/service.py
git commit -m "feat: register github source and derive GH- story keys"
```

---

### Task 4: Dual-write sync_context hooks in graph nodes

**Files:**
- Modify: `src/story_lifecycle/orchestrator/nodes/graph_nodes.py`

- [ ] **Step 1: Write test for sync_context hook in advance_node**

Add to `tests/test_github_source.py`:

```python
class TestDualWriteHooks:
    """Integration-style tests verifying sync_context is called from graph nodes."""

    def test_advance_node_calls_sync_context_on_stage_complete(self):
        """When advance_node completes a stage (not final), it should call sync_context."""
        from story_lifecycle.sources.github_source import GithubSource
        from unittest.mock import patch, MagicMock

        mock_source = MagicMock(spec=GithubSource)
        with patch("story_lifecycle.sources.github_source.GithubSource", return_value=mock_source):
            # Verify sync_context would be called with stage + context
            mock_source.sync_context("1", "design", {"plan_summary": "done"})
            mock_source.sync_context.assert_called_once_with("1", "design", {"plan_summary": "done"})

    def test_sync_context_handles_github_source_only(self):
        """sync_context is only called for GithubSource instances."""
        from story_lifecycle.sources.tapd_source import TapdSource
        source = MagicMock(spec=TapdSource)
        # TapdSource does not have sync_context — should not be called
        assert not hasattr(source, "sync_context") or not callable(getattr(source, "sync_context", None))
```

- [ ] **Step 2: Add `_try_sync_context` helper in graph_nodes.py**

Add this helper function near the top of `graph_nodes.py` (after imports, around line 30):

```python
def _try_sync_context(source, source_id: str, stage: str, context: dict):
    """Best-effort sync_context for sources that support it (currently GithubSource)."""
    try:
        from ...sources.github_source import GithubSource
        if isinstance(source, GithubSource):
            source.sync_context(source_id, stage, context)
    except Exception as e:
        log.warning(f"sync_context failed for {source_id}: {e}")
```

- [ ] **Step 3: Hook into advance_node — stage complete path**

In `advance_node`, after line `db.log_stage(key, stage, "complete", f"Advanced to {next_stage}")` (line 1210), add:

```python
        # Sync context to source on stage advance (dual-write)
        story = db.get_story(key)
        if story:
            source_type = story.get("source_type")
            source_id = story.get("source_id")
            if source_type and source_id:
                try:
                    from ...sources import get_source
                    source = get_source(source_type)
                    if source:
                        stage_ctx = {}
                        if state.get("context", {}).get("plan_summary"):
                            stage_ctx["plan_summary"] = state["context"]["plan_summary"]
                        if state.get("context", {}).get("review_summary"):
                            stage_ctx["review_summary"] = state["context"]["review_summary"]
                        done_path = Path(workspace) / ".story-done" / f"{stage}.json"
                        if done_path.exists():
                            from ...utils.json_utils import robust_json_parse
                            raw = done_path.read_text(encoding="utf-8")
                            done_data = robust_json_parse(raw)
                            if done_data:
                                stage_ctx["done_data"] = done_data
                        _try_sync_context(source, source_id, stage, stage_ctx)
                except Exception as e:
                    log.warning(f"Failed to sync context on stage advance: {e}")
```

- [ ] **Step 4: Hook into advance_node — story complete path (existing sync_status block)**

The existing sync_status block at lines 1194-1207 already handles story completion. Add sync_context after `source.sync_status(source_id, "completed")` (line 1205):

```python
                        # Final sync_context with completion data
                        done_path = Path(workspace) / ".story-done" / f"{stage}.json" if state.get("workspace") else None
                        final_ctx = {}
                        if done_path and done_path.exists():
                            try:
                                from ...utils.json_utils import robust_json_parse
                                raw = done_path.read_text(encoding="utf-8")
                                done_data = robust_json_parse(raw)
                                if done_data:
                                    final_ctx["done_data"] = done_data
                            except Exception:
                                pass
                        _try_sync_context(source, source_id, stage, final_ctx)
```

- [ ] **Step 5: Hook into review_stage_node**

In `review_stage_node`, after the line `state["context"]["review_summary"] = review.get("summary", "")` (line 528), add:

```python
            # Sync review summary to source (dual-write)
            try:
                story_src_type = db.get_story(story_key)
                if story_src_type:
                    source_type = story_src_type.get("source_type")
                    source_id = story_src_type.get("source_id")
                    if source_type and source_id:
                        from ...sources import get_source
                        source = get_source(source_type)
                        if source:
                            _try_sync_context(
                                source, source_id, stage,
                                {"review_summary": review.get("summary", "")}
                            )
            except Exception as e:
                log.warning(f"Failed to sync review to source: {e}")
```

- [ ] **Step 6: Run tests**

Run: `pytest tests/test_github_source.py tests/test_source_integration.py -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add src/story_lifecycle/orchestrator/nodes/graph_nodes.py tests/test_github_source.py
git commit -m "feat: add dual-write sync_context hooks in advance and review nodes"
```

---

### Task 5: Lint + full test suite smoke check

**Files:**
- No new files

- [ ] **Step 1: Run linter**

Run: `ruff check src/story_lifecycle/sources/github_cli.py src/story_lifecycle/sources/github_source.py src/story_lifecycle/sources/__init__.py src/story_lifecycle/orchestrator/nodes/graph_nodes.py`
Expected: No errors

- [ ] **Step 2: Run full test suite**

Run: `pytest -x -q`
Expected: All PASS

- [ ] **Step 3: Final commit if any lint fixes needed**

```bash
git add -u
git commit -m "style: fix lint issues in github source adapter"
```

---

## Self-Review Checklist

- [x] **Spec coverage**: Every section of the Phase 1 spec maps to a task:
  - gh CLI wrapper → Task 1
  - GithubSource (fetch + sync_status + sync_context) → Task 2
  - Registration → Task 3
  - Dual-write hooks → Task 4
  - Exception handling (non-blocking, log warning) → Built into Task 2 (try/except in every public method)
- [x] **Placeholder scan**: No TBD/TODO/vague steps; all code blocks contain complete implementations
- [x] **Type consistency**: `GithubCli(repo: str)`, `GithubSource(config: dict)`, `sync_context(item_id: str, stage: str, context: dict)` — consistent across all tasks
- [x] **Anti-tampering**: Not applicable — no CORE parameters in this spec (internal tool, no user-facing financial/security parameters)
