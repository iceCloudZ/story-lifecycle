"""Tests for story session --writeback CLI command(半自动回写闭环)。

session_cmd 是 click 薄壳(同 consult_cmd):读 STORY_KEY/STORY_STAGE/STORY_ADAPTER env,
调 db.upsert_session 回写 session id。覆盖:正常回写、缺 env、缺 id、落 DB 事件。
"""

from __future__ import annotations

from click.testing import CliRunner

from story_lifecycle.entry.cli.session_cmd import session_cmd
from story_lifecycle.infra.db import models as db


def test_writeback_persists_session(isolated_story_home, monkeypatch):
    """正常:--writeback <id> + STORY_KEY/STAGE/ADAPTER → 落 DB + 事件。"""
    db.upsert_story("CLI-WB-1", title="cli 回写", workspace="/tmp", profile="minimal")
    db.update_story("CLI-WB-1", intake_state="ready", current_stage="design")
    monkeypatch.setenv("STORY_KEY", "CLI-WB-1")
    monkeypatch.setenv("STORY_STAGE", "design")
    monkeypatch.setenv("STORY_ADAPTER", "kimi")

    result = CliRunner().invoke(session_cmd, ["--writeback", "session_cli_xyz"])

    assert result.exit_code == 0
    assert "已回写" in result.output
    row = db.get_session("CLI-WB-1", "design", "kimi")
    assert row["session_id"] == "session_cli_xyz"
    events = db.get_story_events("CLI-WB-1")
    assert any(e["event_type"] == "session_writeback" for e in events)


def test_writeback_missing_id_exits_2(isolated_story_home, monkeypatch):
    """缺 --writeback → exit 2(用法错误)。"""
    monkeypatch.setenv("STORY_KEY", "CLI-WB-2")
    result = CliRunner().invoke(session_cmd, [])
    assert result.exit_code == 2
    assert "--writeback" in result.output


def test_writeback_missing_story_key_exits_2(isolated_story_home, monkeypatch):
    """缺 STORY_KEY env → exit 2。"""
    monkeypatch.delenv("STORY_KEY", raising=False)
    result = CliRunner().invoke(session_cmd, ["--writeback", "sid"])
    assert result.exit_code == 2
    assert "STORY_KEY" in result.output


def test_writeback_defaults_stage_adapter(isolated_story_home, monkeypatch):
    """STORY_STAGE/STORY_ADAPTER 缺省 → design / claude。"""
    db.upsert_story("CLI-WB-3", title="默认", workspace="/tmp", profile="minimal")
    db.update_story("CLI-WB-3", intake_state="ready")
    monkeypatch.setenv("STORY_KEY", "CLI-WB-3")
    monkeypatch.delenv("STORY_STAGE", raising=False)
    monkeypatch.delenv("STORY_ADAPTER", raising=False)

    result = CliRunner().invoke(session_cmd, ["--writeback", "sid-def"])
    assert result.exit_code == 0
    row = db.get_session("CLI-WB-3", "design", "claude")
    assert row["session_id"] == "sid-def"
