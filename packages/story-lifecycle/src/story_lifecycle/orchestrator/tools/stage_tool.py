"""StageTool — default tool that launches a CLI adapter in a multiplexer session."""

from __future__ import annotations

from .base import BaseTool


class StageTool(BaseTool):
    """标准阶段执行：启动 CLI，注入任务书，等待完成。"""

    _tool_name = "stage_tool"

    def execute(self, state: dict, args: dict) -> dict:
        return self._launch_in_session(state, args, args.get("prompt", ""))
