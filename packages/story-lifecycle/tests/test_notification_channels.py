"""通知通道 Provider 单测(管家 WP1,DESIGN-story-butler §3.1/§9)。

- WeChatRemindChannel:mock subprocess(绝不真 ssh)— 成功/非零退出/超时/异常
  全路径;消息经 shlex.quote 拼进远端命令行。
- DesktopPlyerChannel:plyer import 失败静默路径 + fake plyer 成功路径。
- engine/notify.send 兼容 re-export:行为不变(成功发,失败静默)。
"""

import sys
import types
from types import SimpleNamespace

import story_lifecycle.infra.notification.wechat_remind as wr_mod
from story_lifecycle.infra.notification.desktop_plyer import DesktopPlyerChannel
from story_lifecycle.infra.notification.wechat_remind import (
    DEFAULT_COMMAND,
    DEFAULT_HOST,
    WeChatRemindChannel,
)


class TestWeChatRemindChannel:
    def test_success_returns_true(self, monkeypatch):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stderr="")

        monkeypatch.setattr(wr_mod.subprocess, "run", fake_run)
        ch = WeChatRemindChannel()
        assert ch.send("标题", "内容") is True
        assert len(calls) == 1
        argv = calls[0]
        assert argv[0] == "ssh"
        assert argv[1] == DEFAULT_HOST
        # 远端命令 = remind.py 路径 + shlex.quote 后的消息
        assert argv[2].startswith(DEFAULT_COMMAND)
        assert "标题" in argv[2] and "内容" in argv[2]
        # 消息作为单个 shell 参数被 quote(防空格截断/注入)
        assert "'标题" in argv[2]

    def test_nonzero_exit_returns_false(self, monkeypatch):
        monkeypatch.setattr(
            wr_mod.subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(returncode=1, stderr="boom"),
        )
        assert WeChatRemindChannel().send("t", "m") is False

    def test_timeout_returns_false_not_raise(self, monkeypatch):
        def boom(*a, **k):
            raise wr_mod.subprocess.TimeoutExpired(cmd="ssh", timeout=30)

        monkeypatch.setattr(wr_mod.subprocess, "run", boom)
        assert WeChatRemindChannel().send("t", "m") is False

    def test_any_exception_returns_false(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("no ssh binary")

        monkeypatch.setattr(wr_mod.subprocess, "run", boom)
        assert WeChatRemindChannel().send("t", "m") is False

    def test_message_shell_quoted(self, monkeypatch):
        """含空格/引号的消息必须被 shlex.quote 成单个远端参数。"""
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stderr="")

        monkeypatch.setattr(wr_mod.subprocess, "run", fake_run)
        WeChatRemindChannel().send("t", "hello world; rm -rf /")
        remote = calls[0][2]
        assert "hello world; rm -rf /" in remote
        assert remote.rstrip().endswith("'")

    def test_timeout_configurable(self, monkeypatch):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["timeout"] = kwargs.get("timeout")
            return SimpleNamespace(returncode=0, stderr="")

        monkeypatch.setattr(wr_mod.subprocess, "run", fake_run)
        WeChatRemindChannel(timeout=5).send("t", "m")
        assert seen["timeout"] == 5

    def test_available_requires_host_and_command(self):
        assert WeChatRemindChannel().available() is True
        assert WeChatRemindChannel(host="").available() is False
        assert WeChatRemindChannel(command="").available() is False

    def test_send_accepts_tier_arg(self, monkeypatch):
        monkeypatch.setattr(
            wr_mod.subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(returncode=0, stderr=""),
        )
        assert WeChatRemindChannel().send("t", "m", tier="interrupt") is True


class TestDesktopPlyerChannel:
    def test_send_success_with_fake_plyer(self, monkeypatch):
        notified = []
        fake_plyer = types.ModuleType("plyer")
        fake_notification = types.ModuleType("plyer.notification")
        fake_notification.notify = lambda **kw: notified.append(kw)
        fake_plyer.notification = fake_notification
        monkeypatch.setitem(sys.modules, "plyer", fake_plyer)
        monkeypatch.setitem(sys.modules, "plyer.notification", fake_notification)

        ch = DesktopPlyerChannel()
        assert ch.available() is True
        assert ch.send("T", "M") is True
        assert notified and notified[0]["title"] == "T"
        assert notified[0]["message"] == "M"

    def test_import_failure_silent_path(self, monkeypatch):
        """plyer 缺失 → available False;send 返回 False 绝不抛(静默)。"""
        monkeypatch.setitem(sys.modules, "plyer", None)  # import plyer → ImportError
        ch = DesktopPlyerChannel()
        assert ch.available() is False
        assert ch.send("T", "M") is False

    def test_notify_failure_returns_false(self, monkeypatch):
        fake_plyer = types.ModuleType("plyer")
        fake_notification = types.ModuleType("plyer.notification")

        def boom(**kw):
            raise RuntimeError("no dbus")

        fake_notification.notify = boom
        fake_plyer.notification = fake_notification
        monkeypatch.setitem(sys.modules, "plyer", fake_plyer)
        monkeypatch.setitem(sys.modules, "plyer.notification", fake_notification)
        assert DesktopPlyerChannel().send("T", "M") is False

    # ------------------------------------------------------------------
    # v1.0.0 收口补漏:Windows NOTIFYICONDATAW 上限(标题 64/正文 256 字符),
    # 超长 plyer 抛 ValueError: string too long(投递线程里 "sent" 状态是假的)。
    # 截断是通道侧关注点 —— 只在 desktop 通道做,outbox 仍存全量。
    # ------------------------------------------------------------------

    @staticmethod
    def _fake_plyer(monkeypatch, notified):
        fake_plyer = types.ModuleType("plyer")
        fake_notification = types.ModuleType("plyer.notification")
        fake_notification.notify = lambda **kw: notified.append(kw)
        fake_plyer.notification = fake_notification
        monkeypatch.setitem(sys.modules, "plyer", fake_plyer)
        monkeypatch.setitem(sys.modules, "plyer.notification", fake_notification)

    def test_long_message_truncated_no_exception(self, monkeypatch):
        """2000 字长文(真实事故:1981 字 digest)不再炸,送进 plyer 的串≤上限。"""
        notified = []
        self._fake_plyer(monkeypatch, notified)
        ch = DesktopPlyerChannel()
        assert ch.send("T" * 100, "x" * 2000) is True
        assert len(notified) == 1
        title, message = notified[0]["title"], notified[0]["message"]
        assert len(title) <= 60
        assert len(message) <= 240
        assert message.endswith("…(详见看板)")
        assert title.endswith("…(详见看板)")

    def test_short_message_passthrough(self, monkeypatch):
        """未超长的标题/正文原样透传,不加省略标记。"""
        notified = []
        self._fake_plyer(monkeypatch, notified)
        assert DesktopPlyerChannel().send("标题", "短内容") is True
        assert notified[0]["title"] == "标题"
        assert notified[0]["message"] == "短内容"

    def test_truncate_helper_boundaries(self):
        """_truncate_for_windows 边界:恰好等于上限透传,超 1 字即剪。"""
        from story_lifecycle.infra.notification.desktop_plyer import (
            _truncate_for_windows,
        )

        t60 = "t" * 60
        m240 = "m" * 240
        assert _truncate_for_windows(t60, m240) == (t60, m240)

        t, m = _truncate_for_windows(t60 + "X", m240 + "Y")
        assert len(t) <= 60 and t.endswith("…(详见看板)")
        assert len(m) <= 240 and m.endswith("…(详见看板)")
        # 剪尾保留前缀内容
        assert t.startswith("t" * 10) and m.startswith("m" * 10)


class TestEngineNotifyCompat:
    """engine/notify.send 兼容 re-export:存量 import 零破坏,行为不变。"""

    def test_send_still_importable_and_silent_on_missing_plyer(self, monkeypatch):
        from story_lifecycle.orchestrator.engine import notify as engine_notify

        monkeypatch.setitem(sys.modules, "plyer", None)
        engine_notify.send("T", "M")  # 不抛

    def test_send_delegates_to_seam_channel(self, monkeypatch):
        from story_lifecycle.orchestrator.engine import notify as engine_notify

        sent = []
        # send 是 function 内 import → patch Provider 模块上的类属性才生效
        monkeypatch.setattr(
            "story_lifecycle.infra.notification.desktop_plyer.DesktopPlyerChannel",
            lambda: SimpleNamespace(
                send=lambda t, m, tier="batch": sent.append((t, m))
            ),
        )
        engine_notify.send("T", "M")
        assert sent == [("T", "M")]
