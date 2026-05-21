"""ttyd + tmux management — per-story terminal sessions."""

import subprocess
import time
from pathlib import Path
from typing import Optional

# Port range for dynamic ttyd allocation
BASE_PORT = 7701
MAX_PORT = 7799
_next_port = BASE_PORT
_story_ports: dict[str, int] = {}


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
        _next_port = BASE_PORT  # wrap around (assumes old ports are freed)
    _story_ports[story_key] = port
    return port


def release_port(story_key: str):
    _story_ports.pop(story_key, None)


def ensure_ttyd(story_key: str, workspace: str) -> str:
    """Ensure ttyd is running for this story. Returns the ttyd URL path.
       Idempotent — if ttyd is already running, just returns the URL."""
    port = allocate_port(story_key)
    session = session_name(story_key)

    # Create tmux session if not exists (with correct CWD)
    if not _tmux_session_alive(session):
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", session, "-c", workspace],
            capture_output=True, timeout=10)
        time.sleep(0.5)

    # Check if ttyd is already running on this port
    if not _ttyd_running(port):
        subprocess.Popen(
            ["ttyd", "--writable", "--port", str(port),
             "--base-path", f"/ttyd-s/{port}/",
             "tmux", "new-session", "-A", "-s", session],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)  # let ttyd bind

    return f"/ttyd-s/{port}/"


def stop_ttyd(story_key: str):
    """Kill ttyd process for this story's port."""
    port = _story_ports.get(story_key)
    if port:
        subprocess.run(["pkill", "-f", f"ttyd.*port {port}"], capture_output=True)
        release_port(story_key)


def get_ttyd_url(story_key: str) -> Optional[str]:
    """Get the ttyd URL for a story, or None if not started."""
    port = _story_ports.get(story_key)
    if port and _ttyd_running(port):
        return f"/ttyd-s/{port}/"
    return None


def cleanup_orphaned_sessions():
    """Kill tmux sessions that start with 's-' but have no active story."""
    result = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                            capture_output=True, text=True, timeout=5)
    active_sessions = set()
    for line in result.stdout.strip().split("\n"):
        name = line.strip()
        if name and name.startswith("s-"):
            active_sessions.add(name)

    # In Phase 1, we just log. Phase 2: cross-reference with DB.
    for session in active_sessions:
        story_key = session[2:]  # strip "s-" prefix
        if story_key not in _story_ports:
            # Orphan — kill it
            subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)


def _tmux_session_alive(session: str) -> bool:
    try:
        r = subprocess.run(["tmux", "has-session", "-t", session],
                           capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def _ttyd_running(port: int) -> bool:
    try:
        r = subprocess.run(["pgrep", "-f", f"ttyd.*port {port}"],
                           capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def send_keys(session: str, keys: str):
    """Send keystrokes to a tmux session."""
    subprocess.run(["tmux", "send-keys", "-t", session, keys],
                   capture_output=True, timeout=5)


def capture_pane(session: str, lines: int = 20) -> str:
    """Capture last N lines from tmux session."""
    r = subprocess.run(["tmux", "capture-pane", "-t", session, "-p", f"-S", f"-{lines}"],
                       capture_output=True, text=True, timeout=5)
    return r.stdout
