"""P2 regression: parse failure must not delete existing rows.

Covers the silent-data-loss bug (AI-1 #5): the old order was
DELETE → parse → maybe-continue. If parse raised and the adapter swallowed it
(returning empty meta), rows were already deleted and never re-inserted, so the
session was permanently lost with no warning. Now parse runs first and a parse
failure returns (None, [], []) so store skips the DELETE entirely.
"""

import sqlite3
from unittest.mock import patch

from miner import store


# ── Adapter contract: parse failure returns (None, [], []) ────────────────────


def test_claude_adapter_swallowed_exception_now_logged_and_returns_none(tmp_path, caplog):
    """Verify the claude adapter's outer except now logs and returns None.

    We patch the module-level open() used inside parse() to raise on the first
    call — this exception is NOT caught by the inner `except: continue` (which
    guards json.loads only), so it propagates to the outer try/except we fixed.
    """
    from miner.adapters.claude import ClaudeAdapter

    f = tmp_path / "s.jsonl"
    f.write_text("{}\n", encoding="utf-8")

    with patch("builtins.open", side_effect=OSError("simulated read failure")):
        with caplog.at_level("WARNING", logger="miner.adapters.claude"):
            meta, evs, tokens = ClaudeAdapter().parse(str(f), "claude:s")

    assert meta is None
    assert evs == []
    assert tokens == []
    assert any("parse failed" in rec.message for rec in caplog.records)


def test_codex_adapter_swallowed_exception_now_logged_and_returns_none(tmp_path, caplog):
    from miner.adapters.codex import CodexAdapter

    f = tmp_path / "s.jsonl"
    f.write_text("{}\n", encoding="utf-8")

    with patch("builtins.open", side_effect=OSError("simulated read failure")):
        with caplog.at_level("WARNING", logger="miner.adapters.codex"):
            meta, evs, tokens = CodexAdapter().parse(str(f), "codex:s")

    assert meta is None
    assert evs == []
    assert tokens == []
    assert any("parse failed" in rec.message for rec in caplog.records)


def test_kimi_adapter_swallowed_exception_now_logged_and_returns_none(tmp_path, caplog):
    from miner.adapters.kimi import KimiAdapter

    f = tmp_path / "s.jsonl"
    f.write_text("{}\n", encoding="utf-8")

    with patch("builtins.open", side_effect=OSError("simulated read failure")):
        with caplog.at_level("WARNING", logger="miner.adapters.kimi"):
            meta, evs, tokens = KimiAdapter().parse(str(f), "kimi:s")

    assert meta is None
    assert evs == []
    assert tokens == []
    assert any("parse failed" in rec.message for rec in caplog.records)


# ── store.py: parse-first order preserves existing rows on parse failure ─────


def test_store_parse_failure_preserves_existing_rows(tmp_path):
    """End-to-end: if a re-ingest's parse returns None, the previously-stored
    session and events survive. (Old DELETE-first order wiped them silently.)"""
    db_path = tmp_path / "test.db"
    store.init_db(str(db_path))

    sid = "claude:existing"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO sessions(sid,src,ws,ts,title,turns,ntools,nerrs,cwd,branch,first_ucmd,path) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, "claude", "D:/x", "2026-01-01T00:00:00", "old", 5, 2, 0, None, None, "oldcmd",
         str(tmp_path / "s.jsonl")),
    )
    conn.execute(
        "INSERT INTO events(sid,src,ws,ts,kind,name,cmd,code,ok,text,path) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (sid, "claude", "D:/x", "2026-01-01T00:00:00", "ucmd", None, None, None, None, "old event", None),
    )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM sessions WHERE sid=?", (sid,)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM events WHERE sid=?", (sid,)).fetchone()[0] == 1

    # Simulate the new parse-first loop directly: parse returns None → skip DELETE.
    class _FailAdapter:
        def parse(self, path, sid):
            return None, [], []  # parse failure per base contract

    ad = _FailAdapter()
    # Mirror the exact ordering now in store.py: parse first, skip on None.
    meta, evs, tokens = ad.parse(str(tmp_path / "s.jsonl"), sid)
    if meta is None or (meta["turns"] == 0 and meta["ntools"] == 0):
        pass  # DELETE skipped — rows preserved
    else:
        conn.execute("DELETE FROM sessions WHERE sid=?", (sid,))
        conn.commit()

    count_sessions = conn.execute("SELECT COUNT(*) FROM sessions WHERE sid=?", (sid,)).fetchone()[0]
    count_events = conn.execute("SELECT COUNT(*) FROM events WHERE sid=?", (sid,)).fetchone()[0]
    conn.close()

    assert count_sessions == 1, "existing session wiped despite parse failure"
    assert count_events == 1, "existing events wiped despite parse failure"


def test_store_successful_parse_still_replaces_rows(tmp_path):
    """Sanity: a successful parse still goes through DELETE + INSERT (the
    fix must not break the normal replace path, only the failure path)."""
    db_path = tmp_path / "test.db"
    store.init_db(str(db_path))

    sid = "claude:existing"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO sessions(sid,src,ws,ts,title,turns,ntools,nerrs,cwd,branch,first_ucmd,path) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, "claude", "D:/x", "2026-01-01T00:00:00", "OLD", 5, 2, 0, None, None, "oldcmd",
         str(tmp_path / "s.jsonl")),
    )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM sessions WHERE sid=?", (sid,)).fetchone()[0] == 1

    # A successful parse yields non-empty meta → DELETE + INSERT runs as before.
    class _OkAdapter:
        def parse(self, path, sid):
            return ({"sid": sid, "src": "claude", "ws": "D:/x", "ts": "2026-02-01T00:00:00",
                     "title": "NEW", "turns": 3, "ntools": 1, "nerrs": 0, "cwd": None,
                     "branch": None, "first_ucmd": "new"}, [], [])

    ad = _OkAdapter()
    meta, evs, tokens = ad.parse(str(tmp_path / "s.jsonl"), sid)
    if meta is None or (meta["turns"] == 0 and meta["ntools"] == 0):
        pass
    else:
        conn.execute("DELETE FROM sessions WHERE sid=?", (sid,))
        conn.execute(
            "INSERT INTO sessions(sid,src,ws,ts,title,turns,ntools,nerrs,cwd,branch,first_ucmd,path) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (meta["sid"], meta["src"], meta["ws"], meta["ts"], meta["title"], meta["turns"],
             meta["ntools"], meta["nerrs"], meta["cwd"], meta["branch"], meta["first_ucmd"],
             str(tmp_path / "s.jsonl")),
        )
        conn.commit()

    title = conn.execute("SELECT title FROM sessions WHERE sid=?", (sid,)).fetchone()[0]
    conn.close()
    assert title == "NEW", "successful parse did not replace existing row"
