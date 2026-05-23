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
