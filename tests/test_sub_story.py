"""Tests for sub-story P0 feature — DB, service, context inheritance."""

import json
import tempfile
from pathlib import Path

import pytest

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


def test_create_sub_story(tmp_path):
    """create_sub_story should inherit workspace/profile/context from parent."""
    m, original = _init_fresh_db(tmp_path)
    try:
        # Create parent with context
        m.create_story(
            story_key="FEAT-001",
            title="Login feature",
            workspace=str(tmp_path),
        )
        m.update_context("FEAT-001", "prd_path", "prd/FEAT-001.md")
        m.update_context("FEAT-001", "spec_path", ".story-context/FEAT-001/spec.md")

        from story_lifecycle.orchestrator.service import create_sub_story
        sub_key = create_sub_story(
            parent_key="FEAT-001",
            sub_type="bug-fix",
            start_stage="implement",
            description="Fix login blank page",
        )

        assert sub_key == "FEAT-001-sub-1"

        child = m.get_story(sub_key)
        assert child is not None
        assert child["parent_key"] == "FEAT-001"
        assert child["sub_type"] == "bug-fix"
        assert child["current_stage"] == "implement"
        assert child["workspace"] == str(tmp_path)
        assert child["profile"] == "minimal"
        assert child["title"] == "Fix login blank page"

        # Context inherited
        ctx = json.loads(child["context_json"])
        assert ctx["prd_path"] == "prd/FEAT-001.md"
        assert ctx["spec_path"] == ".story-context/FEAT-001/spec.md"
        assert ctx["sub_description"] == "Fix login blank page"

        # Parent moved to waiting_subtasks
        parent = m.get_story("FEAT-001")
        assert parent["status"] == "waiting_subtasks"
    finally:
        m.get_db_path = original


def test_abort_sub_story(tmp_path):
    """abort_story should mark story as aborted (not completed)."""
    m, original = _init_fresh_db(tmp_path)
    try:
        m.create_story(story_key="FEAT-002", title="Parent", workspace=str(tmp_path))
        m.create_story(
            story_key="FEAT-002-sub-1",
            title="Sub",
            workspace=str(tmp_path),
            parent_key="FEAT-002",
            subtask_index=0,
        )
        m.update_story("FEAT-002-sub-1", sub_type="bug-fix")
        m.update_story("FEAT-002", status="waiting_subtasks")

        from story_lifecycle.orchestrator.service import abort_story
        abort_story("FEAT-002-sub-1", "User abort")

        child = m.get_story("FEAT-002-sub-1")
        assert child["status"] == "aborted"
        assert child["last_error"] == "User abort"
    finally:
        m.get_db_path = original


def test_resume_parent(tmp_path):
    """resume_parent should pause active subs and set parent to active."""
    m, original = _init_fresh_db(tmp_path)
    try:
        m.create_story(story_key="FEAT-003", title="Parent", workspace=str(tmp_path))
        m.create_story(
            story_key="FEAT-003-sub-1",
            title="Sub1",
            workspace=str(tmp_path),
            parent_key="FEAT-003",
            subtask_index=0,
        )
        m.update_story("FEAT-003", status="waiting_subtasks")
        m.update_story("FEAT-003-sub-1", status="active")

        from story_lifecycle.orchestrator.service import resume_parent
        resume_parent("FEAT-003", strategy="pause_subs")

        parent = m.get_story("FEAT-003")
        assert parent["status"] == "active"

        child = m.get_story("FEAT-003-sub-1")
        assert child["status"] == "paused"
    finally:
        m.get_db_path = original


def test_resume_parent_abort_subs(tmp_path):
    """resume_parent with abort_subs should abort all subs."""
    m, original = _init_fresh_db(tmp_path)
    try:
        m.create_story(story_key="FEAT-004", title="Parent", workspace=str(tmp_path))
        m.create_story(
            story_key="FEAT-004-sub-1",
            title="Sub1",
            workspace=str(tmp_path),
            parent_key="FEAT-004",
            subtask_index=0,
        )
        m.update_story("FEAT-004", status="waiting_subtasks")
        m.update_story("FEAT-004-sub-1", status="active")

        from story_lifecycle.orchestrator.service import resume_parent
        resume_parent("FEAT-004", strategy="abort_subs")

        child = m.get_story("FEAT-004-sub-1")
        assert child["status"] == "aborted"
    finally:
        m.get_db_path = original


def test_nested_sub_story_rejected(tmp_path):
    """Sub-story cannot create its own sub-stories."""
    m, original = _init_fresh_db(tmp_path)
    try:
        m.create_story(story_key="FEAT-005", title="Grandparent", workspace=str(tmp_path))
        m.create_story(
            story_key="FEAT-005-sub-1",
            title="Child",
            workspace=str(tmp_path),
            parent_key="FEAT-005",
            subtask_index=0,
        )

        from story_lifecycle.orchestrator.service import create_sub_story
        with pytest.raises(ValueError, match="嵌套"):
            create_sub_story(
                parent_key="FEAT-005-sub-1",
                sub_type="bug-fix",
                description="Should fail",
            )
    finally:
        m.get_db_path = original


def test_api_create_sub_story(tmp_path):
    """POST /api/story/{parent_key}/sub should create sub-story."""
    m, original = _init_fresh_db(tmp_path)
    try:
        m.create_story(story_key="API-001", title="Parent", workspace=str(tmp_path))

        from fastapi.testclient import TestClient
        from story_lifecycle.orchestrator.api import app
        client = TestClient(app)

        resp = client.post(
            "/api/story/API-001/sub",
            json={
                "sub_type": "bug-fix",
                "start_stage": "implement",
                "description": "Fix crash",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["storyKey"] == "API-001-sub-1"
        assert data["subType"] == "bug-fix"

        # Parent should be waiting_subtasks
        parent = m.get_story("API-001")
        assert parent["status"] == "waiting_subtasks"
    finally:
        m.get_db_path = original


def test_api_abort_story(tmp_path):
    """POST /api/story/{key}/abort should abort a story."""
    m, original = _init_fresh_db(tmp_path)
    try:
        m.create_story(story_key="API-002", title="To abort", workspace=str(tmp_path))

        from fastapi.testclient import TestClient
        from story_lifecycle.orchestrator.api import app
        client = TestClient(app)

        resp = client.post("/api/story/API-002/abort", json={"reason": "test abort"})
        assert resp.status_code == 200

        s = m.get_story("API-002")
        assert s["status"] == "aborted"
    finally:
        m.get_db_path = original


def test_api_resume_parent(tmp_path):
    """PUT /api/story/{parent_key}/resume should resume parent."""
    m, original = _init_fresh_db(tmp_path)
    try:
        m.create_story(story_key="API-003", title="Parent", workspace=str(tmp_path))
        m.create_story(
            story_key="API-003-sub-1",
            title="Sub",
            workspace=str(tmp_path),
            parent_key="API-003",
            subtask_index=0,
        )
        m.update_story("API-003", status="waiting_subtasks")

        from fastapi.testclient import TestClient
        from story_lifecycle.orchestrator.api import app
        client = TestClient(app)

        resp = client.put(
            "/api/story/API-003/resume",
            json={"strategy": "abort_subs"},
        )
        assert resp.status_code == 200

        parent = m.get_story("API-003")
        assert parent["status"] == "active"
    finally:
        m.get_db_path = original


def test_workspace_mutex(tmp_path):
    """Only one story per workspace should be able to execute at a time."""
    m, original = _init_fresh_db(tmp_path)
    try:
        from story_lifecycle.orchestrator.graph import acquire_workspace, release_workspace
        assert acquire_workspace(str(tmp_path), "MUTEX-001") is True
        assert acquire_workspace(str(tmp_path), "MUTEX-002") is False
        release_workspace(str(tmp_path))
        assert acquire_workspace(str(tmp_path), "MUTEX-002") is True
        release_workspace(str(tmp_path))
    finally:
        m.get_db_path = original


def test_context_size_control(tmp_path):
    """Large parent context should be truncated for sub-story."""
    m, original = _init_fresh_db(tmp_path)
    try:
        m.create_story(story_key="FEAT-006", title="Big ctx", workspace=str(tmp_path))
        # Create context > 1MB
        big_value = "x" * (2 * 1024 * 1024)
        big_ctx = json.dumps({"huge_field": big_value, "small_field": "keep_me"})
        m.update_story("FEAT-006", context_json=big_ctx)

        from story_lifecycle.orchestrator.service import create_sub_story
        sub_key = create_sub_story(
            parent_key="FEAT-006",
            description="Should skip big field",
        )

        child = m.get_story(sub_key)
        ctx = json.loads(child["context_json"])
        assert "huge_field" not in ctx
        assert ctx["small_field"] == "keep_me"
        assert "huge_field" in ctx.get("_skipped_fields", [])
    finally:
        m.get_db_path = original
