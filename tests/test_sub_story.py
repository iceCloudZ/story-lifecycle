"""Tests for sub-story P0 feature — DB, service, context inheritance."""

import json
import tempfile
from pathlib import Path

from story_lifecycle.db.models import (
    get_story,
    init_db,
    create_story,
    get_sub_stories,
    update_story,
    delete_story,
)


def _init_fresh_db(tmp_path):
    """Init DB in a temp dir to avoid polluting real DB.

    Returns (module, original_get_db_path) so caller can restore.
    """
    import story_lifecycle.db.models as m
    original = m.get_db_path
    m.get_db_path = lambda: tmp_path / "story.db"
    m.init_db()
    return m, original


def test_create_story_with_sub_type(tmp_path):
    """sub_type should be stored and retrievable."""
    m, original = _init_fresh_db(tmp_path)
    try:
        m.create_story(
            story_key="PARENT-001",
            title="Parent story",
            workspace=str(tmp_path),
        )
        m.create_story(
            story_key="PARENT-001-sub-1",
            title="Fix login bug",
            workspace=str(tmp_path),
            parent_key="PARENT-001",
            subtask_index=0,
        )
        # Update sub_type via update_story
        m.update_story("PARENT-001-sub-1", sub_type="bug-fix")

        child = m.get_story("PARENT-001-sub-1")
        assert child is not None
        assert child["sub_type"] == "bug-fix"
        assert child["parent_key"] == "PARENT-001"
    finally:
        m.get_db_path = original
