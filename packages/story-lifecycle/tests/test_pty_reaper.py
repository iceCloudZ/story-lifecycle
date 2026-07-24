"""Tests for lazy PTY reaper — dead entries removed on list, kept for get_pty.

DESIGN-session-pty-id-model.md §3.6 / 问题 6:进程自然死亡后 _ptys 条目永不清理。
lazy reaper 在 list_pty_sessions(展示用)调用时顺手清死条目;但 get_pty(WS
连接用)不清 —— 后者需要区分「不存在(4404)」vs「存在但已死(1000)」。
"""
import story_lifecycle.infra.terminal.pty as pty_mod
from story_lifecycle.infra.terminal.pty import ManagedPty, _reap_dead


def _fake_pty(alive: bool) -> ManagedPty:
    """Construct ManagedPty without spawning a process.

    设 _mode="subprocess" + _process.poll() 让 alive 属性返回想要的值
    (subprocess 分支:poll() is None = 活,否则死)。
    """
    from unittest.mock import MagicMock, patch

    proc = MagicMock()
    proc.poll.return_value = None if alive else 1  # None=活, 非0=死
    with patch.object(ManagedPty, "_spawn", lambda self, env: None), patch.object(
        ManagedPty, "_read_loop", lambda self: None
    ):
        pty = ManagedPty(
            "sid", story_key="S", stage="design", adapter="test",
            command=["fake"], cwd="/tmp", purpose="test",
        )
        pty._mode = "subprocess"
        pty._process = proc
        return pty


class TestReapDead:
    def test_removes_dead_entries(self):
        sessions = {"a": _fake_pty(True), "b": _fake_pty(False), "c": _fake_pty(False)}
        _reap_dead(sessions)
        assert set(sessions.keys()) == {"a"}  # 只剩活的

    def test_keeps_all_when_all_alive(self):
        sessions = {"a": _fake_pty(True), "b": _fake_pty(True)}
        _reap_dead(sessions)
        assert len(sessions) == 2

    def test_empty_dict_noop(self):
        sessions = {}
        _reap_dead(sessions)
        assert sessions == {}


class TestListReaperIntegration:
    def test_list_removes_dead_before_returning(self, monkeypatch):
        """list_pty_sessions 清死条目后只返回活的。"""
        monkeypatch.setattr(pty_mod, "_ptys", {
            "S1": {"alive-sid": _fake_pty(True), "dead-sid": _fake_pty(False)},
        })
        result = pty_mod.list_pty_sessions("S1")
        assert len(result) == 1  # 死的被清
        assert result[0]["status"] == "running"
        # 注册表里也只剩活的
        assert set(pty_mod._ptys["S1"].keys()) == {"alive-sid"}

    def test_get_pty_keeps_dead_for_ws_semantics(self, monkeypatch):
        """get_pty 不清死条目(WS handler 需区分 1000 vs 4404)。"""
        dead = _fake_pty(False)
        monkeypatch.setattr(pty_mod, "_ptys", {"S1": {"dead-sid": dead}})
        # 精确查死 session → 返回它(WS handler 据此返回 1000)
        assert pty_mod.get_pty("S1", "dead-sid") is dead
        # 死条目仍在注册表(get_pty 没清)
        assert "dead-sid" in pty_mod._ptys["S1"]
