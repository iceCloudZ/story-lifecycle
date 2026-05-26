"""BenchmarkTool — run performance benchmarks and record results."""

from __future__ import annotations

from .base import BaseTool


class BenchmarkTool(BaseTool):
    """性能基准工具：运行性能测试并记录结果。"""

    _tool_name = "benchmark_tool"

    def execute(self, state: dict, args: dict) -> dict:
        key = state["story_key"]
        stage = state["current_stage"]
        benchmark_config = args.get("benchmark_config", "")

        prompt = "请执行性能基准测试：\n\n"
        if benchmark_config:
            prompt += f"## 测试配置\n{benchmark_config}\n\n"
        prompt += (
            "## 要求\n"
            "1. 运行项目的性能测试\n"
            "2. 记录关键指标（延迟、吞吐量、内存占用等）\n"
            f"3. 将报告写入 .story/context/{key}/benchmark_{stage}.md\n"
            f"4. 完成后写入 .story/done/{key}/{stage}.json\n"
        )

        state = self._launch_in_session(state, args, prompt)
        state["context"]["benchmark_path"] = (
            f".story/context/{key}/benchmark_{stage}.md"
        )
        return state
