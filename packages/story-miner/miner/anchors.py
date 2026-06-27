"""Read story<->session anchors written by story-lifecycle.

story-lifecycle adapters write one JSON line per inject_prompt call to
``<workspace>/.story/runs/<story_key>/anchors.jsonl``.  miner.link uses these
anchors to bind sessions to stories without guessing from cwd+time windows.
"""
import json
import os
from datetime import datetime
from typing import Iterator

REQUIRED_KEYS = {"story_key", "stage", "adapter", "cwd", "ts", "prompt_hash"}


def _anchor_path(workspace: str, story_key: str) -> str:
    return os.path.join(workspace, ".story", "runs", story_key, "anchors.jsonl")


def read_anchors(workspace: str, story_key: str) -> list[dict]:
    """Return all anchor records for a workspace+story_key.

    Returns an empty list if the file does not exist or cannot be read.
    Each record is validated to contain the contract keys.
    """
    path = _anchor_path(workspace, story_key)
    if not os.path.exists(path):
        return []
    anchors = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            if REQUIRED_KEYS.issubset(rec):
                anchors.append(rec)
    return anchors


def iter_workspace_anchors(workspace: str) -> Iterator[tuple[str, list[dict]]]:
    """Yield (story_key, anchors) for every story under ``<workspace>/.story/runs``.

    Skips directories that cannot be read.
    """
    runs_dir = os.path.join(workspace, ".story", "runs")
    if not os.path.isdir(runs_dir):
        return
    for entry in os.scandir(runs_dir):
        if not entry.is_dir():
            continue
        anchors = read_anchors(workspace, entry.name)
        if anchors:
            yield entry.name, anchors
