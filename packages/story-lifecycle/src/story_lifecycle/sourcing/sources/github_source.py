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

# STATUS-CQRS-REFACTOR: 4 态映射(active/paused/completed/failed)。
# 原 blocked/started(implementing)合并:active→implementing label,paused→paused label。
STATUS_MAP = {
    "completed": ("close", "lifecycle:done"),
    "active": ("label", "lifecycle:implementing"),
    "failed": ("label", "lifecycle:failed"),
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
            raw_list = self._cli.list_issues(state="open", label=self.accept_label)
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
