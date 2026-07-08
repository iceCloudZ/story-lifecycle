"""Tests for `story sync --id` (pull a single story by TAPD id).

Covers the case `fetch_pending` misses: a story that exists in TAPD but is
filtered out by owner (custom_field_25) / parent_id rules.
"""

import os
import tempfile

from click.testing import CliRunner

from story_lifecycle.infra.db import models as db
from story_lifecycle.sourcing.sources.base import SourceItem


def _fake_item(item_id="1066988", title="按id拉"):
    return SourceItem(
        id=item_id,
        source="tapd",
        item_type="requirement",
        title=title,
        description="desc",
        priority="高",
        owner="zhangsan",
        deadline="2026-06-15",
        status="open",
        extra={"short_id": item_id, "url": f"https://tapd.cn/{item_id}"},
    )


class TestSyncById:
    def test_sync_by_id_pulls_single_bypassing_filters(
        self, isolated_story_home, monkeypatch
    ):
        """--id pulls one story via get_detail, bypassing owner/parent filters."""
        from story_lifecycle.entry.cli.sync_cmd import sync_cmd
        from story_lifecycle.sourcing.sources import tapd_source

        monkeypatch.setattr(
            tapd_source.TapdSource, "get_detail", lambda self, i: _fake_item(i)
        )
        monkeypatch.setattr(
            "story_lifecycle.entry.cli.sync_cmd._load_tapd_config",
            lambda: {"workspace_id": "123"},
        )

        tmpdir = tempfile.mkdtemp()
        os.makedirs(os.path.join(tmpdir, ".story"), exist_ok=True)
        result = CliRunner().invoke(sync_cmd, ["--id", "1066988", "-w", tmpdir])
        assert result.exit_code == 0, result.output
        s = db.get_story("tapd-1066988")
        assert s is not None
        assert s["title"] == "按id拉"

    def test_sync_by_id_not_found_exits_nonzero(
        self, isolated_story_home, monkeypatch
    ):
        from story_lifecycle.entry.cli.sync_cmd import sync_cmd
        from story_lifecycle.sourcing.sources import tapd_source

        monkeypatch.setattr(
            tapd_source.TapdSource, "get_detail", lambda self, i: None
        )
        monkeypatch.setattr(
            "story_lifecycle.entry.cli.sync_cmd._load_tapd_config",
            lambda: {"workspace_id": "123"},
        )

        result = CliRunner().invoke(sync_cmd, ["--id", "9999999"])
        assert result.exit_code != 0
        assert "未找到" in result.output or "9999999" in result.output

    def test_sync_by_id_is_idempotent(
        self, isolated_story_home, monkeypatch
    ):
        """同一 TAPD id 连续 sync 两次只建一个 story;第二次为更新。"""
        from story_lifecycle.entry.cli.sync_cmd import sync_cmd
        from story_lifecycle.sourcing.sources import tapd_source

        monkeypatch.setattr(
            tapd_source.TapdSource, "get_detail", lambda self, i: _fake_item(i)
        )
        monkeypatch.setattr(
            "story_lifecycle.entry.cli.sync_cmd._load_tapd_config",
            lambda: {"workspace_id": "123"},
        )

        tmpdir = tempfile.mkdtemp()
        os.makedirs(os.path.join(tmpdir, ".story"), exist_ok=True)

        # First sync -> created
        result1 = CliRunner().invoke(
            sync_cmd, ["--id", "1066988", "-w", tmpdir]
        )
        assert result1.exit_code == 0, result1.output
        assert "新建 1" in result1.output

        stories_after_first = db.list_visible_stories(show_all=True)
        assert len([s for s in stories_after_first if s["story_key"] == "tapd-1066988"]) == 1

        # Second sync -> updated, not created
        result2 = CliRunner().invoke(
            sync_cmd, ["--id", "1066988", "-w", tmpdir]
        )
        assert result2.exit_code == 0, result2.output
        assert "更新 1" in result2.output
        assert "新建 0" in result2.output

        stories_after_second = db.list_visible_stories(show_all=True)
        assert len([s for s in stories_after_second if s["story_key"] == "tapd-1066988"]) == 1
