"""E2E tests — full graph execution with real tmux, file system, and DB.

Only AI CLI adapter is mocked. Run in WSL2/Linux (needs tmux).

Usage: pytest tests/test_e2e.py -v
"""

import json
import os
import time
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from story_lifecycle.db import models as db
from story_lifecycle.orchestrator.graph import run_story
from story_lifecycle.orchestrator import nodes

_FAST_POLL = 0.3


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    """Fresh DB per test."""
    db_file = tmp_path / "story.db"
    monkeypatch.setattr(db, "get_db_path", lambda: db_file)
    db.init_db()
    yield


@pytest.fixture(autouse=True)
def mock_adapter():
    """Don't launch real AI CLI."""
    with patch("story_lifecycle.orchestrator.nodes.get_adapter") as m:
        adapter = MagicMock()
        adapter.switch_provider.return_value = None
        adapter.launch_cmd.return_value = "echo e2e-noop"
        m.return_value = adapter
        yield


@pytest.fixture(autouse=True)
def fast_timing(monkeypatch):
    """Speed up polling interval."""
    monkeypatch.setattr(nodes, "POLL_INTERVAL", _FAST_POLL)
    # Speed up all sleeps in nodes module
    real_sleep = time.sleep
    def fast(secs):
        real_sleep(min(secs, 0.05))
    monkeypatch.setattr("time.sleep", fast)


@pytest.fixture(autouse=True)
def cleanup_tmux():
    """Kill all tmux sessions after each test."""
    yield
    os.system("tmux kill-server 2>/dev/null")


def _create_story(key, workspace, profile="minimal"):
    db.create_story(key, f"e2e-{key}", str(workspace), profile, "design")


def _write_done(workspace, key, stage, data):
    done_dir = Path(workspace) / ".story-done" / key
    done_dir.mkdir(parents=True, exist_ok=True)
    (done_dir / f"{stage}.json").write_text(json.dumps(data), encoding="utf-8")


def _run_in_thread(key):
    """Run story in background thread (non-blocking)."""
    t = threading.Thread(target=run_story, args=(key,), daemon=True)
    t.start()
    return t


def _poll_db(key, check_fn, timeout=15):
    """Poll DB until check_fn(story) returns True."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = db.get_story(key)
        if s and check_fn(s):
            return s
        time.sleep(0.2)
    return db.get_story(key)


# ===== P0-1: .story-done/{story_key}/ path =====


class TestDoneFilePath:
    def test_reads_from_story_key_subdirectory(self, tmp_path):
        key = "E2E-PATH-001"
        ws = tmp_path / "ws1"
        ws.mkdir()

        _create_story(key, ws)
        _write_done(ws, key, "design", {
            "spec_path": "docs/spec.md", "complexity": "S", "summary": "ok",
        })

        _run_in_thread(key)

        s = _poll_db(key, lambda s: s["current_stage"] == "implement")
        assert s["current_stage"] == "implement", f"stage={s['current_stage']}, status={s['status']}"
        # .done file should be consumed
        assert not (ws / ".story-done" / key / "design.json").exists()

    def test_old_flat_path_not_consumed(self, tmp_path):
        key = "E2E-PATH-002"
        ws = tmp_path / "ws2"
        ws.mkdir()

        # Write at old flat location (no story_key subdir)
        old_dir = ws / ".story-done"
        old_dir.mkdir()
        (old_dir / "design.json").write_text(
            '{"spec_path":"x","complexity":"S","summary":"stale"}'
        )

        _create_story(key, ws)
        _run_in_thread(key)

        # Wait a bit — story should NOT advance from the flat file
        time.sleep(2)
        s = db.get_story(key)
        assert s["current_stage"] == "design" or s["status"] in ("blocked", "active")
        # Old flat file should still exist (not consumed)
        assert (old_dir / "design.json").exists()

    def test_two_stories_no_collision(self, tmp_path):
        ws = tmp_path / "ws3"
        ws.mkdir()

        key_a, key_b = "E2E-COL-A", "E2E-COL-B"
        _create_story(key_a, ws)
        _create_story(key_b, ws)

        # Only complete story A
        _write_done(ws, key_a, "design", {
            "spec_path": "docs/a.md", "complexity": "S", "summary": "A done",
        })

        _run_in_thread(key_a)
        _run_in_thread(key_b)

        # A should advance
        sa = _poll_db(key_a, lambda s: s["current_stage"] == "implement")
        assert sa["current_stage"] == "implement"

        # B should NOT advance (no done file for B)
        time.sleep(1)
        sb = db.get_story(key_b)
        assert sb["current_stage"] == "design" or sb["status"] in ("active", "blocked")


# ===== P0-2: wait_confirm → execute_stage loop =====


class TestWaitConfirmLoop:
    @pytest.fixture
    def confirm_profile(self):
        """Profile with confirm: true on design stage."""
        profile_dir = Path.home() / ".story-lifecycle" / "profiles"
        profile_dir.mkdir(parents=True, exist_ok=True)
        pf = profile_dir / "e2e-confirm.yaml"
        pf.write_text(
            "version: 2\ncli: claude\nstages:\n  design:\n"
            "    order: 1\n    description: 'needs confirm'\n"
            "    confirm: true\n    expected_outputs: [spec_path]\n    next_default: []\n"
        )
        yield
        pf.unlink(missing_ok=True)

    def test_pause_then_resume_completes(self, tmp_path, confirm_profile):
        key = "E2E-CONF-001"
        ws = tmp_path / "ws4"
        ws.mkdir()

        _create_story(key, ws, profile="e2e-confirm")

        # Run in background — will pause at wait_confirm
        _run_in_thread(key)

        # Wait for paused state
        s = _poll_db(key, lambda s: s["status"] == "paused", timeout=10)
        assert s["status"] == "paused", f"expected paused, got {s['status']}"

        # Write .done file, then resume
        _write_done(ws, key, "design", {
            "spec_path": "docs/spec.md", "summary": "confirmed",
        })
        db.update_story(key, status="active")

        # Should complete (no next stage in this profile)
        s = _poll_db(key, lambda s: s["status"] == "completed", timeout=15)
        assert s["status"] == "completed", f"expected completed, got {s['status']}"
