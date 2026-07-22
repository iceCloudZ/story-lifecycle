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


def auto_confirm_from_profile(profile, stage: str | None = None) -> bool:
    """supervisor 是否用 LLM 自动确认 code-agent 的提问(PTY 轨)。

    解析优先级(与 ``execution_mode`` 同构):
    1. ``stage`` 给定 → 读该 stage 的 ``StageConfig.auto_confirm``(已 merge 过 profile 顶层默认);
    2. 否则 → 读 profile 顶层 ``auto_confirm``。

    **默认 False** —— 人工盯着:supervisor 命中 code-agent 提问时仅落 ``awaiting_confirm``
    事件 + 桌面通知,**不**调 LLM、**不**往 PTY 写答案(token 不浪费,人不被打断)。
    仅全自动场景(benchmark/CI,如 swebench profile)显式设 ``auto_confirm: true`` 才走
    LLM 决策 + 自动回写 PTY。

    防御:profile 为 None / 缺字段 / 异常 → False(默认人工,绝不抛)。
    """
    if profile is None:
        return False
    try:
        if stage is not None:
            stage_cfg = profile.stage(stage) if hasattr(profile, "stage") else None
            if stage_cfg is not None:
                return bool(getattr(stage_cfg, "auto_confirm", False))
        return bool(getattr(profile, "auto_confirm", False))
    except Exception:
        return False
