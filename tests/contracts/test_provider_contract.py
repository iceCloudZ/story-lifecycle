"""Contract test: miner TranscriptStoryContextProvider protocol.

story-lifecycle inject_prompt(stage) calls
``provider.get_context(story_key, workspace, stage) -> str | None``.
This test locks the signature and behavior using a temp fixture DB.
"""
import os
import re
import sqlite3
import tempfile

import pytest

from miner.story_context_provider import TranscriptStoryContextProvider


@pytest.fixture
def provider():
    """Provider backed by a temp fixture database."""
    db_path = _create_fixture_db()
    return TranscriptStoryContextProvider({"db_path": db_path})


def _create_fixture_db():
    """Create a temp SQLite db with the schema + fixture data miner expects."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE stories(
                story_id TEXT PRIMARY KEY,
                workspace TEXT,
                title TEXT,
                status TEXT,
                stage TEXT,
                first_ts TEXT,
                last_ts TEXT
            );
            CREATE TABLE sessions(
                sid TEXT PRIMARY KEY,
                src TEXT,
                ws TEXT,
                ts TEXT,
                title TEXT,
                turns INTEGER,
                ntools INTEGER,
                nerrs INTEGER,
                first_ucmd TEXT,
                cwd TEXT,
                branch TEXT,
                story_id TEXT
            );
            CREATE TABLE events(
                id INTEGER PRIMARY KEY,
                sid TEXT,
                src TEXT,
                ws TEXT,
                ts TEXT,
                kind TEXT,
                name TEXT,
                cmd TEXT,
                code TEXT,
                ok INTEGER,
                text TEXT,
                path TEXT
            );
            """
        )
        db.execute(
            "INSERT INTO stories VALUES (?,?,?,?,?,?,?)",
            ("STORY-42", "hc-all", "fix login", "active", "design",
             "2026-06-20T09:00:00", "2026-06-20T18:00:00"),
        )
        db.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("s1", "claude", "hc-all", "2026-06-20T10:00:00",
             "login debug", 5, 3, 0, "debug login failure", "D:/hc-all", "main", "STORY-42"),
        )
        db.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (1, "s1", "claude", "hc-all", "2026-06-20T10:00:00",
             "tool", "Read", "", "", 1, "", "src/auth.py"),
        )
    return path


def test_get_context_signature_and_returns_str_for_known_story(provider):
    """Provider must return a non-empty str for a known story/stage."""
    ctx = provider.get_context("STORY-42", "D:/hc-all", "design")
    assert isinstance(ctx, str)
    assert len(ctx) > 0
    assert "历史上下文" in ctx


def test_get_context_returns_none_for_unknown_story(provider):
    """Provider must return None for unknown stories (never raises)."""
    assert provider.get_context("NOPE-999", "D:/hc-all", "design") is None


def test_get_context_never_raises(provider):
    """Provider contract: no exceptions bubble up."""
    assert provider.get_context("BAD-WS", "not-a-path", "design") is None


def test_get_context_no_pii_leak(provider):
    """Provider output must mask phone numbers and emails."""
    ctx = provider.get_context("STORY-42", "D:/hc-all", "design") or ""
    assert not re.search(r"\b09\d{9}\b", ctx)
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", ctx)


def test_get_context_accepts_tapd_prefixed_key(provider):
    """story-lifecycle story_key may be 'tapd-STORY-42'; numeric fallback must work."""
    ctx = provider.get_context("tapd-STORY-42", "D:/hc-all", "design")
    assert isinstance(ctx, str)
    assert len(ctx) > 0
