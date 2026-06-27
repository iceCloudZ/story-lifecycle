"""Tests that miner scripts emit unified knowledge artifacts (M5/M6)."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

# Import scripts as modules (they live under scripts/)
_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, _SCRIPTS)
import generate_playbooks  # noqa: E402
import failure_mode  # noqa: E402
from miner import config  # noqa: E402


def _seed_db(db_path: str):
    """Create a minimal transcripts.db with enough data to produce playbooks/failures."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            sid TEXT PRIMARY KEY,
            src TEXT,
            ws TEXT,
            ts TEXT,
            title TEXT,
            turns INTEGER,
            ntools INTEGER,
            nerrs INTEGER,
            cwd TEXT,
            branch TEXT,
            first_ucmd TEXT,
            story_id TEXT
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY,
            sid TEXT,
            kind TEXT,
            name TEXT,
            cmd TEXT,
            code TEXT,
            ok INTEGER,
            text TEXT,
            path TEXT
        );
        CREATE TABLE stories (
            story_id TEXT PRIMARY KEY,
            title TEXT,
            branch TEXT,
            dir_path TEXT
        );
        """
    )

    # Three hc-all sessions around "排查" (debug theme)
    sessions = [
        ("s1", "kimi", "hc-all", "2026-06-01T10:00:00", "排查订单状态机", 5, 4, 1, "D:/hc-all", "feature/ice/test", "排查一下 BorrowController 为什么状态没变", "STORY-42"),
        ("s2", "kimi", "hc-all", "2026-06-01T11:00:00", "debug loan", 6, 5, 0, "D:/hc-all", "feature/ice/test", "debug loan status", "STORY-42"),
        ("s3", "kimi", "hc-all", "2026-06-01T12:00:00", "再看报错", 4, 3, 1, "D:/hc-all", "feature/ice/test", "为什么报错", "STORY-42"),
        ("s4", "claude", "java-agent", "2026-06-01T13:00:00", "other ws", 3, 2, 0, "D:/java-agent", "main", " unrelated", None),
    ]
    conn.executemany(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        sessions,
    )
    conn.execute("INSERT INTO stories VALUES (?, ?, ?, ?)", ("STORY-42", "借款状态机排查", "feature/ice/test", "story/42"))

    # Tools: file reads + bash to aggregate
    code_path = "D:/hc-all/hc-order/src/main/java/com/ys/hc/order/controller/BorrowController.java"
    events = [
        ("s1", "tool", "Read", None, None, None, None, code_path),
        ("s1", "tool", "Bash", "mvn clean compile", None, None, None, None),
        ("s1", "result", None, None, None, 0, "cannot find symbol BorrowController", None),
        ("s2", "tool", "Read", None, None, None, None, code_path),
        ("s2", "tool", "Bash", "cli_sql -e 'select * from t_loan_order'", None, None, None, None),
        ("s2", "tool", "Read", None, None, None, None, "D:/hc-all/hc-order/src/main/java/com/ys/hc/order/service/impl/BorrowServiceImpl.java"),
        ("s3", "tool", "Read", None, None, None, None, code_path),
        ("s3", "tool", "Bash", "git status", None, None, None, None),
        ("s3", "result", None, None, None, 0, "cannot find symbol BorrowServiceImpl", None),
        ("s4", "tool", "Read", None, None, None, None, "D:/java-agent/src/main/java/Foo.java"),
    ]
    conn.executemany(
        "INSERT INTO events (sid, kind, name, cmd, code, ok, text, path) VALUES (?,?,?,?,?,?,?,?)",
        events,
    )
    conn.commit()
    conn.close()


@pytest.fixture
def fixture_env(tmp_path):
    """Provide a temporary DB + workspace and patch scripts to use them."""
    db_path = str(tmp_path / "transcripts.db")
    _seed_db(db_path)

    ws = tmp_path / "hc-all"
    ws.mkdir()

    # Patch script-level constants
    orig_db = generate_playbooks.DB
    orig_config_db = config.DB_PATH
    generate_playbooks.DB = db_path
    config.DB_PATH = db_path

    yield {"db_path": db_path, "workspace": str(ws), "tmp": tmp_path}

    generate_playbooks.DB = orig_db
    config.DB_PATH = orig_config_db


def test_generate_playbooks_writes_sidecars(fixture_env):
    ws = fixture_env["workspace"]
    generate_playbooks._write_playbooks_for_workspace(
        sqlite3.connect(fixture_env["db_path"]), ws, "hc-all"
    )

    playbook_dir = Path(ws) / ".story" / "knowledge" / "playbooks"
    assert (playbook_dir / "debug.md").exists()
    assert (playbook_dir / "debug.md.json").exists()

    meta = json.loads((playbook_dir / "debug.md.json").read_text(encoding="utf-8"))
    assert meta["type"] == "playbook"
    assert meta["theme"] == "debug"
    assert meta["session_count"] >= 3
    assert any("BorrowController.java" in f["path"] for f in meta["top_files"])


def test_generate_playbooks_by_story_sidecar(fixture_env):
    ws = fixture_env["workspace"]
    generate_playbooks._write_playbooks_for_workspace(
        sqlite3.connect(fixture_env["db_path"]), ws, "hc-all"
    )

    story_md = Path(ws) / ".story" / "knowledge" / "playbooks" / "by-story" / "STORY-42.md"
    story_json = story_md.with_suffix(".md.json")
    assert story_md.exists()
    assert story_json.exists()

    meta = json.loads(story_json.read_text(encoding="utf-8"))
    assert meta["linked_story"] == "STORY-42"
    assert meta["session_count"] >= 3


def test_failure_mode_writes_unified_failure_knowledge(fixture_env):
    ws = fixture_env["workspace"]
    conn = sqlite3.connect(fixture_env["db_path"])
    failure_mode._analyze_and_write(conn, ws_tag="hc-all", workspace=ws)
    conn.close()

    failures_json = Path(ws) / ".story" / "knowledge" / "failures" / "failure-knowledge.json"
    assert failures_json.exists()

    data = json.loads(failures_json.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["total_failures"] >= 2
    ids = {f["id"] for f in data["failures"]}
    assert "failure:编译/构建错误" in ids
    entry = next(f for f in data["failures"] if f["id"] == "failure:编译/构建错误")
    assert entry["common_tools"]


def test_knowledge_index_ingests_miner_outputs(fixture_env):
    ws = fixture_env["workspace"]
    conn = sqlite3.connect(fixture_env["db_path"])
    generate_playbooks._write_playbooks_for_workspace(conn, ws, "hc-all")
    failure_mode._analyze_and_write(conn, ws_tag="hc-all", workspace=ws)
    conn.close()

    from knowledge.generator import generate_index

    index = generate_index(os.path.join(ws, ".story", "knowledge"))
    types = {e["type"] for e in index["entries"]}
    assert "playbook" in types
    assert "failure" in types

    # By-story playbook links to its failure(s)
    story_pb = next(e for e in index["entries"] if e.get("linked_story") == "STORY-42")
    assert any(link.startswith("failure:") for link in story_pb.get("links", []))
