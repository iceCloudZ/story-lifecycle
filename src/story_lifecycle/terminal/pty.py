"""Cross-platform PTY management for Web Board terminal sessions.

Windows: pywinpty (used by VS Code)
Unix: stdlib pty + subprocess
"""

import asyncio
import atexit
import os
import signal
import subprocess
import sys
import threading
from typing import Optional


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
        # Ensure UTF-8 on Windows
        if sys.platform == "win32":
            merge_env.setdefault("PYTHONIOENCODING", "utf-8")

        self._spawn(merge_env)

    def _spawn(self, env: dict):
        if sys.platform == "win32":
            self._spawn_winpty(env)
        else:
            self._spawn_unix(env)

        # Start background reader thread
        self._read_thread = threading.Thread(
            target=self._read_loop, daemon=True, name=f"pty-read-{self.story_id}"
        )
        self._read_thread.start()

    def _spawn_winpty(self, env: dict):
        import winpty

        self._process = winpty.PTY(
            cols=120,
            rows=30,
        )
        # winpty spawn needs the program and args separately
        program = self.command[0]
        args = self.command[1:] if len(self.command) > 1 else []
        self._process.spawn(program, args=args, cwd=self.cwd, env=env)

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

    def _read_loop(self):
        """Background thread: blocking read from PTY, push to async queue."""
        try:
            while self._alive:
                data = self._blocking_read(4096)
                if not data:
                    self._alive = False
                    break
                # Push to queue — try_put to avoid blocking the thread
                try:
                    self._queue.put_nowait(data)
                except asyncio.QueueFull:
                    # Drop oldest to avoid memory blowup
                    try:
                        self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    self._queue.put_nowait(data)
        except Exception:
            self._alive = False

    def _blocking_read(self, size: int) -> bytes:
        if sys.platform == "win32":
            return self._process.read(size, timeout=100)  # 100ms timeout
        else:
            return os.read(self._master_fd, size)

    @property
    def alive(self) -> bool:
        if sys.platform == "win32":
            return self._process is not None and self._alive
        else:
            return self._process is not None and self._process.poll() is None

    def write(self, data: bytes):
        """Write user input to PTY stdin."""
        if not self._process:
            return
        try:
            if sys.platform == "win32":
                self._process.write(data.decode("utf-8", errors="replace"))
            else:
                os.write(self._master_fd, data)
        except Exception:
            pass

    def resize(self, cols: int, rows: int):
        """Resize PTY."""
        try:
            if sys.platform == "win32":
                self._process.set_size(cols, rows)
            else:
                import fcntl
                import termios
                import struct

                winsize = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, winsize)
        except Exception:
            pass

    def kill(self):
        """Terminate the PTY process."""
        self._alive = False
        try:
            if sys.platform == "win32":
                # winpty doesn't have a direct kill; close signals exit
                pass
            else:
                os.killpg(self._unix_pid, signal.SIGTERM)
        except Exception:
            pass
        try:
            if sys.platform != "win32":
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
    """Get existing PTY for a story."""
    with _lock:
        return _ptys.get(story_id)


def kill_pty(story_id: str):
    """Kill and remove PTY for a story."""
    with _lock:
        pty = _ptys.pop(story_id, None)
        if pty:
            pty.kill()


def cleanup_all():
    """Kill all PTY processes. Called on server shutdown."""
    with _lock:
        for pty in _ptys.values():
            pty.kill()
        _ptys.clear()


atexit.register(cleanup_all)
