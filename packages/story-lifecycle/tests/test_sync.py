"""Tests for TAPD sync service."""

from story_lifecycle.infra.db import models as db
from story_lifecycle.sourcing.sources.base import SourceItem


class TestSyncService:
    def test_sync_creates_new_stories(self, isolated_story_home):
        from story_lifecycle.orchestrator.service.sync_service import sync_tapd

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
        from story_lifecycle.orchestrator.service.sync_service import sync_tapd

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
        from story_lifecycle.orchestrator.service.sync_service import sync_tapd

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
        from story_lifecycle.orchestrator.service.sync_service import sync_tapd

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
        from story_lifecycle.orchestrator.service.sync_service import sync_tapd

        result = sync_tapd([], workspace="/tmp/test-ws")
        assert result["created"] == 0
        assert result["updated"] == 0


class TestStateMapping:
    """状态治理:TAPD status → lifecycle_state 映射(state_map)+ 防回退。

    2026-08-28 对齐 workspace 44381896 自定义工作流(印闪 SOP V1.7):自定义状态是
    不透明 status_N(非中文、非老系统枚举),映射表换真实 key。防回退沿 story_states
    的 next 链判断。
    """

    def _item(self, item_id, item_type="requirement", status="", title="t"):
        return SourceItem(
            id=item_id,
            source="tapd",
            item_type=item_type,
            title=title,
            description="",
            status=status,
            extra={},
        )

    def test_new_story_resolved_maps_to_jiexiang(self, isolated_story_home):
        """新建 story:TAPD resolved(已实现) → lifecycle_state=结项(从无到有,无防回退)。"""
        from story_lifecycle.orchestrator.service.sync_service import sync_tapd

        items = [self._item("5001", "requirement", status="resolved", title="已实现需求")]
        sync_tapd(items, workspace="/tmp/ws")
        s = db.get_story("tapd-5001")
        assert s["lifecycle_state"] == "结项"

    def test_new_story_custom_status_3_maps_to_kaifa(self, isolated_story_home):
        """新建 story:自定义 status_3(开发中) → 开发。SOP V1.7 开发段。"""
        from story_lifecycle.orchestrator.service.sync_service import sync_tapd

        items = [self._item("5006", "requirement", status="status_3", title="开发中需求")]
        sync_tapd(items, workspace="/tmp/ws")
        assert db.get_story("tapd-5006")["lifecycle_state"] == "开发"

    def test_new_story_smoke_passed_maps_to_ceshi(self, isolated_story_home):
        """新建 story:status_40(冒烟测试通过,SOP V1.7 新增) → 测试。"""
        from story_lifecycle.orchestrator.service.sync_service import sync_tapd

        items = [self._item("5007", "requirement", status="status_40", title="冒烟通过")]
        sync_tapd(items, workspace="/tmp/ws")
        assert db.get_story("tapd-5007")["lifecycle_state"] == "测试"

    def test_new_story_release_pending_maps_to_shangxian(self, isolated_story_home):
        """新建 story:status_7(待发布) → 上线。"""
        from story_lifecycle.orchestrator.service.sync_service import sync_tapd

        items = [self._item("5008", "requirement", status="status_7", title="待发布")]
        sync_tapd(items, workspace="/tmp/ws")
        assert db.get_story("tapd-5008")["lifecycle_state"] == "上线"

    def test_new_bug_in_progress_maps_to_kaifa(self, isolated_story_home):
        """新建 bug:TAPD in_progress(处理中) → lifecycle_state=开发。

        旧 key assigned/resolving 在 44381896 缺陷工作流(系统枚举)里不存在。
        """
        from story_lifecycle.orchestrator.service.sync_service import sync_tapd

        items = [self._item("bug_6001", "bug", status="in_progress", title="处理中")]
        sync_tapd(items, workspace="/tmp/ws")
        s = db.get_story("tapd-bug_6001")
        assert s["lifecycle_state"] == "开发"

    def test_update_maps_resolved_to_jiexiang(self, isolated_story_home):
        """已有 story(开发态)同步 resolved(已实现) → 推进到结项。"""
        from story_lifecycle.orchestrator.service.sync_service import sync_tapd

        db.upsert_story_from_source(
            source_type="tapd", source_id="5002", title="进行中", tapd_status="status_3"
        )
        db.update_story("tapd-5002", lifecycle_state="开发")
        items = [self._item("5002", "requirement", status="resolved", title="已实现")]
        sync_tapd(items, workspace="/tmp/ws")
        assert db.get_story("tapd-5002")["lifecycle_state"] == "结项"

    def test_is_forward_prevents_regression(self, isolated_story_home):
        """防回退:story 已在'上线',同步映射到'开发'不写(防回退)。"""
        from story_lifecycle.orchestrator.service.sync_service import sync_tapd

        db.upsert_story_from_source(
            source_type="tapd", source_id="5003", title="已上线"
        )
        db.update_story("tapd-5003", lifecycle_state="上线")
        # status_3(开发中) 映射到"开发",但当前是"上线",开发不在上线的 next 链上 → 不写
        items = [self._item("5003", "requirement", status="status_3", title="更新")]
        sync_tapd(items, workspace="/tmp/ws")
        assert db.get_story("tapd-5003")["lifecycle_state"] == "上线"  # 未回退

    def test_unmapped_status_leaves_lifecycle_unchanged(self, isolated_story_home):
        """未命中映射表的 tapd_status(如前段产品态 status_17 待规划)不动 lifecycle_state。"""
        from story_lifecycle.orchestrator.service.sync_service import sync_tapd

        db.upsert_story_from_source(
            source_type="tapd", source_id="5004", title="新需求"
        )
        db.update_story("tapd-5004", lifecycle_state="测试")
        items = [self._item("5004", "requirement", status="status_17", title="更新")]
        sync_tapd(items, workspace="/tmp/ws")
        assert db.get_story("tapd-5004")["lifecycle_state"] == "测试"  # 未动

    def test_status_only_backfills_lifecycle(self, isolated_story_home):
        """存量回填:已有 story 重跑同步,走更新分支按映射刷新 lifecycle_state。"""
        from story_lifecycle.orchestrator.service.sync_service import sync_tapd

        # 模拟存量:tapd_status=resolved 但 lifecycle_state 还停在开发(历史未映射)
        db.upsert_story_from_source(
            source_type="tapd", source_id="5005", title="存量", tapd_status="resolved"
        )
        db.update_story("tapd-5005", lifecycle_state="开发")
        items = [self._item("5005", "requirement", status="resolved", title="存量")]
        sync_tapd(items, workspace="/tmp/ws", status_only=True)
        assert db.get_story("tapd-5005")["lifecycle_state"] == "结项"

    def test_status_names_persisted(self, isolated_story_home):
        """status_names 字典随同步落 tapd_status_name 列(自定义 status_N 展示译名)。"""
        from story_lifecycle.orchestrator.service.sync_service import sync_tapd

        items = [self._item("5009", "requirement", status="status_3", title="开发中")]
        sync_tapd(
            items, workspace="/tmp/ws", status_names={"status_3": "开发中"}
        )
        assert db.get_story("tapd-5009")["tapd_status_name"] == "开发中"


class TestSopPauseStates:
    """SOP 全局旁路:status_10(已暂缓) → 本地 pause;离开暂缓态只 resume
    由 TAPD 暂缓的(reason=tapd_suspended),手动 pause 不动。"""

    def _item(self, item_id, status, title="t"):
        return SourceItem(
            id=item_id,
            source="tapd",
            item_type="requirement",
            title=title,
            description="",
            status=status,
            extra={},
        )

    def test_suspend_pauses_active_story(self, isolated_story_home):
        from story_lifecycle.orchestrator.service.sync_service import sync_tapd

        db.upsert_story_from_source(
            source_type="tapd", source_id="6002", title="开发中", tapd_status="status_3"
        )
        db.update_story("tapd-6002", lifecycle_state="开发")
        sync_tapd([self._item("6002", "status_10")], workspace="/tmp/ws")
        s = db.get_story("tapd-6002")
        assert s["status"] == "paused"
        assert "tapd_suspended" in (s["context_json"] or "")
        # 暂缓不映射 lifecycle_state(仍开发)
        assert s["lifecycle_state"] == "开发"

    def test_resume_only_tapd_suspended(self, isolated_story_home):
        from story_lifecycle.orchestrator.service.sync_service import sync_tapd

        db.upsert_story_from_source(
            source_type="tapd", source_id="6003", title="暂缓中", tapd_status="status_10"
        )
        db.update_story("tapd-6003", lifecycle_state="开发")
        from story_lifecycle.sourcing.state_machine import pause as sm_pause

        sm_pause("tapd-6003", reason="tapd_suspended")
        # TAPD 离开暂缓态(回到开发中) → 自动恢复 active
        sync_tapd([self._item("6003", "status_3")], workspace="/tmp/ws")
        s = db.get_story("tapd-6003")
        assert s["status"] == "active"
        assert "tapd_suspended" not in (s["context_json"] or "")

    def test_manual_pause_not_resumed(self, isolated_story_home):
        from story_lifecycle.orchestrator.service.sync_service import sync_tapd

        db.upsert_story_from_source(
            source_type="tapd", source_id="6004", title="手动暂停", tapd_status="status_3"
        )
        from story_lifecycle.sourcing.state_machine import pause as sm_pause

        sm_pause("tapd-6004", reason="manual_fail")
        sync_tapd([self._item("6004", "status_3")], workspace="/tmp/ws")
        assert db.get_story("tapd-6004")["status"] == "paused"  # 手动 pause 不动

    def test_new_story_synced_paused(self, isolated_story_home):
        """新同步进来就已是暂缓态 → 创建后直接 pause(编排线程 no-op)。"""
        from story_lifecycle.orchestrator.service.sync_service import sync_tapd

        sync_tapd([self._item("6005", "status_10")], workspace="/tmp/ws")
        s = db.get_story("tapd-6005")
        assert s["status"] == "paused"
        assert "tapd_suspended" in (s["context_json"] or "")


class TestTapdSourceFetchAll:
    def test_fetch_all_overrides_status_filter(self):
        from unittest.mock import patch

        from story_lifecycle.sourcing.sources.tapd_source import TapdSource

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
