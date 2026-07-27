"""sid 捕获策略执行器 — 三种 session-id 模型在 PTY spawn 路径上的统一入口。

策略本身定义在 adapter 钩子上(AGENTS.md「Session-id model」契约):

- ``prespecified_session_id=True``(claude):sid = 确定性 uuid5,NEW 时已入库,
  无需捕获 → 本模块 no-op。
- 输出行捕获(kimi):``make_sid_capturer`` 返回 on_output 回调,CLI 退出时把
  sid 吐到终端(``To resume this session: kimi -r session_<uuid>``)→ 挂 daemon
  线程消费 ``pty.add_tap()``,实时喂回调(命中即回填 DB)。
- 文件/系统捕获(opencode):CLI 不吐 sid,但把会话写进存储(``opencode.db``
  SQLite)→ 挂 post-exit watcher,PTY 死亡后调 ``capture_sid_post_exit``,
  返回非空即回填 DB。

谁用:api 交互式 spawn 路径(``api._spawn_story_agent_pty``)。它的 PTY 由用户
自行 /exit,planner 的 stage-done 捕获钩(clean_exit_pty + 同一组 adapter 钩子)
打不到,必须在 spawn 时 arm。planner 全自动路径有自己的捕获时机,不用本模块。

所有线程都是 best-effort:任何异常只结束线程,绝不影响 PTY 主流程。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone

from ..db import models as db

log = logging.getLogger("story-lifecycle.sid_capture")


def now_utc_iso() -> str:
    """UTC ISO 时间戳(秒精度)— 文件扫描捕获的时间窗口下界(对齐 planner 用法)。

    调用方必须在 spawn **之前**取:opencode 的 session 行 time_created 是 CLI
    启动那一刻,spawn 后取的 since 会把它漏掉。
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def arm_sid_capture(
    adapter,
    pty,
    *,
    story_key: str,
    stage: str,
    cwd: str = "",
    since_ts: str = "",
) -> None:
    """按 adapter 的 sid 模型 arm 相应的捕获机制(见模块 docstring 的三路策略)。

    - prespecified → 直接返回(sid 已在 NEW 时入库)。
    - make_sid_capturer 非 None → tap  drain 线程。
    - 其余 → post-exit watcher(调 capture_sid_post_exit;默认实现返回 None,
      无线索时只是空转一次,代价可忽略)。

    ``since_ts`` 缺省取当前时刻 —— 但正确用法是调用方在 spawn 前取好传入。
    """
    if getattr(adapter, "prespecified_session_id", False):
        return
    since = since_ts or now_utc_iso()
    adapter_name = getattr(adapter, "name", "") or ""

    on_output = adapter.make_sid_capturer(story_key, stage, cwd or None, since)
    if on_output is not None:
        _start_sid_capture_tap(pty, on_output)

    _start_post_exit_capture(
        adapter, pty, story_key=story_key, stage=stage, cwd=cwd or None, since_ts=since
    )
    log.debug(
        "[%s] sid capture armed: adapter=%s stage=%s tap=%s",
        story_key,
        adapter_name,
        stage,
        on_output is not None,
    )


def _start_sid_capture_tap(pty, on_output) -> None:
    """Daemon 线程:消费 PTY 输出 tap,实时喂 sid 捕获回调(输出行捕获模型)。

    同步轮询 ``tap.get_nowait()``(对齐 ``_wait_ready`` 的消费模式,不依赖事件
    循环);PTY 死亡且 tap 排空后 remove_tap 退出。
    """
    tap = pty.add_tap()

    def _drain() -> None:
        try:
            while True:
                got = False
                while True:
                    try:
                        chunk = tap.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    got = True
                    if isinstance(chunk, (bytes, bytearray)):
                        on_output(chunk.decode("utf-8", errors="replace"))
                    else:
                        on_output(str(chunk))
                if not pty.alive and not got:
                    break
                time.sleep(0.2)
        except Exception:  # noqa: BLE001 — 捕获失败不拖垮 PTY
            pass
        finally:
            try:
                pty.remove_tap(tap)
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(
        target=_drain, daemon=True, name=f"sid-capture-{pty.session_id}"
    ).start()


def _start_post_exit_capture(
    adapter, pty, *, story_key: str, stage: str, cwd: str | None, since_ts: str
) -> None:
    """Daemon 线程:PTY 死亡后调 capture_sid_post_exit 回填 DB(文件扫描模型)。

    对未覆写该钩子的 adapter(默认实现返回 None)只是死亡后空转一次,代价可忽略;
    这样 spawner 不需要判断 adapter 是否实现了文件捕获 —— 策略判断留在 adapter。
    """

    def _watch() -> None:
        try:
            while pty.alive:
                time.sleep(0.5)
            sid = adapter.capture_sid_post_exit(story_key, stage, cwd, since_ts)
            if sid:
                db.set_session_id(
                    story_key, stage, getattr(adapter, "name", "") or "", sid
                )
        except Exception as exc:  # noqa: BLE001 — best-effort
            log.debug(
                "[%s] post-exit sid capture failed (%s); resume disabled for stage=%s",
                story_key,
                exc,
                stage,
            )

    threading.Thread(
        target=_watch, daemon=True, name=f"sid-post-exit-{pty.session_id}"
    ).start()
