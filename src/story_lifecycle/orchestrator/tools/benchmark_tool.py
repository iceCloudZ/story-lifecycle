"""BenchmarkTool — run performance benchmarks and record results."""

from __future__ import annotations

import time

from ...adapters import get_adapter
from ...db import models as db
from ...terminal import ttyd


class BenchmarkTool:
    """Run performance benchmarks and write results."""

    def execute(self, state: dict, args: dict) -> dict:
        key = state["story_key"]
        stage = state["current_stage"]
        workspace = state["workspace"]

        adapter_name = args.get("adapter", "claude")
        model = args.get("model", "sonnet")
        benchmark_config = args.get("benchmark_config", "")

        prompt = "请执行性能基准测试：\n\n"
        if benchmark_config:
            prompt += f"## 测试配置\n{benchmark_config}\n\n"
        prompt += (
            "## 要求\n"
            "1. 运行项目的性能测试（如 pytest-benchmark、locust 等）\n"
            "2. 记录关键指标（延迟、吞吐量、内存占用等）\n"
            f"3. 将报告写入 .story-context/{key}/benchmark_{stage}.md\n"
            f"4. 完成后写入 .story-done/{key}/{stage}.json\n"
        )

        ttyd.ensure_ttyd(key, workspace)
        session = ttyd.session_name(key)
        if ttyd.session_alive(session):
            ttyd.send_keys(session, "C-c")
            time.sleep(0.5)
        if not ttyd.session_alive(session):
            ttyd.create_session(session, workspace)

        adapter = get_adapter(adapter_name)
        launch = adapter.launch_cmd(model)
        ttyd.send_keys(session, launch, "Enter")
        time.sleep(8)
        ttyd.paste_text(session, prompt)
        ttyd.send_keys(session, "Enter")

        state["execution_count"] = state.get("execution_count", 0) + 1
        state["stage_start_time"] = time.time()
        state["last_error"] = None
        state["context"]["benchmark_path"] = f".story-context/{key}/benchmark_{stage}.md"

        db.log_event(key, stage, "execute", {
            "attempt": state["execution_count"],
            "tool": "benchmark_tool",
        })
        db.update_story(key, execution_count=state["execution_count"], last_error=None)
        return state

    def describe(self) -> str:
        return "性能基准工具：运行性能测试并记录结果"
