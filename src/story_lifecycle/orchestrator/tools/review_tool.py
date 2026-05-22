"""ReviewTool — dedicated code review sub-agent."""

from __future__ import annotations

from .base import BaseTool


class ReviewTool(BaseTool):
    """代码审查工具：结构化代码审查，产出审查报告。"""
    _tool_name = "review_tool"

    def execute(self, state: dict, args: dict) -> dict:
        key = state["story_key"]
        stage = state["current_stage"]
        review_criteria = args.get("review_criteria", "")
        prompt_text = args.get("prompt", "")

        prompt = "请对当前代码进行结构化审查：\n\n"
        if review_criteria:
            prompt += f"## 审查标准\n{review_criteria}\n\n"
        if prompt_text:
            prompt += f"## 任务背景\n{prompt_text}\n\n"
        prompt += (
            "## 审查要求\n"
            "1. 检查代码质量（命名、结构、错误处理）\n"
            "2. 检查安全性（注入、权限、敏感数据）\n"
            "3. 检查测试覆盖\n"
            "4. 检查性能和可维护性\n"
            f"5. 将审查报告写入 .story-context/{key}/code_review_{stage}.md\n"
            f"6. 完成后写入 .story-done/{key}/{stage}.json\n"
        )

        state = self._launch_in_session(state, args, prompt)
        state["context"]["code_review_path"] = f".story-context/{key}/code_review_{stage}.md"
        return state
