"""Tests for clean-exit PTY teardown — `/exit` via bracketed paste before force-kill.

Why this exists: ``ManagedPty.kill()`` force-kills the process tree (Job Object /
taskkill /T). claude Code only flushes its ``~/.claude/projects/<proj>/<uuid>.jsonl``
transcript on a *clean* `/exit`; a force-kill mid-run truncates it, so
``--resume`` later picks up an incomplete history (this once masqueraded as
"--session-id is a no-op"). On serve restart / shutdown we therefore send
``/exit`` first, wait for the PTY to die, and only force-kill as a fallback.

These tests verify the *protocol* (write sequence + alive polling + flag) using a
hand-written fake PTY — we can't spawn a real interactive claude in a unit test.
"""

import pytest

import story_lifecycle.infra.terminal.pty as pty_mod


class _FakePty:
    """Minimal stand-in for ManagedPty.

    Records writes, exposes a controllable ``alive`` sequence (each access pops
    the next value, then falls back to ``default_alive``), and counts kill calls.
    """

    def __init__(self, alive_seq=(), default_alive=True):
        self._alive_seq = list(alive_seq)
        self._default = default_alive
        self.writes = []
        self.kill_calls = 0

    @property
    def alive(self):
        if self._alive_seq:
            return self._alive_seq.pop(0)
        return self._default

    def write(self, data):
        self.writes.append(bytes(data))

    def kill(self):
        self.kill_calls += 1


@pytest.fixture
def fast_timings(monkeypatch):
    """Zero the clean-exit sleeps so timeout tests don't block for real seconds."""
    monkeypatch.setattr(pty_mod, "_CLEAN_EXIT_PASTE_DELAY", 0)
    monkeypatch.setattr(pty_mod, "_CLEAN_EXIT_POLL_INTERVAL", 0)


def test_clean_exit_sends_bracketed_exit_then_carriage_return(fast_timings):
    """clean_exit must bracketed-paste `/exit`, then submit with `\r`.

    Bare PTY writes are treated as paste by Ink and don't submit; bracketed
    paste (`\\x1b[200~ … \\x1b[201~`) fills the input box and `\r` submits it.
    """
    # PTY exits immediately on the first alive poll.
    pty = _FakePty(alive_seq=[False])

    result = pty_mod.clean_exit_pty(pty, timeout=1.0)

    assert result is True
    assert pty.writes[0] == b"\x1b[200~/exit\x1b[201~"
    assert pty.writes[1] == b"\r"
    assert len(pty.writes) == 2  # exactly those two writes, nothing more


def test_clean_exit_returns_true_when_pty_dies_within_timeout(fast_timings):
    """If the PTY dies after a couple of polls, clean_exit returns True."""
    pty = _FakePty(alive_seq=[True, True, False], default_alive=True)

    result = pty_mod.clean_exit_pty(pty, timeout=1.0)

    assert result is True


def test_clean_exit_returns_false_when_pty_never_exits(fast_timings, monkeypatch):
    """If the PTY is still alive when the deadline passes, return False so the
    caller knows to force-kill."""
    monkeypatch.setattr(pty_mod, "_CLEAN_EXIT_POLL_INTERVAL", 0)
    pty = _FakePty(default_alive=True)  # never dies

    result = pty_mod.clean_exit_pty(pty, timeout=0.05)

    assert result is False
    # it still tried the clean exit (sent /exit + \r) before giving up
    assert b"\x1b[200~/exit\x1b[201~" in pty.writes
    assert b"\r" in pty.writes


def test_cleanup_all_tries_clean_exit_then_force_kills_each(fast_timings):
    """cleanup_all(prefer_clean_exit=True) sends /exit to every PTY then kills it."""
    fake_a = _FakePty(alive_seq=[False])
    fake_b = _FakePty(alive_seq=[False])
    pty_mod._ptys = {"story-1": {"s1": fake_a}, "story-2": {"s2": fake_b}}

    pty_mod.cleanup_all(prefer_clean_exit=True)

    for fake in (fake_a, fake_b):
        assert b"\x1b[200~/exit\x1b[201~" in fake.writes
        assert b"\r" in fake.writes
        assert fake.kill_calls == 1  # force-kill still runs as the backstop
    assert pty_mod._ptys == {}  # registry cleared


def test_cleanup_all_force_kills_without_clean_exit_when_disabled(fast_timings):
    """cleanup_all(prefer_clean_exit=False) skips the /exit dance and just kills."""
    fake = _FakePty(default_alive=True)
    pty_mod._ptys = {"story-1": {"s1": fake}}

    pty_mod.cleanup_all(prefer_clean_exit=False)

    assert fake.writes == []  # no /exit attempt
    assert fake.kill_calls == 1
    assert pty_mod._ptys == {}


def test_api_kill_all_pty_endpoint_cleans_all_sessions(monkeypatch):
    """DELETE /api/pty (no story_id) cleanly tears down every PTY session.

    Guards against a route collision with DELETE /api/pty/{story_id}: a bare
    /api/pty must resolve to its own handler and call cleanup_all with the
    clean-exit flag, not 404 or 422.
    """
    import story_lifecycle.orchestrator.service.api as api_mod
    from fastapi.testclient import TestClient

    captured = {}

    def _fake_cleanup(prefer_clean_exit=True):
        captured["prefer_clean_exit"] = prefer_clean_exit

    monkeypatch.setattr(api_mod, "cleanup_all", _fake_cleanup)

    client = TestClient(api_mod.app)
    r = client.delete("/api/pty")

    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert captured == {"prefer_clean_exit": True}
