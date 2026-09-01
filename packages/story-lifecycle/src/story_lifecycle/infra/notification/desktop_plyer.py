"""桌面弹窗通道(Provider)— plyer 软依赖。

迁移自 ``orchestrator/engine/notify.py``(DESIGN-story-butler §3.1 seam 化):
行为保持一致 —— plyer 不可用/失败时静默(日志 debug)返回 False,绝不抛。
``engine/notify.py`` 保留 ``send`` 函数作为兼容 re-export。
"""

from __future__ import annotations

import logging

from .base import NotificationChannel, TIER_BATCH

log = logging.getLogger("story-lifecycle.notification.desktop")


class DesktopPlyerChannel(NotificationChannel):
    """plyer 桌面通知(Windows/macOS/Linux 气泡)。"""

    name = "desktop"

    def available(self) -> bool:
        """plyer 可导入即可用(import 失败 → False,投递线程会标 skipped)。"""
        try:
            import plyer  # noqa: F401 — 只探测软依赖
        except Exception:  # noqa: BLE001 — 任何 import 异常都视为不可用
            return False
        return True

    def send(self, title: str, message: str, tier: str = TIER_BATCH) -> bool:
        try:
            from plyer import notification

            notification.notify(title=title, message=message, timeout=5)
            return True
        except Exception:  # noqa: BLE001 — 桌面通知 best-effort
            log.debug("desktop notify skipped (plyer unavailable): %s", title)
            return False
