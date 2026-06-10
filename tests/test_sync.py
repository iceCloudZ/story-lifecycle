"""Tests for TAPD sync service."""

from story_lifecycle.db import models as db
from story_lifecycle.sources.base import SourceItem


class TestSyncService:
    def test_sync_creates_new_stories(self, isolated_story_home):
        from story_lifecycle.orchestrator.sync_service import sync_tapd

        items = [
            SourceItem(
                id="1001",
                source="tapd",
                item_type="requirement",
                title="用户登录",
                description="实现登录功能",
                priority="高",
                owner="zhangsan",
                deadline="2026-06-15",
                status="open",
                extra={"short_id": "1001", "url": "https://tapd.cn/1001"},
            ),
            SourceItem(
                id="bug_2001",
                source="tapd",
                item_type="bug",
                title="白屏问题",
                description="打开页面白屏",
                priority="紧急",
                owner="zhangsan",
                deadline="2026-06-11",
                status="new",
                extra={"severity": "严重", "url": "https://tapd.cn/bug/2001"},
            ),
        ]

        result = sync_tapd(items, workspace="/tmp/test-ws")

        assert result["created"] == 2
        assert result["updated"] == 0

        s1 = db.get_story("tapd-1001")
        assert s1 is not None
        assert s1["title"] == "用户登录"
        assert s1["deadline"] == "2026-06-15"
        assert s1["source_type"] == "tapd"

        s2 = db.get_story("tapd-bug_2001")
        assert s2 is not None
        assert s2["title"] == "白屏问题"

    def test_sync_updates_existing_stories(self, isolated_story_home):
        from story_lifecycle.orchestrator.sync_service import sync_tapd

        db.upsert_story_from_source(
            source_type="tapd",
            source_id="1001",
            title="旧标题",
            tapd_status="open",
        )

        items = [
            SourceItem(
                id="1001",
                source="tapd",
                item_type="requirement",
                title="新标题",
                description="更新",
                priority="高",
                deadline="2026-06-20",
                status="progressing",
                extra={"url": "https://tapd.cn/1001"},
            ),
        ]

        result = sync_tapd(items, workspace="/tmp/test-ws")
        assert result["created"] == 0
        assert result["updated"] == 1

        s = db.get_story("tapd-1001")
        assert s["title"] == "新标题"
        assert s["tapd_status"] == "progressing"
        assert s["deadline"] == "2026-06-20"

    def test_sync_dry_run_does_not_write(self, isolated_story_home):
        from story_lifecycle.orchestrator.sync_service import sync_tapd

        items = [
            SourceItem(
                id="1001",
                source="tapd",
                item_type="requirement",
                title="Dry run",
                description="",
                extra={},
            ),
        ]

        result = sync_tapd(items, workspace="/tmp/test-ws", dry_run=True)
        assert result["would_create"] == 1

        s = db.get_story("tapd-1001")
        assert s is None

    def test_sync_status_only_skips_new(self, isolated_story_home):
        from story_lifecycle.orchestrator.sync_service import sync_tapd

        db.upsert_story_from_source(
            source_type="tapd", source_id="1001", title="已存在"
        )

        items = [
            SourceItem(
                id="1001",
                source="tapd",
                item_type="requirement",
                title="更新",
                description="",
                status="done",
                extra={},
            ),
            SourceItem(
                id="9999",
                source="tapd",
                item_type="requirement",
                title="新的",
                description="",
                extra={},
            ),
        ]

        result = sync_tapd(items, workspace="/tmp/test-ws", status_only=True)
        assert result["updated"] == 1
        assert result["skipped"] == 1
        assert db.get_story("tapd-9999") is None

    def test_sync_empty_items(self, isolated_story_home):
        from story_lifecycle.orchestrator.sync_service import sync_tapd

        result = sync_tapd([], workspace="/tmp/test-ws")
        assert result["created"] == 0
        assert result["updated"] == 0
