"""Tests for ManagedPty.kill() — process teardown (incl. Windows Job Object)."""

import sys
import time


def test_kill_terminates_spawned_process(tmp_path):
    """kill() must stop the spawned process. On Windows it goes through the
    KILL_ON_JOB_CLOSE Job Object (or taskkill /T fallback); on Unix, killpg."""
    from story_lifecycle.infra.terminal.pty import ManagedPty

    cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
    pty = ManagedPty("kill-test", cmd, str(tmp_path), purpose="test")
    try:
        time.sleep(0.8)
        assert pty.alive, "process should be alive right after spawn"
        pty.kill()
        time.sleep(0.8)
        assert not pty.alive, "process should be dead after kill()"
    finally:
        pty.kill()
