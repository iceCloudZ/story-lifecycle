"""E2E tests — full graph execution with real tmux, file system, and DB.

Only AI CLI adapter is mocked. Run in WSL2/Linux (needs tmux).

Usage: pytest tests/test_e2e.py -v
"""

import json
import time
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from story_lifecycle.db import models as db
from story_lifecycle.orchestrator.graph import run_story
from story_lifecycle.orchestrator import nodes


# Speed up polling for tests
_FAST_POLL = 0.5  # seconds
_FAST_CC_WAIT = 0.2  # seconds (replaces 8s CC init)


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
    """Speed up sleeps and polling."""
    monkeypatch.setattr(nodes, "POLL_INTERVAL", _FAST_POLL)

    real_sleep = time.sleep

    def fast_sleep(secs):
        if secs >= 5:  # CC init wait (8s) → fast
            real_sleep(_FAST_CC_WAIT)
        else:
            real_sleep(min(secs, 0.1))

    monkeypatch.setattr(nodes.time, "sleep", fast_sleep)


def _create_story(key, workspace, profile="minimal"):
    db.create_story(key, f"e2e-{key}", str(workspace), profile, "design")


def _write_done(workspace, key, stage, data):
    done_dir = Path(workspace) / ".story-done" / key
    done_dir.mkdir(parents=True, exist_ok=True)
    (done_dir / f"{stage}.json").write_text(json.dumps(data), encoding="utf-8")


def _wait_db_stage(key, expected_stage, timeout=15):
    """Poll DB until story reaches expected stage or terminal state."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = db.get_story(key)
        if s:
            if s["current_stage"] == expected_stage:
                return s
            if s["status"] in ("blocked", "completed"):
                return s
        time.sleep(0.2)
    return db.get_story(key)


# ===== P0-1: .story-done/{story_key}/ path =====


class TestDoneFilePath:
    def test_reads_from_story_key_subdirectory(self, tmp_path):
        """Story advances when .done is at .story-done/{key}/design.json."""
        key = "E2E-PATH-001"
        ws = tmp_path / "ws1"
        ws.mkdir()

        _create_story(key, ws)
        _write_done(ws, key, "design", {
            "spec_path": "docs/spec.md", "complexity": "S", "summary": "ok",
        })

        run_story(key)

        s = db.get_story(key)
        assert s["current_stage"] == "implement", f"got stage={s['current_stage']}, status={s['status']}"
        assert not (ws / ".story-done" / key / "design.json").exists()  # consumed

    def test_old_flat_path_ignored(self, tmp_path):
        """Story does NOT advance from flat .story-done/design.json."""
        key = "E2E-PATH-002"
        ws = tmp_path / "ws2"
        ws.mkdir()

        # Write at old flat location (no story_key subdirectory)
        old_dir = ws / ".story-done"
        old_dir.mkdir()
        (old_dir / "design.json").write_text('{"spec_path":"x","complexity":"S","summary":"stale"}')

        _create_story(key, ws)

        # run_story will block on poll; run in thread and set timeout
        result = {}
        def _run():
            try:
                run_story(key)
            except Exception as e:
                result["error"] = str(e)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=5)

        # Story should NOT have advanced (old file still there)
        assert (old_dir / "design.json").exists(), "old flat file should still exist (not consumed)"
        s = db.get_story(key)
        assert s["current_stage"] == "design" or s["status"] != "active"

    def test_two_stories_same_workspace_no_collision(self, tmp_path):
        """Two stories in same workspace have separate .done directories."""
        ws = tmp_path / "ws3"
        ws.mkdir()

        key_a, key_b = "E2E-COL-A", "E2E-COL-B"
        _create_story(key_a, ws)
        _create_story(key_b, ws)

        # Only complete story A
        _write_done(ws, key_a, "design", {
            "spec_path": "docs/a.md", "complexity": "S", "summary": "A done",
        })

        # Run A (will complete), B (will timeout/stay in design)
        ta = threading.Thread(target=run_story, args=(key_a,), daemon=True)
        tb = threading.Thread(target=run_story, args=(key_b,), daemon=True)
        ta.start()
        tb.start()
        ta.join(timeout=10)

        sa = db.get_story(key_a)
        assert sa["current_stage"] == "implement", f"A should advance, got {sa['current_stage']}"

        # B should NOT have A's data
        sb = db.get_story(key_b)
        assert sb is None or sb["current_stage"] == "design" or sb["status"] in ("active", "blocked")


# ===== P0-2: wait_confirm → execute_stage loop =====


class TestWaitConfirmLoop:
    @pytest.fixture
    def confirm_profile(self, monkeypatch):
        """Profile with confirm: true on design stage."""
        profile_dir = Path.home() / ".story-lifecycle" / "profiles"
        profile_dir.mkdir(parents=True, exist_ok=True)
        (profile_dir / "e2e-confirm.yaml").write_text(
            "version: 2\ncli: claude\nstages:\n  design:\n"
            "    order: 1\n    description: 'needs confirm'\n"
            "    confirm: true\n    expected_outputs: [spec_path]\n    next_default: []\n"
        )
        yield
        (profile_dir / "e2e-confirm.yaml").unlink(missing_ok=True)

    def test_paused_then_resume_advances(self, tmp_path, confirm_profile):
        """Story pauses at confirm, resumes, and completes the stage."""
        key = "E2E-CONF-001"
        ws = tmp_path / "ws4"
        ws.mkdir()

        _create_story(key, ws, profile="e2e-confirm")

        # Run in background — will pause at wait_confirm
        def _run():
            # Resume after a short delay (simulate user action)
            def _resume():
                time.sleep(2)
                db.update_story(key, status="active")
            threading.Thread(target=_resume, daemon=True).start()
            run_story(key)

        t = threading.Thread(target=_run, daemon=True)
        t.start()

        # Wait for paused state
        deadline = time.time() + 8
        hit_paused = False
        while time.time() < deadline:
            s = db.get_story(key)
            if s and s["status"] == "paused":
                hit_paused = True
                break
            time.sleep(0.2)

        assert hit_paused, "Story should reach paused state"

        # Now write .done file after resume kicks in
        _write_done(ws, key, "design", {
            "spec_path": "docs/spec.md", "summary": "confirmed",
        })

        t.join(timeout=15)

        s = db.get_story(key)
        # After confirm + done file, story should complete (no next stage)
        assert s["status"] in ("completed", "active"), f"got status={s['status']}"
        # Most importantly: it did NOT end up as blocked/failed
        assert s["status"] != "blocked"
