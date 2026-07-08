"""Tests for PTY WebSocket close-code semantics.

These tests guard against the infinite-reconnect bug where a dead PTY session
was accepted and immediately closed, but the frontend kept retrying every 3s.
The backend now returns explicit close codes so the UI can stop reconnecting
when the session is gone or the process has exited.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from story_lifecycle.infra.terminal import pty as pty_mod
from story_lifecycle.infra.terminal.pty import ManagedPty
from story_lifecycle.orchestrator.service.api import app


class _FakeProcess:
    """Minimal process stand-in for the ``alive`` property."""

    def __init__(self, returncode=None):
        self._returncode = returncode

    def poll(self):
        return self._returncode


def _fake_pty(story_id: str = "t", alive: bool = True) -> ManagedPty:
    """Construct ManagedPty without spawning a process or starting read thread."""
    with patch.object(ManagedPty, "_spawn", lambda self, env: None), patch.object(
        ManagedPty, "_read_loop", lambda self: None
    ):
        pty = ManagedPty(story_id, ["fake"], "/tmp", purpose="test")
        pty._mode = "subprocess"
        pty._process = _FakeProcess(None if alive else 0)
        return pty


@pytest.fixture
def isolated_pty_registry(monkeypatch):
    """Provide an empty per-test PTY registry and clean it up afterwards."""
    original = pty_mod._ptys
    monkeypatch.setattr(pty_mod, "_ptys", {})
    yield
    pty_mod._ptys = original


class TestPtyWebSocketCloseCodes:
    def test_session_not_found_returns_4404(self, isolated_pty_registry):
        """Connecting to a non-existent session must yield close code 4404."""
        client = TestClient(app)
        with client.websocket_connect("/ws/pty/STORY-1/pty-missing") as ws:
            msg = ws.receive_json()
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()

        assert msg["type"] == "error"
        assert msg["code"] == "session_not_found"
        assert exc.value.code == 4404

    def test_dead_session_returns_1000(self, isolated_pty_registry):
        """A session whose PTY process has exited must yield close code 1000."""
        pty_mod._ptys["STORY-1"] = {"pty-dead": _fake_pty("STORY-1", alive=False)}

        client = TestClient(app)
        with client.websocket_connect("/ws/pty/STORY-1/pty-dead") as ws:
            msg = ws.receive_json()
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()

        assert msg["type"] == "exit"
        assert msg["reason"] == "process_ended"
        assert exc.value.code == 1000

    def test_alive_session_streams_data(self, isolated_pty_registry):
        """An alive session must stream queued PTY output to the client."""
        pty = _fake_pty("STORY-1", alive=True)
        pty_mod._ptys["STORY-1"] = {"pty-alive": pty}

        # Put a chunk into the PTY queue; the handler will forward it.
        pty._queue.put_nowait(b"hello terminal")

        client = TestClient(app)
        with client.websocket_connect("/ws/pty/STORY-1/pty-alive") as ws:
            data = ws.receive_bytes()
            assert data == b"hello terminal"
