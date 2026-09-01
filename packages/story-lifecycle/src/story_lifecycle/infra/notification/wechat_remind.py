"""微信提醒通道(Provider)— ssh 到 101 执行 clawbot-inbox remind.py。

出方向链路(DESIGN-story-butler §2):桌面 → ``ssh <host> <remind_py> "<消息>"``
→ 101 的 clawbot 推微信。已生产化(brief_push 同链路)。任何失败(ssh 不通 /
非零退出 / 超时)返回 False 绝不抛 —— 投递线程按 False 走退避重试(降级矩阵
§5:101 失联 push 攒 batch,恢复后补发,丢失 0)。
"""

from __future__ import annotations

import logging
import shlex
import subprocess

from .base import NotificationChannel, TIER_BATCH

log = logging.getLogger("story-lifecycle.notification.wechat")

# 默认目标(config.yaml notification.channels.wechat 可覆盖)
DEFAULT_HOST = "101"
DEFAULT_COMMAND = (
    "/home/ubuntu/clawbot-inbox/venv/bin/python /home/ubuntu/clawbot-inbox/remind.py"
)
DEFAULT_TIMEOUT = 30.0


class WeChatRemindChannel(NotificationChannel):
    """经 ssh 在 101 上调 remind.py 的微信推送通道。"""

    name = "wechat"

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        command: str = DEFAULT_COMMAND,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.host = host
        self.command = command
        self.timeout = timeout

    def available(self) -> bool:
        """host + 命令都配置了才算可用。"""
        return bool(self.host and self.command)

    def send(self, title: str, message: str, tier: str = TIER_BATCH) -> bool:
        """推一条微信文本。返回 False 的情形:ssh 失败/超时/远端非零退出。

        消息经 ``shlex.quote`` 拼进远端命令行 —— 远端是 shell 解析,不 quote
        的空格/引号会截断命令甚至注入。
        """
        text = f"{title}\n{message}".strip()
        try:
            remote_cmd = f"{self.command} {shlex.quote(text)}"
            proc = subprocess.run(
                ["ssh", self.host, remote_cmd],
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except Exception:  # noqa: BLE001 — ssh 不通/超时/机器没 ssh,一律 False
            log.debug("wechat remind failed (non-fatal): %s", title)
            return False
        if proc.returncode != 0:
            log.debug(
                "wechat remind non-zero exit (%s): %s",
                proc.returncode,
                (proc.stderr or "")[-200:],
            )
            return False
        return True
