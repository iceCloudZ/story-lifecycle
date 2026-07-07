"""外接 stdio MCP server —— design 阶段「claude 逐问 + 人答」HITL(runbook 重做)。

**为何外接 MCP**(不是侧文件/PTY/in-process sdk_mcp_servers):见 memory
`story-lifecycle-design-hitl` 2026-07-07 方向变更。实测本机 "claude"(glm 网关变体):
- `-p` 无 AskUserQuestion;PTY 下 AskUserQuestion 渲染成 TUI(解 ANSI+驱动脆、boot 慢)。
- 控制协议 `sdk_mcp_servers`(in-process)在该变体上**未注册**。
- **外接 stdio MCP server 经 `.mcp.json` 加载——实测 claude 真的调用
  `mcp__lifecycle__clarify` 并用返回值继续**(context 保留、不重 spawn)= 正道。

机制:
- claude 遇歧义调 ``mcp__lifecycle__clarify(question, options)`` → 本 server 的
  ``handle_clarify_call`` 落 ``clarification_request`` 事件(DB)→ **阻塞**轮询 DB 等
  ``clarification_answer`` 事件(由前端 POST /clarify/answer 落)→ 回 MCP result
  (text = 人答)→ claude 带答继续。MCP 调用天然阻塞 → claude 上下文保留,无需重 spawn。

本模块:可单测的纯核心(``handle_clarify_call``/``poll_clarify_answer``/``CLARIFY_TOOL``)
+ 薄 stdio JSONRPC 循环(``run_server``,I/O 层)。DB 经 env ``STORY_HOME`` + story_key(env
``STORY_KEY``,编排层 spawn claude 时注入,claude 子进程继承)定位。
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from typing import Callable

# MCP tools/list 暴露的 clarify 工具定义。
CLARIFY_TOOL = {
    "name": "clarify",
    "description": (
        "Ask the human a clarifying question when a key ambiguity blocks the design "
        "(multiple choices / missing info / funder差异). Call ONCE per ambiguity, "
        "with concrete options. The human's answer is returned as text — continue the "
        "design based on it (do not re-ask answered questions)."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The specific question to ask."},
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "2-4 concrete options to choose from.",
            },
            "header": {
                "type": "string",
                "description": "Short one-line topic (optional, defaults to question).",
            },
        },
        "required": ["question", "options"],
    },
}

# 人答等待上限(单次 clarify)。HITL 是异步的,人可能慢——给足;超时则让 claude 自行决断。
_ANSWER_TIMEOUT_S = 45 * 60
_POLL_INTERVAL_S = 2.0


def _event_payload(ev: dict) -> dict:
    """解码事件 payload(DB 存 JSON 字符串;测试可能传 dict)→ dict,失败返 {}。

    ``db.get_story_events`` 返回的 payload 是 JSON 字符串,本模块的函数要兼容 str/dict。
    """
    p = ev.get("payload")
    if isinstance(p, str):
        try:
            return json.loads(p)
        except (json.JSONDecodeError, ValueError):
            return {}
    return p if isinstance(p, dict) else {}


def handle_clarify_call(
    *,
    story_key: str,
    question: str,
    options: list[str],
    header: str = "",
    log_event_fn: Callable,
    await_answer_fn: Callable,
    id_factory: Callable[[], str] | None = None,
) -> dict:
    """处理一次 ``clarify`` 工具调用:落请求事件 → 阻塞等人答 → 回 MCP result。

    Args:
        story_key: 故事 key(运行时从 env STORY_KEY 读)。
        question/options/header: 提问内容(header 缺省取 question)。
        log_event_fn: ``(story_key, stage, event_type, payload)`` 落事件(注入,可测)。
        await_answer_fn: ``(story_key, request_id, timeout) -> str | None``,阻塞等人答
            (注入;生产用 ``poll_clarify_answer``,测试用 fake)。
        id_factory: 生成 request_id(注入,测试);缺省 uuid4 hex[:12]。

    Returns:
        MCP ``result`` dict(即 ``mcp_response.result``):
        ``{"content":[{"type":"text","text":<人答>}],"isError":False}``。
        超时/无人答 → text 为「自行判断」提示、``isError`` 仍 False(绝不无限卡 claude)。
    """
    rid = (id_factory or (lambda: uuid.uuid4().hex[:12]))()
    log_event_fn(
        story_key,
        "design",
        "clarification_request",
        {
            "id": rid,
            "header": header or question,
            "question": question,
            "options": list(options),
        },
    )
    answer = await_answer_fn(story_key, rid, _ANSWER_TIMEOUT_S)
    if answer is None:
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "(no human answer within timeout — proceed with the most "
                        "conservative option and record the assumption; do not block)"
                    ),
                }
            ],
            "isError": False,
        }
    return {"content": [{"type": "text", "text": str(answer)}], "isError": False}


def poll_clarify_answer(
    story_key: str,
    request_id: str,
    *,
    get_events_fn: Callable[[str], list[dict]],
    max_polls: int = 1350,  # _ANSWER_TIMEOUT_S / _POLL_INTERVAL_S ≈ 1350
    poll_interval: float = _POLL_INTERVAL_S,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> str | None:
    """轮询 DB 的 clarification_answer 事件,返回匹配 request_id 的 answer;超时 None。

    生产:get_events_fn = db.get_story_events;事件由 POST /clarify/answer 落。
    测试:注入 fake get_events_fn + sleep_fn=noop,可无实时延迟验证。

    匹配:event_type=clarification_answer 且 payload.id == request_id(防串答)。
    """
    for _ in range(max_polls):
        for ev in get_events_fn(story_key) or []:
            if ev.get("event_type") != "clarification_answer":
                continue
            payload = _event_payload(ev)
            if payload.get("id") == request_id:
                return payload.get("answer")
        sleep_fn(poll_interval)
    return None


def get_pending_clarification(
    story_key: str,
    *,
    get_events_fn: Callable[[str], list[dict]],
) -> dict | None:
    """DB 事件里找「最新未答的 clarification_request」(GET /clarify 用)。

    Returns:
        ``{id, question, options, header}`` 或 None(无 request / 最新 request 已答)。

    用于前端轮询:展示当前待答问题。事件驱动——request 由 MCP server 落,answer 由
    POST /clarify/answer 落;pending = 最新 request 且无匹配 id 的 answer。
    """
    events = get_events_fn(story_key) or []
    answered: set = set()
    latest_request = None
    for ev in events:
        etype = ev.get("event_type")
        payload = _event_payload(ev)
        if etype == "clarification_answer":
            if payload.get("id") is not None:
                answered.add(payload["id"])
        elif etype == "clarification_request":
            latest_request = payload  # 顺序遍历,后者覆盖 → 最新
    if not latest_request:
        return None
    if latest_request.get("id") in answered:
        return None
    return {
        "id": latest_request.get("id"),
        "question": str(latest_request.get("question", "")),
        "options": list(latest_request.get("options", []) or []),
        "header": latest_request.get("header") or str(latest_request.get("question", "")),
    }


# ---- stdio JSONRPC loop (薄 I/O 层) ----------------------------------------


def write_mcp_config(config_path, python_bin: str) -> str:
    """写 ``.mcp.json``(指向本 server,经 ``claude --mcp-config <path>`` 加载),返回路径。

    claude 启动时加载此配置 → 连上 lifecycle MCP server → tools/list 暴露 clarify 工具。
    用 ``-m`` 跑模块(相对 import ``...infra.db`` 才能解析)。python_bin 用编排层
    ``sys.executable``(其 env 已装 story_lifecycle)。STORY_KEY 不写这里——走 spawn env
    继承(claude 子进程 → MCP server 子进程都继承编排层注入的 STORY_KEY)。
    """
    import json as _json
    from pathlib import Path as _Path

    p = _Path(config_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        _json.dumps(
            {
                "mcpServers": {
                    "lifecycle": {
                        "command": python_bin,
                        "args": ["-m", "story_lifecycle.orchestrator.mcp.clarify_server"],
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return str(p)


def run_server() -> None:
    """stdio MCP server 主循环(JSONRPC over stdin/stdout)。

    工具集:``clarify``。握手:initialize → notifications/initialized → tools/list →
    tools/call。clarify 调用经 ``handle_clarify_call``(落事件 + 阻塞轮询 DB)。
    story_key 从 env ``STORY_KEY`` 读(编排层 spawn claude 时注入)。
    """
    story_key = os.environ.get("STORY_KEY", "")
    # 延迟 import 避免纯单测时拉起 DB。
    from ...infra.db import models as db

    def _emit(story_key, stage, event_type, payload):
        try:
            db.log_event(story_key, stage, event_type, payload)
        except Exception:
            pass

    def _await(story_key, request_id, timeout):
        return poll_clarify_answer(
            story_key, request_id, get_events_fn=db.get_story_events,
        )

    def _send(obj):
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        mid = msg.get("id")
        if method == "initialize":
            _send({
                "jsonrpc": "2.0", "id": mid,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "lifecycle", "version": "1.0"},
                },
            })
        elif method == "notifications/initialized":
            pass  # 通知,无需响应
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [CLARIFY_TOOL]}})
        elif method == "tools/call":
            params = msg.get("params", {}) or {}
            name = params.get("name")
            args = params.get("arguments", {}) or {}
            if name == "clarify":
                result = handle_clarify_call(
                    story_key=story_key,
                    question=str(args.get("question", "")),
                    options=list(args.get("options", []) or []),
                    header=str(args.get("header", "") or ""),
                    log_event_fn=_emit,
                    await_answer_fn=_await,
                )
                _send({"jsonrpc": "2.0", "id": mid, "result": result})
            else:
                _send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": f"unknown tool: {name}"}],
                    "isError": True,
                }})


if __name__ == "__main__":
    run_server()
