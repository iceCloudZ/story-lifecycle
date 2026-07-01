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

from ...llm_client import get_llm, with_story_key
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
    knowledge_dir = Path(workspace) / ".story-knowledge" / story_key
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
    context_dir = Path(workspace) / ".story" / "context" / story_key
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

    compressed_file = Path(workspace) / ".story-knowledge" / story_key / "compressed.md"
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
- 每个阶段选择合适的 CLI 工具（claude 或 codex）
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
4. CLI（claude/codex）会自己理解需求并设计方案，你不需要代劳
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
    from ...db import models as db

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

    t0 = time.monotonic()
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
                    action = {
                        "action": "launch",
                        "adapter": args.get("adapter", "claude"),
                        "stage": args.get("stage", ""),
                        "focus": args.get("focus", ""),
                        "done_file": args.get(
                            "done_file",
                            f".story-done/{story_key}-{args.get('stage', '')}.json",
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
    from pathlib import Path as _P

    done_dir = _P(workspace) / ".story" / "done" / story_key
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


@with_story_key()
def continue_orchestrator_agent(story_key: str, headless: bool = False):
    """用户确认规划后，执行 action list。

    遍历 action list，逐个执行：
    - launch: 启动 CLI，轮询 done file
    - skip: 记录跳过

    执行在后台线程中运行。
    """
    from ...db import models as db
    from ...adapters import get_adapter
    from ..engine.profile_loader import resolve_profile
    from ...json_helpers import robust_json_parse
    from ...terminal.pty import ensure_agent_pty

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
    db.update_story(
        story_key,
        context_json=json.dumps(ctx, ensure_ascii=False),
        status="active",
    )

    # 解析 profile 用于生成 prompt 和质量门禁配置
    profile_stages = {}
    quality_cfg = {}
    try:
        rp = resolve_profile(profile_name)
        profile_stages = {name: cfg for name, cfg in rp.stages.items()}
        quality_cfg = rp.quality or {}
    except Exception:
        pass

    # 逐个执行 action；使用 while 以便在 verify gate 触发 retry 时插入重试 action
    idx = 0
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
            adapter_name = action.get("adapter", "claude")
            focus = action.get("focus", "")
            done_file_rel = action.get(
                "done_file",
                f".story-done/{story_key}-{stage}.json",
            )

            # 更新当前阶段
            db.update_story(story_key, current_stage=stage)

            # 查项目绑定，拼成分支隔离提示，让 CLI 自行判断是否建 worktree/切分支
            project_lines = []
            for sp in db.get_story_projects(story_key):
                proj = db.get_project(sp["project_id"])
                if not proj:
                    continue
                project_lines.append(
                    f"- 仓库 `{proj['repo_path']}`: 分支 `{sp['branch']}`, "
                    f"基线 `{sp.get('base_branch', 'main')}`"
                )
            project_section = "\n".join(project_lines)

            # 构建 CLI prompt
            from ...context_providers import get_transcript_context

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
            )

            # 写入 prompt 文件
            prompt_dir = Path(workspace) / ".story" / "context" / story_key
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
                    log.info("[%s] HEADLESS spawn stage=%s cmd=%s", story_key, stage, launch_cmd)
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
                        )
                        headless_proc.stdin.write(cli_prompt.encode("utf-8"))
                        headless_proc.stdin.close()
                    except Exception as exc:
                        db.update_story(
                            story_key, status="failed",
                            last_error=f"Stage {stage} headless spawn failed: {exc}",
                        )
                        return
                    log.info(
                        "[%s] HEADLESS pid=%s stage=%s (polling done file, not exit)",
                        story_key, headless_proc.pid, stage,
                    )
                else:
                    ensure_agent_pty(
                        story_key,
                        launch_cmd,
                        workspace,
                        cli_prompt,  # prompt 作为第 4 个参数注入到 PTY
                    )
                    log.info("[%s] PTY session started for stage=%s", story_key, stage)
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
            poll_timeout = 30 * 60  # 30 minutes
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
                    stderr_tail, stdout_tail = b"", b""
                    try:
                        if headless_proc.stderr:
                            stderr_tail = headless_proc.stderr.read()[-500:]
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
                            story_key, rc, headless_attempt, HEADLESS_MAX_ATTEMPTS,
                            stderr_tail, stdout_tail,
                        )
                        headless_attempt += 1
                        try:
                            headless_proc = _sp.Popen(
                                launch_cmd, cwd=workspace,
                                stdin=_sp.PIPE, stdout=_sp.PIPE, stderr=_sp.PIPE,
                            )
                            headless_proc.stdin.write(cli_prompt.encode("utf-8"))
                            headless_proc.stdin.close()
                        except Exception as exc:
                            db.update_story(
                                story_key, status="failed",
                                last_error=f"Stage {stage}: headless retry spawn failed: {exc}",
                            )
                            return
                        log.info(
                            "[%s] HEADLESS retry pid=%s stage=%s (attempt %d)",
                            story_key, headless_proc.pid, stage, headless_attempt,
                        )
                        continue
                    log.warning(
                        "[%s] claude exited rc=%d without done file after %d attempts; "
                        "giving up. stdout_tail=%r",
                        story_key, rc, HEADLESS_MAX_ATTEMPTS, stdout_tail,
                    )
                    db.update_story(
                        story_key, status="failed",
                        last_error=(
                            f"Stage {stage}: claude exited (rc={rc}) without done file "
                            f"after {HEADLESS_MAX_ATTEMPTS} attempts"
                        ),
                    )
                    return
                # 检查 done file
                if done_path.exists():
                    try:
                        # robust_json_parse 接收 Path（内部自读，并容忍 markdown 包裹/
                        # 半写文件：解析失败会抛异常，由下方 except 捕获后轮询重试，
                        # 等 claude 把 done file 写完整再消费）。
                        done_data = robust_json_parse(done_path) or {}
                        db.log_event(story_key, stage, "completed", done_data)
                        log.info(
                            f"[{story_key}] Stage {stage} completed: "
                            f"{done_data.get('summary', '')[:100]}"
                        )
                        # 保留 done file 作为阶段完成证据：real-E2E asserters 与
                        # 审计都需要事后读取 {stage}.json。每个 stage 的 done 路径唯一，
                        # 重跑由 reset_workspace 清理 done/ 目录，无需在此 unlink。
                        # headless：done file 已出现 → 回收 claude 进程（它往往仍在运行）
                        if headless_proc is not None:
                            _kill_headless(headless_proc)
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
                    stage_cfg.max_retries
                    if hasattr(stage_cfg, "max_retries")
                    else 2
                )
                ctx["last_verify_summary"] = done_data.get("summary", "")
                gate_result = run_verify_gate(
                    story_key=story_key,
                    stage=stage,
                    workspace=workspace,
                    context=ctx,
                    quality_cfg=quality_cfg,
                    max_retries=max_retries,
                )
                if gate_result["decision"] == "retry":
                    retry_done_file = (
                        f".story/done/{story_key}/verify"
                        f"-round{gate_result['round']}.json"
                    )
                    actions.insert(
                        idx + 1,
                        {
                            "action": "launch",
                            "stage": "verify",
                            "adapter": adapter_name,
                            "focus": (
                                f"repair round {gate_result['round']}/"
                                f"{gate_result['retry_limit']} — address HIGH findings"
                            ),
                            "done_file": retry_done_file,
                        },
                    )
                    ctx["_agent_actions"] = actions
                    db.update_story(
                        story_key,
                        context_json=json.dumps(ctx, ensure_ascii=False),
                    )
                    log.info(
                        "[%s] Verify gate blocked (round %d/%d); retry scheduled",
                        story_key,
                        gate_result["round"],
                        gate_result["retry_limit"],
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
) -> str:
    """构建给 CLI 的执行 prompt。"""
    from ...story_paths import story_evidence_dir
    from .prompt_sections import build_kb_tool_section, build_knowledge_section, build_quality_section

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

    # 项目仓库与分支隔离：注入每个绑定仓库的分支/基线/路径，由 CLI 自行判断
    # 是否需要 worktree 或切分支。后端的 prepare_worktrees 仍是可选的手动 API，
    # 这里走“让 CLI 判断”的路线。
    worktree_section = ""
    if project_section:
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
{quality_section}
### 关键要点
{focus}
{worktree_section}
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
