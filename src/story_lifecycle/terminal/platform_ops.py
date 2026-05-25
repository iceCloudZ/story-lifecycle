"""Platform abstraction — Windows vs Unix (Linux/macOS)."""

import os
import shutil
import subprocess


# ---- process: kill by port ----


def _kill_by_port_windows(port: int):
    try:
        r = subprocess.run(
            ["netstat", "-aon"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in r.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                pid = line.split()[-1]
                subprocess.run(
                    ["taskkill", "/F", "/PID", pid],
                    capture_output=True,
                    timeout=5,
                )
                break
    except Exception:
        pass


def _kill_by_port_unix(port: int):
    try:
        subprocess.run(
            ["pkill", "-f", f"ttyd.*port {port}"],
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass


# ---- process: check port in use ----


def _port_in_use_windows(port: int) -> bool:
    try:
        r = subprocess.run(
            ["netstat", "-aon"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return any(
            f":{port}" in line and "LISTENING" in line for line in r.stdout.splitlines()
        )
    except Exception:
        return False


def _port_in_use_unix(port: int) -> bool:
    try:
        r = subprocess.run(
            ["pgrep", "-f", f"ttyd.*port {port}"],
            capture_output=True,
            timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


# ---- file locking ----


def _file_lock_windows(f):
    import msvcrt

    msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)


def _file_lock_unix(f):
    import fcntl

    fcntl.flock(f.fileno(), fcntl.LOCK_EX)


# ---- constants ----

is_windows = os.name == "nt"

CREATE_NEW_CONSOLE = 0x00000010 if is_windows else 0


def resolve_executable(name: str) -> str:
    """Resolve a CLI tool name to its full path.

    On Windows, npm-installed tools ship as ``<name>.cmd`` wrappers.
    ``shutil.which("claude")`` won't find them via ``subprocess.run``
    without ``shell=True``, but ``shutil.which("claude.cmd")`` will.

    Returns the resolved path, or *name* unchanged as fallback.
    """
    if is_windows:
        resolved = shutil.which(f"{name}.cmd") or shutil.which(name)
        if resolved:
            return resolved
    else:
        resolved = shutil.which(name)
        if resolved:
            return resolved
    return name


def subprocess_needs_shell() -> bool:
    """Whether ``subprocess.run`` needs ``shell=True`` for CLI commands.

    True on Windows (``.cmd`` wrappers require it), False elsewhere.
    """
    return is_windows


def to_posix_path(path: str) -> str:
    """Convert Windows backslashes to forward slashes for bash compatibility."""
    return path.replace("\\", "/")


def _find_git_bash() -> str | None:
    """Find Git Bash executable (not WSL bash)."""
    for candidate in (
        os.path.join(os.environ.get("ProgramFiles", ""), "Git", "bin", "bash.exe"),
        os.path.join(
            os.environ.get(
                "ProgramFiles(x86)",
                "",
            ),
            "Git",
            "bin",
            "bash.exe",
        ),
    ):
        if os.path.isfile(candidate):
            return candidate
    return None


def open_terminal_window(title: str, bash_args: list[str]):
    """Open a new terminal window running a bash command.

    Windows: uses Git Bash with CREATE_NEW_CONSOLE (avoids WSL bash).
    Unix: uses common terminal emulators.
    """
    if is_windows:
        bash_exe = _find_git_bash() or "bash"
        subprocess.Popen(
            [bash_exe, *bash_args],
            creationflags=CREATE_NEW_CONSOLE,
        )
    else:
        cmd = " ".join(bash_args)
        for term in ("gnome-terminal", "konsole", "xfce4-terminal", "xterm"):
            if shutil.which(term):
                subprocess.Popen(
                    [term, "-e", "bash", "-c", cmd],
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return


DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

if is_windows:
    detached_flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
else:
    detached_flags = 0


# ---- bind once at import ----

if is_windows:
    kill_by_port = _kill_by_port_windows
    port_in_use = _port_in_use_windows
    file_lock = _file_lock_windows
else:
    kill_by_port = _kill_by_port_unix
    port_in_use = _port_in_use_unix
    file_lock = _file_lock_unix
