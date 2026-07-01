"""Tests for story calendar CLI command."""

from datetime import datetime, timedelta, timezone

import pytest
from click.testing import CliRunner

from story_lifecycle.infra.db import models as db


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def seeded_db(isolated_story_home):
    db.init_db()
    s1, _ = db.upsert_story_from_source(
        source_type="tapd",
        source_id="1001",
        title="逾期需求",
        deadline="2020-01-01",
    )
    db.update_story(s1["story_key"], intake_state="ready", status="active")
    near = (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%Y-%m-%d")
    s2, _ = db.upsert_story_from_source(
        source_type="tapd",
        source_id="1002",
        title="近期需求",
        deadline=near,
    )
    db.update_story(s2["story_key"], intake_state="ready", status="active")
    far = (datetime.now(timezone.utc) + timedelta(days=60)).strftime("%Y-%m-%d")
    s3, _ = db.upsert_story_from_source(
        source_type="tapd",
        source_id="1003",
        title="远期需求",
        deadline=far,
    )
    db.update_story(s3["story_key"], intake_state="ready", status="active")
    s4, _ = db.upsert_story_from_source(
        source_type="tapd",
        source_id="1004",
        title="无截止日期",
    )
    db.update_story(s4["story_key"], intake_state="ready", status="active")


class TestCalendarCmd:
    def test_calendar_shows_overdue(self, runner, seeded_db):
        from story_lifecycle.cli.calendar_cmd import calendar_cmd

        result = runner.invoke(calendar_cmd, [])
        assert result.exit_code == 0
        assert "逾期" in result.output

    def test_calendar_shows_near_future(self, runner, seeded_db):
        from story_lifecycle.cli.calendar_cmd import calendar_cmd

        result = runner.invoke(calendar_cmd, [])
        assert result.exit_code == 0
        assert "近期需求" in result.output

    def test_calendar_hides_far_future(self, runner, seeded_db):
        from story_lifecycle.cli.calendar_cmd import calendar_cmd

        result = runner.invoke(calendar_cmd, [])
        assert result.exit_code == 0
        assert "远期需求" not in result.output

    def test_calendar_empty(self, runner, isolated_story_home):
        from story_lifecycle.cli.calendar_cmd import calendar_cmd

        db.init_db()
        result = runner.invoke(calendar_cmd, [])
        assert result.exit_code == 0
        assert "没有" in result.output
