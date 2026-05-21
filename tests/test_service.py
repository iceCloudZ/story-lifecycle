"""Tests for the shared service layer."""

import tempfile


def test_import_service():
    from story_lifecycle.orchestrator.service import create_and_start_story

    assert callable(create_and_start_story)


def test_create_and_start_story():
    from story_lifecycle.orchestrator.service import create_and_start_story
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
