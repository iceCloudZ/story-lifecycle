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


def permission_tool_response(
    *,
    tool_name: str,
    tool_input: dict,
    story_facts: dict,
    llm_invoke: Callable[[str], str],
    log_event_fn: Callable | None = None,
) -> dict:
    """MCP ``--permission-prompt-tool`` 返回形:decide_permission + 落日志 + MCP 契约 dict。

    Claude 经 ``--permission-prompt-tool mcp__lifecycle__permission`` 调本工具时,MCP server
    Handler 调本函数。返回 ``{behavior: "allow"|"deny", updatedInput, message}``(MCP 契约):
    - ``behavior``: ``decide_permission`` 的 allow/deny。
    - ``updatedInput``: 原样回传(不改写 Claude 的工具输入)。
    - ``message``: 决策理由(给 Claude 看)。

    ``log_event_fn`` 注入时落 ``supervisor_decision`` 事件(审计 + 层5 反思数据源)。
    """
    decision = decide_permission(
        tool_name=tool_name,
        tool_input=tool_input,
        story_facts=story_facts,
        llm_invoke=llm_invoke,
    )
    if log_event_fn is not None:
        try:
            log_event_fn(
                story_facts.get("story_key", ""),
                story_facts.get("stage", ""),
                "supervisor_decision",
                {
                    "adapter": "claude",
                    "question": _summarize_perm(tool_name, tool_input),
                    "options": [ALLOW, DENY],
                    "choice": decision["behavior"],
                    "reason": decision["reason"],
                },
            )
        except Exception:
            pass
    return {
        "behavior": decision["behavior"],
        "updatedInput": tool_input,
        "message": decision["reason"],
    }


def supervise_claude_stream(
    *,
    lines,
    story_facts: dict,
    llm_invoke: Callable[[str], str],
    log_event_fn: Callable,
    permission_tool: str = DEFAULT_PERM_TOOL,
) -> list[dict]:
    """Claude 轨决策循环(defer/resume 路径,0b-2 选项 b,**不走 MCP**)。

    消费 ``claude -p --output-format stream-json`` 的行流:每行过 ``extract_awaiting``,
    命中"在等人"(permission_request / elicitation / permission MCP 工具调用)→
    ``decide_response`` 决策 → ``log_decision`` 落 ``supervisor_decision`` 事件。
    非命中行(system/init、thinking、正常 tool_use、result)→ 跳过(不调 LLM)。

    与 codex/kimi PTY 轨的 ``supervisor.supervise_pty_session`` 对称:共用 ``decide_response``
    决策大脑,只是感知源是 stream-json 行(结构化)而非 PTY 文本(regex)。

    Handler(caller)拿到返回的 decisions 后,用 ``claude -p --resume <session>`` 把答案回填
    Claude(本机 Claude 全 allow,无真 permission_request,该回填环境阻断)。

    Args:
        lines: stream-json 行的可迭代(每行一条 JSON 事件文本)。

    Returns:
        ``[{question, options, choice, reason}]`` —— 命中并决策过的点。
    """
    from .supervisor import decide_response, log_decision

    decisions: list[dict] = []
    for line in lines:
        awaiting = extract_awaiting(line, permission_tool=permission_tool)
        if not awaiting:
            continue
        question, options = awaiting
        decision = decide_response(
            question=question,
            options=options,
            story_facts=story_facts,
            llm_invoke=llm_invoke,
        )
        log_decision(
            story_key=story_facts.get("story_key", ""),
            stage=story_facts.get("stage", ""),
            adapter="claude",
            question=question,
            options=options,
            decision=decision,
            log_event_fn=log_event_fn,
        )
        decisions.append(
            {
                "question": question,
                "options": options,
                "choice": decision["choice"],
                "reason": decision["reason"],
            }
        )
    return decisions


def build_resume_command(
    *,
    session_id: str,
    decision: dict,
    claude_bin: str = "claude",
) -> list[str]:
    """构造 ``claude -p --resume`` argv(0b-3 回填半)。

    ``supervise_claude_stream`` 命中 awaiting + 决策后,Handler 用本命令 resume Claude
    继续(决策本身已落 ``supervisor_decision`` 事件)。继续接 stream-json,supervisor
    持续监督 resume 后的流。

    **注**:真回填(答案怎么注入 Claude 的下一个 turn)是 Claude 版本相关机制;本机 Claude
    全 allow(无真 permission_request),该 round-trip 无法触发验证。本函数只构造文档化的
    resume 基命令,decision 通过日志可审计。

    Args:
        session_id: Claude 会话 id(stream-json 的 ``session_id``)。
        decision: supervisor 决策(留作 Handler 拼注入参数的依据;本函数暂只用其存在性)。

    Returns:
        ``[claude_bin, "-p", "--resume", session_id, "--output-format", "stream-json", "--verbose"]``。
    """
    if not session_id:
        raise ValueError("session_id 不能为空")
    _ = decision  # 决策已落日志;真注入参数按 Claude 版本在 Handler 拼装
    return [
        claude_bin,
        "-p",
        "--resume",
        session_id,
        "--output-format",
        "stream-json",
        "--verbose",
    ]


def supervise_headless_stdout(
    *,
    proc,
    adapter: str,
    story_facts: dict,
    llm_invoke: Callable[[str], str],
    log_event_fn: Callable,
    permission_tool: str = DEFAULT_PERM_TOOL,
    stderr_tail: list | None = None,
) -> list[dict]:
    """同步消费 headless proc 的 stdout(drain + 检测提问 + 决策/日志)。

    双重价值:
    - **drain stdout**:headless 路径(claude -p / kimi -p)stdout 是 PIPE,但主循环只轮询
      done file 从不读 stdout → 输出超 ~64KB pipe 缓冲会阻塞 proc、永不写 done(潜在死锁)。
      本函数持续读 stdout 防阻塞。
    - **层1 观察**:命中"在等人"(claude 的 permission_request/elicitation;kimi 的选择提问)
      → ``decide_response`` + 落 ``supervisor_decision`` 事件。

    **observe-only**:headless stdin 在 prompt 注入后已关,**不能写回答案**。所以本函数检测+
    决策+日志,但不回写 agent(headless agent 也一般不提问;真要答需走 interactive PTY 轨的
    ``supervise_pty_session``)。

    **stderr drain**(``stderr_tail``):headless agent(kimi 叙述、claude 日志)大量写 stderr;
    planner 的 Popen 用 stderr=PIPE,若不排空,stderr 超 64KB 管道缓冲 → proc 阻塞在 stderr
    写 → 同样死锁(且 stdout drain 救不了,因为 proc 卡在 stderr)。传 ``stderr_tail``(list)
    时,本函数起一个嵌套 daemon 线程并发排空 stderr、滚动保留尾部(~8KB),供 caller(planner
    的 retry 诊断)读取。真实 bug 回归见 ``test_drains_stderr_preventing_pipe_deadlock``。

    Args:
        proc: subprocess.Popen(有 ``.stdout`` / ``.stderr``)。
        adapter: claude → 解 stream-json;其它 → 用 ``make_awaiting_fn`` 文本检测。
        stderr_tail: 可选 list;非空时并发排空 proc.stderr,滚动尾部追加进此 list。

    Returns:
        ``[{question, options, choice, reason}]`` —— 命中并决策过的点。
    """
    from .supervisor import decide_response, log_decision

    # 并发排空 stderr,防 stderr PIPE 满致 proc 阻塞(kimi/claude 大量写 stderr)。
    # 嵌套 daemon 线程:与下方 stdout 循环并行;stderr 关闭时 readline 返 b"" 自然退出。
    if stderr_tail is not None and getattr(proc, "stderr", None) is not None:
        import threading as _th

        def _drain_stderr():
            try:
                for raw in iter(proc.stderr.readline, b""):
                    stderr_tail.append(raw.decode("utf-8", "replace"))
                    # 滚动:总长留 ~8KB,够 retry 诊断看尾部即可,不无限涨内存。
                    while sum(len(s) for s in stderr_tail) > 8192 and len(stderr_tail) > 1:
                        stderr_tail.pop(0)
            except Exception:
                pass

        _th.Thread(target=_drain_stderr, daemon=True, name="drain-headless-stderr").start()

    decisions: list[dict] = []
    # kimi 等(非 claude)用文本 awaiting detector;claude 用 stream-json extract_awaiting
    text_detect = None if adapter == "claude" else None
    if adapter != "claude":
        try:
            from .awaiting_detector import make_awaiting_fn

            text_detect = make_awaiting_fn(adapter)
        except Exception:
            text_detect = None

    try:
        for raw in iter(proc.stdout.readline, b""):
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            # claude: stream-json;其它: 文本 detector
            awaiting = extract_awaiting(line, permission_tool=permission_tool)
            if not awaiting and text_detect is not None:
                awaiting = text_detect(line)
            if not awaiting:
                continue
            question, options = awaiting
            decision = decide_response(
                question=question,
                options=options,
                story_facts=story_facts,
                llm_invoke=llm_invoke,
            )
            log_decision(
                story_key=story_facts.get("story_key", ""),
                stage=story_facts.get("stage", ""),
                adapter=adapter,
                question=question,
                options=options,
                decision=decision,
                log_event_fn=log_event_fn,
            )
            decisions.append(
                {
                    "question": question,
                    "options": options,
                    "choice": decision["choice"],
                    "reason": decision["reason"],
                }
            )
    except Exception:
        pass
    return decisions

