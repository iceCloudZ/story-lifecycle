"""Agent 工具定义 — OpenAI function calling 格式。

Supervisor Agent 通过这些工具规划、执行、监控开发生命周期。
"""

ORCHESTRATOR_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "plan_step",
            "description": (
                "规划一个执行步骤。规划阶段使用，Agent 用此工具声明"
                "一个阶段要用什么 CLI 工具、关注什么要点。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "adapter": {
                        "type": "string",
                        "enum": ["claude", "codex"],
                        "description": "CLI 工具",
                    },
                    "stage": {
                        "type": "string",
                        "description": "阶段名称（如 design / implement / test）",
                    },
                    "focus": {
                        "type": "string",
                        "description": "2-3 个关键要点，告诉 CLI 应该关注什么",
                    },
                    # done_file 不再由 Agent 决定:由 planner 按 story_key+stage 统一规范化
                    # (.story/done/<key>/<stage>.json),杜绝跨 story 撞名(BUG #7)。
                },
                "required": ["adapter", "stage", "focus"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "launch_cli",
            "description": "启动 CLI 工具执行指定阶段。执行阶段使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "adapter": {
                        "type": "string",
                        "enum": ["claude", "codex"],
                    },
                    "stage": {"type": "string"},
                    "focus": {
                        "type": "string",
                        "description": "给 CLI 的关键要点",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "给 CLI 的完整执行指令（可选，覆盖默认 prompt）",
                    },
                },
                "required": ["adapter", "stage", "focus"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_done_file",
            "description": "检查 CLI 是否已完成（检查 .story-done 文件）",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "done file 路径（相对于 workspace）",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "超时秒数",
                        "default": 1800,
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skip_stage",
            "description": "跳过不需要的阶段",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                    "stage": {
                        "type": "string",
                        "description": "要跳过的阶段名",
                    },
                },
                "required": ["reason", "stage"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mark_complete",
            "description": "标记当前阶段完成",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "files_changed": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mark_failed",
            "description": "标记当前阶段失败",
            "parameters": {
                "type": "object",
                "properties": {
                    "error": {"type": "string"},
                    "stage": {
                        "type": "string",
                        "description": "失败的阶段名",
                    },
                },
                "required": ["error"],
            },
        },
    },
]
