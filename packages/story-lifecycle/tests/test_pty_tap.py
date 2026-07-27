"""Tests for ManagedPty broadcast tap —— supervisor 旁路消费 PTY 输出。

supervisor(codex/kimi 轨)需要消费 PTY 输出流来识别"AI 在等人",但
`_queue` 当前是单消费者(`_pty_ws_handler` 用 `pty._queue.get()`)。
add_tap 注册旁路 queue,每条输出复制一份;主 `_queue` 保持兼容(Web Board 不受影响)。
"""

from unittest.mock import patch

from story_lifecycle.infra.terminal.pty import ManagedPty


def _fake_pty(story_id: str = "t") -> ManagedPty:
    """Construct ManagedPty without spawning a process or starting read thread."""
    with patch.object(ManagedPty, "_spawn", lambda self, env: None), patch.object(
        ManagedPty, "_read_loop", lambda self: None
    ):
        return ManagedPty(story_id, story_key="S", stage="design", adapter="test", command=["fake"], cwd="/tmp", purpose="test")


class TestDistribute:
    def test_puts_data_to_main_queue_and_all_taps(self):
        """_distribute 把每条数据放主 _queue(Web Board)+ 所有 taps(supervisor)。"""
        pty = _fake_pty()
        tap_a = pty.add_tap()
        tap_b = pty.add_tap()

        pty._distribute(b"chunk-1")
        pty._distribute(b"chunk-2")

        # 主 queue 收到(Web Board 兼容)
        assert pty._queue.get_nowait() == b"chunk-1"
        assert pty._queue.get_nowait() == b"chunk-2"
        # 每个 tap 收到副本
        assert tap_a.get_nowait() == b"chunk-1"
        assert tap_a.get_nowait() == b"chunk-2"
        assert tap_b.get_nowait() == b"chunk-1"
        assert tap_b.get_nowait() == b"chunk-2"


class TestRemoveTap:
    def test_removed_tap_stops_receiving(self):
        """remove_tap 后,tap 不再收新数据(主 queue 不受影响)。"""
        pty = _fake_pty()
        tap = pty.add_tap()

        pty._distribute(b"before")
        assert tap.get_nowait() == b"before"

        pty.remove_tap(tap)
        pty._distribute(b"after")

        assert tap.empty()  # remove 后不再收
        # 主 queue FIFO: before 仍在(remove 不影响主 queue),after 紧随
        assert pty._queue.get_nowait() == b"before"
        assert pty._queue.get_nowait() == b"after"


class TestScrollback:
    def test_accumulates_output_for_replay(self):
        """scrollback() 按序返回全部历史输出(WS attach 回放用)。"""
        pty = _fake_pty()

        pty._distribute(b"aaa")
        pty._distribute(b"bbb")

        assert pty.scrollback() == b"aaabbb"

    def test_caps_at_max_bytes_dropping_oldest(self):
        """超过 cap 从头部截断,保留最近的输出。"""
        from story_lifecycle.infra.terminal.pty import _SCROLLBACK_MAX_BYTES

        pty = _fake_pty()
        chunk = b"x" * 1024
        for _ in range(_SCROLLBACK_MAX_BYTES // 1024 + 8):
            pty._distribute(chunk)

        back = pty.scrollback()
        assert len(back) <= _SCROLLBACK_MAX_BYTES
        assert back == chunk * (len(back) // 1024)  # 尾部是最近的完整 chunk

    def test_empty_scrollback(self):
        """无输出时 scrollback 为空字节串(attach 直接进直播)。"""
        pty = _fake_pty()
        assert pty.scrollback() == b""
