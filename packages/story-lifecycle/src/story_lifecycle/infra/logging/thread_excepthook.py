"""全局线程异常钩子 —— 让逃逸到线程顶层的未处理异常可见。

背景:serve 路径上多个 worker 线程(headless stdout/stderr drain、PTY supervisor、
pty read loop、sid capture)历史上用 ``except Exception: pass`` 静默吞异常,死掉
时零日志 —— "serve 跑几个就崩/卡"几乎肯定是其中之一静默死亡,但日志里没有任何
traceback 可读,复现白跑。

本模块装一个 ``threading.excepthook`` 作为**兜底**:任何逃逸到线程顶层(没被各线程
自身的 try/except 捕住)的未处理异常,都会被打到 ``story-lifecycle`` logger,而不是
被 Python 默认 hook 默默打印到 stderr(serve 只配了 StreamHandler→stderr,在 tmux
滚屏里容易丢)。

注意:各 worker 线程**自身**的 ``except Exception: pass`` 仍会在到达本 hook 之前拦
下异常。所以本 hook 是第二道防线;第一道是把那些 ``pass`` 改成 ``log.exception``
(见 executors.py / claude_stream.py / pty.py / sid_capture.py)。两道一起,线程死亡
才完全可见 —— 满足 AGENTS.md 硬规则「每个非执行分支必须产生可见的诊断日志」。
"""

from __future__ import annotations

import logging
import threading

_installed = False
_original_hook = None


def install() -> None:
    """安装全局 ``threading.excepthook``(幂等:重复调用无副作用)。

    在 serve 启动时(api.py lifespan)调一次。把逃逸到线程顶层的未处理异常打到
    ``story-lifecycle`` logger,带上线程名,便于定位是哪个 worker 死了。
    """
    global _installed, _original_hook
    if _installed:
        return
    _original_hook = threading.excepthook
    log = logging.getLogger("story-lifecycle")

    def _hook(args):
        try:
            log.exception(
                "[thread %s] unhandled exception (thread terminating)",
                getattr(args.thread, "name", "?"),
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
        except Exception:
            # 钩子本身绝不能再抛 —— 回退到原 hook(Python 默认打 stderr)
            if _original_hook is not None:
                _original_hook(args)

    threading.excepthook = _hook
    _installed = True
