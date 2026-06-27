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
