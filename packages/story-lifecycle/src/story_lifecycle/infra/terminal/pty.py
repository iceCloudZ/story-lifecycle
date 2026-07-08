"""Cross-platform PTY management for Web Board terminal sessions.

Windows: pywinpty (optional, used by VS Code)
Unix: stdlib pty + subprocess
Fallback: subprocess.Popen (no PTY, but output captured via pipe)
"""

import asyncio
import atexit
import os
import re
import signal
import subprocess
import sys
import threading
import time
from typing import Optional


def _has_winpty() -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("winpty") is not None
    except Exception:
        return False


def _has_unix_pty() -> bool:
    return sys.platform not in ("win32", "emscripten") and hasattr(os, "fork")


# ---- Windows Job Object: kill the whole process tree on .kill() ----
# taskkill /T walks parent-child links and misses children that detached from
# the parent (codex.exe helpers survived kill_pty, leaking hundreds of MB). A
# Job Object with KILL_ON_JOB_CLOSE kills every process in the job when its
# handle is closed — including detached ones. We assign the spawned process to
# such a job at spawn time and CloseHandle it on kill().
_WIN_JOB_OK = False
if sys.platform == "win32":
    try:
        import ctypes
        from ctypes import wintypes

        _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _k32.CreateJobObjectW.restype = wintypes.HANDLE
        _k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        _k32.SetInformationJobObject.restype = wintypes.BOOL
        _k32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        _k32.OpenProcess.restype = wintypes.HANDLE
        _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        _k32.AssignProcessToJobObject.restype = wintypes.BOOL
        _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        _k32.CloseHandle.restype = wintypes.BOOL
        _k32.CloseHandle.argtypes = [wintypes.HANDLE]

        class _IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_void_p),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        _JobObjectExtendedLimitInformation = 9
        _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        _PROCESS_SET_QUOTA = 0x0100
        _PROCESS_TERMINATE = 0x0001
        _WIN_JOB_OK = True
    except Exception:
        _WIN_JOB_OK = False


def _create_kill_job(pid: int):
    """Assign `pid` (and its future children) to a KILL_ON_JOB_CLOSE Job Object.
    Returns the job handle (int) to keep alive; CloseHandle on it kills the job.
    Returns None on non-Windows or any failure (caller falls back to taskkill)."""
    if not _WIN_JOB_OK:
        return None
    try:
        job = _k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _k32.SetInformationJobObject(
            job,
            _JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            _k32.CloseHandle(job)
            return None
        proc = _k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not proc:
            _k32.CloseHandle(job)
            return None
        ok = _k32.AssignProcessToJobObject(job, proc)
        _k32.CloseHandle(proc)
        if not ok:
            _k32.CloseHandle(job)
            return None
        return job
    except Exception:
        return None


def _close_kill_job(job):
    """Close the job handle -> KILL_ON_JOB_CLOSE kills the whole process tree."""
    if job and _WIN_JOB_OK:
        try:
            _k32.CloseHandle(job)
        except Exception:
            pass


class ManagedPty:
    """A single PTY process bound to a story."""

    def __init__(
        self,
        story_id: str,
        command: list[str],
        cwd: str,
        env: dict | None = None,
        purpose: str = "shell",
    ):
        self.story_id = story_id
        self.command = command
        self.cwd = cwd
        self.purpose = purpose
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=512)
        self._taps: list[
            asyncio.Queue
        ] = []  # 旁路 taps(supervisor 等),每条输出复制一份
        self._alive = True
        self._process: object | None = None
        self._read_thread: threading.Thread | None = None
        self._job = None  # Windows Job Object handle (KILL_ON_JOB_CLOSE) or None

        merge_env = dict(os.environ)
        if env:
            merge_env.update(env)
        if sys.platform == "win32":
            merge_env.setdefault("PYTHONIOENCODING", "utf-8")

        self._spawn(merge_env)

    def _spawn(self, env: dict):
        if sys.platform == "win32" and _has_winpty():
            self._spawn_winpty(env)
        elif _has_unix_pty():
            self._spawn_unix(env)
        else:
            self._spawn_subprocess(env)

        self._read_thread = threading.Thread(
            target=self._read_loop, daemon=True, name=f"pty-read-{self.story_id}"
        )
        self._read_thread.start()

    def _spawn_winpty(self, env: dict):
        from winpty import PtyProcess

        self._process = PtyProcess.spawn(
            self.command,
            cwd=self.cwd,
            env=env,
            dimensions=(30, 120),
        )
        self._mode = "winpty"
        self._job = _create_kill_job(getattr(self._process, "pid", None))

    def _spawn_unix(self, env: dict):
        import pty as _pty

        master, slave = _pty.openpty()
        self._master_fd = master
        proc = subprocess.Popen(
            self.command,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=self.cwd,
            env=env,
            preexec_fn=os.setsid,
        )
        os.close(slave)
        self._process = proc
        self._unix_pid = proc.pid
        self._mode = "unix"

    def _spawn_subprocess(self, env: dict):
        """Fallback: plain subprocess with pipes (no PTY)."""
        proc = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=self.cwd,
            env=env,
        )
        self._process = proc
        self._mode = "subprocess"
        self._job = _create_kill_job(proc.pid)

    def _read_loop(self):
        try:
            while self._alive:
                data = self._blocking_read(4096)
                if not data:
                    self._alive = False
                    break
                self._distribute(data)
        except Exception:
            self._alive = False

    def _distribute(self, data: bytes) -> None:
        """Put data to main queue (Web Board) + all taps (supervisor).

        Drop oldest on full to avoid blocking the read thread.
        """
        for q in [self._queue, *self._taps]:
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(data)
                except asyncio.QueueFull:
                    pass

    def add_tap(self, maxsize: int = 512) -> asyncio.Queue:
        """Register a tap queue that receives a copy of every output chunk.

        Used by supervisor (codex/kimi 轨) to consume PTY output without
        stealing from the Web Board's main ``_queue``. Returns the tap queue;
        consumer reads via ``await tap.get()``. Remove with ``remove_tap``.
        """
        tap = asyncio.Queue(maxsize=maxsize)
        self._taps.append(tap)
        return tap

    def remove_tap(self, tap: asyncio.Queue) -> None:
        """Unregister a tap queue (stops receiving new output)."""
        try:
            self._taps.remove(tap)
        except ValueError:
            pass

    def _blocking_read(self, size: int) -> bytes:
        if self._mode == "winpty":
            try:
                data = self._process.read(size)
                return data.encode("utf-8", errors="replace")
            except EOFError:
                return b""
        elif self._mode == "unix":
            return os.read(self._master_fd, size)
        else:
            # subprocess fallback
            data = self._process.stdout.read(size)
            return data if data else b""

    @property
    def alive(self) -> bool:
        if self._mode == "winpty":
            return self._process is not None and self._process.isalive()
        elif self._mode == "unix":
            return self._process is not None and self._process.poll() is None
        else:
            return self._process is not None and self._process.poll() is None

    def write(self, data: bytes):
        if not self._process:
            return
        try:
            if self._mode == "winpty":
                self._process.write(data.decode("utf-8", errors="replace"))
            elif self._mode == "subprocess":
                self._process.stdin.write(data)
                self._process.stdin.flush()
            else:
                os.write(self._master_fd, data)
        except Exception:
            pass

    def resize(self, cols: int, rows: int):
        if self._mode == "winpty":
            try:
                self._process.setwinsize(rows, cols)
            except Exception:
                pass
        elif self._mode == "unix":
            try:
                import fcntl
                import struct
                import termios

                winsize = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, winsize)
            except Exception:
                pass

    def kill(self):
        self._alive = False
        pid = None
        try:
            pid = self._process.pid
        except Exception:
            pass

        # Kill the WHOLE process tree, not just the direct child.
        # Unix runs the child in its own process group (setsid) -> killpg.
        # Windows: close the KILL_ON_JOB_CLOSE Job Object (kills the whole job,
        # including grandchildren that detached — taskkill /T misses those).
        # Fall back to taskkill /T if no job was set up.
        if self._mode == "unix":
            try:
                os.killpg(self._unix_pid, signal.SIGTERM)
            except Exception:
                pass
            try:
                os.close(self._master_fd)
            except Exception:
                pass
            return

        # Windows: winpty or subprocess fallback
        if self._job:
            _close_kill_job(self._job)
            self._job = None
        elif sys.platform == "win32" and pid:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    timeout=10,
                )
            except Exception:
                pass
        try:
            if self._mode == "winpty":
                self._process.terminate(force=True)
            else:
                self._process.terminate()
        except Exception:
            pass


# -------- PTY Registry (multi-session) --------

# _ptys: story_id → { session_id → ManagedPty }
_ptys: dict[str, dict[str, ManagedPty]] = {}
_lock = threading.Lock()
_session_counter = 0


def _next_session_id(story_id: str) -> str:
    global _session_counter
    _session_counter += 1
    return f"pty-{story_id}-{_session_counter}"


def spawn_pty(
    story_id: str,
    command: list[str],
    cwd: str,
    env: dict | None = None,
    purpose: str = "shell",
    session_id: str = "",
) -> tuple[str, ManagedPty]:
    """Spawn a new PTY session for a story. Returns (session_id, pty)."""
    with _lock:
        if not session_id:
            session_id = _next_session_id(story_id)
        pty = ManagedPty(session_id, command, cwd, env, purpose=purpose)
        _ptys.setdefault(story_id, {})[session_id] = pty
        return session_id, pty


def get_pty(story_id: str, session_id: str = "") -> Optional[ManagedPty]:
    """Get a specific PTY session, or the first available one."""
    with _lock:
        sessions = _ptys.get(story_id, {})
        if session_id:
            return sessions.get(session_id)
        # Return first alive session
        for s in sessions.values():
            if s.alive:
                return s
        return None


def list_pty_sessions(story_id: str) -> list[dict]:
    """List all PTY sessions for a story."""
    with _lock:
        sessions = _ptys.get(story_id, {})
        return [
            {
                "session_id": sid,
                "adapter": pty.purpose,
                "stage": "",
                "model": "",
                "status": "running" if pty.alive else "exited",
                "started_at": "",
            }
            for sid, pty in sessions.items()
        ]


def _wait_ready(pty: "ManagedPty", marker: str | None, timeout: float) -> bool:
    """Poll PTY output (via broadcast tap) until ``marker`` regex matches, or timeout.

    Fixes interactive-agent idle (real-run §7.1 follow-up): ``ensure_agent_pty``
    used a fixed ``sleep(2.0)`` before injecting the prompt; agents that take >2s
    to show their input prompt (loading skills, indexing) swallow the early
    injection → idle → stage timeout. Here we tap the output stream and inject
    only once the agent signals readiness (or after ``timeout`` as a fallback).

    ``marker=None`` → no wait (legacy path), return True immediately.
    Returns True if ready (or no marker), False on timeout.
    """
    if not marker:
        return True
    tap = pty.add_tap()
    try:
        deadline = time.time() + timeout
        buf = ""
        while time.time() < deadline:
            try:
                chunk = tap.get_nowait()
            except asyncio.QueueEmpty:
                time.sleep(0.1)
                continue
            if isinstance(chunk, (bytes, bytearray)):
                buf += chunk.decode("utf-8", errors="replace")
            else:
                buf += str(chunk)
            if re.search(marker, buf):
                return True
        return False
    finally:
        try:
            pty.remove_tap(tap)
        except Exception:
            pass


def ensure_agent_pty(
    story_id: str,
    command: list[str],
    cwd: str,
    prompt: str,
    env: dict | None = None,
    startup_delay: float = 2.0,
    readiness_marker: str | None = None,
    readiness_timeout: float = 30.0,
) -> tuple[str, ManagedPty]:
    """Start a new agent PTY session. Returns (session_id, pty).

    If ``readiness_marker`` is set, poll PTY output until the marker matches (or
    ``readiness_timeout``) before injecting ``prompt`` — fixes interactive-agent
    idle (slow startup swallowing the early injection). Without a marker, falls
    back to the legacy fixed ``startup_delay`` sleep.
    """
    session_id, pty = spawn_pty(
        story_id,
        command,
        cwd,
        env=env,
        purpose="agent",
    )
    if readiness_marker:
        _wait_ready(pty, readiness_marker, readiness_timeout)
    elif startup_delay:
        time.sleep(startup_delay)

    if prompt:
        pty.write(prompt.encode("utf-8") + b"\r")
    return session_id, pty


def kill_pty(story_id: str, session_id: str = ""):
    """Kill a specific PTY session, or all sessions for a story (force-kill only).

    For an *immediate* teardown use this. For a transcript-flushing teardown
    (let claude `/exit` cleanly before killing) use ``cleanup_all`` instead.
    """
    with _lock:
        sessions = _ptys.get(story_id, {})
        if session_id:
            pty = sessions.pop(session_id, None)
            if pty:
                pty.kill()
        else:
            for pty in sessions.values():
                pty.kill()
            sessions.clear()


# Clean-exit teardown tunables. Exported as module constants so tests can zero
# them (avoid blocking on real sleeps); kept here rather than kwargs so the
# protocol is consistent across all callers.
_CLEAN_EXIT_PASTE_DELAY = 0.4  # bracketed-paste settle before the `\r` submit
_CLEAN_EXIT_POLL_INTERVAL = 0.2  # how often to re-check `pty.alive`
_CLEAN_EXIT_TIMEOUT = 10.0  # default patience before force-killing


def clean_exit_pty(pty: "ManagedPty", timeout: float = _CLEAN_EXIT_TIMEOUT) -> bool:
    """Ask a PTY's agent to exit cleanly, then wait for it to die.

    Public (no leading underscore) so the planner can reclaim an interactive
    stage PTY after its done file appears — same /exit-then-wait protocol
    ``cleanup_all`` uses on teardown.

    Sends ``/exit`` via **bracketed paste** (``\\x1b[200~ … \\x1b[201~``) — bare
    PTY writes are treated as a paste by claude's Ink input and never submit
    (claude-code#15553) — followed by a ``\\r`` keystroke to submit it. Polls
    ``pty.alive`` until the process is gone or ``timeout`` elapses.

    Returns True if the PTY exited within ``timeout`` (transcript flushed), False
    if it's still alive (caller should ``pty.kill()`` as the force-kill fallback).

    Why this matters: claude only flushes its
    ``~/.claude/projects/<proj>/<uuid>.jsonl`` transcript on a clean ``/exit``;
    a force-kill mid-run truncates it, so ``--resume`` later resumes from an
    incomplete history. See docs/claude-code-agent-internals.md §2.2.
    """
    pty.write(b"\x1b[200~" + b"/exit" + b"\x1b[201~")
    time.sleep(_CLEAN_EXIT_PASTE_DELAY)
    pty.write(b"\r")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not pty.alive:
            return True
        time.sleep(_CLEAN_EXIT_POLL_INTERVAL)
    return not pty.alive


def cleanup_all(prefer_clean_exit: bool = True):
    """Tear down every PTY across all stories.

    With ``prefer_clean_exit=True`` (default) each PTY is asked to ``/exit``
    cleanly first (so claude flushes its transcript) and force-killed only as a
    fallback / backstop. With ``prefer_clean_exit=False`` every PTY is
    force-killed immediately — use that for an explicit, no-wait teardown.

    The PTYs are snapshotted + removed from the registry under the lock, then the
    (potentially slow, up to ``_CLEAN_EXIT_TIMEOUT`` per PTY) clean-exit + kill
    runs *outside* the lock so it doesn't block concurrent spawns/reads.
    """
    with _lock:
        ptys = [pty for sessions in _ptys.values() for pty in sessions.values()]
        _ptys.clear()
    for pty in ptys:
        if prefer_clean_exit:
            clean_exit_pty(pty)
        pty.kill()


atexit.register(cleanup_all)
