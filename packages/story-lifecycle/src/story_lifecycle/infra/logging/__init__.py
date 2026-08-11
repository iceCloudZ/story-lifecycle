"""全局日志/异常诊断工具(目前含 thread_excepthook)。"""

from .thread_excepthook import install

__all__ = ["install"]
