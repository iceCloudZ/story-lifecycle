"""ResearchTool — search docs and analyze codebase before implementing."""

from __future__ import annotations

import time

from ...adapters import get_adapter
from ...db import models as db
from ...terminal import ttyd


class ResearchTool:
    """Research first, then implement: searches docs and analyzes codebase."""

    def execute(self, state: dict, args: dict) -> dict:
        key = state["story_key"]
        stage = state["current_stage"]
        workspace = state["workspace"]

        adapter_name = args.get("adapter", "claude")
        model = args.get("model", "sonnet")
        instructions_file = args.get("instructions_file") or args.get("prompt", "")

        # Build research prompt
        prompt = "请先调研以下内容，然后将研究报告写入指定文件：\n\n"
        if instructions_file:
            prompt += f"## 任务背景\n{instructions_file}\n\n"
        prompt += (
            "## 调研要求\n"
            "1. 搜索项目文档（docs/、README 等）\n"
            "2. 分析相关代码结构和依赖关系\n"
            "3. 总结技术方案和潜在风险\n"
            f"4. 将报告写入 .story-context/{key}/research_{stage}.md\n"
            f"5. 完成后写入 .story-done/{key}/{stage}.json\n"
        )

        # Ensure session
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
        state["context"]["research_path"] = f".story-context/{key}/research_{stage}.md"

        db.log_event(key, stage, "execute", {
            "attempt": state["execution_count"],
            "tool": "research_tool",
        })
        db.update_story(key, execution_count=state["execution_count"], last_error=None)
        return state

    def describe(self) -> str:
        return "调研工具：搜索文档、分析代码库，产出研究报告"
