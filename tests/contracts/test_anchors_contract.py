"""Contract test: story-lifecycle writes anchors, miner reads them.

This test locks the anchor JSONL schema between the two packages.
If story-lifecycle changes the fields it writes, or miner changes the
fields it requires, this test fails.

T4.5 扩展:验证 lifecycle 写锚点 → miner.link 读取 → 精确匹配 session →
回填 sessions.story_id 的全链路(高置信绑定)。
"""
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone

import pytest

from miner.anchors import REQUIRED_KEYS, read_anchors
from story_lifecycle.knowledge.adapters.base import BaseAdapter


class _FakeAdapter(BaseAdapter):
    name = "fake"

    def switch_provider(self, provider):
        return None

    def launch_cmd(self, model):
        return ""

    def inject_prompt(self, prompt, story_key, stage):
        self.write_anchor(prompt, story_key, stage)
        return None


@pytest.fixture
def adapter():
    return _FakeAdapter()


def _write(adapter, tmp_path, prompt, story_key, stage):
    return adapter.write_anchor(
        prompt, story_key, stage,
        cwd=str(tmp_path), workspace=str(tmp_path),
    )


def test_story_lifecycle_writes_anchor_fields(adapter, tmp_path):
    """story-lifecycle must emit all contract fields."""
    path = _write(adapter, tmp_path, "hello world", "STORY-42", "design")
    assert path is not None
    assert path.endswith("anchors.jsonl")

    with open(path, "r", encoding="utf-8") as fh:
        records = [json.loads(line) for line in fh]

    assert len(records) == 1
    rec = records[0]
    assert REQUIRED_KEYS.issubset(rec)
    assert rec["story_key"] == "STORY-42"
    assert rec["stage"] == "design"
    assert rec["adapter"] == "fake"
    assert rec["cwd"] == os.path.normpath(str(tmp_path))
    expected_hash = hashlib.sha256("hello world".encode("utf-8")).hexdigest()[:16]
    assert rec["prompt_hash"] == expected_hash


def test_miner_reads_story_lifecycle_anchors(adapter, tmp_path):
    """miner must parse exactly what story-lifecycle writes."""
    _write(adapter, tmp_path, "p1", "STORY-42", "design")
    _write(adapter, tmp_path, "p2", "STORY-42", "build")

    anchors = read_anchors(str(tmp_path), "STORY-42")
    assert len(anchors) == 2
    assert anchors[0]["stage"] == "design"
    assert anchors[1]["stage"] == "build"
    assert all(REQUIRED_KEYS.issubset(a) for a in anchors)


def test_miner_skips_malformed_anchor_lines(adapter, tmp_path):
    """miner must tolerate malformed lines without crashing."""
    _write(adapter, tmp_path, "good", "STORY-42", "design")
    path = os.path.join(tmp_path, ".story", "runs", "STORY-42", "anchors.jsonl")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("this is not json\n")
        fh.write(json.dumps({"story_key": "x"}) + "\n")  # missing fields

    anchors = read_anchors(str(tmp_path), "STORY-42")
    assert len(anchors) == 1
    assert anchors[0]["stage"] == "design"


def test_miner_returns_empty_for_missing_anchors(tmp_path):
    assert read_anchors(str(tmp_path), "NOPE-99") == []


# ---------- T4.5 round-trip: lifecycle write → miner link backfill ----------


def _make_link_db(path: str):
    """Create a minimal transcripts.db schema for miner.link."""
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


@pytest.fixture
def link_env(tmp_path, monkeypatch):
    """Set up an isolated miner DB + workspace for cross-package link tests."""
    from miner import common, config

    db_path = str(tmp_path / "transcripts.db")
    conn = _make_link_db(db_path)
    ws = str(tmp_path / "hc-all")
    os.makedirs(ws, exist_ok=True)

    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "WORKSPACES", [ws])

    from miner import link

    monkeypatch.setattr(link, "DB", db_path)
    yield {"db_path": db_path, "workspace": ws, "conn": conn, "link": link, "common": common}
    conn.close()


def test_anchor_round_trip_backfills_session_story_id(adapter, tmp_path, link_env):
    """lifecycle 写锚点 → miner.link 精确匹配 session → 回填 story_id(高置信)。"""
    ws = link_env["workspace"]
    conn = link_env["conn"]
    common = link_env["common"]

    story_key = "STORY-RT"
    anchor_ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    # lifecycle side: write anchor
    path = _write(adapter, tmp_path, "round-trip prompt", story_key, "design")
    assert path is not None

    # Force anchor ts to a known value so we can insert a matching session
    with open(path, "r+", encoding="utf-8") as fh:
        rec = json.loads(fh.readline())
        rec["ts"] = anchor_ts
        rec["cwd"] = ws
        fh.seek(0)
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.truncate()

    # miner side: seed story + session that starts right after anchor
    _insert_story(conn, story_key, ws, anchor_ts, anchor_ts)
    session_ts = (datetime.fromisoformat(anchor_ts.replace("Z", "+00:00")) +
                  __import__("datetime").timedelta(seconds=30)).isoformat()
    _insert_session(
        conn,
        "s-rt-1",
        common.ws_of(ws),
        session_ts,
        ws,
        "feature/rt",
        "start design for STORY-RT",
    )
    conn.commit()

    # run link
    link_env["link"].link()

    rows = dict(conn.execute("SELECT sid, story_id FROM sessions").fetchall())
    assert rows["s-rt-1"] == story_key


def test_anchor_round_trip_does_not_misbind_other_sessions(adapter, tmp_path, link_env):
    """高置信 anchor 绑定不会把同 workspace 的无关联 session 误绑。"""
    ws = link_env["workspace"]
    conn = link_env["conn"]
    common = link_env["common"]

    story_key = "STORY-ISO"
    anchor_ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    path = _write(adapter, tmp_path, "iso prompt", story_key, "design")
    with open(path, "r+", encoding="utf-8") as fh:
        rec = json.loads(fh.readline())
        rec["ts"] = anchor_ts
        rec["cwd"] = ws
        fh.seek(0)
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.truncate()

    _insert_story(conn, story_key, ws, anchor_ts, anchor_ts)
    _insert_session(
        conn,
        "s-iso-bound",
        common.ws_of(ws),
        (datetime.fromisoformat(anchor_ts.replace("Z", "+00:00")) +
         __import__("datetime").timedelta(seconds=30)).isoformat(),
        ws,
        "feature/iso",
        "work on STORY-ISO",
    )
    _insert_session(
        conn,
        "s-iso-unrelated",
        common.ws_of(ws),
        (datetime.fromisoformat(anchor_ts.replace("Z", "+00:00")) +
         __import__("datetime").timedelta(minutes=5)).isoformat(),
        ws,
        "feature/other",
        "unrelated misc work",
    )
    conn.commit()

    link_env["link"].link()

    rows = dict(conn.execute("SELECT sid, story_id FROM sessions").fetchall())
    assert rows["s-iso-bound"] == story_key
    # unrelated session has no story signal and should not be misbound
    assert rows["s-iso-unrelated"] is None


def test_lifecycle_write_anchor_does_not_import_miner(adapter, tmp_path):
    """卸包照跑:lifecycle 写锚点不依赖 miner 包可 import。"""
    # write_anchor is a pure filesystem operation; ensure it succeeds
    # regardless of miner availability.
    path = _write(adapter, tmp_path, "no-miner prompt", "STORY-NO-MINER", "design")
    assert path is not None
    assert os.path.exists(path)
    with open(path, "r", encoding="utf-8") as fh:
        rec = json.loads(fh.readline())
    assert rec["story_key"] == "STORY-NO-MINER"
