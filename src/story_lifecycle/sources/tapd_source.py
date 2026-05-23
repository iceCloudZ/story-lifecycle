from __future__ import annotations

import logging
import time

from .base import SourceItem, StorySource
from .tapd_api import TapdApi

log = logging.getLogger(__name__)


class TapdSource(StorySource):
    def __init__(self, config: dict):
        self._api = TapdApi(
            workspace_id=config.get("workspace_id", ""),
        )
        self.owner = config.get("owner", "")
        if self.owner and not self.owner.endswith(";"):
            self.owner += ";"
        self.story_status_filter = config.get(
            "story_status", "open,progressing,reopened"
        )
        self.bug_status_filter = config.get(
            "bug_status", "new,reopened,assigned,resolving"
        )

    def fetch_pending(self) -> list[SourceItem]:
        items = []
        if self.story_status_filter:
            items.extend(self._fetch_stories())
        if self.bug_status_filter:
            items.extend(self._fetch_bugs())
        return items

    def _fetch_stories(self) -> list[SourceItem]:
        seen_ids: set[str] = set()
        results: list[SourceItem] = []
        statuses = [s.strip() for s in self.story_status_filter.split(",") if s.strip()]
        if not statuses or "*" in statuses:
            statuses = [None]  # single pass without status filter
        for status in statuses:
            params = {
                "entity_type": "stories",
                "limit": 20,
                "owner": self.owner,
                "parent_id": "0",
            }
            if status:
                params["status"] = status
            raw_list = self._api.get_stories(params)
            for r in raw_list:
                flat = r.get("Story", r)
                item = self._parse_story(flat)
                if item.id not in seen_ids:
                    seen_ids.add(item.id)
                    results.append(item)
        return results

    def _fetch_bugs(self) -> list[SourceItem]:
        seen_ids: set[str] = set()
        results: list[SourceItem] = []
        statuses = [s.strip() for s in self.bug_status_filter.split(",") if s.strip()]
        if not statuses or "*" in statuses:
            statuses = [None]  # single pass without status filter
        for status in statuses:
            params = {
                "limit": 20,
                "owner": self.owner,
            }
            if status:
                params["status"] = status
            raw_list = self._api.get_bugs(params)
            for r in raw_list:
                flat = r.get("Bug", r)
                item = self._parse_bug(flat)
                if item.id not in seen_ids:
                    seen_ids.add(item.id)
                    results.append(item)
        return results

    def get_detail(self, item_id: str) -> SourceItem | None:
        if item_id.startswith("bug_"):
            raw = self._api.get_bug_detail(item_id.removeprefix("bug_"))
            return self._parse_bug(raw) if raw else None
        raw = self._api.get_story_detail(item_id)
        return self._parse_story(raw) if raw else None

    def sync_status(self, item_id: str, status: str):
        TAPD_STATUS_MAP = {
            "completed": "done",
            "blocked": "reopen",
            "aborted": "postponed",
        }
        tapd_status = TAPD_STATUS_MAP.get(status)
        if not tapd_status:
            return
        if item_id.startswith("bug_"):
            self._api.update_bug(item_id.removeprefix("bug_"), {"status": tapd_status})
        else:
            self._api.update_story(item_id, {"status": tapd_status})

    def test_connection(self) -> bool:
        try:
            self._api.get_stories({"limit": 1})
            return True
        except Exception:
            return False

    def _parse_story(self, raw: dict) -> SourceItem:
        full_id = str(raw.get("id", ""))
        ws_id = str(self._api.workspace_id)
        short_id = (
            full_id[len(ws_id) + 3 :].lstrip("0")
            if len(full_id) > len(ws_id) + 3
            else full_id
        )
        return SourceItem(
            id=full_id,
            source="tapd",
            item_type="requirement",
            title=raw.get("name", ""),
            description=raw.get("description", ""),
            priority=raw.get("priority_label", ""),
            owner=raw.get("owner", ""),
            status=raw.get("status", ""),
            parent_id=None,
            extra={
                "short_id": short_id,
                "category": raw.get("category_name", ""),
                "iteration_id": raw.get("iteration_id", ""),
            },
            fetched_at=time.time(),
        )

    def _parse_bug(self, raw: dict) -> SourceItem:
        return SourceItem(
            id=f"bug_{raw.get('id', '')}",
            source="tapd",
            item_type="bug",
            title=raw.get("title", ""),
            description=raw.get("description", ""),
            priority=raw.get("priority_label", ""),
            owner=raw.get("current_owner", ""),
            status=raw.get("status", ""),
            parent_id=raw.get("story_id", None),
            extra={"severity": raw.get("severity", "")},
            fetched_at=time.time(),
        )
