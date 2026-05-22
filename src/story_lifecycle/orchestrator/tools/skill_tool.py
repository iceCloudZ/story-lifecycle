"""SkillTool — execute a skill command without full stage orchestration."""

from __future__ import annotations

from .base import BaseTool


class SkillTool(BaseTool):
    """纯 skill 执行：在已有 CLI 会话中运行 skill，不重新启动。"""
    _tool_name = "skill_tool"

    def execute(self, state: dict, args: dict) -> dict:
        skill = args.get("skill", "")
        prompt = args.get("prompt", "")
        skill_prompt = f"请执行 skill: `{skill}`\n\n{prompt}"
        return self._launch_in_session(state, args, skill_prompt)
