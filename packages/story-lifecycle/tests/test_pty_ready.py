"""PTY readiness detection — fix interactive-agent idle (real-run §7.1 follow-up).

``ensure_agent_pty`` used a fixed ``sleep(2.0)`` before injecting the prompt.
Interactive agents (claude / kimi-code) that take >2s to show their input prompt
(loading skills, indexing) swallow the early injection → the agent sits idle and
the stage times out. Fix: poll PTY output via the broadcast tap until a readiness
marker matches (or a timeout), then inject. Marker is None → no wait (legacy
path). Both claude and kimi PTY rails share this injection, so the fix serves
both.
"""

import asyncio
from unittest.mock import patch

from story_lifecycle.infra.terminal.pty import (
    ManagedPty,
    _wait_ready,
    ensure_agent_pty,
)


def _fake_pty() -> ManagedPty:
    """Construct ManagedPty without spawning a process or starting the read thread."""
    with patch.object(ManagedPty, "_spawn", lambda self, env: None), patch.object(
        ManagedPty, "_read_loop", lambda self: None
    ):
        return ManagedPty("t", ["fake"], "/tmp", purpose="test")


class TestWaitReady:
    def test_detects_marker_in_tap_output(self):
        pty = _fake_pty()
        pre = asyncio.Queue()
        for c in [b"Welcome to Claude Code\n", b"\n> "]:
            pre.put_nowait(c)
        pty.add_tap = lambda maxsize=512: pre
        pty.remove_tap = lambda tap: None
        # marker present → True (no need to wait full timeout)
        assert _wait_ready(pty, marker=r">\s*$", timeout=1.0) is True

    def test_returns_false_on_timeout_when_no_marker(self):
        pty = _fake_pty()
        empty = asyncio.Queue()
        pty.add_tap = lambda maxsize=512: empty
        pty.remove_tap = lambda tap: None
        assert _wait_ready(pty, marker=r">", timeout=0.3) is False

    def test_no_marker_skips_wait(self):
        # marker=None → legacy path, return True immediately (no tap polling).
        pty = _fake_pty()
        assert _wait_ready(pty, marker=None, timeout=5.0) is True


class TestEnsureAgentPtyReadiness:
    def test_waits_for_marker_before_injecting(self, monkeypatch):
        pty = _fake_pty()
        wrote = []
        pty.write = lambda data: wrote.append(data)
        pre = asyncio.Queue()
        pre.put_nowait(b"> ")  # agent already showing its prompt
        pty.add_tap = lambda maxsize=512: pre
        pty.remove_tap = lambda tap: None
        monkeypatch.setattr(
            "story_lifecycle.infra.terminal.pty.spawn_pty",
            lambda *a, **k: ("sid", pty),
        )
        ensure_agent_pty(
            "s", ["fake"], "/tmp", "do work",
            readiness_marker=r">", readiness_timeout=1.0,
        )
        # prompt injected after marker seen
        assert wrote == [b"do work\r"]

    def test_legacy_path_still_sleeps_when_no_marker(self, monkeypatch):
        # No marker → fall back to startup_delay (legacy). Smoke: still injects.
        pty = _fake_pty()
        wrote = []
        pty.write = lambda data: wrote.append(data)
        monkeypatch.setattr(
            "story_lifecycle.infra.terminal.pty.spawn_pty",
            lambda *a, **k: ("sid", pty),
        )
        ensure_agent_pty("s", ["fake"], "/tmp", "do work", startup_delay=0)
        assert wrote == [b"do work\r"]
