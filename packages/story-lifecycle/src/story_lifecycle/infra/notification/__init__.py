"""通知通道 seam(DESIGN-story-butler §3.1,仓库 capability-seam 约定)。

Definition / Provider 分离:
- Definition: ``base.NotificationChannel`` —— 消费方(路由器 / 投递线程 /
  事件出口)只 import 本抽象,绝不 import 具体实现或按实现名/isinstance 分支。
- Provider: ``desktop_plyer.DesktopPlyerChannel`` / ``wechat_remind.WeChatRemindChannel``
  (可多个并存、可替换)。
- 消费方: ``router.route``(纯 Decider)/ ``emitter.emit_event``(事件出口,
  只写 outbox)/ ``delivery.NotificationThread``(Handler,唯一做投递副作用的地方)。
"""

from .base import (
    TIER_BATCH,
    TIER_DIGEST,
    TIER_INTERRUPT,
    NotificationChannel,
)
from .router import (
    DEFAULT_ROUTES,
    ChannelAction,
    in_quiet_hours,
    load_notification_section,
    resolve_tier,
    route,
)
from .emitter import emit_event

__all__ = [
    "ChannelAction",
    "DEFAULT_ROUTES",
    "NotificationChannel",
    "TIER_BATCH",
    "TIER_DIGEST",
    "TIER_INTERRUPT",
    "emit_event",
    "in_quiet_hours",
    "load_notification_section",
    "resolve_tier",
    "route",
]
