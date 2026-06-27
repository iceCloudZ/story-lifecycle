"""Tests for miner.link anchor-first binding (I2)."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta

import pytest

from miner import common, config


def _make_db(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sessions(
            sid TEXT PRIMARY KEY, src TEXT, ws TEXT, ts TEXT, title TEXT,
            turns INT, ntools INT, nerrs INT, cwd TEXT, branch TEXT, first_ucmd TEXT, path TEXT
        );
        CREATE TABLE stories(
            story_id TEXT PRIMARY KEY, workspace TEXT, title TEXT, status TEXT,
            stage TEXT, spec_path TEXT, complexity TEXT, branch TEXT,
            ts_design TEXT, ts_build TEXT, ts_verify TEXT, first_ts TEXT, last_ts TEXT, dir_path TEXT
        );
        """
    )
    conn.commit()
    return conn


def _insert_session(conn, sid, ws, ts, cwd, branch, first_ucmd, title=None):
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, "kimi", ws, ts, title, 1, 1, 0, cwd, branch, first_ucmd, None),
    )


def _insert_story(conn, story_id, workspace, first_ts, last_ts, branch=None):
    conn.execute(
        "INSERT INTO stories VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (story_id, workspace, "title", "active", "design", None, None, branch,
         None, None, None, first_ts, last_ts, "."),
    )


def _write_anchor(workspace: str, story_key: str, ts: str, cwd: str):
    runs = os.path.join(workspace, ".story", "runs", story_key)
    os.makedirs(runs, exist_ok=True)
    path = os.path.join(runs, "anchors.jsonl")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "story_key": story_key,
                    "stage": "design",
                    "adapter": "kimi",
                    "cwd": cwd,
                    "ts": ts,
                    "prompt_hash": "deadbeef",
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    return path


@pytest.fixture
def link_env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "transcripts.db")
    conn = _make_db(db_path)

    ws = str(tmp_path / "hc-all")
    os.makedirs(ws, exist_ok=True)

    # Monkeypatch config
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "WORKSPACES", [ws])

    # Import link after monkeypatch
    from miner import link

    monkeypatch.setattr(link, "DB", db_path)
    yield {"db_path": db_path, "workspace": ws, "conn": conn, "link": link}
    conn.close()


def test_anchor_binding_is_high_confidence_and_eliminates_misbinding(link_env):
    ws = link_env["workspace"]
    conn = link_env["conn"]

    # Story A: has an anchor at 2026-06-01 10:00
    anchor_ts = "2026-06-01T10:00:00"
    _write_anchor(ws, "STORY-A", anchor_ts, ws)
    _insert_story(conn, "STORY-A", ws, "2026-06-01T09:00:00", "2026-06-01T11:00:00")

    # Sessions:
    # s1: story A, right after anchor -> should anchor-link
    _insert_session(
        conn,
        "s1",
        common.ws_of(ws),
        "2026-06-01",
        ws,
        "feature/a",
        "implement STORY-A endpoint",
    )
    # s2: another session same day, no ID mention -> should NOT be misbound to A
    _insert_session(
        conn,
        "s2",
        common.ws_of(ws),
        "2026-06-01",
        ws,
        "feature/other",
        "refactor unrelated util",
    )
    # s3: mentions STORY-A but old -> heuristic link
    _insert_session(
        conn,
        "s3",
        common.ws_of(ws),
        "2026-05-01",
        ws,
        None,
        "review STORY-A spec",
    )
    conn.commit()

    link_env["link"].link()

    rows = {
        sid: story_id
        for sid, story_id in conn.execute("SELECT sid, story_id FROM sessions").fetchall()
    }
    assert rows["s1"] == "STORY-A"
    assert rows["s3"] == "STORY-A"
    assert rows["s2"] is None, "session without story signal must not be misbound"


def test_story_sign_sessions_binding_rate_above_eighty(link_env):
    ws = link_env["workspace"]
    conn = link_env["conn"]

    # Two stories with anchors
    _write_anchor(ws, "STORY-1", "2026-06-01T10:00:00", ws)
    _write_anchor(ws, "STORY-2", "2026-06-02T10:00:00", ws)
    _insert_story(conn, "STORY-1", ws, "2026-06-01T09:00:00", "2026-06-01T11:00:00")
    _insert_story(conn, "STORY-2", ws, "2026-06-02T09:00:00", "2026-06-02T11:00:00")

    story_sign = [
        ("s1", "2026-06-01", "start STORY-1 design"),
        ("s2", "2026-06-01", "continue STORY-1 implementation"),
        ("s3", "2026-06-02", "tapd story STORY-2 review"),
        ("s4", "2026-06-02", "spec update for STORY-2"),
        ("s5", "2026-06-03", "unrelated misc work"),
    ]
    for sid, ts, ucmd in story_sign:
        _insert_session(conn, sid, common.ws_of(ws), ts, ws, None, ucmd)
    conn.commit()

    link_env["link"].link()

    rows = dict(conn.execute("SELECT sid, story_id FROM sessions").fetchall())
    sign_sids = [sid for sid, _, ucmd in story_sign if "story" in ucmd.lower() or "tapd" in ucmd.lower() or "spec" in ucmd.lower()]
    linked_count = sum(1 for sid in sign_sids if rows.get(sid))
    rate = linked_count / len(sign_sids)
    assert rate >= 0.8, f"story-sign binding rate {rate:.0%} below 80%"


def test_to_datetime_normalizes_mixed_tz_awareness():
    """_to_datetime must return offset-aware datetimes for both Z (full ISO) and
    naive inputs, so aware session ts can be compared with naive anchor ts without
    raising TypeError. Regression: full_ts emits aware ts; anchors are naive."""
    from miner import link
    aware = link._to_datetime("2026-06-22T16:13:11.475Z")
    naive = link._to_datetime("2026-06-17T12:00:00")
    assert aware is not None and aware.tzinfo is not None
    assert naive is not None and naive.tzinfo is not None
    # this comparison raised TypeError before the fix
    assert aware >= naive
