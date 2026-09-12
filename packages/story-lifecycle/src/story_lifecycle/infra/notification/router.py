"""路由器(纯 Decider)— 事件类型 × 配置 → 通道动作列表。

DESIGN-story-butler §3.1:规则来源 = 代码内默认表 + ``config.yaml`` 可覆盖
(``notification:`` 段:routes / quiet_hours / channels)。**零副作用** —— 只读
事件与配置,不写库、不发消息、无日志之外的 I/O;投递(ssh/plyer/写状态)归
``delivery.NotificationThread``(Handler)。

免打扰时段(默认 22:00-07:30,可配):interrupt 降级为 batch,不丢(emitter 在
payload 标 deferred)。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .base import TIER_BATCH, TIER_DIGEST, TIER_INTERRUPT

# 档位 → 通道展开(interrupt 立即走微信+桌面;batch/digest 只落桌面)。
TIER_CHANNELS: dict[str, tuple[str, ...]] = {
    TIER_INTERRUPT: ("wechat", "desktop"),
    TIER_BATCH: ("desktop",),
    TIER_DIGEST: ("desktop",),
}
VALID_TIERS = tuple(TIER_CHANNELS)

# 代码内默认路由表(v1 五种事件,DESIGN §3.1)。未知事件默认 batch(宁攒勿扰,
# 打断要省着用 —— 验收线 §6.1:一天打断 ≤ 个位数)。
DEFAULT_ROUTES: dict[str, str] = {
    "awaiting_question": TIER_INTERRUPT,
    "stuck_detected": TIER_INTERRUPT,
    "gate_waiting": TIER_INTERRUPT,
    "judge_rejected": TIER_INTERRUPT,
    "judge_escalated": TIER_INTERRUPT,
    "patrol_failed": TIER_INTERRUPT,
    "stage_completed": TIER_BATCH,
}

# 默认免打扰时段(跨午夜:22:00-07:30)
DEFAULT_QUIET_HOURS = ("22:00", "07:30")


@dataclass(frozen=True)
class ChannelAction:
    """一条通道投递动作(路由结果)。tier 随行 —— 投递线程透传给 channel.send。"""

    channel: str
    tier: str


def load_notification_section(config: dict | None = None) -> dict:
    """取 ``notification:`` 配置段(无段/非 dict → {},全走代码内默认)。

    ``config`` None 时从 config.yaml 读(function 内 import,测试可 monkeypatch
    ``infra.config.get_config``)。读取失败 → {}(配置坏了不能挡通知)。
    """
    if config is None:
        try:
            from ...infra.config import get_config

            config = get_config()
        except Exception:  # noqa: BLE001 — 配置读取失败按无配置处理
            config = {}
    section = (config or {}).get("notification")
    return section if isinstance(section, dict) else {}


def resolve_tier(event_type: str, section: dict) -> str:
    """事件类型 → 档位:config routes 覆盖 > 默认表 > 未知事件 batch。

    非法档位值(不在 VALID_TIERS)按未知事件处理,回 batch(配置打错字不炸)。
    """
    routes = section.get("routes")
    tier = (routes or {}).get(event_type) if isinstance(routes, dict) else None
    if tier is None:
        tier = DEFAULT_ROUTES.get(event_type, TIER_BATCH)
    return tier if tier in VALID_TIERS else TIER_BATCH


def _parse_hhmm(value) -> int | None:
    """"HH:MM" → 当天分钟数;解析失败 None(坏配置 → 免打扰失效,不挡通知)。"""
    try:
        parts = str(value).split(":")
        hour, minute = int(parts[0]), int(parts[1])
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour * 60 + minute
    except (ValueError, IndexError, TypeError):
        pass
    return None


def in_quiet_hours(now: datetime, section: dict) -> bool:
    """now 是否落在免打扰时段。支持跨午夜(22:00-07:30)。

    ``quiet_hours`` 键存在且为 null/{} → 显式关闭免打扰;键不存在 → 默认
    22:00-07:30。start/end 解析失败 → 该次不降级(通知宁多勿丢)。
    """
    if "quiet_hours" in section:
        quiet = section.get("quiet_hours")
        if not isinstance(quiet, dict):
            return False  # 显式 null / 坏类型 → 关闭
    else:
        quiet = {}
    start = _parse_hhmm(quiet.get("start", DEFAULT_QUIET_HOURS[0]))
    end = _parse_hhmm(quiet.get("end", DEFAULT_QUIET_HOURS[1]))
    if start is None or end is None or start == end:
        return False
    t = now.hour * 60 + now.minute
    if start < end:
        return start <= t < end
    return t >= start or t < end  # 跨午夜区间


def route(
    event_type: str,
    project: str = "",
    config: dict | None = None,
    now: datetime | None = None,
) -> list[ChannelAction]:
    """事件 → 通道动作列表(纯函数,零副作用)。

    Args:
        event_type: 事件类型(DEFAULT_ROUTES 之一,或任意自定义事件)。
        project: 归属项目(预留 —— 默认策略不分项目,后续可加 per-project 覆盖)。
        config: 完整 config dict;None 则读 config.yaml(测试可注入 {})。
        now: 注入当前时间(免打扰判定;测试可控时);None 取本地时间。

    Returns:
        按档位展开的通道动作(interrupt → wechat+desktop;batch/digest → desktop)。
        免打扰时段内 interrupt 降级为 batch(降级不丢,emitter 标 deferred)。
    """
    del project  # 预留:per-project 路由覆盖尚未启用
    section = load_notification_section(config)
    tier = resolve_tier(event_type, section)
    if now is None:
        now = datetime.now()
    if tier == TIER_INTERRUPT and in_quiet_hours(now, section):
        tier = TIER_BATCH
    return [ChannelAction(channel=c, tier=tier) for c in TIER_CHANNELS[tier]]
