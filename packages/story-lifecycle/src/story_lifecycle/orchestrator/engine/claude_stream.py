"""Claude 轨 stream-json 解析(0b-1)+ 许可决策侧(0b-2 决策半)。

Claude **不走 PTY**(§2.3)——走 ``claude -p --output-format stream-json`` 的结构化事件。
本模块解析事件流,识别 Claude "在等人" 的信号,产出统一 ``(question, options)``,
喂**同一个** ``decide_response`` 决策大脑(与 codex/kimi PTY 轨共用 ``supervisor.decide_response``)。

"在等人" 的三种信号:
1. **permission MCP 工具调用**(官方推荐,0b-2):``--permission-prompt-tool mcp__lifecycle__permission``
   配置后,Claude 需要许可时调用 lifecycle 暴露的 MCP ``permission`` 工具 → 表现为
   ``assistant`` 消息里 ``tool_use`` 的 ``name == permission_tool``。本模块识别它,
   MCP 工具的 Handler 调 ``decide_permission`` 返回 allow/deny。
2. **permission_request 事件**:未经 MCP 工具路由的裸许可请求。
3. **elicitation / idle_prompt**:Claude 提的选择/澄清问题(options 非空)。

非上述信号(system/init、thinking、正常 tool_use、result 等)→ ``None``(短路,不调 LLM)。
"""

from __future__ import annotations

import json
from typing import Callable

ALLOW = "allow"
DENY = "deny"

# lifecycle 暴露的 permission MCP 工具名(对应 --permission-prompt-tool 参数)。
DEFAULT_PERM_TOOL = "mcp__lifecycle__permission"


def parse_line(line: str) -> dict | None:
    """解析一行 stream-json → dict;非 JSON / 空行 / 非 dict → None。"""
    line = (line or "").strip()
    if not line:
        return None
    try:
        event = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    return event if isinstance(event, dict) else None


def extract_awaiting(
    line: str, *, permission_tool: str = DEFAULT_PERM_TOOL
) -> tuple[str, list[str]] | None:
    """解析一行 stream-json,若是"在等人"信号 → (question, options),否则 None。

    Args:
        line: 一行 stream-json 文本。
        permission_tool: lifecycle 暴露的 permission MCP 工具名(可配置)。
    """
    event = parse_line(line)
    if not event:
        return None
    etype = event.get("type", "")

    # (1) 裸 permission_request 事件
    if etype == "permission_request":
        opts = event.get("options") or [ALLOW, DENY]
        return (
            _summarize_perm(event.get("tool_name", "tool"), event.get("input")),
            list(opts),
        )

    # (2) elicitation / idle_prompt(选择/澄清)
    if etype in ("elicitation", "elicitation_dialog", "idle_prompt"):
        msg = (
            event.get("message")
            or event.get("prompt")
            or event.get("question")
            or ""
        )
        opts = event.get("options") or []
        if not opts:
            return None
        return (str(msg), list(opts))

    # (3) assistant 调 permission MCP 工具(官方 --permission-prompt-tool 路由)
    if etype == "assistant":
        for content in (event.get("message", {}) or {}).get("content", []) or []:
            if (
                isinstance(content, dict)
                and content.get("type") == "tool_use"
                and content.get("name") == permission_tool
            ):
                inp = content.get("input", {}) or {}
                return (
                    _summarize_perm(
                        inp.get("tool_name", "tool"), inp.get("input", {})
                    ),
                    [ALLOW, DENY],
                )

    return None


def _summarize_perm(tool_name: str, tool_input: dict) -> str:
    """把 (tool, input) 压成给决策器/日志看的简短 question 文本。"""
    s = json.dumps(tool_input, ensure_ascii=False) if tool_input else ""
    if len(s) > 160:
        s = s[:160] + "…"
    return f"允许 {tool_name} 执行? 输入: {s}"


def decide_permission(
    *,
    tool_name: str,
    tool_input: dict,
    story_facts: dict,
    llm_invoke: Callable[[str], str],
) -> dict:
    """决策侧(0b-2):permission MCP 工具被 Claude 调用时,跑 decide_response 选 allow/deny。

    Handler(MCP 工具实现)拿到 Claude 的 ``(tool_name, input)``,调本函数 →
    返回 ``{"behavior": "allow"|"deny", "reason": str}`` 回填给 Claude。

    纯决策:LLM 通过 ``llm_invoke`` 注入,零副作用。复用 ``supervisor.decide_response``
    (选项固定 [allow, deny],守 §2.2 原则 5:固定选项顺序)。
    """
    from .supervisor import decide_response

    decision = decide_response(
        question=_summarize_perm(tool_name, tool_input),
        options=[ALLOW, DENY],
        story_facts=story_facts,
        llm_invoke=llm_invoke,
    )
    return {"behavior": decision["choice"], "reason": decision["reason"]}
