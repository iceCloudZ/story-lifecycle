"""Tests for TranscriptStoryContextProvider against the real transcripts.db.

Run from the project root: ``PYTHONPATH=. python -m pytest tests/``
"""

import os
import re

import pytest

from miner.story_context_provider import TranscriptStoryContextProvider

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(_PROJ, "data", "transcripts.db")

pytestmark = pytest.mark.skipif(
    not os.path.exists(DB), reason="data/transcripts.db not present"
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
