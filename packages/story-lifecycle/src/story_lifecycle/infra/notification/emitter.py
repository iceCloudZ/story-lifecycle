"""emit_event — 事件出口(观察性,DESIGN-story-butler §3.1)。

编排决策点(supervisor/gate/judge)调 ``emit_event``:**只同步写 outbox 一行**
(快,毫秒级),立即返回;投递(ssh/plyer/改状态)归 NotificationThread 异步
drain —— 编排线程绝不因通知阻塞。

硬规则(AGENTS.md / DESIGN §0):事件出口只观察、只发通知,**绝不碰 story
状态** —— 它不是第二调度器。任何失败都吞掉(log debug),绝不炸调用方。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

log = logging.getLogger("story-lifecycle.notification.emitter")

# 事件缺省标题(调用方传了 title 则覆盖)
_EVENT_TITLES = {
    "awaiting_question": "Agent 需要确认",
    "stuck_detected": "Agent 卡住需介入",
    "gate_waiting": "Story 停在确认门",
    "judge_rejected": "Stage 判定打回",
    "judge_escalated": "Stage 判定升级转人",
    "stage_completed": "Stage 完成",
}


def emit_event(
    event_type: str,
    *,
    story_key: str = "",
    project: str = "",
    stage: str = "",
    title: str = "",
    message: str = "",
    payload: dict | None = None,
    config: dict | None = None,
    now: datetime | None = None,
) -> int | None:
    """发一个"已发生事实"的观察事件 → outbox 一行(pending),返回行 id。

    流程:路由(纯函数,定档位+通道)→ 写一行 outbox → 唤醒投递线程(如在跑)。
    任何异常吞掉返回 None —— 通知是观察,绝不能影响编排主流程。

    Args:
        event_type: 事件类型(v1 五种见 router.DEFAULT_ROUTES,未知事件默认 batch)。
        story_key / project / stage: 归属维度(stage 无列,进 payload)。
        title / message: 展示文案;title 空则按事件类型取缺省。
        payload: 附加结构化信息(与路由/重试元数据合并存 payload_json)。
        config: 显式配置(测试注入);None 读 config.yaml。
        now: 注入时间(免打扰判定;测试可控时)。

    Returns:
        outbox 行 id;失败 None。
    """
    try:
        from ..db import outbox as outbox_db
        from . import router as notif_router

        section = notif_router.load_notification_section(config)
        actions = notif_router.route(event_type, project=project, config=config, now=now)
        if not actions:
            return None
        tier = actions[0].tier
        channels = [a.channel for a in actions]
        # 免打扰降级(§3.1:interrupt 降 batch 并在消息里标 deferred,不丢)
        original_tier = notif_router.resolve_tier(event_type, section)
        deferred = original_tier == "interrupt" and tier != "interrupt"
        if deferred:
            message = (message + f"（免打扰时段，已降级为{tier}）").strip()

        data = dict(payload or {})
        data["stage"] = stage
        data["channels"] = channels
        data.setdefault("delivered", [])
        if deferred:
            data["deferred"] = True

        nid = outbox_db.enqueue_notification(
            event_type=event_type,
            story_key=story_key,
            project=project,
            tier=tier,
            title=title or _EVENT_TITLES.get(event_type, event_type),
            message=message,
            payload_json=json.dumps(data, ensure_ascii=False),
        )
    except Exception:  # noqa: BLE001 — 事件出口绝不炸调用方
        log.debug("emit_event %s failed (non-fatal)", event_type, exc_info=True)
        return None
    # 写完再唤醒投递线程(在跑才有效;不在跑等 serve 起/线程起来后 drain)
    try:
        from .delivery import try_wake

        try_wake()
    except Exception:  # noqa: BLE001 — 唤醒失败不影响 emit 本身
        pass
    return nid
