"""design 阶段「claude 逐问 + 人答」提问检测层(runbook 块3)。

替代 runbook 原设的「AskUserQuestion tool_use」——**claude -p(headless)无此工具**
(实测:claude 明言 "There is no AskUserQuestion tool in my environment")。本仓库也无
MCP server。故提问检测走**侧文件协议**(主)+ stream marker / AskUserQuestion tool_use
(防御,供未来 PTY 模式或工具恢复时)。

侧文件协议(主路径):
    claude 遇关键歧义 → 写 ``clarify_request.json`` 后退出(**不**写 design.json)。
    编排层 poll loop 并行查 done file(design.json)与本侧文件;后者先现 → 暂停
    story(``awaiting-clarify``)→ emit clarification_request → SSE 推前端 → 等人答 →
    消费(deletes)本文件 → respawn claude 带累计 Q&A → claude 出下一问或收敛 design.json。

为何侧文件优于 stream 解析:claude 写文件是它最擅长的事(JSON 干净),poll 复用已验证的
done-file 机制,版本无关、模式无关(headless/PTY 通用)。stream marker 仅作冗余兜底。

本模块纯(文件/行 → dict|None);DB/SSE/回注由编排层(supervisor + planner poll loop)接。
"""

from __future__ import annotations

import json
from pathlib import Path

CLARIFY_MARKER = "<<CLARIFY>>"
CLARIFY_REQUEST_FILENAME = "clarify_request.json"


def clarify_request_rel(done_file) -> str:
    """侧文件相对路径(相对 workspace,正斜杠),取自 done file 同目录。

    prompt 注入(claude 写文件)+ poll loop(编排层查文件)必须指向**同一文件**,
    路径在此集中算,避免两端各拼各的漂移。正斜杠跨 OS(prompt/claude/cwd=workspace 均兼容)。
    """
    parent = Path(done_file).parent
    return "/".join((*parent.parts, CLARIFY_REQUEST_FILENAME))


def read_clarify_request(path) -> dict | None:
    """读侧文件 ``clarify_request.json`` → 规整提问 dict 或 None。

    Returns:
        ``{id, question, header, options, context}``(缺字段补默认)或 None。

    Failsafe:文件不存在 / 损坏 JSON / options 空 / 缺 question → None,**绝不抛**
    (poll loop 高频调用,任何异常都不得阻塞设计推进)。
    """
    p = Path(path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    question = data.get("question")
    options = data.get("options")
    if not question or not isinstance(options, list) or not options:
        return None  # 无效提问:让 claude 自决而非空问
    return {
        "id": data.get("id"),
        "question": str(question),
        "header": data.get("header") or str(question),
        "options": [str(o) for o in options],
        "context": data.get("context"),
    }


def clear_clarify_request(path) -> bool:
    """消费后删侧文件(防 respawn 重复触发同一提问)。

    Returns: 是否删除了(文件存在且删成功 → True;不存在 → False)。Failsafe。
    """
    p = Path(path)
    try:
        if not p.exists():
            return False
        p.unlink()
        return True
    except OSError:
        return False


def extract_clarification_from_stream(line: str) -> dict | None:
    """stream-json 行 → 提问 dict 或 None(防御路径)。

    两条检测:
    1. assistant ``text`` 内 ``<<CLARIFY>> {json}`` marker —— claude -p 文本输出兜底。
    2. ``AskUserQuestion`` tool_use —— 防御(claude -p 暂无此工具;PTY/未来可用);
       input.questions[].options 取 label 列表。

    普通文本/普通工具调用(Read/Write/Bash/permission)/ 非 JSON → None。
    """
    line = (line or "").strip()
    if not line:
        return None
    try:
        event = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(event, dict):
        return None

    for content in (event.get("message", {}) or {}).get("content", []) or []:
        if not isinstance(content, dict):
            continue

        # (1) text 内 <<CLARIFY>> marker
        if content.get("type") == "text":
            hit = _parse_marker(content.get("text", ""))
            if hit:
                return hit

        # (2) AskUserQuestion tool_use(防御)
        if content.get("type") == "tool_use" and content.get("name") == "AskUserQuestion":
            req = _parse_askuser_input(content.get("input", {}) or {})
            if req:
                return req

    return None


def _parse_marker(text: str) -> dict | None:
    """从文本里抠 ``<<CLARIFY>> {json}``,返回规整提问 dict 或 None。"""
    text = text or ""
    idx = text.find(CLARIFY_MARKER)
    if idx < 0:
        return None
    tail = text[idx + len(CLARIFY_MARKER):].lstrip()
    data = _extract_first_json_object(tail)
    if not isinstance(data, dict):
        return None
    question = data.get("question")
    options = data.get("options")
    if not question or not isinstance(options, list) or not options:
        return None
    return {
        "id": data.get("id"),
        "question": str(question),
        "header": data.get("header") or str(question),
        "options": [str(o) for o in options],
        "context": data.get("context"),
    }


def _parse_askuser_input(inp: dict) -> dict | None:
    """AskUserQuestion input(questions[])→ 规整提问 dict(取首个 question)。"""
    questions = inp.get("questions") or []
    if not questions or not isinstance(questions, list):
        return None
    q0 = questions[0]
    if not isinstance(q0, dict):
        return None
    question = q0.get("question")
    raw_options = q0.get("options") or []
    options = []
    for o in raw_options:
        if isinstance(o, dict) and o.get("label"):
            options.append(str(o["label"]))
        elif isinstance(o, str):
            options.append(o)
    if not question or not options:
        return None
    return {
        "id": None,
        "question": str(question),
        "header": q0.get("header") or str(question),
        "options": options,
        "context": q0.get("context"),
    }


def _extract_first_json_object(s: str):
    """从字符串开头起,解析第一个完整 JSON 对象(括号配平);失败返回 None。

    marker 后 JSON 可能跟尾巴文字,逐字符配平 ``{}`` 取首个对象。
    """
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start:i + 1])
                except (json.JSONDecodeError, ValueError):
                    return None
    return None
