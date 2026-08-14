"""编排线程低延迟唤醒(PLAN-dsh-absorption A4,dsh 事件驱动吸收的最小形态)。

重要进程内信号把 ``OrchestratorThread`` 的 ``wait(poll_interval)`` 提前唤醒
(≤5s → ~0s)。**不新增第二条调度路径** —— 硬规则不变:编排线程是唯一调度
入口,wake 只是让同一次 tick 提前发生。

为什么是中立模块:scheduler.py imports executors.py(engine/),supervisor /
claude_stream 若 import scheduler 会成环。本模块不 import 任何编排层模块,
双方都 import 它(注册表模式)。

已知边界:跨进程信号(``story tool declare`` 是独立 CLI 进程写 DB)唤不醒,
declare→judge 仍靠 5s DB 轮询 —— 接受(文件信号过度设计)。

wake 点(三处,语义一致「会话/判定结束,调度线程现在就该看一眼」):
- scheduler._judge_task 尾部:judge 完成 → 立即处理决策
- supervisor.supervise_pty_session finally:PTY 死亡/会话结束 → 立即接管
- claude_stream.supervise_headless_stdout 返回前:headless proc EOF → 立即接管
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)

_lock = threading.Lock()
_wake_event: threading.Event | None = None


def register(event: threading.Event | None) -> None:
    """OrchestratorThread 启动时注册它的 wake event;传 None 注销。"""
    global _wake_event
    with _lock:
        _wake_event = event


def wake() -> None:
    """唤醒编排线程(下一轮 tick 提前)。无注册时是 no-op(测试/CLI 场景)。"""
    with _lock:
        ev = _wake_event
    if ev is not None:
        ev.set()
