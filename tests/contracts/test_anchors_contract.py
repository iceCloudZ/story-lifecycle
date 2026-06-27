"""Contract test: story-lifecycle writes anchors, miner reads them.

This test locks the anchor JSONL schema between the two packages.
If story-lifecycle changes the fields it writes, or miner changes the
fields it requires, this test fails.
"""
import hashlib
import json
import os

import pytest

from miner.anchors import REQUIRED_KEYS, read_anchors
from story_lifecycle.adapters.base import BaseAdapter


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
