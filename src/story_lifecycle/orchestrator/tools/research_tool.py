"""ResearchTool — search docs and analyze codebase before implementing."""

from __future__ import annotations

from .base import BaseTool


class ResearchTool(BaseTool):
    """调研工具：搜索文档、分析代码库，产出研究报告。"""

    _tool_name = "research_tool"

    def execute(self, state: dict, args: dict) -> dict:
        key = state["story_key"]
        stage = state["current_stage"]
        instructions = args.get("prompt", "")

        prompt = "请先调研以下内容，然后将研究报告写入指定文件：\n\n"
        if instructions:
            prompt += f"## 任务背景\n{instructions}\n\n"
        prompt += (
            "## 调研要求\n"
            "1. 搜索项目文档（docs/、README 等）\n"
            "2. 分析相关代码结构和依赖关系\n"
            "3. 总结技术方案和潜在风险\n"
            f"4. 将报告写入 .story/context/{key}/research_{stage}.md\n"
            f"5. 完成后写入 .story/done/{key}/{stage}.json\n"
        )

        state = self._launch_in_session(state, args, prompt)
        state["context"]["research_path"] = f".story/context/{key}/research_{stage}.md"
        return state
