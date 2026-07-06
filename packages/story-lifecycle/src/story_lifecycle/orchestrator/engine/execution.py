"""Execution mode selection for stage tools."""

from enum import Enum


class ExecutionMode(str, Enum):
    INTERACTIVE_PTY = "interactive_pty"
    HEADLESS = "headless"


def parse_execution_mode(value: str | None) -> ExecutionMode:
    """Return a validated mode, defaulting ordinary work to interactive PTY."""
    if value in (None, ""):
        return ExecutionMode.INTERACTIVE_PTY
    try:
        return ExecutionMode(value)
    except ValueError as exc:
        allowed = ", ".join(mode.value for mode in ExecutionMode)
        raise ValueError(
            f"Unsupported execution_mode {value!r}; expected one of: {allowed}"
        ) from exc


def headless_from_profile(profile) -> bool:
    """Profile 的 ``execution_mode == headless`` → ``True``(走 headless 路径:kimi -p wrapper +
    stderr drain,验证可跑通);否则 ``False``(interactive PTY,默认)。

    realtest profile 显式 ``headless``:PTY 路径下 kimi-code 交互模式 prompt 注入后 idle
    (未触发执行),而 headless 路径(kimi -p <prompt>)经 smoke + 流式验证可跑通,故 profile
    显式选 headless。本函数把 profile 的声明翻成 continue_orchestrator_agent 的 headless 位。

    防御:profile 为 None / 缺 execution_mode / 值非法 → False(默认 PTY,绝不抛)。
    """
    if profile is None:
        return False
    try:
        mode = getattr(profile, "execution_mode", None)
        return parse_execution_mode(mode) == ExecutionMode.HEADLESS
    except Exception:
        return False
