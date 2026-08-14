"""Tests for story daily CLI command(PLAN-proactive-cadence Part A)。"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from click.testing import CliRunner

from story_lifecycle.infra.db import models as db


@pytest.fixture
def runner():
    return CliRunner()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@pytest.fixture
def seeded_db(isolated_story_home):
    db.init_db()
    # 逾期未完成(ready/active)
    s1, _ = db.upsert_story_from_source(
        source_type="tapd", source_id="1001", title="逾期需求", deadline="2020-01-01"
    )
    db.update_story(s1["story_key"], intake_state="ready", status="active")
    # 明日到期
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    s2, _ = db.upsert_story_from_source(
        source_type="tapd", source_id="1002", title="明日到期", deadline=tomorrow
    )
    db.update_story(s2["story_key"], intake_state="ready", status="active")
    # 今日新落 candidate(sync 建的都落 candidate —— 简报必须扫 candidate 池)
    s3, _ = db.upsert_story_from_source(
        source_type="tapd", source_id="1003", title="今日新需求"
    )
    # 受阻(paused)与失败(failed)
    s4, _ = db.upsert_story_from_source(
        source_type="tapd", source_id="1004", title="卡住需求"
    )
    db.update_story(s4["story_key"], intake_state="ready", status="paused")
    s5, _ = db.upsert_story_from_source(
        source_type="tapd", source_id="1005", title="失败需求"
    )
    db.update_story(s5["story_key"], intake_state="ready", status="failed")
    # 终态噪音:结项但 deadline 早过 → 不进任何视图(真实数据里 candidate 池
    # 存在大量 TAPD 远端关闭映射结项的 story,不过滤会灌爆「已过期」)
    s6, _ = db.upsert_story_from_source(
        source_type="tapd", source_id="1006", title="结项噪音", deadline="2020-01-01"
    )
    db.update_story(s6["story_key"], lifecycle_state="结项")


class TestDailyCmd:
    def test_daily_md_sections(self, runner, seeded_db):
        from story_lifecycle.entry.cli.daily_cmd import daily_cmd

        result = runner.invoke(daily_cmd, ["--md"])
        assert result.exit_code == 0
        assert "逾期需求" in result.output
        assert "明日到期" in result.output
        assert "今日新需求" in result.output
        assert "卡住需求" in result.output
        assert "失败需求" in result.output
        assert "结项噪音" not in result.output

    def test_daily_json_structure(self, runner, seeded_db):
        from story_lifecycle.entry.cli.daily_cmd import daily_cmd

        result = runner.invoke(daily_cmd, ["--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["counts"]["overdue"] == 1
        assert data["counts"]["due_soon"] == 1
        # new_today 含所有当日新建(种子全在今日建,结项噪音除外 → 5);
        # 关键断言是 candidate 的新落在列(sync 落 candidate,不扫会漏「主动接单」)
        assert data["counts"]["new_today"] == 5
        assert data["counts"]["blocked"] == 2
        new_titles = {item["title"]: item for item in data["new_today"]}
        assert new_titles["今日新需求"]["intake_state"] == "candidate"
        # 投影只带简报字段,不泄漏 context_json 整行大字段
        assert set(data["overdue"][0]) <= {
            "story_key",
            "title",
            "deadline",
            "tapd_status",
            "lifecycle_state",
            "current_stage",
            "status",
            "intake_state",
            "tapd_url",
        }

    def test_daily_empty(self, runner, isolated_story_home):
        from story_lifecycle.entry.cli.daily_cmd import daily_cmd

        db.init_db()
        result = runner.invoke(daily_cmd, ["--md"])
        assert result.exit_code == 0
        assert "没有需要关注" in result.output
        result = runner.invoke(daily_cmd, ["--json"])
        assert result.exit_code == 0
        assert json.loads(result.output)["counts"] == {
            "overdue": 0,
            "due_soon": 0,
            "new_today": 0,
            "blocked": 0,
            "in_progress": 0,
        }
