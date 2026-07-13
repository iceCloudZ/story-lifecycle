"""Tests for stage output registration into story_document (BUG #17).

Validates that _register_stage_outputs reads done_data["files_changed"]
and creates story_document rows with the correct kind per stage.
"""

import json


def _setup_story(db, story_key="TEST-REG-001"):
    """Create a minimal story row so create_document can target it."""
    db.init_db()
    db.upsert_story(
        story_key,
        title="Registration test",
        workspace="/tmp/test",
        profile="minimal",
        current_stage="design",
        status="active",
    )


def test_design_stage_registers_spec_doc():
    """design stage → files_changed registered with kind='spec'."""
    from story_lifecycle.orchestrator.engine.planner import (
        _register_stage_outputs,
    )
    from story_lifecycle.infra.db import models as db

    _setup_story(db)
    _register_stage_outputs(
        "TEST-REG-001",
        "design",
        {"files_changed": ["story/123/design.md"], "summary": "done"},
    )
    docs = db.get_story_documents("TEST-REG-001")
    specs = [d for d in docs if d["kind"] == "spec"]
    assert len(specs) == 1
    assert specs[0]["ref"] == "story/123/design.md"


def test_build_stage_registers_plan_doc():
    """build stage → kind='plan'."""
    from story_lifecycle.orchestrator.engine.planner import (
        _register_stage_outputs,
    )
    from story_lifecycle.infra.db import models as db

    _setup_story(db, "TEST-REG-002")
    db.upsert_story(
        "TEST-REG-002",
        title="build test",
        workspace="/tmp/test",
        profile="minimal",
        current_stage="build",
        status="active",
    )
    _register_stage_outputs(
        "TEST-REG-002",
        "build",
        {"files_changed": ["story/123/plan.md", "src/Main.java"]},
    )
    docs = db.get_story_documents("TEST-REG-002")
    plans = [d for d in docs if d["kind"] == "plan"]
    assert len(plans) == 2


def test_done_file_filtered_out():
    """done handshake file (.story/done/...) must NOT be registered."""
    from story_lifecycle.orchestrator.engine.planner import (
        _register_stage_outputs,
    )
    from story_lifecycle.infra.db import models as db

    _setup_story(db, "TEST-REG-003")
    db.upsert_story(
        "TEST-REG-003",
        title="filter test",
        workspace="/tmp/test",
        profile="minimal",
        current_stage="design",
        status="active",
    )
    _register_stage_outputs(
        "TEST-REG-003",
        "design",
        {
            "files_changed": [
                "story/123/design.md",
                ".story/done/TEST-REG-003/design.json",
            ]
        },
    )
    docs = db.get_story_documents("TEST-REG-003")
    refs = [d["ref"] for d in docs]
    assert "story/123/design.md" in refs
    assert not any(".story/done/" in r for r in refs)


def test_idempotent():
    """Same done data twice → no duplicate rows."""
    from story_lifecycle.orchestrator.engine.planner import (
        _register_stage_outputs,
    )
    from story_lifecycle.infra.db import models as db

    _setup_story(db, "TEST-REG-004")
    db.upsert_story(
        "TEST-REG-004",
        title="idempotent test",
        workspace="/tmp/test",
        profile="minimal",
        current_stage="design",
        status="active",
    )
    done = {"files_changed": ["story/123/design.md"]}
    _register_stage_outputs("TEST-REG-004", "design", done)
    _register_stage_outputs("TEST-REG-004", "design", done)
    docs = db.get_story_documents("TEST-REG-004")
    specs = [d for d in docs if d["kind"] == "spec" and "design.md" in d["ref"]]
    assert len(specs) == 1


def test_empty_files_changed_no_error():
    """Empty files_changed → no crash, no documents."""
    from story_lifecycle.orchestrator.engine.planner import (
        _register_stage_outputs,
    )
    from story_lifecycle.infra.db import models as db

    _setup_story(db, "TEST-REG-005")
    db.upsert_story(
        "TEST-REG-005",
        title="empty test",
        workspace="/tmp/test",
        profile="minimal",
        current_stage="design",
        status="active",
    )
    _register_stage_outputs("TEST-REG-005", "design", {"summary": "done"})
    docs = db.get_story_documents("TEST-REG-005")
    assert docs == [] or all(d["kind"] != "spec" for d in docs)


def test_unknown_stage_skipped():
    """Stage not in mapping → no-op (no crash)."""
    from story_lifecycle.orchestrator.engine.planner import (
        _register_stage_outputs,
    )
    from story_lifecycle.infra.db import models as db

    _setup_story(db, "TEST-REG-006")
    # Should not raise even with weird stage
    _register_stage_outputs(
        "TEST-REG-006", "unknown_stage", {"files_changed": ["x.md"]}
    )
