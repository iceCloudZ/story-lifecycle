"""Tests for TranscriptStoryContextProvider against the real transcripts.db.

Run from the project root: ``PYTHONPATH=. python -m pytest tests/``
"""

import os
import re
import sqlite3

import pytest

from miner.story_context_provider import TranscriptStoryContextProvider

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(_PROJ, "data", "transcripts.db")


def _db_has_stories(path: str) -> bool:
    if not os.path.exists(path):
        return False
    try:
        conn = sqlite3.connect(path)
        cur = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='stories'")
        ok = cur.fetchone() is not None
        conn.close()
        return ok
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_has_stories(DB), reason="data/transcripts.db not present or has no stories table"
)


@pytest.fixture(scope="module")
def provider():
    return TranscriptStoryContextProvider({"db_path": DB})


class TestTranscriptContextProvider:
    def test_known_story_returns_context(self, provider):
        ctx = provider.get_context("1065518", "D:/hc-all", "design")
        assert ctx is not None
        assert 0 < len(ctx) < 600  # target <500 chars, allow margin

    def test_context_has_relevant_signal(self, provider):
        ctx = provider.get_context("1065518", "D:/hc-all", "design") or ""
        # should reference the workspace, sessions, tools, or research cues
        assert any(k in ctx for k in ("hc-all", "hc-", "工具", "调研", "session", "历史", "transcript"))

    def test_no_pii_leak(self, provider):
        ctx = provider.get_context("1065518", "D:/hc-all", "design") or ""
        assert not re.search(r"\b09\d{9}\b", ctx)  # PH mobile
        assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", ctx)  # email

    def test_tapd_prefix_story_key_matches(self, provider):
        # story-lifecycle story_key may be "tapd-1065518" — numeric fallback
        ctx = provider.get_context("tapd-1065518", "D:/hc-all", "design")
        assert ctx is not None

    def test_unknown_story_returns_none(self, provider):
        ctx = provider.get_context("NONEXISTENT-ZZZ-99999", "D:/no-such-ws", "design")
        assert ctx is None

    def test_unrelated_story_does_not_match_same_workspace_neighbor(self, provider):
        """Regression: a story_key with no direct record must NOT fall back to a
        different story in the same workspace (cross-story contamination).

        1066988 has no row in `stories`; 1064837 lives in the same D:/hc-all
        workspace. The provider must return None for 1066988, not silently serve
        1064837's history.
        """
        ctx = provider.get_context("1066988", "D:/hc-all", "design")
        assert ctx is None
