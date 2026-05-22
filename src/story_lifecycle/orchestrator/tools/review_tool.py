"""ReviewTool — dedicated code review sub-agent."""

from __future__ import annotations

import time

from ...adapters import get_adapter
from ...db import models as db
from ...terminal import ttyd


class ReviewTool:
    """Structured code review by an execution CLI sub-agent."""

    def execute(self, state: dict, args: dict) -> dict:
        key = state["story_key"]
        stage = state["current_stage"]
        workspace = state["workspace"]

        adapter_name = args.get("adapter", "claude")
        model = args.get("model", "sonnet")
        review_criteria = args.get("review_criteria", "")
        prompt = args.get("prompt", "")

        review_prompt = "请对当前代码进行结构化审查：\n\n"
        if review_criteria:
            review_prompt += f"## 审查标准\n{review_criteria}\n\n"
        if prompt:
            review_prompt += f"## 任务背景\n{prompt}\n\n"
        review_prompt += (
            "## 审查要求\n"
            "1. 检查代码质量（命名、结构、错误处理）\n"
            "2. 检查安全性（注入、权限、敏感数据）\n"
            "3. 检查测试覆盖\n"
            "4. 检查性能和可维护性\n"
            f"5. 将审查报告写入 .story-context/{key}/code_review_{stage}.md\n"
            f"6. 完成后写入 .story-done/{key}/{stage}.json\n"
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
        ttyd.paste_text(session, review_prompt)
        ttyd.send_keys(session, "Enter")

        state["execution_count"] = state.get("execution_count", 0) + 1
        state["stage_start_time"] = time.time()
        state["last_error"] = None
        state["context"]["code_review_path"] = f".story-context/{key}/code_review_{stage}.md"

        db.log_event(key, stage, "execute", {
            "attempt": state["execution_count"],
            "tool": "review_tool",
        })
        db.update_story(key, execution_count=state["execution_count"], last_error=None)
        return state

    def describe(self) -> str:
        return "代码审查工具：结构化代码审查，产出审查报告"
