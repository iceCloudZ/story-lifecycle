"""kimi sid 磁盘扫描捕获(双保险)测试。

kimi 原只有退出正则一路捕获(``To resume this session: kimi -r session_<uuid>``),
运行中 DB 无 sid、崩溃没吐行也捕获不到。新增第二路:扫
``~/.kimi-code/sessions/wd_<basename>_<hash>/session_<uuid>``(spawn 即建目录),
live 轮询 + post-exit 兜底共用 ``scan_kimi_session_id``。
"""

import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from story_lifecycle.infra.terminal import sid_capture
from story_lifecycle.knowledge.adapters.shell import ShellAdapter, scan_kimi_session_id


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mk_session(root: Path, wd_name: str, sid: str, mtime: float) -> Path:
    d = root / ".kimi-code" / "sessions" / wd_name / sid
    d.mkdir(parents=True)
    os.utime(d, (mtime, mtime))
    return d


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Path.home() 指向 tmp_path,隔离真实 ~/.kimi-code。"""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    return tmp_path


class TestScanKimiSessionId:
    def test_returns_newest_session_after_since(self, fake_home):
        t0 = time.time() - 100
        _mk_session(fake_home, "wd_myproj_abc123", "session_old-1", t0)
        _mk_session(fake_home, "wd_myproj_abc123", "session_new-2", t0 + 50)

        assert scan_kimi_session_id("/ws/myproj", _iso(t0 + 10)) == "session_new-2"

    def test_ignores_sessions_older_than_since(self, fake_home):
        t0 = time.time() - 100
        _mk_session(fake_home, "wd_myproj_abc123", "session_old-1", t0)

        assert scan_kimi_session_id("/ws/myproj", _iso(t0 + 10)) is None

    def test_ignores_other_basename_dirs(self, fake_home):
        t0 = time.time() - 100
        _mk_session(fake_home, "wd_other_abc123", "session_x-1", t0 + 50)

        assert scan_kimi_session_id("/ws/myproj", _iso(t0)) is None

    def test_returns_none_when_root_missing(self, fake_home):
        assert scan_kimi_session_id("/ws/myproj", _iso(time.time())) is None

    def test_returns_none_when_cwd_missing(self, fake_home):
        """cwd 为空时不扫:无前缀会把无关会话(用户自己开的 kimi)错误回填。"""
        _mk_session(fake_home, "wd_myproj_abc123", "session_a-1", time.time())
        assert scan_kimi_session_id(None, None) is None

    def test_no_since_takes_newest_overall(self, fake_home):
        t0 = time.time() - 100
        _mk_session(fake_home, "wd_myproj_abc123", "session_old-1", t0)
        _mk_session(fake_home, "wd_myproj_abc123", "session_new-2", t0 + 50)

        assert scan_kimi_session_id("/ws/myproj", None) == "session_new-2"


class TestShellAdapterHooks:
    def test_kimi_live_hook_delegates_to_scan(self, fake_home):
        t0 = time.time() - 100
        _mk_session(fake_home, "wd_myproj_abc123", "session_a-1", t0 + 50)
        adapter = ShellAdapter({}, name="kimi")

        assert adapter.capture_sid_live("S", "design", "/ws/myproj", _iso(t0)) == "session_a-1"

    def test_kimi_post_exit_hook_delegates_to_scan(self, fake_home):
        t0 = time.time() - 100
        _mk_session(fake_home, "wd_myproj_abc123", "session_a-1", t0 + 50)
        adapter = ShellAdapter({}, name="kimi")

        assert (
            adapter.capture_sid_post_exit("S", "design", "/ws/myproj", _iso(t0))
            == "session_a-1"
        )

    def test_non_kimi_hooks_return_none(self, fake_home):
        adapter = ShellAdapter({}, name="codex")

        assert adapter.capture_sid_live("S", "design", "/ws/x", None) is None
        assert adapter.capture_sid_post_exit("S", "design", "/ws/x", None) is None


class _StubPty:
    def __init__(self):
        self.alive = True
        self.session_id = "pty-stub"


class _LiveScanAdapter:
    """只有 live 扫描钩子的 adapter(输出行/文件退出捕获都无)。"""

    name = "kimi"
    prespecified_session_id = False

    def make_sid_capturer(self, *a, **k):
        return None

    def capture_sid_live(self, story_key, stage, cwd, since_ts):
        return "session_live-1"

    def capture_sid_post_exit(self, *a, **k):
        return None


class TestLiveScanWiring:
    def test_arm_sid_capture_backfills_db_via_live_scan(self, monkeypatch):
        """live 轮询线程扫到 sid 即回填 DB,不等会话退出。"""
        calls = []
        monkeypatch.setattr(sid_capture.db, "get_session", lambda *a: None)
        monkeypatch.setattr(
            sid_capture.db, "set_session_id", lambda *a: calls.append(a)
        )

        pty = _StubPty()
        try:
            sid_capture.arm_sid_capture(
                _LiveScanAdapter(), pty, story_key="S", stage="design", cwd="/ws/myproj"
            )
            deadline = time.time() + 3
            while not calls and time.time() < deadline:
                time.sleep(0.05)
        finally:
            pty.alive = False

        assert calls == [("S", "design", "kimi", "session_live-1")]
