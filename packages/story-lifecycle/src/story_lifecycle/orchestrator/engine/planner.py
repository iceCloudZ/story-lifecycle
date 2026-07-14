"""Smart Orchestrator — plan and review via LLM.

Agent mode (Function Calling): run_orchestrator_agent plans a structured
action list via plan_step/skip_stage tool calls; continue_orchestrator_agent
executes them with verify-gate. The legacy text-JSON planning path
(plan_stage / build_plan_prompt / /plan/generate) has been removed.
All LLM calls delegate to LLMClient.
"""

import json
import logging
import time
from pathlib import Path

from ...infra.llm_client import get_llm, with_story_key
from ...infra.story_paths import safe_story_path
from ...infra.paths import stage_done_file_rel
from .agent_tools import ORCHESTRATOR_TOOLS

log = logging.getLogger("story-lifecycle.planner")

STORY_HOME = Path.home() / ".story-lifecycle"


def _load_team_knowledge() -> str:
    knowledge_dir = STORY_HOME / "knowledge"
    parts = []
    if knowledge_dir.exists():
        for f in sorted(knowledge_dir.glob("*.md")):
            content = f.read_text(encoding="utf-8")[:500]
            parts.append(f"### {f.stem}\n{content}")
    return "\n\n".join(parts) if parts else "（无团队记忆）"


def _load_story_knowledge(workspace: str, story_key: str) -> str:
    knowledge_dir = safe_story_path(workspace, ".story-knowledge", story_key)
    parts = []
    if knowledge_dir.exists():
        for f in sorted(knowledge_dir.glob("*.md")):
            content = f.read_text(encoding="utf-8")[:800]
            parts.append(f"### {f.stem}\n{content}")
    return "\n\n".join(parts) if parts else "（无 Story 知识）"


@with_story_key()
def compress_context(workspace: str, story_key: str, current_stage: str) -> str | None:
    """Condenser：将历史 context 文件压缩为知识库摘要。

    触发条件：.story/context/ 下超过 4 个文件。
    """
    context_dir = safe_story_path(workspace, ".story", "context", story_key)
    if not context_dir.exists():
        return None

    files = sorted(context_dir.glob("*.md"))
    if len(files) <= 4:
        return None

    llm = get_llm()
    if not llm.api_key:
        return None

    history_parts = []
    for f in files:
        content = f.read_text(encoding="utf-8")
        history_parts.append(f"### {f.name}\n{content}")

    prompt = f"""将以下多个阶段的历史记录压缩为一个简洁的知识摘要。
保留关键决策、约束、已验证的结论和未解决的问题。
去除过程细节（如 adapter 选择、model 配置等）。

{"".join(history_parts)}

输出 markdown，包含：
- 已确认的设计决策
- 技术约束和边界条件
- 已完成产出的摘要
- 未解决的问题（如有）"""

    compressed = llm.invoke(prompt, temperature=0.2)

    compressed_file = safe_story_path(
        workspace, ".story-knowledge", story_key, "compressed.md"
    )
    compressed_file.parent.mkdir(parents=True, exist_ok=True)
    compressed_file.write_text(compressed, encoding="utf-8")

    # Archive old files instead of deleting
    keep = {f"plan_{current_stage}.md", f"review_{current_stage}.md"}
    archive = context_dir / "archive"
    archive.mkdir(exist_ok=True)
    import shutil

    for f in context_dir.glob("*.md"):
        if f.name not in keep:
            shutil.move(str(f), str(archive / f.name))

    return str(compressed_file.relative_to(workspace))


# ══════════════════════════════════════════════════════════════════
# Agent Mode — Function Calling 驱动的编排循环
# ══════════════════════════════════════════════════════════════════


def _build_agent_system_prompt(
    *,
    profile_stages: dict | None = None,
    story_title: str = "",
    story_key: str = "",
) -> str:
    """构建 Agent 的 system prompt。"""
    stages_hint = ""
    if profile_stages:
        lines = []
        for name, cfg in profile_stages.items():
            desc = cfg.get("description", "") if isinstance(cfg, dict) else ""
            cli = cfg.get("cli", "claude") if isinstance(cfg, dict) else "claude"
            lines.append(f"  - {name}: {desc} (CLI: {cli})")
        stages_hint = "\n".join(lines)
    else:
        stages_hint = "  - design: 代码调研与方案设计\n  - build: 实施计划与编码实现\n  - verify: 验证与交付证据"

    return f"""你是开发任务编排 Agent。根据需求信息，用工具规划并执行开发流程。

## 你的职责
- 根据需求决定需要执行哪些阶段
- 每个阶段选择合适的 CLI 工具（claude / codex / kimi），参考 profile 给各阶段配的 CLI 提示
- 给每个阶段指定 2-3 个关键要点（focus）
- 规划完成后暂停，等待用户确认

## 当前 Story
- Key: {story_key}
- 标题: {story_title}

## 可用阶段
{stages_hint}

## 规则
1. 对每个需要执行的阶段，调用 plan_step 工具
2. 对不需要的阶段（如纯前端需求不需要后端设计），调用 skip_stage
3. focus 要简洁（2-3 个要点），不要写详细设计
4. CLI（claude/codex/kimi）会自己理解需求并设计方案，你不需要代劳
5. 规划完所有阶段后停止调用工具"""


def _build_agent_user_message(
    *,
    story_key: str,
    title: str,
    content: str,
    workspace: str = "",
    profile_stages: dict | None = None,
) -> str:
    """构建 Agent 的初始 user message。"""
    parts = [
        "## 需求信息",
        f"标题: {title}",
    ]
    if content:
        parts.append(f"内容:\n{content[:3000]}")
    if workspace:
        parts.append(f"工作目录: {workspace}")

    # 阶段建议
    if profile_stages:
        stage_names = list(profile_stages.keys())
        parts.append(f"\n请为以下阶段做规划: {', '.join(stage_names)}")

    return "\n".join(parts)


@with_story_key()
def run_orchestrator_agent(
    story_key: str,
    *,
    on_action=None,
) -> dict:
    """Supervisor Agent 规划循环：生成结构化 action list。

    使用 Function Calling 替代文本 JSON 规划。Agent 调用 plan_step/skip_stage
    工具来声明每个阶段的执行计划。

    Args:
        story_key: Story 唯一标识
        on_action: 回调函数，每个 tool_call 时调用，用于 SSE 推送

    Returns:
        {"status": "planning", "actions": [...]}
    """
    from ...infra.db import models as db

    story = db.get_story(story_key)
    if not story:
        raise ValueError(f"Story not found: {story_key}")

    title = story.get("title", "")
    content = story.get("content", "")
    workspace = story.get("workspace", "")
    profile_name = story.get("profile", "minimal")

    # 解析 profile 获取阶段列表
    profile_stages = None
    try:
        from ..engine.profile_loader import resolve_profile

        rp = resolve_profile(profile_name)
        profile_stages = {
            name: {
                "description": cfg.description,
                "cli": cfg.cli,
            }
            for name, cfg in rp.stages.items()
        }
    except Exception:
        pass

    # 构建 messages
    system_prompt = _build_agent_system_prompt(
        profile_stages=profile_stages,
        story_title=title,
        story_key=story_key,
    )
    user_msg = _build_agent_user_message(
        story_key=story_key,
        title=title,
        content=content,
        workspace=workspace,
        profile_stages=profile_stages,
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]

    # Agent 循环：收集 plan_step / skip_stage 调用
    actions = []
    llm = get_llm()
    max_rounds = 10

    try:
        for round_idx in range(max_rounds):
            resp = llm.invoke_with_tools(
                messages,
                ORCHESTRATOR_TOOLS,
                tool_choice="auto",
                temperature=0.1,
                timeout=90,
            )

            # 记录 assistant 回复
            assistant_msg = resp["message"].copy()
            # 确保 tool_calls 是序列化友好的格式
            if resp["tool_calls"]:
                serializable_calls = []
                for tc in resp["tool_calls"]:
                    fn = tc.get("function", {})
                    args = fn.get("arguments", {})
                    if isinstance(args, dict):
                        args = json.dumps(args, ensure_ascii=False)
                    serializable_calls.append(
                        {
                            "id": tc.get("id", ""),
                            "type": tc.get("type", "function"),
                            "function": {
                                "name": fn.get("name", ""),
                                "arguments": args,
                            },
                        }
                    )
                assistant_msg["tool_calls"] = serializable_calls
            messages.append(assistant_msg)

            tool_calls = resp["tool_calls"]
            if not tool_calls:
                # Agent 说完了（没有更多 tool calls）
                break

            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}

                if name == "plan_step":
                    _llm_done = args.get("done_file")
                    if _llm_done:
                        # schema 已删 done_file,但兼容旧模型仍可能传;忽略并记录(BUG #7)
                        log.info(
                            "[%s] ignoring LLM-provided done_file=%r; using canonical path",
                            story_key,
                            _llm_done,
                        )
                    action = {
                        "action": "launch",
                        "adapter": args.get("adapter", "claude"),
                        "stage": args.get("stage", ""),
                        "focus": args.get("focus", ""),
                        "done_file": stage_done_file_rel(
                            story_key, args.get("stage", "")
                        ),
                    }
                    actions.append(action)
                    if on_action:
                        on_action({"type": "action", "action": action})

                elif name == "skip_stage":
                    action = {
                        "action": "skip",
                        "stage": args.get("stage", ""),
                        "reason": args.get("reason", ""),
                    }
                    actions.append(action)
                    if on_action:
                        on_action({"type": "action", "action": action})

                # 喂回 tool result 给 Agent
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": json.dumps(
                            {"status": "recorded"}, ensure_ascii=False
                        ),
                    }
                )
    except Exception:
        raise

    # 写入 DB：暂停等用户确认
    ctx = {}
    try:
        ctx = json.loads(story.get("context_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        pass
    ctx["_agent_actions"] = actions
    ctx["_plan_confirmed"] = False
    # FC 路径补写 plan_summary：把所有 launch action 的 "stage: focus"
    # 拼成总览，修复下游 verify gate / repair packet 的 Plan 断链
    # （ISS-004）。同时让 GET /plan 的 plan_summary UI 字段非空。
    ctx["plan_summary"] = "; ".join(
        f"{a.get('stage', '')}: {a.get('focus', '')}"
        for a in actions
        if a.get("action") == "launch"
    )
    db.update_story(
        story_key,
        context_json=json.dumps(ctx, ensure_ascii=False),
        status="planning",
    )

    return {"status": "planning", "actions": actions}


# headless claude/codex 是真实 AI，非确定：偶发 rc!=0 退出（API 抖动/限流/崩溃）
# 而没写 done file。给每个 stage 最多重试这么多次（含首次），扛住瞬时抖动。
HEADLESS_MAX_ATTEMPTS = 3


def _kill_headless(proc):
    """Best-effort kill of a headless AI CLI process AND its child tree.

    claude/codex CLIs spawn children (node runtime, MCP servers); killing only
    the top PID orphans them — and a claude that already wrote its done file but
    keeps running will otherwise linger. On Windows use ``taskkill /T`` to take
    the whole tree; elsewhere fall back to ``proc.kill()``.
    """
    import os as _os
    import subprocess as _sp

    try:
        if proc.poll() is None:
            if _os.name == "nt":
                _sp.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    timeout=15,
                )
            else:
                proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _write_retrospect(workspace: str, story_key: str, actions: list) -> None:
    """聚合各 stage 的 done.json 摘要，写 story 级 retrospect.md。

    落到 ``<workspace>/.story/done/<story_key>/retrospect.md``，供 real-E2E 断言
    与人工复盘读取。这是 story 完成时的轻量复盘（来自各阶段 done 产物）；基于
    transcript 的深度复盘仍由 agent-transcript-miner 的 retrospect.py 负责。
    best-effort：写失败只告警，不影响 story 完成状态。
    """

    done_dir = safe_story_path(workspace, ".story", "done", story_key)
    lines = [f"# Retrospect — {story_key}", ""]
    n = 0
    for action in actions or []:
        if action.get("action") != "launch":
            continue
        stage = action.get("stage", "")
        dj = done_dir / f"{stage}.json"
        if not dj.exists():
            continue
        try:
            data = json.loads(dj.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        lines.append(f"## {stage}")
        lines.append(str(data.get("summary", "（无摘要）")))
        fc = data.get("files_changed") or []
        if fc:
            lines.append("")
            lines.append("**变更文件：** " + ", ".join(f"`{f}`" for f in fc))
        lines.append("")
        n += 1
    if n == 0:
        lines.append("（未捕获到任何阶段 done 产物）")
    try:
        done_dir.mkdir(parents=True, exist_ok=True)
        (done_dir / "retrospect.md").write_text("\n".join(lines), encoding="utf-8")
        log.info("[%s] wrote retrospect.md (%d stages)", story_key, n)
    except OSError as exc:
        log.warning("[%s] failed to write retrospect.md: %s", story_key, exc)


# stage → story_document.kind 映射(对齐 auto_discovery.py 的约定:
# design→spec, build→plan, verify→test_report)。不在映射里的 stage 跳过。
_STAGE_DOC_KIND = {
    "design": "spec",
    "build": "plan",
    "verify": "test_report",
}


def _register_stage_outputs(story_key: str, stage: str, done_data: dict) -> None:
    """把 stage done 产出的文件登记进 story_document(BUG #17)。

    纯确定性:读 done_data["files_changed"],按 stage 推导 kind,调
    db.create_document(幂等)。让前端「文档」卡片可追溯 design/plan/test_report。

    - 过滤 .story/done/*.json(done 握手文件本身不算文档)。
    - files_changed 为空时,也读 done_data 的显式路径字段(spec_path 等)兜底。
    - stage 不在 _STAGE_DOC_KIND 里则跳过(防御)。
    """
    kind = _STAGE_DOC_KIND.get(stage)
    if not kind:
        return

    from ...infra.db import models as db  # 延迟 import(避免循环)

    paths: list[str] = []
    for f in done_data.get("files_changed") or []:
        if f and ".story/done/" not in f and ".story\\done\\" not in f:
            paths.append(f)
    # 兜底:done JSON 的显式路径字段(claude 偶尔不写 files_changed)
    for key in ("spec_path", "research_path", "plan_path", "test_report_path"):
        v = done_data.get(key)
        if isinstance(v, str) and v:
            paths.append(v)

    for ref in paths:
        try:
            db.create_document(
                story_key,
                kind,
                ref=ref,
                summary="",
                source="ai",
                verification_state="unverified",
            )
        except Exception:  # noqa: BLE001 — 单个文件登记失败不影响其他
            log.exception(
                "[%s] create_document failed for stage=%s ref=%s",
                story_key,
                stage,
                ref,
            )


def _build_verify_history_facts(*, db, failed_adapter, gate_round, retry_limit):
    """层5 回注:查全局决策事件 → reflect playbook → transition ``history_facts``。

    飞轮闭环:历史 recovery 换 adapter 成功 → reflect 沉淀规则 → 当前 verify-gate
    失败时 ``same_failure_swap_succeeded=True`` → decide_transition 选 swap_approach。
    任何异常 → 安全兜底(不阻塞 verify-gate 主流程)。
    """
    try:
        from ..learning.reflection import build_transition_history_facts

        raw = db.get_recent_events_by_type(
            ["recovery_action", "judge_verdict", "transition_decision"], limit=100
        )
        parsed = []
        for r in raw:
            try:
                payload = json.loads(r.get("payload") or "{}")
            except Exception:
                payload = {}
            parsed.append(
                {
                    "story_key": r.get("story_key", ""),
                    "event_type": r.get("event_type", ""),
                    "payload": payload,
                }
            )
        return build_transition_history_facts(
            events=parsed,
            failed_adapter=failed_adapter,
            gate_round=gate_round,
            retry_limit=retry_limit,
        )
    except Exception:
        return {
            "failure_count_on_stage": gate_round,
            "max_retries": retry_limit,
            "same_failure_swap_succeeded": False,
        }


@with_story_key()
def continue_orchestrator_agent(story_key: str, headless: bool = False):
    """用户确认规划后，执行 action list。

    遍历 action list，逐个执行：
    - launch: 启动 CLI，轮询 done file
    - skip: 记录跳过

    执行在后台线程中运行。
    """
    from ...infra.db import models as db
    from ...knowledge.adapters import get_adapter
    from ..engine.profile_loader import resolve_profile
    from ...infra.json_helpers import robust_json_parse
    from ...infra.terminal.pty import ensure_agent_pty

    story = db.get_story(story_key)
    if not story:
        raise ValueError(f"Story not found: {story_key}")

    workspace = story.get("workspace", "")
    title = story.get("title", "")
    profile_name = story.get("profile", "minimal")

    ctx = {}
    try:
        ctx = json.loads(story.get("context_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        pass

    actions = ctx.get("_agent_actions", [])
    if not actions:
        log.warning(f"No actions found for {story_key}")
        db.update_story(story_key, status="failed", last_error="No actions to execute")
        return

    # 更新状态为执行中
    ctx["_plan_confirmed"] = True
    # 进入执行即清除确认闸标记(确认闸 paused 后 resume,经 /advance → start_story_async
    # 重进此函数;_stage_gate 只在 paused 期间有意义,执行启动即失效)。
    ctx.pop("_stage_gate", None)
    # STORY-STATE-MODEL: 同理清 Story 状态闸标记(/lifecycle/advance 推进后重进)。
    ctx.pop("_story_state_gate", None)
    db.update_story(
        story_key,
        context_json=json.dumps(ctx, ensure_ascii=False),
        status="active",
    )

    # 解析 profile 用于生成 prompt 和质量门禁配置
    profile_stages = {}
    quality_cfg = {}
    story_states = {}  # STORY-STATE-MODEL: Story 业务状态机定义(开发/测试/...)
    rp = None
    try:
        rp = resolve_profile(profile_name)
        profile_stages = {name: cfg for name, cfg in rp.stages.items()}
        quality_cfg = rp.quality or {}
        story_states = rp.story_states or {}
    except Exception:
        pass
    # STORY-STATE-MODEL: 初始化 lifecycle_state(Story 业务状态,独立第一公民)。
    # 优先级:ctx._lifecycle_state(resume 续用)> DB lifecycle_state > 默认"开发"。
    # 写回 DB + ctx 保证一致。无 story_states 的 profile → lifecycle_state 仍存但
    # driver 不按状态机跑(退化扁平,向后兼容)。
    lifecycle_state = (
        ctx.get("_lifecycle_state") or story.get("lifecycle_state") or "开发"
    )
    if ctx.get("_lifecycle_state") != lifecycle_state:
        ctx["_lifecycle_state"] = lifecycle_state
        db.update_story(
            story_key,
            lifecycle_state=lifecycle_state,
            context_json=json.dumps(ctx, ensure_ascii=False),
        )
    # profile 的 execution_mode 覆盖 headless(默认 PTY;realtest 等 profile 显式 headless →
    # headless 路径:kimi -p wrapper + stderr drain,真跑验证可跑通。PTY 路径 kimi idle 见 docs)。
    from .execution import headless_from_profile

    if headless_from_profile(rp):
        headless = True

    # resume 跳过已完成 stage(PLAN-stage-confirm-gate):_completed_stages 记录已跑完的
    # stage,resume 时从第一个未完成 launch action 开始,不重 spawn 不重跑(见 docs)。
    completed_stages = list(ctx.get("_completed_stages", []))
    if not completed_stages:
        # 认领游离 done:用户可能手动跑出了某 stage 的 done file(如 design.json)而
        # 从未走过自动链路(_completed_stages 为空)。扫一遍 launch actions,凡 done file
        # 已存在的 stage 认领进 _completed_stages —— 点「开始」后不重跑。
        orphan_claimed = []
        for _a in actions:
            if _a.get("action") != "launch":
                continue
            _st = _a.get("stage")
            if not _st:
                continue
            _done_rel = _a.get("done_file", stage_done_file_rel(story_key, _st))
            if (Path(workspace) / _done_rel).exists():
                orphan_claimed.append(_st)
        if orphan_claimed:
            completed_stages = orphan_claimed
            ctx["_completed_stages"] = completed_stages
            # 对每个被认领的 stage 记一次 completed 事件(读 done file 作 payload):
            # 跳过的 stage 也要在 timeline / 质量统计里出现,与正常 done 路径一致。
            for _st in orphan_claimed:
                try:
                    _dp = Path(workspace) / stage_done_file_rel(story_key, _st)
                    _dd = robust_json_parse(_dp) or {}
                except Exception:
                    _dd = {}
                db.log_event(story_key, _st, "completed", _dd)
            log.info(
                "[%s] claimed orphan done files as completed: %s",
                story_key,
                completed_stages,
            )

    # 算 start_idx:第一个 stage ∉ _completed_stages 的 launch action 下标。
    start_idx = 0
    for _i, _a in enumerate(actions):
        if _a.get("action") == "launch":
            _st = _a.get("stage", f"stage_{_i}")
            if _st in completed_stages:
                continue
            start_idx = _i
            break
        start_idx = _i + 1  # skip actions 仍推进
    else:
        start_idx = len(actions)  # 全部已完成 → 末尾(while 不进)

    if start_idx > 0:
        log.info(
            "[%s] resuming from action %d (completed_stages=%s)",
            story_key,
            start_idx,
            completed_stages,
        )

    # 逐个执行 action；使用 while 以便在 verify gate 触发 retry 时插入重试 action
    idx = start_idx
    while idx < len(actions):
        action = actions[idx]
        if action.get("action") == "skip":
            stage = action.get("stage", f"stage_{idx}")
            reason = action.get("reason", "")
            db.log_event(story_key, stage, "skipped", {"reason": reason})
            log.info(f"[{story_key}] Skipped stage {stage}: {reason}")
            idx += 1
            continue

        if action.get("action") == "launch":
            stage = action.get("stage", f"stage_{idx}")
            adapter_name = action.get("adapter", "")
            # profile 兜底:profile 该 stage 配了 cli 时,覆盖 LLM 规划的 adapter
            # (LLM plan_step enum 可能不含 profile 配的值,如 kimi;用户也可在
            # 确认前改 adapter,该覆盖在 /plan/confirm 时写回 _agent_actions)。
            if stage in profile_stages:
                _cfg_cli = getattr(profile_stages[stage], "cli", "") or ""
                if _cfg_cli and _cfg_cli != adapter_name:
                    log.info(
                        "[%s] stage %s: profile cli=%r overrides action adapter=%r",
                        story_key,
                        stage,
                        _cfg_cli,
                        adapter_name,
                    )
                    adapter_name = _cfg_cli
                    action["adapter"] = _cfg_cli  # 回写,供下游一致
            if not adapter_name:
                adapter_name = "claude"
            focus = action.get("focus", "")
            # done_file 强制规范化:不信任 action 里(可能来自老规划/LLM 自由生成)的值,
            # 统一用 .story/done/<key>/<stage>.json,杜绝跨 story 撞名(BUG #7)。
            _action_done = action.get("done_file")
            done_file_rel = stage_done_file_rel(story_key, stage)
            if _action_done and _action_done != done_file_rel:
                log.info(
                    "[%s] overriding action done_file=%r -> canonical %r",
                    story_key,
                    _action_done,
                    done_file_rel,
                )
                action["done_file"] = (
                    done_file_rel  # 回写规范化值,供下游(prompt/resume)一致
                )

            # 更新当前阶段
            db.update_story(story_key, current_stage=stage)

            # BUG #18: build 阶段(改代码)前,自动为每个绑定仓库建 worktree+feature 分支。
            # design 不需要(只调研不改代码)。prepare_worktrees 幂等(已建走 REUSE)。
            # 失败不阻断 build——降级到主 workspace(符合现有容错基调)。
            if stage == "build" and db.get_story_projects(story_key):
                try:
                    from ..workspace.worktree.handler import prepare_worktrees

                    prepared = prepare_worktrees(story_key)
                    created = [r for r in prepared if r.get("action") == "create"]
                    if created:
                        log.info(
                            "[%s] worktrees prepared for build: %s",
                            story_key,
                            [r.get("worktree_path") for r in created],
                        )
                except Exception:
                    log.exception(
                        "[%s] prepare_worktrees failed; build proceeds on main workspace",
                        story_key,
                    )

            # 查项目绑定，拼成分支隔离提示
            # BUG #18: worktree 已建时显示 worktree 路径(让 agent 直接 cd 进去),
            # 否则降级显示分支/基线(advisory,让 agent 自行判断)。
            project_lines = []
            for sp in db.get_story_projects(story_key):
                proj = db.get_project(sp["project_id"])
                if not proj:
                    continue
                wt = sp.get("worktree_path", "")
                if wt:
                    project_lines.append(
                        f"- 仓库 `{proj['repo_path']}` → worktree `{wt}` "
                        f"(分支 `{sp['branch']}`, 基线 `{sp.get('base_branch', 'main')}`)"
                    )
                else:
                    project_lines.append(
                        f"- 仓库 `{proj['repo_path']}`: 分支 `{sp['branch']}`, "
                        f"基线 `{sp.get('base_branch', 'main')}`"
                    )
            project_section = "\n".join(project_lines)

            # 构建 CLI prompt
            from ...knowledge.context_providers import get_transcript_context

            transcript_ctx = get_transcript_context(story_key, workspace, stage)
            cli_prompt = _build_cli_prompt(
                story_key=story_key,
                title=title,
                stage=stage,
                focus=focus,
                done_file=done_file_rel,
                profile_stages=profile_stages,
                prd_path=ctx.get("prd_path", ""),
                project_section=project_section,
                workspace=workspace,
                transcript_section=transcript_ctx or "",
                interactive=not headless,  # BUG #9:交互式路径走"终端直接问人"
            )

            # 写入 prompt 文件
            prompt_dir = safe_story_path(workspace, ".story", "context", story_key)
            prompt_dir.mkdir(parents=True, exist_ok=True)
            prompt_file = prompt_dir / f"prompt_{stage}.md"
            prompt_file.write_text(cli_prompt, encoding="utf-8")

            # 启动 CLI
            try:
                adapter = get_adapter(adapter_name)
                # 获取 stage model 配置
                model = ""
                if stage in profile_stages:
                    cfg = profile_stages[stage]
                    model = cfg.model if hasattr(cfg, "model") else ""
                if headless:
                    launch_cmd = adapter.headless_launch_cmd(model=model, prompt="")
                else:
                    launch_cmd = adapter.interactive_launch_cmd(model=model)

                # design 阶段 + claude + headless:接外接 MCP clarify 工具(--mcp-config 加载
                # lifecycle server)+ 注入 STORY_KEY env(MCP server 经继承读它定位 story DB)。
                # 仅 headless 路径走 MCP clarify;交互式路径(interactive_pty)走"终端直接问人"
                # (BUG #9,见 handoff-design-hitl §11 + build_design_dimensions_section)。
                # 见 orchestrator/mcp/clarify_server.py + memory story-lifecycle-design-hitl。
                story_env = None
                if stage == "design" and adapter_name == "claude" and headless:
                    import os as _os
                    import sys as _sys

                    try:
                        from ..mcp.clarify_server import write_mcp_config

                        _mcp_cfg = (
                            safe_story_path(workspace, ".story", "context", story_key)
                            / "clarify_mcp.json"
                        )
                        write_mcp_config(_mcp_cfg, _sys.executable)
                        launch_cmd = list(launch_cmd) + ["--mcp-config", str(_mcp_cfg)]
                        story_env = {**_os.environ, "STORY_KEY": story_key}
                        log.info(
                            "[%s] design clarify MCP wired: --mcp-config=%s STORY_KEY set",
                            story_key,
                            _mcp_cfg,
                        )
                    except Exception:
                        log.exception(
                            "[%s] design clarify MCP wiring failed (clarify unavailable)",
                            story_key,
                        )
                _ctx_markers = (
                    "上下文",
                    "context",
                    "DDL",
                    "CREATE TABLE",
                    "Nacos",
                    "PRD",
                    "表结构",
                    "接口定义",
                )
                log.info(
                    "[%s] >>> EXECUTE stage=%s adapter=%s model=%s cmd=%s workspace=%s",
                    story_key,
                    stage,
                    adapter_name,
                    model or "-",
                    launch_cmd,
                    workspace,
                )
                log.info(
                    "[%s] injecting prompt into PTY: %d chars; contains-context=%s; head=%r",
                    story_key,
                    len(cli_prompt),
                    any(m in cli_prompt for m in _ctx_markers),
                    cli_prompt[:120],
                )
                headless_proc = None
                _agent_pty = None  # interactive 分支才赋值(见 else);此处初始化让 done 块两类分支都能安全引用
                _stderr_tail = []  # headless stderr 排空 holder(kimi 叙述/claude 日志 → 防 PIPE 死锁 + retry 诊断)
                if headless:
                    import subprocess as _sp

                    # I2 miner binding：headless 路径不经过 adapter.inject_prompt()，
                    # 显式补写 anchor，使 miner.link 能按 (cwd+ts) 精确回填
                    # sessions.story_id。best-effort，绝不阻断 spawn。
                    try:
                        adapter.write_anchor(
                            prompt=cli_prompt,
                            story_key=story_key,
                            stage=stage,
                            cwd=workspace,
                            workspace=workspace,
                        )
                    except Exception:
                        pass
                    log.info(
                        "[%s] HEADLESS spawn stage=%s cmd=%s",
                        story_key,
                        stage,
                        launch_cmd,
                    )
                    # 非阻塞启动：done file 才是完成信号。claude -p 写完 done file 后
                    # 往往继续运行很久不自行退出，blocking subprocess.run 会一路卡到超时；
                    # 改用 Popen 与 done-file 轮询并发——done file 一出现即 kill claude、
                    # 推进下一阶段（headless_proc 在下方 poll 循环里被回收）。
                    try:
                        headless_proc = _sp.Popen(
                            launch_cmd,
                            cwd=workspace,
                            stdin=_sp.PIPE,
                            stdout=_sp.PIPE,
                            stderr=_sp.PIPE,
                            env=story_env,
                        )
                        headless_proc.stdin.write(cli_prompt.encode("utf-8"))
                        headless_proc.stdin.close()
                    except Exception as exc:
                        db.update_story(
                            story_key,
                            status="failed",
                            last_error=f"Stage {stage} headless spawn failed: {exc}",
                        )
                        return
                    # §4.1 层1 supervisor(headless):daemon 线程消费 stdout —— 双重价值:
                    # (a) drain stdout 防 PIPE 缓冲满致 proc 阻塞(主循环只轮询 done,从不读 stdout);
                    # (b) 命中提问(claude permission/elicitation、kimi 选择)→ decide_response + 落 supervisor_decision。
                    # observe-only:headless stdin 已关,不回写答案。
                    try:
                        import threading as _th
                        from .claude_stream import supervise_headless_stdout

                        _sup_llm = get_llm().invoke
                        _sup_sf = {
                            "story_key": story_key,
                            "stage": stage,
                            "summary": focus,
                        }
                        _sup_proc = headless_proc
                        _sup_stderr = _stderr_tail  # drain 线程排空 stderr 到此 holder

                        def _drain_headless():
                            try:
                                supervise_headless_stdout(
                                    proc=_sup_proc,
                                    adapter=adapter_name,
                                    story_facts=_sup_sf,
                                    llm_invoke=_sup_llm,
                                    log_event_fn=db.log_event,
                                    stderr_tail=_sup_stderr,
                                )
                            except Exception:
                                pass

                        _th.Thread(
                            target=_drain_headless,
                            daemon=True,
                            name=f"supervise-h-{story_key}",
                        ).start()
                    except Exception:
                        pass
                    log.info(
                        "[%s] HEADLESS pid=%s stage=%s (polling done file, not exit)",
                        story_key,
                        headless_proc.pid,
                        stage,
                    )
                else:
                    _pty_session, _agent_pty = ensure_agent_pty(
                        story_key,
                        launch_cmd,
                        workspace,
                        cli_prompt,  # prompt 作为第 4 个参数注入到 PTY
                        readiness_marker=getattr(adapter, "readiness_marker", None),
                        env=story_env,
                    )
                    log.info("[%s] PTY session started for stage=%s", story_key, stage)
                    # §4.1 层1 supervisor(interactive PTY):daemon 线程跑 supervise_pty_session。
                    # run_story 在 ThreadPoolExecutor 线程里(无 asyncio loop)→ 独立 daemon 线程 + new_event_loop。
                    # pty 死时 supervise_pty_session 退出(轮询 pty.alive)。
                    try:
                        import asyncio as _aio
                        import threading as _th

                        from .awaiting_detector import make_awaiting_fn
                        from .supervisor import supervise_pty_session

                        _sup_llm = get_llm().invoke
                        _sup_sf = {
                            "story_key": story_key,
                            "stage": stage,
                            "summary": focus,
                        }
                        _sup_pty = _agent_pty
                        _sup_det = make_awaiting_fn(adapter_name)

                        def _supervise_pty():
                            try:
                                loop = _aio.new_event_loop()
                                _aio.set_event_loop(loop)
                                loop.run_until_complete(
                                    supervise_pty_session(
                                        pty=_sup_pty,
                                        adapter=adapter_name,
                                        story_facts=_sup_sf,
                                        is_awaiting_fn=_sup_det,
                                        llm_invoke=_sup_llm,
                                        log_event_fn=db.log_event,
                                    )
                                )
                            except Exception:
                                pass

                        _th.Thread(
                            target=_supervise_pty,
                            daemon=True,
                            name=f"supervise-p-{story_key}",
                        ).start()
                    except Exception:
                        pass
            except Exception as exc:
                log.error(
                    f"[{story_key}] Failed to launch {adapter_name} for {stage}: {exc}"
                )
                db.update_story(
                    story_key,
                    status="failed",
                    last_error=f"CLI launch failed for {stage}: {exc}",
                )
                return

            # 更新执行上下文
            ctx["_active_execution"] = {
                "mode": "interactive_pty",
                "adapter": adapter_name,
                "stage": stage,
                "start_time": time.time(),
            }
            db.update_story(
                story_key,
                context_json=json.dumps(ctx, ensure_ascii=False),
            )

            # 轮询 done file
            done_path = Path(workspace) / done_file_rel
            poll_timeout = (
                45 * 60
            )  # 45 minutes(realtest:大 codebase 上 kimi design/build 较慢,§0.1 时间不限,留余量)
            poll_interval = 5  # seconds
            elapsed = 0
            headless_attempt = 1  # headless 重试计数（首次=1）

            while elapsed < poll_timeout:
                # headless：claude 若已退出却没写 done file，提前失败（不等满 30min）
                if (
                    headless_proc is not None
                    and headless_proc.poll() is not None
                    and not done_path.exists()
                ):
                    rc = headless_proc.returncode
                    stderr_tail, stdout_tail = "", b""
                    try:
                        # stderr 已由 drain daemon(supervise_headless_stdout)排空到 _stderr_tail;
                        # 不再 headless_proc.stderr.read()(与 drain 线程争用 / 阻塞)。
                        stderr_tail = "".join(_stderr_tail)[-500:]
                        if headless_proc.stdout:
                            stdout_tail = headless_proc.stdout.read()[-800:]
                    except Exception:
                        pass
                    # claude 非确定：偶发 rc!=0 退出（API 抖动/限流/崩溃）却没写 done
                    # file → 重试，扛住瞬时抖动（共享下方 poll_timeout 预算，不另加时）。
                    if headless_attempt < HEADLESS_MAX_ATTEMPTS:
                        log.warning(
                            "[%s] claude exited rc=%d before done file (attempt %d/%d); "
                            "re-launching. stderr=%r stdout_tail=%r",
                            story_key,
                            rc,
                            headless_attempt,
                            HEADLESS_MAX_ATTEMPTS,
                            stderr_tail,
                            stdout_tail,
                        )
                        headless_attempt += 1
                        try:
                            headless_proc = _sp.Popen(
                                launch_cmd,
                                cwd=workspace,
                                stdin=_sp.PIPE,
                                stdout=_sp.PIPE,
                                stderr=_sp.PIPE,
                                env=story_env,
                            )
                            headless_proc.stdin.write(cli_prompt.encode("utf-8"))
                            headless_proc.stdin.close()
                        except Exception as exc:
                            db.update_story(
                                story_key,
                                status="failed",
                                last_error=f"Stage {stage}: headless retry spawn failed: {exc}",
                            )
                            return
                        log.info(
                            "[%s] HEADLESS retry pid=%s stage=%s (attempt %d)",
                            story_key,
                            headless_proc.pid,
                            stage,
                            headless_attempt,
                        )
                        continue
                    log.warning(
                        "[%s] claude exited rc=%d without done file after %d attempts; "
                        "giving up. stdout_tail=%r",
                        story_key,
                        rc,
                        HEADLESS_MAX_ATTEMPTS,
                        stdout_tail,
                    )
                    db.update_story(
                        story_key,
                        status="failed",
                        last_error=(
                            f"Stage {stage}: claude exited (rc={rc}) without done file "
                            f"after {HEADLESS_MAX_ATTEMPTS} attempts"
                        ),
                    )
                    return
                # design 逐问澄清:claude 阻塞在 mcp__lifecycle__clarify 调用上等人答,
                # 此期间不写 done file。若不感知这一阻塞,45min poll_timeout 会把"等人答"
                # 误判为超时 fail(BUG #10)。检测到 pending clarification → 重置 elapsed,
                # 让超时只在 claude 真卡死(非澄清)时触发。澄清是有限轮次(prompt 约束 ≤3 轮),
                # 不会无限重置。
                try:
                    from ..mcp.clarify_server import get_pending_clarification

                    if (
                        get_pending_clarification(
                            story_key, get_events_fn=db.get_story_events
                        )
                        is not None
                    ):
                        if elapsed > 0:
                            log.info(
                                "[%s] design blocked on clarification; "
                                "resetting poll timeout clock (was %ds/%ds)",
                                story_key,
                                elapsed,
                                poll_timeout,
                            )
                        elapsed = 0
                except Exception:
                    pass  # clarify 检测失败不影响主轮询
                # 检查 done file
                if done_path.exists():
                    try:
                        # robust_json_parse 接收 Path（内部自读，并容忍 markdown 包裹/
                        # 半写文件：解析失败会抛异常，由下方 except 捕获后轮询重试，
                        # 等 claude 把 done file 写完整再消费）。
                        done_data = robust_json_parse(done_path) or {}
                        db.log_event(story_key, stage, "completed", done_data)
                        # BUG #17: 登记 stage 产出文件进 story_document(纯确定性,
                        # 让前端「文档」卡片可追溯)。失败不阻塞主流程。
                        try:
                            _register_stage_outputs(story_key, stage, done_data)
                        except Exception:
                            log.exception(
                                "[%s] register stage outputs failed for %s",
                                story_key,
                                stage,
                            )
                        log.info(
                            f"[{story_key}] Stage {stage} completed: "
                            f"{done_data.get('summary', '')[:100]}"
                        )
                        # 保留 done file 作为阶段完成证据：real-E2E asserters 与
                        # 审计都需要事后读取 {stage}.json。每个 stage 的 done 路径唯一，
                        # 重跑由 reset_workspace 清理 done/ 目录，无需在此 unlink。
                        # 记进度(PLAN-stage-confirm-gate):追加当前 stage 到 _completed_stages
                        # 并持久化,resume 时 start_idx 跳过本 stage(不重 spawn PTY)。
                        if stage not in completed_stages:
                            completed_stages.append(stage)
                        ctx["_completed_stages"] = completed_stages
                        db.update_story(
                            story_key,
                            context_json=json.dumps(ctx, ensure_ascii=False),
                        )
                        # 回收 stage 进程(done 已确认,transcript 已写完整):
                        # headless 走 _kill_headless;interactive PTY 对齐 headless —— 先
                        # clean_exit_pty(/exit 握手 flush transcript,最长 _CLEAN_EXIT_TIMEOUT)
                        # 再 .kill() 兜底。需要时用 claude --resume <per-stage uuid5> 回查。
                        if headless_proc is not None:
                            _kill_headless(headless_proc)
                        if _agent_pty is not None:
                            try:
                                from ...infra.terminal.pty import clean_exit_pty

                                clean_exit_pty(_agent_pty)
                            except Exception:
                                log.exception(
                                    "[%s] clean_exit_pty failed for stage %s; force-killing",
                                    story_key,
                                    stage,
                                )
                            try:
                                _agent_pty.kill()
                            except Exception:
                                pass
                        # STORY-STATE-MODEL: Story 状态闸(业务层,优先于阶段间闸)。
                        # 当前 lifecycle_state 定义的所有 stages 全 done → 按该状态 confirm
                        # 规则转移:ui_button→paused 等人;config(auto)→直接推进;none→推进。
                        # 触发转移后不再走下方阶段间闸。无 story_states 配置 → 跳过(向后兼容)。
                        _state_handled = False
                        if story_states and lifecycle_state in story_states:
                            _state_def = story_states[lifecycle_state] or {}
                            _state_stages = list(_state_def.get("stages") or [])
                            if _state_stages and all(
                                _ss in completed_stages for _ss in _state_stages
                            ):
                                _next_state = _state_def.get("next")
                                _confirm = _state_def.get("confirm") or {}
                                _ctype = _confirm.get("type", "none")
                                # config 类型:auto_advance 看 key 指定的环境/全局配置
                                _auto = False
                                if _ctype == "config":
                                    import os as _os

                                    _ck = _confirm.get("key", "")
                                    _auto = _os.environ.get(
                                        f"STORY_{_ck}".upper(), ""
                                    ).lower() in ("1", "true", "yes")
                                if _next_state and (_ctype in ("none",) or _auto):
                                    # 无条件 / 配置自动 → 直接推进到下一 Story 状态
                                    lifecycle_state = _next_state
                                    ctx["_lifecycle_state"] = _next_state
                                    ctx.pop("_story_state_gate", None)
                                    db.update_story(
                                        story_key,
                                        lifecycle_state=_next_state,
                                        context_json=json.dumps(
                                            ctx, ensure_ascii=False
                                        ),
                                    )
                                    db.log_event(
                                        story_key,
                                        stage,
                                        "story_state_transition",
                                        {
                                            "from": lifecycle_state,
                                            "to": _next_state,
                                            "auto": True,
                                        },
                                    )
                                    log.info(
                                        "[%s] story state auto-advanced: %s → %s",
                                        story_key,
                                        stage,
                                        _next_state,
                                    )
                                    _state_handled = True
                                elif _next_state and _ctype == "ui_button":
                                    # 人工确认 → paused,前端显示 Story 状态闸卡片
                                    ctx["_story_state_gate"] = {
                                        "from": lifecycle_state,
                                        "to": _next_state,
                                        "awaiting_confirm": True,
                                        "label": _confirm.get(
                                            "label", f"进入{_next_state}"
                                        ),
                                    }
                                    db.update_story(
                                        story_key,
                                        status="paused",
                                        context_json=json.dumps(
                                            ctx, ensure_ascii=False
                                        ),
                                    )
                                    db.log_event(
                                        story_key,
                                        stage,
                                        "story_state_gate_reached",
                                        {"from": lifecycle_state, "to": _next_state},
                                    )
                                    log.info(
                                        "[%s] story state gate: %s done → paused awaiting confirm to advance %s → %s",
                                        story_key,
                                        stage,
                                        lifecycle_state,
                                        _next_state,
                                    )
                                    return  # 释放 driver;点「进入下一状态」→ /lifecycle/advance
                                elif not _next_state:
                                    # 终态:所有 Story 状态跑完 → 整个 story 完成
                                    db.update_story(story_key, status="completed")
                                    log.info(
                                        "[%s] reached terminal story state %s (all done)",
                                        story_key,
                                        lifecycle_state,
                                    )
                                    _write_retrospect(workspace, story_key, actions)
                                    return
                        # 阶段间闸(PLAN-stage-confirm-gate):仅当 Story 状态闸未处理时执行。
                        # stage_cfg.confirm=True 且后面还有未完成 launch action → paused。
                        # verify 是最后阶段无下一 stage,走自己的 gate,不受此影响。
                        if not _state_handled:
                            stage_cfg = profile_stages.get(stage)
                            confirm_on = bool(
                                stage_cfg
                                and getattr(stage_cfg, "confirm", False)
                                and stage != "verify"
                            )
                            if confirm_on:
                                _next_stage = None
                                for _j in range(idx + 1, len(actions)):
                                    _na = actions[_j]
                                    if _na.get("action") == "launch":
                                        _ns = _na.get("stage", f"stage_{_j}")
                                        if _ns not in completed_stages:
                                            _next_stage = _ns
                                            break
                                if _next_stage is not None:
                                    ctx["_stage_gate"] = {
                                        "completed_stage": stage,
                                        "next_stage": _next_stage,
                                        "awaiting_confirm": True,
                                    }
                                    db.update_story(
                                        story_key,
                                        status="paused",
                                        context_json=json.dumps(
                                            ctx, ensure_ascii=False
                                        ),
                                    )
                                    db.log_event(
                                        story_key,
                                        stage,
                                        "stage_gate_reached",
                                        {
                                            "completed_stage": stage,
                                            "next_stage": _next_stage,
                                        },
                                    )
                                    log.info(
                                        "[%s] stage gate: %s done → paused awaiting confirm to advance to %s",
                                        story_key,
                                        stage,
                                        _next_stage,
                                    )
                                    return  # 释放 driver claim;点「推进」→ /advance 重进
                        break
                    except Exception as exc:
                        log.error(f"[{story_key}] Error parsing done file: {exc}")

                time.sleep(poll_interval)
                elapsed += poll_interval
            else:
                # 超时：回收 headless claude 进程，避免孤儿残留
                if headless_proc is not None:
                    _kill_headless(headless_proc)
                log.warning(
                    f"[{story_key}] Stage {stage} timed out after {poll_timeout}s"
                )
                db.update_story(
                    story_key,
                    status="failed",
                    last_error=f"Stage {stage} timed out",
                )
                return

            # Verify-stage quality gate: HIGH findings block and trigger repair round
            if stage == "verify":
                from ...orchestrator.evaluation.gate import run_verify_gate

                stage_cfg = profile_stages.get(stage)
                max_retries = (
                    stage_cfg.max_retries if hasattr(stage_cfg, "max_retries") else 2
                )
                ctx["last_verify_summary"] = done_data.get("summary", "")
                ctx["last_done_data"] = (
                    done_data  # §4.2:喂给 judge_verify_stage(层4 @ gate)
                )
                gate_result = run_verify_gate(
                    story_key=story_key,
                    stage=stage,
                    workspace=workspace,
                    context=ctx,
                    quality_cfg=quality_cfg,
                    max_retries=max_retries,
                )
                if gate_result["decision"] == "retry":
                    # 层2 transition(阶段3 接入):decide_transition 决定怎么转,替硬编码 insert。
                    from .transition import build_repair_action, decide_transition

                    reason_text = str(gate_result.get("reason", "")).lower()
                    failure_mode = (
                        "missing_dependency"
                        if any(
                            k in reason_text
                            for k in ("depend", "import", "no module", "no such file")
                        )
                        else "quality"
                    )
                    # 层5 回注:reflect playbook → history_facts(让 swap_approach 真触发,
                    # 替硬编码 False)。飞轮:历史"换 adapter 成功"→ 当前同类失败时 decide_transition 选 swap_approach。
                    history_facts = _build_verify_history_facts(
                        db=db,
                        failed_adapter=adapter_name,
                        gate_round=gate_result.get("round", 1),
                        retry_limit=gate_result.get("retry_limit", max_retries),
                    )
                    if failure_mode == "missing_dependency":
                        history_facts["missing_dep"] = gate_result.get("reason", "")
                    transition = decide_transition(
                        gate_decision={"pass": False, "rework_point": "verify"},
                        failure_mode=failure_mode,
                        history_facts=history_facts,
                    )
                    db.log_event(story_key, stage, "transition_decision", transition)
                    repair_action = build_repair_action(
                        transition_decision=transition,
                        story_key=story_key,
                        gate_result=gate_result,
                        adapter_name=adapter_name,
                    )
                    if repair_action is None:
                        # escalate/proceed/skip → 不插 action,标失败(transition 决策已落事件)
                        db.update_story(
                            story_key,
                            status="failed",
                            last_error=str(transition.get("reason", ""))[:500],
                        )
                        return
                    actions.insert(idx + 1, repair_action)
                    ctx["_agent_actions"] = actions
                    db.update_story(
                        story_key,
                        context_json=json.dumps(ctx, ensure_ascii=False),
                    )
                    log.info(
                        "[%s] Verify gate blocked (round %d/%d); transition=%s adapter=%s",
                        story_key,
                        gate_result["round"],
                        gate_result["retry_limit"],
                        transition["action"],
                        repair_action.get("adapter", "-"),
                    )
                elif gate_result["decision"] == "fail":
                    db.update_story(
                        story_key,
                        status="failed",
                        last_error=gate_result["reason"],
                    )
                    return

        idx += 1

    # 所有 action 执行完毕
    db.update_story(story_key, status="completed")
    log.info(f"[{story_key}] All stages completed")
    # story 完成时生成 retrospect.md（聚合各 stage done 摘要）
    _write_retrospect(workspace, story_key, actions)


def _build_cli_prompt(
    *,
    story_key: str,
    title: str,
    stage: str,
    focus: str,
    done_file: str,
    profile_stages: dict,
    prd_path: str = "",
    project_section: str = "",
    workspace: str = "",
    transcript_section: str = "",
    interactive: bool = False,
) -> str:
    """构建给 CLI 的执行 prompt。

    ``interactive``:交互式终端路径(``claude "query"``,无 MCP)传 True —— design 维度
    协议的逐问澄清改为「在终端直接问人」(见 prompt_sections.build_design_dimensions_section)。
    """
    from ...infra.story_paths import story_evidence_dir
    from .prompt_sections import (
        build_design_dimensions_section,
        build_kb_tool_section,
        build_quality_section,
    )

    stage_desc = ""
    if stage in profile_stages:
        cfg = profile_stages[stage]
        stage_desc = cfg.description if hasattr(cfg, "description") else str(cfg)

    story_dir = story_evidence_dir(workspace or Path.cwd(), story_key, title)

    # PRD 注入：只注入文件路径，让 CLI 自行读取（内联内容会把上下文撑爆）。
    # PRD 在 story-lifecycle Intake 阶段落到 story evidence 目录，路径存在
    # context_json.prd_path。
    prd_section = ""
    if prd_path:
        prd_section = (
            f"\n### PRD / 需求详情\n请读取 PRD 文件了解完整需求: `{prd_path}`\n"
        )

    # Quality checklist injection for verify stage (uses existing quality_checklist slot
    # semantics without touching prompt_renderer vars_map).
    # section 内容走共享 helper（与 _render_prompt 同一份），verify 门控留在本调用点。
    quality_section = ""
    if stage == "verify":
        checklist = build_quality_section(story_key, stage)
        if checklist.strip():
            quality_section = f"\n{checklist}\n"

    # Knowledge context injection（冷启动 outcome/process 知识，按 task_type）。
    # 镜像 quality_section：经共享 helper 取、failsafe（任何异常不阻塞 prompt 渲染）。
    # Agentic RAG：不预注入死包，给 agent kb.py 工具引导（agent 自己决定查什么）。
    # task_type 让 agent 知道查哪个域；kb.py 做精确取数（graph/bugs/playbook）。
    knowledge_section = build_kb_tool_section(story_key, workspace, stage)

    # design 阶段:维度 checklist(brainstorming 发散 + checklist 收敛)+ 逐问澄清
    # (调 mcp__lifecycle__clarify)+ 高价值维度 playbook。遇关键岔路 claude 调外接 MCP clarify
    # 工具(见 orchestrator/mcp/),人答经它返回,claude 带答继续(context 保留)。
    # 详见 memory story-lifecycle-design-hitl。
    dimensions_section = ""
    if stage == "design":
        dimensions_section = build_design_dimensions_section(
            story_key, workspace, stage, interactive=interactive
        )

    # BUG #18: worktree 已建(build 阶段 prepare_worktrees 跑过)→ 确定性指令:
    # "直接在 worktree 路径下改代码,不要自己建 worktree 或切分支"。
    # worktree 未建(design 阶段 / prepare 失败 / 无绑定)→ 降级 advisory(原逻辑)。
    _has_worktree = "→ worktree" in project_section
    worktree_section = ""
    if project_section and _has_worktree:
        worktree_section = f"""
### 项目仓库与分支隔离（worktree 已就绪）

系统已为每个绑定仓库创建好 worktree 和 feature 分支，**请直接在对应 worktree 路径下改代码**：

{project_section}

**不要自己创建 worktree 或切换分支**——隔离环境已由编排层准备完毕。
直接 `cd` 到上述 worktree 路径，在对应分支上写代码即可。

**硬约束**：若发现 worktree 路径不存在或分支异常，**立即停止**，将错误写入完成协议的 `summary` 字段并把 `status` 设为 `"error"`，不要尝试在主分支或其他分支上继续。
"""
    elif project_section:
        worktree_section = f"""
### 项目仓库与分支隔离

已绑定以下项目仓库，系统为每个仓库规划了工作分支：

{project_section}

**由你判断本次改动需要的隔离级别**：
- 纯文档/分析类改动 → 可直接在当前工作区进行，无需隔离
- 涉及代码修改、跨服务、或高风险 → 建议建立隔离环境

建立隔离环境的两种方式（按项目仓库分别执行）：
- 方式 A（独立目录，推荐用于多项目并行）： `git -C <repo_path> worktree add <新路径> <分支>` 或基于基线 `git -C <repo_path> worktree add -b <分支> <新路径> <基线>`
- 方式 B（在主仓库切分支）： `git -C <repo_path> checkout -b <分支> <基线>`（已有则 `git -C <repo_path> checkout <分支>`）

**硬约束**：若 git 操作失败（分支已存在且冲突、无权限、仓库不可写等），**立即停止后续工作**，将错误写入完成协议的 `summary` 字段并把 `status` 设为 `"error"`，不要尝试在错误的分支或主分支上继续。
"""

    # 执行约束（根因 guard，real-run 2026-07-06）：代码阶段 agent（kimi）会自作主张跑
    # mvn/tsc 自检 -> 大 repo 上耗时>10min 阻塞 -> 永远到不了 done 握手 -> stage 失败。
    # 显式禁止耗时构建/编译/测试命令，让 agent 专注写代码 + done；编译/测试归后续阶段/CI。
    exec_constraint_section = (
        "\n### 执行约束（重要）\n"
        "本阶段**只写代码/文档 + 写完成协议（done）**。**不要运行**耗时的构建/编译/测试命令"
        "（`mvn`、`gradle`、`mvnw`、`npm install`、`yarn install`、`tsc`、`jest`、`vitest`、`pytest` 等）"
        "—— 它们在大型仓库上常阻塞超过 10 分钟，会让你永远写不到 done 握手；"
        "编译/类型/测试由后续阶段或 CI 负责，语法/类型问题靠阅读判断即可。\n"
    )

    return f"""## 任务: {stage}

### Story 信息
- Key: {story_key}
- 标题: {title}
- Story 证据目录: {story_dir}

### 阶段说明
{stage_desc}
{prd_section}
{transcript_section}
{knowledge_section}
{dimensions_section}
{quality_section}
### 关键要点
{focus}
{worktree_section}
{exec_constraint_section}
### 完成协议
完成后必须写入文件 `{done_file}`，内容为 JSON:
{{"stage": "{stage}", "status": "done", "summary": "完成摘要", "files_changed": []}}

注意：JSON 必须是纯 JSON，不要包裹在 markdown 代码块中。"""


@with_story_key()
def run_orchestrator_agent_async(story_key: str, *, on_action=None) -> dict:
    """同步版本的 Agent 规划（直接调用，不进线程池）。

    用于 SSE 端点：规划在 generator 中执行，SSE 流式推送每个 action。
    """
    return run_orchestrator_agent(story_key, on_action=on_action)
