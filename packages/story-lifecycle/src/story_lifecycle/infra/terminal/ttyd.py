"""Terminal session management — per-story sessions via Zellij."""

import json
import os
import shlex
import shutil
import subprocess
import threading
import time
import tempfile

from pathlib import Path
from typing import Optional

from .platform_ops import kill_by_port, port_in_use
from ..story_paths import safe_segment

STORY_HOME = Path.home() / ".story-lifecycle"

# Port range for dynamic ttyd allocation
BASE_PORT = 7701
MAX_PORT = 7799
_next_port = BASE_PORT
_story_ports: dict[str, int] = {}
_port_lock = threading.Lock()

# Multiplexer detection (Zellij is cross-platform: Linux/macOS/Windows)
_MPLEX = "zellij" if shutil.which("zellij") else None


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
    cmd = [
        "zellij",
        "attach",
        "--create-background",
        name,
        "options",
        "--default-cwd",
        workspace,
    ]
    if os.name == "nt":
        cmd.extend(["--default-shell", "powershell.exe"])
    _run(
        cmd,
        capture_output=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.5)


def _strip_ansi(text: str) -> str:
    """Strip ANSI escape sequences from text."""
    import re

    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def session_alive(name: str) -> bool:
    """Check if a session exists and is alive."""
    if not _MPLEX:
        return False
    r = _run(["zellij", "list-sessions"], text=True, timeout=5)
    if r.returncode == 0:
        for line in _strip_ansi(r.stdout).splitlines():
            parts = line.strip().split()
            if not parts:
                continue
            if parts[0] == name and "EXITED" not in line:
                return True
    return False


class SessionState:
    """Granular session state for TUI entry decisions."""

    LIVE = "live"
    EXITED = "exited"
    MISSING = "missing"
    UNKNOWN = "unknown"


def resolve_session_state(name: str) -> str:
    """Resolve the detailed state of a session: live/exited/missing/unknown."""
    if not _MPLEX:
        return SessionState.UNKNOWN

    r = _run(["zellij", "list-sessions"], text=True, timeout=5)
    if r.returncode != 0:
        # Zellij returns 1 when no sessions exist (legitimate MISSING)
        # vs actual errors like permission/crash (UNKNOWN)
        err = (r.stderr or "").lower()
        if "no session" in err or "not found" in err or r.returncode == 1:
            return SessionState.MISSING
        return SessionState.UNKNOWN
    for line in _strip_ansi(r.stdout).splitlines():
        parts = line.strip().split()
        if not parts:
            continue
        if parts[0] == name:
            return SessionState.EXITED if "EXITED" in line else SessionState.LIVE
    return SessionState.MISSING


def delete_exited_session(name: str) -> bool:
    """Delete a dead Zellij session so a foreground layout can reuse the name."""
    if not _MPLEX:
        return False
    r = _run(["zellij", "list-sessions"], text=True, timeout=5)
    if r.returncode != 0:
        return False
    for line in _strip_ansi(r.stdout).splitlines():
        parts = line.strip().split()
        if parts and parts[0] == name and "EXITED" in line:
            deleted = _run(["zellij", "delete-session", name], text=True, timeout=5)
            return deleted.returncode == 0
    return False


def kill_session(name: str):
    """Kill a session."""
    if _MPLEX:
        _run(["zellij", "kill-session", name])


def send_keys(name: str, *keys: str):
    """Send keys to a session's active pane.

    Special key names: "Enter", "C-c" (translated per multiplexer).
    Plain strings are written as characters.
    """
    if not _MPLEX:
        return
    for k in keys:
        if k == "Enter":
            _run(["zellij", "--session", name, "action", "send-keys", "enter"])
        elif k == "C-c":
            _run(["zellij", "--session", name, "action", "send-keys", "ctrl-c"])
        elif k == "C-d":
            _run(["zellij", "--session", name, "action", "send-keys", "ctrl-d"])
        else:
            _run(["zellij", "--session", name, "action", "write-chars", k])


def capture_pane(name: str, lines: int = 20) -> str:
    """Capture the visible content of a session's pane."""
    if not _MPLEX:
        return ""
    r = _run(
        ["zellij", "--session", name, "action", "dump-screen"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    output = r.stdout or ""
    return "\n".join(output.rstrip().split("\n")[-lines:])


def paste_text(name: str, text: str):
    """Paste text into a session's active pane."""
    if _MPLEX:
        _run(["zellij", "--session", name, "action", "write-chars", text])


def attach_cmd(name: str) -> str:
    """Return the shell command to attach to a session."""
    return f"zellij attach {name}"


def attach_args(name: str) -> list[str]:
    """Return argv to attach to a session without invoking a shell."""
    return ["zellij", "attach", name]


def enter_session_args(name: str, workspace: str) -> list[str]:
    """Return argv to create and enter a session without invoking a shell."""
    cmd = [
        "zellij",
        "attach",
        "--create",
        name,
        "options",
        "--default-cwd",
        workspace,
    ]
    if os.name == "nt":
        cmd.extend(["--default-shell", "powershell.exe"])
    return cmd


def enter_session_cmd(name: str, workspace: str) -> str:
    """Return the command to create and enter a session."""
    cmd = f"zellij attach --create {name} options --default-cwd {workspace}"
    if os.name == "nt":
        cmd += " --default-shell powershell.exe"
    return cmd


def list_sessions() -> list[str]:
    """List all session names."""
    if not _MPLEX:
        return []
    r = _run(["zellij", "list-sessions"], text=True, timeout=5)
    if r.returncode == 0:
        return [
            _strip_ansi(line.strip().split()[0])
            for line in r.stdout.strip().split("\n")
            if line.strip() and "EXITED" not in _strip_ansi(line)
        ]
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
    ws_q = shlex.quote(ws)
    pf_q = shlex.quote(pf)
    # launch_cmd is the intended multi-token shell command (e.g. "codex
    # --model gpt-4") and is meant to be word-split by the shell, so it must
    # NOT be quoted on the execution line. Only quote it for the display
    # echo so a malicious value can't break out of the echo.
    launch_cmd_display = shlex.quote(launch_cmd)
    script = Path(tempfile.gettempdir()) / f"story-launch-{safe_segment(story_key)}.sh"
    script.write_text(
        f"#!/bin/bash\n"
        f'cd {ws_q} 2>/dev/null || {{ echo "ERROR: cannot cd to {ws_q}"; exit 1; }}\n'
        f'echo "Starting: {launch_cmd_display}"\n'
        f'{launch_cmd} "$(cat {pf_q})"\n'
        f'echo ""\n'
        f'echo "Story {safe_segment(story_key)} done (exit code: $?). Closing in 3s..."\n'
        f"sleep 3\n",
        encoding="utf-8",
    )
    script_posix = platform_ops.to_posix_path(str(script))
    platform_ops.open_terminal_window(
        f"Story {story_key}",
        [script_posix],
    )


def zellij_execution_args(
    story_key: str, workspace: str, launch_cmd: str, prompt_file: str
) -> list[str] | None:
    """Generate foreground Zellij execution assets and return argv.

    Creates:
    - story-launch-{story_key}.sh (bash script that runs the CLI with prompt)
    - story-zellij-{story_key}.kdl (Zellij layout pointing to the script)

    Returns argv for ``zellij --session <name> --new-session-with-layout <kdl>``
    or None if Zellij is not available.

    Does NOT call subprocess.run or create any background session.
    """
    if not _MPLEX:
        return None

    from . import platform_ops

    # On Windows, find Git Bash absolute path — Zellij may not have it in PATH
    if os.name == "nt":
        bash_path = platform_ops._find_git_bash()
        if not bash_path:
            return None
        bash_posix = platform_ops.to_posix_path(bash_path)
    else:
        bash_posix = "bash"

    ws = platform_ops.to_posix_path(workspace)
    pf = platform_ops.to_posix_path(prompt_file)
    ws_q = shlex.quote(ws)
    pf_q = shlex.quote(pf)
    # launch_cmd is the intended multi-token shell command (see launch_cli).
    launch_cmd_display = shlex.quote(launch_cmd)

    # Generate launch script (reuse the same pattern as launch_cli)
    # Write an exit marker so poll_completion can detect CLI process exit.
    exit_marker = Path(tempfile.gettempdir()) / f"story-exit-{safe_segment(story_key)}"
    exit_marker_posix = platform_ops.to_posix_path(str(exit_marker))
    script = Path(tempfile.gettempdir()) / f"story-launch-{safe_segment(story_key)}.sh"
    script.write_text(
        f"#!/bin/bash\n"
        f'cd {ws_q} 2>/dev/null || {{ echo "ERROR: cannot cd to {ws_q}"; exit 1; }}\n'
        f'echo "Starting: {launch_cmd_display}"\n'
        f'{launch_cmd} "$(cat {pf_q})"\n'
        f"_ec=$?\n"
        f'echo ""\n'
        f'echo $_ec > "{exit_marker_posix}"\n'
        f"if [ $_ec -eq 0 ]; then\n"
        f'    echo "CLI exited (code: 0). If the task completed, .done file was written."\n'
        f"else\n"
        f'    echo "CLI exited with error (code: $_ec)."\n'
        f"fi\n"
        f'echo "Press Ctrl+D or type exit to return to TUI."\n',
        encoding="utf-8",
    )
    script_posix = platform_ops.to_posix_path(str(script))

    # Generate Zellij layout KDL — use resolved bash path, not bare "bash"
    session = session_name(story_key)
    kdl = Path(tempfile.gettempdir()) / f"story-zellij-{safe_segment(story_key)}.kdl"
    kdl.write_text(
        f"layout {{\n"
        f'    pane command="{bash_posix}" {{\n'
        f'        args "{script_posix}"\n'
        f"    }}\n"
        f"}}\n",
        encoding="utf-8",
    )
    kdl_posix = platform_ops.to_posix_path(str(kdl))

    return [
        "zellij",
        "--session",
        session,
        "--new-session-with-layout",
        kdl_posix,
    ]
