"""Cross-platform PTY management for Web Board terminal sessions.

Windows: pywinpty (optional, used by VS Code)
Unix: stdlib pty + subprocess
Fallback: subprocess.Popen (no PTY, but output captured via pipe)
"""

import asyncio
import atexit
import os
import signal
import subprocess
import sys
import threading
from typing import Optional


def _has_winpty() -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("winpty") is not None
    except Exception:
        return False


def _has_unix_pty() -> bool:
    return sys.platform not in ("win32", "emscripten") and hasattr(os, "fork")


class ManagedPty:
    """A single PTY process bound to a story."""

    def __init__(
        self, story_id: str, command: list[str], cwd: str, env: dict | None = None
    ):
        self.story_id = story_id
        self.command = command
        self.cwd = cwd
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=512)
        self._alive = True
        self._process: object | None = None
        self._read_thread: threading.Thread | None = None

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

    def _read_loop(self):
        try:
            while self._alive:
                data = self._blocking_read(4096)
                if not data:
                    self._alive = False
                    break
                try:
                    self._queue.put_nowait(data)
                except asyncio.QueueFull:
                    try:
                        self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    self._queue.put_nowait(data)
        except Exception:
            self._alive = False

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
        try:
            if self._mode == "winpty":
                self._process.terminate(force=True)
            elif self._mode == "subprocess":
                self._process.terminate()
            elif self._mode == "unix":
                os.killpg(self._unix_pid, signal.SIGTERM)
        except Exception:
            pass
        try:
            if self._mode == "unix":
                os.close(self._master_fd)
        except Exception:
            pass


# -------- PTY Registry --------

_ptys: dict[str, ManagedPty] = {}
_lock = threading.Lock()


def spawn_pty(
    story_id: str, command: list[str], cwd: str, env: dict | None = None
) -> ManagedPty:
    """Spawn a new PTY for a story. Kills existing one if any."""
    with _lock:
        existing = _ptys.get(story_id)
        if existing:
            existing.kill()
        pty = ManagedPty(story_id, command, cwd, env)
        _ptys[story_id] = pty
        return pty


def get_pty(story_id: str) -> Optional[ManagedPty]:
    with _lock:
        return _ptys.get(story_id)


def kill_pty(story_id: str):
    with _lock:
        pty = _ptys.pop(story_id, None)
        if pty:
            pty.kill()


def cleanup_all():
    with _lock:
        for pty in _ptys.values():
            pty.kill()
        _ptys.clear()


atexit.register(cleanup_all)
