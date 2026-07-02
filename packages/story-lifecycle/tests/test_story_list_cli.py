"""Tests for story list/show/advance/done CLI commands."""

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
        from story_lifecycle.entry.cli.list_cmd import list_cmd

        result = runner.invoke(list_cmd)
        assert result.exit_code == 0
        assert "1001" in result.output or "tapd" in result.output

    def test_list_empty(self, runner, isolated_story_home):
        from story_lifecycle.entry.cli.list_cmd import list_cmd

        db.init_db()
        result = runner.invoke(list_cmd)
        assert result.exit_code == 0
        assert "没有 story" in result.output


class TestShowCmd:
    def test_show_existing(self, runner, seeded_db):
        from story_lifecycle.entry.cli.list_cmd import show_cmd

        result = runner.invoke(show_cmd, ["tapd-1001"])
        assert result.exit_code == 0
        assert "测试需求" in result.output
        assert "2026-06-15" in result.output

    def test_show_nonexistent(self, runner, isolated_story_home):
        from story_lifecycle.entry.cli.list_cmd import show_cmd

        db.init_db()
        result = runner.invoke(show_cmd, ["NOPE"])
        assert result.exit_code == 1


class TestAdvanceCmd:
    def test_advance_moves_stage(self, runner, seeded_db):
        from story_lifecycle.entry.cli.list_cmd import advance_cmd

        s = db.get_story("tapd-1001")
        assert s["current_stage"] == "design"

        result = runner.invoke(advance_cmd, ["tapd-1001"])
        assert result.exit_code == 0

        s = db.get_story("tapd-1001")
        assert s["current_stage"] == "build"

    def test_advance_to_done(self, runner, isolated_story_home):
        from story_lifecycle.entry.cli.list_cmd import advance_cmd

        db.init_db()
        db.create_story("ADV-001", "推进测试", "/tmp", current_stage="verify")

        runner.invoke(advance_cmd, ["ADV-001"])
        s = db.get_story("ADV-001")
        assert s["current_stage"] == "done"
        assert s["status"] == "completed"


class TestDoneCmd:
    def test_done_marks_completed(self, runner, seeded_db):
        from story_lifecycle.entry.cli.list_cmd import done_cmd

        result = runner.invoke(done_cmd, ["tapd-1001"])
        assert result.exit_code == 0

        s = db.get_story("tapd-1001")
        assert s["status"] == "completed"
        assert s["current_stage"] == "done"

    def test_done_uses_current_python_executable(self, runner, seeded_db, monkeypatch):
        """I4: done_cmd 必须使用当前解释器调用 retrospect，避免 PATH 错乱。"""
        import subprocess
        import sys
        from story_lifecycle.entry.cli import list_cmd

        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            class _R:
                returncode = 0
                stdout = "ok"
                stderr = ""
            return _R()

        monkeypatch.setattr(subprocess, "run", fake_run)
        # ensure script path exists so the hook actually runs
        monkeypatch.setattr(list_cmd, "_MINER_RETROSPECT_SCRIPT", __file__)

        runner.invoke(list_cmd.done_cmd, ["tapd-1001"])

        assert calls, "subprocess.run should have been called"
        assert calls[0][0] == sys.executable, "first arg must be sys.executable"


class TestRetrospectScriptResolution:
    """ISS-007: retrospect-script path is env/config driven, not hardcoded to
    the monorepo layout. Lifecycle must run (graceful-skip) outside the monorepo."""

    def test_env_override_takes_priority(self, monkeypatch):
        import os
        from story_lifecycle.entry.cli.list_cmd import _resolve_retrospect_script
        import story_lifecycle.infra.config as cfg

        monkeypatch.setenv("STORY_RETROSPECT_SCRIPT", "/custom/retrospect.py")
        # config must NOT win over env
        monkeypatch.setattr(cfg, "get_config", lambda: {"retrospect_script": "/from-config.py"})
        assert _resolve_retrospect_script() == os.path.normpath("/custom/retrospect.py")

    def test_config_used_when_no_env(self, monkeypatch):
        import os
        from story_lifecycle.entry.cli.list_cmd import _resolve_retrospect_script
        import story_lifecycle.infra.config as cfg

        monkeypatch.delenv("STORY_RETROSPECT_SCRIPT", raising=False)
        monkeypatch.setattr(cfg, "get_config", lambda: {"retrospect_script": "/from-config.py"})
        assert _resolve_retrospect_script() == os.path.normpath("/from-config.py")

    def test_monorepo_fallback(self, monkeypatch):
        import os
        from story_lifecycle.entry.cli.list_cmd import _resolve_retrospect_script
        import story_lifecycle.infra.config as cfg

        monkeypatch.delenv("STORY_RETROSPECT_SCRIPT", raising=False)
        monkeypatch.setattr(cfg, "get_config", lambda: {})
        path = _resolve_retrospect_script()
        assert path.endswith(os.path.normpath("story-miner/scripts/retrospect.py"))
