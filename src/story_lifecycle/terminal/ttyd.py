"""ttyd + tmux management — per-story terminal sessions."""

import os
import subprocess
import time
from pathlib import Path
from typing import Optional

# Port range for dynamic ttyd allocation
BASE_PORT = 7701
MAX_PORT = 7799
_next_port = BASE_PORT
_story_ports: dict[str, int] = {}

# Platform availability
_WINDOWS = os.name == 'nt'
_HAS_TMUX = False

if not _WINDOWS:
    try:
        r = subprocess.run(["which", "tmux"], capture_output=True, text=True, timeout=5)
        _HAS_TMUX = bool(r.stdout.strip())
    except Exception:
        _HAS_TMUX = False


def _run(cmd: list, **kwargs):
    """Run a command, silently no-op if tmux not available (e.g. on Windows)."""
    if not _HAS_TMUX:
        return subprocess.CompletedProcess(cmd, 0, b"", b"")
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("timeout", 10)
    try:
        return subprocess.run(cmd, **kwargs)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 0, b"", b"")


def session_name(story_key: str) -> str:
    """tmux session name for a story: s-STORY-1065520"""
    return f"s-{story_key}"


def allocate_port(story_key: str) -> int:
    """Allocate a unique port for a story's ttyd. Reuses existing if assigned."""
    global _next_port
    if story_key in _story_ports:
        return _story_ports[story_key]
    port = _next_port
    _next_port += 1
    if _next_port > MAX_PORT:
        _next_port = BASE_PORT
    _story_ports[story_key] = port
    return port


def release_port(story_key: str):
    _story_ports.pop(story_key, None)


def ensure_ttyd(story_key: str, workspace: str) -> str:
    """Ensure ttyd is running for this story. Returns the ttyd URL path."""
    port = allocate_port(story_key)
    session = session_name(story_key)

    if not _HAS_TMUX:
        return f"/ttyd-s/{port}/"

    if not _tmux_session_alive(session):
        _run(["tmux", "new-session", "-d", "-s", session, "-c", workspace])
        time.sleep(0.5)

    if not _ttyd_running(port):
        subprocess.Popen(
            ["ttyd", "--writable", "--port", str(port),
             "--base-path", f"/ttyd-s/{port}/",
             "tmux", "new-session", "-A", "-s", session],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)

    return f"/ttyd-s/{port}/"


def stop_ttyd(story_key: str):
    port = _story_ports.get(story_key)
    if port:
        _run(["pkill", "-f", f"ttyd.*port {port}"])
        release_port(story_key)


def get_ttyd_url(story_key: str) -> Optional[str]:
    port = _story_ports.get(story_key)
    if port and _ttyd_running(port):
        return f"/ttyd-s/{port}/"
    return None


def cleanup_orphaned_sessions():
    """Kill tmux sessions with no active story."""
    if not _HAS_TMUX:
        return
    result = _run(["tmux", "list-sessions", "-F", "#{session_name}"],
                  capture_output=True, text=True, timeout=5)
    for line in result.stdout.strip().split("\n"):
        name = line.strip()
        if name and name.startswith("s-"):
            story_key = name[2:]
            if story_key not in _story_ports:
                _run(["tmux", "kill-session", "-t", name])


def _tmux_session_alive(session: str) -> bool:
    try:
        r = _run(["tmux", "has-session", "-t", session])
        return r.returncode == 0
    except Exception:
        return False


def _ttyd_running(port: int) -> bool:
    try:
        r = _run(["pgrep", "-f", f"ttyd.*port {port}"])
        return r.returncode == 0
    except Exception:
        return False


def send_keys(session: str, *keys: str):
    """Send keys to tmux. Separate arguments are sent as separate tmux send-keys args.
       Use the literal string 'Enter' to press Enter (must be a separate arg)."""
    _run(["tmux", "send-keys", "-t", session, *keys])


def capture_pane(session: str, lines: int = 20) -> str:
    r = _run(["tmux", "capture-pane", "-t", session, "-p", "-S", f"-{lines}"],
             capture_output=True, text=True, timeout=5)
    return r.stdout or ""
