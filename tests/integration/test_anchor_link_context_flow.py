"""Integration test: story-lifecycle anchor -> miner.link -> context provider.

This test exercises the full I2/I3 data flow in a single temp workspace:
1. story-lifecycle adapter writes an anchor.
2. miner.link binds a matching session to the story.
3. miner TranscriptStoryContextProvider returns historical context for that story.
"""

import sqlite3
from datetime import datetime

import pytest

from story_lifecycle.adapters.base import BaseAdapter
from miner.story_context_provider import TranscriptStoryContextProvider


class _FakeAdapter(BaseAdapter):
    name = "kimi"

    def switch_provider(self, provider):
        return None

    def launch_cmd(self, model):
        return ""

    def inject_prompt(self, prompt, story_key, stage):
        return None


@pytest.fixture
def flow_env(tmp_path, monkeypatch):
    """Set up an isolated workspace + temp miner DB for the integration flow."""
    ws = tmp_path / "hc-all"
    ws.mkdir()
    db_path = tmp_path / "transcripts.db"

    # Configure miner to use the temp DB/workspace
    from miner import config as miner_config
    from miner import store, story_ingest, link

    monkeypatch.setattr(miner_config, "DB_PATH", str(db_path))
    monkeypatch.setattr(miner_config, "WORKSPACES", [str(ws)])
    monkeypatch.setattr(link, "DB", str(db_path))

    store.init_db(str(db_path))
    story_ingest.init_db(str(db_path))

    return {"ws": str(ws), "db_path": str(db_path)}


def _insert_story(conn, story_id, workspace):
    conn.execute(
        "INSERT INTO stories(story_id,workspace,title,status,stage,first_ts,last_ts) "
        "VALUES (?,?,?,?,?,?,?)",
        (story_id, workspace, "fix login", "active", "design",
         "2026-06-27T09:00:00", "2026-06-27T18:00:00"),
    )


def _insert_session(conn, sid, ws, ts, cwd, branch, first_ucmd):
    conn.execute(
        "INSERT INTO sessions(sid,src,ws,ts,title,turns,ntools,nerrs,cwd,branch,first_ucmd) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (sid, "kimi", ws, ts, None, 1, 1, 0, cwd, branch, first_ucmd),
    )


def test_anchor_link_and_context_provider(flow_env):
    ws = flow_env["ws"]
    db_path = flow_env["db_path"]

    # Phase 1: story-lifecycle writes an anchor
    adapter = _FakeAdapter()
    anchor_path = adapter.write_anchor(
        prompt="implement login",
        story_key="STORY-123",
        stage="design",
        cwd=ws,
        workspace=ws,
    )
    assert anchor_path is not None

    # Seed the DB with a session that the anchor should match
    with sqlite3.connect(db_path) as conn:
        _insert_story(conn, "STORY-123", ws)
        _insert_session(
            conn, "s1", ws[-6:], datetime.now().isoformat(), ws, "feature/login", "impl login"
        )
        conn.commit()

    # Phase 2: miner.link binds the session to the story
    from miner import link

    link.link()

    with sqlite3.connect(db_path) as conn:
        story_id = conn.execute(
            "SELECT story_id FROM sessions WHERE sid=?", ("s1",)
        ).fetchone()[0]
    assert story_id == "STORY-123"

    # Phase 3: context provider returns historical context
    provider = TranscriptStoryContextProvider({"db_path": db_path})
    ctx = provider.get_context("STORY-123", ws, "design")
    assert isinstance(ctx, str)
    assert "历史上下文" in ctx
