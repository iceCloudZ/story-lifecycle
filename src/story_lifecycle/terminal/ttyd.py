"""Terminal session management — per-story sessions via Zellij (preferred) or tmux."""

import json
import os
import shutil
import subprocess
import threading
import time
import tempfile

from pathlib import Path
from typing import Optional

from .platform_ops import kill_by_port, port_in_use

STORY_HOME = Path.home() / ".story-lifecycle"

# Port range for dynamic ttyd allocation
BASE_PORT = 7701
MAX_PORT = 7799
_next_port = BASE_PORT
_story_ports: dict[str, int] = {}
_port_lock = threading.Lock()

# Multiplexer detection (zellij preferred over tmux, cross-platform)
_MPLEX = None  # "zellij" or "tmux"

for cmd in ("zellij", "tmux"):
    if shutil.which(cmd):
        _MPLEX = cmd
        break


def _run(cmd: list, **kwargs):
    """Run a command, silently no-op if no multiplexer available."""
    if not _MPLEX:
        return subprocess.CompletedProcess(cmd, 0, b"", b"")
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("timeout", 10)
    try:
        return subprocess.run(cmd, **kwargs)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 0, b"", b"")


# -------- persistent port registry --------


def _ports_file() -> Path:
    return STORY_HOME / "ports.json"


def _load_ports():
    """Load persisted port mappings into _story_ports."""
    global _next_port
    pf = _ports_file()
    if pf.exists():
        try:
            data = json.loads(pf.read_text(encoding="utf-8"))
            _story_ports.update(data)
            if _story_ports:
                _next_port = max(_story_ports.values()) + 1
        except (json.JSONDecodeError, ValueError):
            pass
    if _next_port > MAX_PORT:
        _next_port = BASE_PORT


def _save_ports():
    """Persist current _story_ports to disk."""
    STORY_HOME.mkdir(parents=True, exist_ok=True)
    _ports_file().write_text(json.dumps(_story_ports, indent=2), encoding="utf-8")


# Load persisted ports on module import
_load_ports()


def session_name(story_key: str) -> str:
    """Session name for a story: s-STORY-1065520"""
    return f"s-{story_key}"


def allocate_port(story_key: str) -> int:
    """Allocate a unique port for a story's ttyd. Reuses existing if assigned."""
    global _next_port
    with _port_lock:
        if story_key in _story_ports:
            return _story_ports[story_key]
        port = _next_port
        _next_port += 1
        if _next_port > MAX_PORT:
            _next_port = BASE_PORT
        _story_ports[story_key] = port
        _save_ports()
        return port


def release_port(story_key: str):
    with _port_lock:
        _story_ports.pop(story_key, None)
        _save_ports()


# -------- multiplexer abstraction --------


def create_session(name: str, workspace: str):
    """Create a detached/background session with the given CWD."""
    if not _MPLEX:
        return
    if _MPLEX == "zellij":
        os.system(
            f"zellij attach --create-background {name} options --default-cwd {workspace}"
        )
    elif _MPLEX == "tmux":
        _run(["tmux", "new-session", "-d", "-s", name, "-c", workspace])
    time.sleep(0.5)


def session_alive(name: str) -> bool:
    """Check if a session exists and is alive."""
    if _MPLEX == "zellij":
        r = _run(["zellij", "list-sessions"], text=True, timeout=5)
        if r.returncode == 0:
            return name in r.stdout.split()
        return False
    elif _MPLEX == "tmux":
        r = _run(["tmux", "has-session", "-t", name])
        return r.returncode == 0
    return False


def kill_session(name: str):
    """Kill a session."""
    if _MPLEX == "zellij":
        _run(["zellij", "kill-session", name])
    elif _MPLEX == "tmux":
        _run(["tmux", "kill-session", "-t", name])


def send_keys(name: str, *keys: str):
    """Send keys to a session's active pane.

    Special key names: "Enter", "C-c" (translated per multiplexer).
    Plain strings are written as characters.
    """
    if _MPLEX == "zellij":
        for k in keys:
            if k == "Enter":
                _run(["zellij", "--session", name, "action", "send-keys", "enter"])
            elif k == "C-c":
                _run(["zellij", "--session", name, "action", "send-keys", "ctrl-c"])
            elif k == "C-d":
                _run(["zellij", "--session", name, "action", "send-keys", "ctrl-d"])
            else:
                _run(["zellij", "--session", name, "action", "write-chars", k])
    elif _MPLEX == "tmux":
        _run(["tmux", "send-keys", "-t", name, *keys])


def capture_pane(name: str, lines: int = 20) -> str:
    """Capture the visible content of a session's pane."""
    if _MPLEX == "zellij":
        r = _run(
            ["zellij", "--session", name, "action", "dump-screen"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        output = r.stdout or ""
        return "\n".join(output.rstrip().split("\n")[-lines:])
    elif _MPLEX == "tmux":
        r = _run(
            ["tmux", "capture-pane", "-t", name, "-p", "-S", f"-{lines}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return r.stdout or ""
    return ""


def paste_text(name: str, text: str):
    """Paste text into a session's active pane."""
    if _MPLEX == "zellij":
        _run(["zellij", "--session", name, "action", "write-chars", text])
    elif _MPLEX == "tmux":
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(text)
            tmp = f.name
        buf = f"sp-{name}"
        _run(["tmux", "load-buffer", "-b", buf, tmp])
        _run(["tmux", "paste-buffer", "-b", buf, "-t", name])
        os.unlink(tmp)


def attach_cmd(name: str) -> str:
    """Return the shell command to attach to a session."""
    if _MPLEX == "zellij":
        return f"zellij attach {name}"
    return f"tmux attach -t {name}"


def enter_session_cmd(name: str, workspace: str) -> str:
    """Return the command to create and enter a session."""
    if _MPLEX == "zellij":
        return f"zellij attach --create {name} options --default-cwd {workspace}"
    return f"tmux new-session -A -s {name} -c {workspace}"


def list_sessions() -> list[str]:
    """List all session names."""
    if _MPLEX == "zellij":
        r = _run(["zellij", "list-sessions"], text=True, timeout=5)
        if r.returncode == 0:
            return [
                line.strip() for line in r.stdout.strip().split("\n") if line.strip()
            ]
        return []
    elif _MPLEX == "tmux":
        r = _run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0:
            return [
                line.strip() for line in r.stdout.strip().split("\n") if line.strip()
            ]
        return []
    return []


# -------- ttyd management --------


def ensure_ttyd(story_key: str, workspace: str) -> str:
    """Ensure a session exists for this story.

    If ttyd is available, starts a web terminal. Otherwise just creates
    the multiplexer session (attach via CLI).
    """
    port = allocate_port(story_key)
    session = session_name(story_key)

    if not _MPLEX:
        return f"/ttyd-s/{port}/"

    if not session_alive(session):
        create_session(session, workspace)

    if shutil.which("ttyd") and not _ttyd_running(port):
        mplex_cmd = enter_session_cmd(session, workspace).split()
        subprocess.Popen(
            [
                "ttyd",
                "--writable",
                "--port",
                str(port),
                "--base-path",
                f"/ttyd-s/{port}/",
            ]
            + mplex_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1)

    return f"/ttyd-s/{port}/"


def stop_ttyd(story_key: str):
    _launched.discard(story_key)
    port = _story_ports.get(story_key)
    if port:
        kill_by_port(port)
        release_port(story_key)


def get_ttyd_url(story_key: str) -> Optional[str]:
    port = _story_ports.get(story_key)
    if port and _ttyd_running(port):
        return f"/ttyd-s/{port}/"
    return None


def cleanup_orphaned_sessions():
    """Kill sessions with no active story."""
    if not _MPLEX:
        return
    for name in list_sessions():
        if name.startswith("s-"):
            story_key = name[2:]
            if story_key not in _story_ports:
                kill_session(name)


def _ttyd_running(port: int) -> bool:
    try:
        return port_in_use(port)
    except Exception:
        return False


# -------- independent CLI launch (no multiplexer) --------


_launched: set[str] = set()
_mplex_launched: set[str] = set()  # stories actually launched via multiplexer


def clear_launch_state(story_key: str):
    """Clear launch tracking for a story (called when stage completes)."""
    _launched.discard(story_key)
    _mplex_launched.discard(story_key)


def launch_cli(story_key: str, workspace: str, launch_cmd: str, prompt_file: str):
    """Launch a CLI tool independently so it survives the caller exiting."""
    # Allow re-launch for new stages by using stage-specific key
    launch_id = f"{story_key}:{prompt_file}"
    if launch_id in _launched:
        return
    _launched.add(launch_id)
    # Also track the story key for cleanup
    _launched.add(story_key)

    from . import platform_ops

    ws = platform_ops.to_posix_path(workspace)
    pf = platform_ops.to_posix_path(prompt_file)
    script = Path(tempfile.gettempdir()) / f"story-launch-{story_key}.sh"
    script.write_text(
        f"#!/bin/bash\n"
        f'cd "{ws}" 2>/dev/null || {{ echo "ERROR: cannot cd to {ws}"; exit 1; }}\n'
        f'echo "Starting: {launch_cmd}"\n'
        f"{launch_cmd} \"$(cat '{pf}')\"\n"
        f'echo ""\n'
        f'echo "Story {story_key} done (exit code: $?). Closing in 3s..."\n'
        f"sleep 3\n",
        encoding="utf-8",
    )
    script_posix = platform_ops.to_posix_path(str(script))
    platform_ops.open_terminal_window(
        f"Story {story_key}",
        [script_posix],
    )
