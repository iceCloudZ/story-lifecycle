"""Tests for the shared service layer."""

import tempfile


def test_import_service():
    from story_lifecycle.orchestrator.service.story_service import create_and_start_story

    assert callable(create_and_start_story)


def test_create_and_start_story():
    from story_lifecycle.orchestrator.service.story_service import create_and_start_story
    from story_lifecycle.db.models import get_story, init_db

    init_db()
    with tempfile.TemporaryDirectory() as tmp:
        result = create_and_start_story(
            story_key="TEST-001",
            title="Test story",
            profile="minimal",
            workspace=tmp,
        )
        assert result == "TEST-001"

        s = get_story("TEST-001")
        assert s is not None
        assert s["story_key"] == "TEST-001"
        assert s["title"] == "Test story"
        assert s["status"] == "active"


class TestStoryNewFields:
    def test_upsert_story_from_source_creates_new(self, isolated_story_home):
        from story_lifecycle.db import models as db

        story, created = db.upsert_story_from_source(
            source_type="tapd",
            source_id="1123456700001",
            title="TAPD 需求",
            deadline="2026-06-15",
            priority="高",
            owner="zhangsan",
            tapd_status="open",
            tapd_url="https://www.tapd.cn/1234/prong/stories/view/1123456700001",
        )
        assert created is True
        assert story["deadline"] == "2026-06-15"
        assert story["priority"] == "高"
        assert story["source_type"] == "tapd"

    def test_upsert_story_from_source_updates_existing(self, isolated_story_home):
        from story_lifecycle.db import models as db

        db.upsert_story_from_source(
            source_type="tapd", source_id="1123456700002", title="原始标题"
        )
        story, created = db.upsert_story_from_source(
            source_type="tapd",
            source_id="1123456700002",
            title="更新标题",
            tapd_status="progressing",
        )
        assert created is False
        assert story["title"] == "更新标题"
        assert story["tapd_status"] == "progressing"
