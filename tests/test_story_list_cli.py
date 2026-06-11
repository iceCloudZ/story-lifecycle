"""Tests for story list/show/advance/done CLI commands."""

import pytest
from click.testing import CliRunner
from story_lifecycle.db import models as db


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def seeded_db(isolated_story_home):
    db.init_db()
    s1, _ = db.upsert_story_from_source(
        source_type="tapd",
        source_id="1001",
        title="测试需求",
        deadline="2026-06-15",
        priority="高",
        tapd_status="open",
    )
    db.update_story(s1["story_key"], intake_state="ready", status="active")
    s2, _ = db.upsert_story_from_source(
        source_type="tapd",
        source_id="1002",
        title="已完成",
        status="completed",
    )
    db.update_story(
        s2["story_key"], intake_state="ready", status="completed", current_stage="done"
    )


class TestListCmd:
    def test_list_shows_stories(self, runner, seeded_db):
        from story_lifecycle.cli.list_cmd import list_cmd

        result = runner.invoke(list_cmd)
        assert result.exit_code == 0
        assert "1001" in result.output or "tapd" in result.output

    def test_list_empty(self, runner, isolated_story_home):
        from story_lifecycle.cli.list_cmd import list_cmd

        db.init_db()
        result = runner.invoke(list_cmd)
        assert result.exit_code == 0
        assert "没有 story" in result.output


class TestShowCmd:
    def test_show_existing(self, runner, seeded_db):
        from story_lifecycle.cli.list_cmd import show_cmd

        result = runner.invoke(show_cmd, ["tapd-1001"])
        assert result.exit_code == 0
        assert "测试需求" in result.output
        assert "2026-06-15" in result.output

    def test_show_nonexistent(self, runner, isolated_story_home):
        from story_lifecycle.cli.list_cmd import show_cmd

        db.init_db()
        result = runner.invoke(show_cmd, ["NOPE"])
        assert result.exit_code == 1


class TestAdvanceCmd:
    def test_advance_moves_stage(self, runner, seeded_db):
        from story_lifecycle.cli.list_cmd import advance_cmd

        s = db.get_story("tapd-1001")
        assert s["current_stage"] == "design"

        result = runner.invoke(advance_cmd, ["tapd-1001"])
        assert result.exit_code == 0

        s = db.get_story("tapd-1001")
        assert s["current_stage"] == "implement"

    def test_advance_to_done(self, runner, isolated_story_home):
        from story_lifecycle.cli.list_cmd import advance_cmd

        db.init_db()
        db.create_story("ADV-001", "推进测试", "/tmp", current_stage="test")

        runner.invoke(advance_cmd, ["ADV-001"])
        s = db.get_story("ADV-001")
        assert s["current_stage"] == "done"
        assert s["status"] == "completed"


class TestDoneCmd:
    def test_done_marks_completed(self, runner, seeded_db):
        from story_lifecycle.cli.list_cmd import done_cmd

        result = runner.invoke(done_cmd, ["tapd-1001"])
        assert result.exit_code == 0

        s = db.get_story("tapd-1001")
        assert s["status"] == "completed"
        assert s["current_stage"] == "done"
