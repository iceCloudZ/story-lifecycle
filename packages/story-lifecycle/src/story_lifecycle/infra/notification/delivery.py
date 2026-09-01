"""NotificationThread — 通知投递线程(Handler,DESIGN-story-butler §3.1)。

参照 ``orchestrator/scheduler.py`` 的 OrchestratorThread 模式:serve 启动时起、
停时止的一个 daemon 线程。循环:每 30s tick(emit 唤醒可提前),drain outbox 的
pending 行 → 按路由结果(payload.channels)调 channel.send → 成功标 sent /
失败退避重试(1m/5m/30m;attempts≥5 标 failed 保留)。

**唯一做投递副作用的地方**。事件出口(emit)绝不在调用方线程里投递。
已送达通道记在 payload_json.delivered,重试不重复发同一条弹窗(至少一次
语义 per channel)。重试到期判定:_last_attempt_ts(payload_json)+ 退避表;
attempts=0 的行立即到期。
"""

from __future__ import annotations

import json
import logging
import threading
import time

from ..db import outbox as outbox_db
from .base import NotificationChannel
from .desktop_plyer import DesktopPlyerChannel
from .router import load_notification_section
from .wechat_remind import (
    DEFAULT_COMMAND,
    DEFAULT_HOST,
    DEFAULT_TIMEOUT,
    WeChatRemindChannel,
)

log = logging.getLogger("story-lifecycle.notification.delivery")

#: 轮询周期(秒)。emit 会 set wake event 提前打断等待(interrupt 不等满 30s)。
TICK_SECONDS = 30.0
#: 失败退避表(秒):第 n 次失败后等 BACKOFF[min(n-1, len-1)] —— 1m / 5m / 30m。
BACKOFF_SECONDS = [60, 300, 1800]
#: attempts ≥ 5 → failed(行保留,可审计;降级矩阵 §5:丢失 0)
MAX_ATTEMPTS = 5
#: 单轮 drain 最多处理的行数(防一条坏行饿死队列;实际远用不到)
_DRAIN_LIMIT = 200


def build_channels(section: dict | None = None) -> dict[str, NotificationChannel]:
    """按配置段构造通道注册表(Provider 装配点 —— 消费方只认 NotificationChannel)。

    ``notification.channels.<name>.enabled: false`` 可关通道;未配置段 → 两通道
    全默认(桌面 plyer + 微信 ssh 101)。未知通道名忽略。
    """
    section = section if isinstance(section, dict) else {}
    channels_cfg = section.get("channels") or {}
    channels: dict[str, NotificationChannel] = {}

    desktop_cfg = channels_cfg.get("desktop")
    if not isinstance(desktop_cfg, dict) or desktop_cfg.get("enabled", True):
        channels["desktop"] = DesktopPlyerChannel()

    wechat_cfg = channels_cfg.get("wechat")
    if not isinstance(wechat_cfg, dict):
        channels["wechat"] = WeChatRemindChannel()
    elif wechat_cfg.get("enabled", True):
        channels["wechat"] = WeChatRemindChannel(
            host=str(wechat_cfg.get("host") or DEFAULT_HOST),
            command=str(wechat_cfg.get("command") or DEFAULT_COMMAND),
            timeout=float(wechat_cfg.get("timeout") or DEFAULT_TIMEOUT),
        )
    return channels


def _is_due(row: dict, now_ts: float) -> bool:
    """到期判定:从未投递(attempts=0)立即;否则按退避表对照 _last_attempt_ts。"""
    if int(row.get("attempts") or 0) <= 0:
        return True
    try:
        payload = json.loads(row.get("payload_json") or "{}")
        last = float(payload.get("_last_attempt_ts") or 0)
    except (ValueError, TypeError):
        return True
    attempts = int(row.get("attempts") or 1)
    backoff = BACKOFF_SECONDS[min(attempts - 1, len(BACKOFF_SECONDS) - 1)]
    return now_ts - last >= backoff


class NotificationThread(threading.Thread):
    """通知投递线程(daemon)。一个实例,serve 启动时起,serve 停时止。"""

    def __init__(
        self,
        tick_interval: float = TICK_SECONDS,
        channels: dict[str, NotificationChannel] | None = None,
        config: dict | None = None,
    ):
        super().__init__(daemon=True, name="notification")
        self._tick_interval = tick_interval
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._config = config
        self._channels = channels

    # ---- 生命周期(对齐 OrchestratorThread)----

    def stop(self):
        """通知线程停止(serve 停时调)。set wake 让等待立即返回。"""
        self._stop_event.set()
        self._wake_event.set()

    def run(self):
        log.info("notification thread started (tick=%ss)", self._tick_interval)
        while not self._stop_event.is_set():
            try:
                self.drain_once()
            except Exception:  # noqa: BLE001 — 单轮失败不死的常驻线程
                log.exception("notification drain failed (non-fatal, continuing)")
            self._wake_event.wait(self._tick_interval)
            self._wake_event.clear()
        log.info("notification thread stopped")

    # ---- 一轮 drain ----

    def _resolve_channels(self) -> dict[str, NotificationChannel]:
        """通道注册表:注入优先;否则按 config 装配(懒,首次 drain 时)。"""
        if self._channels is None:
            self._channels = build_channels(
                load_notification_section(self._config)
            )
        return self._channels

    def drain_once(self, now_ts: float | None = None) -> int:
        """投递一轮到期 pending 行,返回处理行数(测试可直接调,不起线程)。"""
        now = now_ts if now_ts is not None else time.time()
        handled = 0
        for row in outbox_db.list_pending_notifications(limit=_DRAIN_LIMIT):
            if not _is_due(row, now):
                continue
            handled += 1
            try:
                self._deliver_row(row, now)
            except Exception:  # noqa: BLE001 — 单行失败不挡其余行
                log.exception(
                    "[outbox#%s] deliver failed (non-fatal)", row.get("id")
                )
        return handled

    # ---- 单行投递 ----

    def _deliver_row(self, row: dict, now_ts: float) -> None:
        """按 payload.channels 逐通道投递,按结果落状态(见模块 docstring)。"""
        nid = row["id"]
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except (ValueError, TypeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        wanted = list(payload.get("channels") or [])
        if not wanted:
            # 老数据/异常数据兜底:按 tier 展开(与路由器 TIER_CHANNELS 同语义)
            wanted = (
                ["wechat", "desktop"] if row.get("tier") == "interrupt" else ["desktop"]
            )
        delivered = {c for c in (payload.get("delivered") or []) if isinstance(c, str)}
        channels = self._resolve_channels()

        errors: list[str] = []
        sent_any = False
        skipped_any = False
        for name in wanted:
            if name in delivered:
                continue  # 已送达,重试不重复发(至少一次 per channel)
            ch = channels.get(name)
            if ch is None or not ch.available():
                skipped_any = True
                delivered.add(name)  # 不可用 → 跳过,不再重试
                log.debug("[outbox#%s] channel %s unavailable → skip", nid, name)
                continue
            try:
                ok = ch.send(
                    row.get("title") or "",
                    row.get("message") or "",
                    row.get("tier") or "batch",
                )
            except Exception as exc:  # noqa: BLE001 — 通道异常按失败重试
                ok = False
                errors.append(f"{name}: {exc}")
            if ok:
                delivered.add(name)
                sent_any = True
            else:
                errors.append(f"{name}: send failed")

        payload["delivered"] = sorted(delivered)
        payload["_last_attempt_ts"] = now_ts
        payload_json = json.dumps(payload, ensure_ascii=False)
        attempts = int(row.get("attempts") or 0) + 1
        remaining = [c for c in wanted if c not in delivered]
        error_text = "; ".join(errors)[:500]

        if not remaining:
            # 所有目标通道都出结果:成功过 → sent;全是跳过 → skipped
            if sent_any or (not skipped_any and not errors):
                outbox_db.mark_notification_sent(nid, payload_json=payload_json)
                log.info("[outbox#%s] sent via %s", nid, ",".join(delivered))
            else:
                outbox_db.mark_notification_skipped(
                    nid, payload_json=payload_json
                )
                log.info("[outbox#%s] skipped (channels unavailable)", nid)
            return

        # 还有通道没送达:失败退避重试;attempts ≥ 5 → failed 保留
        if attempts >= MAX_ATTEMPTS:
            outbox_db.mark_notification_failed(
                nid, attempts=attempts, last_error=error_text, payload_json=payload_json
            )
            log.warning(
                "[outbox#%s] failed after %s attempts: %s", nid, attempts, error_text
            )
            return
        outbox_db.mark_notification_attempt(
            nid, attempts=attempts, last_error=error_text, payload_json=payload_json
        )
        log.debug(
            "[outbox#%s] attempt %s failed, retry after backoff: %s",
            nid,
            attempts,
            error_text,
        )


# ---- 单例:serve 启动时 create,停时 stop(api.py lifespan 管生命周期)----

_thread: NotificationThread | None = None
_thread_lock = threading.Lock()


def get_notification_thread() -> NotificationThread:
    """获取全局投递线程实例(惰性创建并启动)。"""
    global _thread
    with _thread_lock:
        if _thread is None or not _thread.is_alive():
            _thread = NotificationThread()
            _thread.start()
        return _thread


def stop_notification_thread():
    """停投递线程(serve 停时调)。"""
    global _thread
    with _thread_lock:
        if _thread is not None:
            _thread.stop()
            _thread = None


def try_wake():
    """emit 后提前唤醒投递线程(interrupt 不等满一个 tick);不在跑则 no-op。"""
    with _thread_lock:
        thr = _thread
    if thr is not None and thr.is_alive():
        thr._wake_event.set()
