import pytest

from story_lifecycle.db import models as db


def test_source_id_columns(isolated_story_home):
    db.create_story("S1", "Story 1", "", "minimal")
    db.create_story("S2", "Story 2", "", "minimal")

    db.update_story("S1", source_type="tapd", source_id="1001234")
    db.update_story("S2", source_type="tapd", source_id="1001235")

    found = db.find_by_source_id("tapd", "1001234")
    assert found is not None
    assert found["story_key"] == "S1"

    assert db.find_by_source_id("tapd", "9999999") is None

    found2 = db.find_by_source_id("tapd", "1001235")
    assert found2["story_key"] == "S2"


def test_create_story_from_source(isolated_story_home):
    """create_story_from_source should create a story with source metadata."""
    from story_lifecycle.sources.base import SourceItem
    from story_lifecycle.orchestrator.service import create_story_from_source

    item = SourceItem(
        id="1144381896001001234",
        source="tapd",
        item_type="requirement",
        title="用户登录功能",
        description="<p>实现登录</p>",
        priority="P0",
        owner="赵子豪",
        status="open",
    )

    result = create_story_from_source(item, auto_start=False)
    assert result.status == "created"
    assert result.story_key is not None
    assert result.story_key.startswith("TAPD-")

    story = db.get_story(result.story_key)
    assert story is not None
    assert story["source_type"] == "tapd"
    assert story["source_id"] == "1144381896001001234"


def test_derive_story_key():
    from story_lifecycle.sources.base import SourceItem
    from story_lifecycle.orchestrator.service import _derive_story_key

    tapd_item = SourceItem(id="1144381896001001234", source="tapd", item_type="requirement", title="", description="")
    assert _derive_story_key(tapd_item) == "TAPD-001234"

    jira_item = SourceItem(id="JIRA-567", source="jira", item_type="requirement", title="", description="")
    # last 6 chars of "JIRA-567" is "RA-567", prefixed with "JIRA-" -> "JIRA-RA-567"
    result = _derive_story_key(jira_item)
    assert result == "JIRA-RA-567"
