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

    def test_upsert_idempotent_when_key_exists_without_source_id(self, isolated_story_home):
        """Regression: a hand-created story with story_key='tapd-X' but no
        source_id must not UNIQUE-crash when upsert links the same source.
        Root cause: create_story() doesn't store source_type/source_id, so
        find_by_source_id misses it and the create branch hits UNIQUE."""
        db.create_story(
            story_key="tapd-9999",
            title="手工创建",
            workspace="/tmp/ws",
            current_stage="design",
        )
        story, created = db.upsert_story_from_source(
            source_type="tapd",
            source_id="9999",
            title="更新标题",
            workspace="/tmp/ws",
        )
        assert created is False
        assert story["story_key"] == "tapd-9999"
        assert story["source_id"] == "9999"  # source_id now linked

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


class TestTapdSourceFetchAll:
    def test_fetch_all_overrides_status_filter(self):
        from unittest.mock import patch

        from story_lifecycle.sources.tapd_source import TapdSource

        source = TapdSource(
            {
                "workspace_id": "12345",
                "story_status": "open",
                "bug_status": "new",
            }
        )

        assert source.story_status_filter == "open"
        assert source.bug_status_filter == "new"

        with (
            patch.object(source, "_fetch_stories", return_value=[]) as mock_s,
            patch.object(source, "_fetch_bugs", return_value=[]) as mock_b,
        ):
            source.fetch_pending(fetch_all=True)
            mock_s.assert_called_once()
            mock_b.assert_called_once()

        assert source.story_status_filter == "open"
        assert source.bug_status_filter == "new"
