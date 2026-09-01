"""Cross-platform desktop notification via plyer.

兼容 re-export(管家系统 WP1,DESIGN-story-butler §3.1):实现已迁移到
``infra/notification/desktop_plyer.py``(通知通道 seam 的 Provider)。
本模块保留 ``send`` 函数,存量调用方零改动;新代码请用 NotificationChannel seam
(桌面弹窗/微信打断由投递线程按路由表异步执行,不再直连 plyer)。
"""

import logging

log = logging.getLogger("story-lifecycle.notify")


def send(title: str, message: str) -> None:
    """Send a desktop notification. Silent fallback if plyer unavailable."""
    try:
        from ...infra.notification.desktop_plyer import DesktopPlyerChannel

        DesktopPlyerChannel().send(title, message)
    except Exception:
        log.debug(f"Notification skipped (plyer unavailable): {title} — {message}")
