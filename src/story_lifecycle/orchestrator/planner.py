"""Smart Orchestrator — plan and review via LLM.

Two modes:
1. **Legacy text-based planning** — plan_stage / review_stage / review_plan
2. **Agent mode (new)** — run_orchestrator_agent / continue_orchestrator_agent
   Uses Function Calling to generate structured tool calls instead of text JSON.

All LLM calls delegate to LLMClient.
"""

import json
import logging
import time
from pathlib import Path

from ..llm_client import get_llm
from ..schemas import PlanResult, ReviewResult, PlanReviewResult
from .agent_tools import ORCHESTRATOR_TOOLS

log = logging.getLogger("story-lifecycle.planner")

STORY_HOME = Path.home() / ".story-lifecycle"
MAX_REVIEW_RETRIES = 3


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


def build_plan_prompt(
    state: dict,
    stage_config: dict,
    adapters: list[str],
) -> str:
    """构建编排 prompt，不调用 LLM。"""
    prompt = f"""你是任务编排器。你的职责是决定如何执行当前阶段，不是做具体设计。

## Story 信息
- Key: {state.get("story_key")}
- 标题: {state.get("title")}
- 当前阶段: {state.get("current_stage")}
- 已重试次数: {state.get("execution_count", 0)}
- 阶段描述: {stage_config.get("description", "")}

## 可用 CLI 工具
{json.dumps(adapters)}

## 阶段配置
{json.dumps(stage_config, ensure_ascii=False, indent=2)}

请返回 JSON（不要输出其他内容）：
{{{{
  "adapter": "使用哪个 CLI 工具（如 claude/codex）",
  "provider": "使用哪个 provider（或 null）",
  "model": "使用哪个 model（或 null）",
  "skip": false,
  "summary": "一句话摘要，描述当前阶段要做什么",
  "focus": "2-3 个关键要点，告诉 CLI 应该关注什么。简洁，不要写详细设计",
  "reasoning": "决策理由（一句话）",
  "trajectory_score": 0.85,
  "done_file": ".story-done/{state.get("story_key")}-{state.get("current_stage")}.json",
  "done_schema": "CLI 完成后必须写入此 JSON 文件：{{\"stage\": \"{state.get("current_stage")}\", \"status\": \"done\", \"summary\": \"完成摘要\", \"files_changed\": []}}"
}}}}

注意：
- focus 要简洁（2-3 个要点），不要写详细的设计方案或任务书
- summary 和 focus 是给用户看的概要，不是给 CLI 的执行指令
- done_file 是 CLI 必须写入的完成信号文件路径
- CLI（如 claude/codex）会自己理解需求并设计方案，你不需要代劳
- 如果发现当前阶段不必要，可以 skip: true"""
    return prompt


def plan_stage(
    state: dict,
    stage_config: dict,
    adapters: list[str],
) -> dict:
    """编排角色：决定如何执行当前阶段。"""
    retry_hint = ""
    previous_review = state.get("review_summary", "")
    if previous_review and state.get("execution_count", 0) > 0:
        retry_hint = f"## 上次 Review 反馈\n{previous_review}"

    prompt = build_plan_prompt(state, stage_config, adapters)
    if retry_hint:
        prompt += f"\n\n{retry_hint}"

    llm = get_llm()
    t0 = time.monotonic()
    try:
        result = llm.invoke_structured(prompt, PlanResult, temperature=0.1, timeout=90)
        _trace_llm(
            model=llm.model,
            usage={},
            duration_ms=int((time.monotonic() - t0) * 1000),
            story_key=state.get("story_key", ""),
            stage=state.get("current_stage", ""),
        )
        return result.model_dump()
    except Exception as exc:
        _trace_llm(
            model=llm.model,
            usage={},
            duration_ms=int((time.monotonic() - t0) * 1000),
            success=False,
            error=type(exc).__name__,
            story_key=state.get("story_key", ""),
            stage=state.get("current_stage", ""),
        )
        raise


def review_stage(
    state: dict, stage_config: dict, stage_output: dict, *, reviewer_model: str = ""
) -> dict:
    """QA/评审员角色：结构化审查阶段产出质量。"""
    execution_count = state.get("execution_count", 0)
    workspace = state.get("workspace", "")
    story_key = state.get("story_key", "")

    story_knowledge = _load_story_knowledge(workspace, story_key)

    fatigue_hint = ""
    if execution_count >= MAX_REVIEW_RETRIES - 1:
        fatigue_hint = f"""
## ⚠️ 重试疲劳警告
该阶段已经重试了 {execution_count} 次，接近 {MAX_REVIEW_RETRIES} 次上限。
如果问题仍然无法解决，请务必返回 quality: "fail"，让人工介入。"""

    prev_score = state.get("trajectory_score")
    score_hint = ""
    if prev_score is not None and prev_score < 0.5:
        score_hint = f"""
## ⚠️ 路径评分偏低
前序阶段路径评分: {prev_score}/1.0。如果当前产出仍未改善，建议 quality: "fail" 以触发重新规划或切换工具。"""

    prompt = f"""你是一个开发团队的 QA/评审员。你是评审员，只读不改——你不修改任何代码或文件，只负责审查、记录问题和建议。

一个阶段刚刚完成，请进行质量审查。

## Story 信息
- Key: {state.get("story_key")}
- 阶段: {state.get("current_stage")}
- 已重试次数: {execution_count} / {MAX_REVIEW_RETRIES}
- 阶段描述: {stage_config.get("description", "")}

## 阶段产出
{json.dumps(stage_output, ensure_ascii=False, indent=2)}

## 预期产出字段
{json.dumps(stage_config.get("expected_outputs", []))}

## 已有上下文索引
{json.dumps(state.get("context", {}), ensure_ascii=False, indent=2)}

## Story 知识库
{story_knowledge}
{fatigue_hint}
{score_hint}

请审查产出质量。返回 JSON：
{{{{
  "quality": "pass|revise|fail",
  "summary": "一句话审查结论（存入 state context）",
  "feedback": "详细审查意见（写入文件）",
  "issues": [
    {{{{
      "type": "问题类型（如 missing_error_handling, missing_test, wrong_api 等）",
      "severity": "high|medium|low",
      "location": "文件:位置",
      "description": "问题描述"
    }}}}
  ],
  "suggestions": ["具体改进建议，可操作"],
  "trajectory_score": 0.8,
  "context_updates": {{{{}}}},
  "reasoning": "判断理由"
}}}}

判断标准：
- pass: 产出满足预期，可以 advance。仍可记录低优先级 issues 和 suggestions 供后续参考。
- revise: 产出存在明显缺陷（issues 中至少一个 severity=high），需要返工
- fail: 不可恢复的问题，或已达到重试上限
- trajectory_score: 路径评分 (0-1)，反映从 Story 开始到现在的整体质量趋势
  - 1.0: 完美，一切按预期进行
  - 0.5-0.8: 有小问题但方向正确
  - <0.5: 方向跑偏或质量问题严重，需要重新规划"""

    llm = get_llm()
    t0 = time.monotonic()
    try:
        result = llm.invoke_structured(
            prompt, ReviewResult, temperature=0.1, timeout=90
        )
        _trace_llm(
            model=llm.model,
            usage={},
            duration_ms=int((time.monotonic() - t0) * 1000),
            story_key=state.get("story_key", ""),
            stage=state.get("current_stage", ""),
        )
        return result.model_dump()
    except Exception as exc:
        _trace_llm(
            model=llm.model,
            usage={},
            duration_ms=int((time.monotonic() - t0) * 1000),
            success=False,
            error=type(exc).__name__,
            story_key=state.get("story_key", ""),
            stage=state.get("current_stage", ""),
        )
        raise


def review_plan(
    state: dict,
    plan: dict,
    stage_config: dict,
    reviewer_model: str = "",
) -> dict:
    """Plan Reviewer 角色：对执行计划进行对抗性审查。"""
    workspace = state.get("workspace", "")
    story_key = state.get("story_key", "")

    story_knowledge = _load_story_knowledge(workspace, story_key)

    prompt = f"""你是一个开发团队的技术评审员，专门负责审查执行计划的质量。你的职责是确保计划具备足够的范围覆盖、上下文完整性和可行性。

一份执行计划刚刚生成，请进行质量审查。

## Story 信息
- Key: {state.get("story_key")}
- 标题: {state.get("title")}
- 当前阶段: {state.get("current_stage")}
- 阶段描述: {stage_config.get("description", "")}

## 执行计划
{json.dumps(plan, ensure_ascii=False, indent=2)}

## 已有上下文索引
{json.dumps(state.get("context", {}), ensure_ascii=False, indent=2)}

## Story 知识库
{story_knowledge}

请审查计划质量。返回 JSON：
{{{{
  "quality": "pass|revise",
  "blockers": [
    {{{{
      "severity": "high|medium|low",
      "category": "scope|context|feasibility",
      "description": "问题描述"
    }}}}
  ],
  "suggestions": ["具体改进建议，可操作"],
  "reasoning": "判断理由"
}}}}

判断标准：
- pass: 计划范围合理、指令具体明确、与知识库对齐，可以执行
- revise: 计划存在严重问题（blockers 中至少一个 severity=high），需要重新生成
  - scope 问题：计划范围过大或过小，遗漏关键步骤
  - context 问题：计划缺少必要的前序上下文或团队规范
  - feasibility 问题：计划中包含不可行的技术方案或不存在的工具/接口

注意：
- 只关注严重问题（severity=high），中等和低等问题记入 suggestions 即可
- 不要因为风格偏好或非关键细节而触发 revise
- 优先检查：adapter 是否有效、extra_instructions 是否具体可操作、是否遗漏 stage_config 要求的步骤"""

    llm = get_llm()
    t0 = time.monotonic()
    try:
        result = llm.invoke_structured(
            prompt, PlanReviewResult, temperature=0.1, timeout=90
        )
        _trace_llm(
            model=llm.model,
            usage={},
            duration_ms=int((time.monotonic() - t0) * 1000),
            story_key=state.get("story_key", ""),
            stage=state.get("current_stage", ""),
        )
        return result.model_dump()
    except Exception as exc:
        _trace_llm(
            model=llm.model,
            usage={},
            duration_ms=int((time.monotonic() - t0) * 1000),
            success=False,
            error=type(exc).__name__,
            story_key=state.get("story_key", ""),
            stage=state.get("current_stage", ""),
        )
        raise


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


# ── tracing ──


def _trace_llm(
    *,
    model: str,
    usage: dict,
    duration_ms: int,
    operation: str = "plan_stage",
    story_key: str = "",
    stage: str = "",
    success: bool = True,
    error: str = "",
):
    try:
        from ..db.models import log_llm_trace

        log_llm_trace(
            story_key=story_key,
            stage=stage,
            operation=operation,
            model=model,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
            duration_ms=duration_ms,
            success=success,
            error=error,
        )
    except Exception:
        pass


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
    from ..db import models as db

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
        from .nodes.profile_loader import resolve_profile

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

        _trace_llm(
            model=llm.model,
            usage={},
            duration_ms=int((time.monotonic() - t0) * 1000),
            operation="agent_plan",
            story_key=story_key,
        )
    except Exception as exc:
        _trace_llm(
            model=llm.model,
            usage={},
            duration_ms=int((time.monotonic() - t0) * 1000),
            operation="agent_plan",
            story_key=story_key,
            success=False,
            error=str(exc),
        )
        raise

    # 写入 DB：暂停等用户确认
    ctx = {}
    try:
        ctx = json.loads(story.get("context_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        pass
    ctx["_agent_actions"] = actions
    ctx["_plan_confirmed"] = False
    db.update_story(
        story_key,
        context_json=json.dumps(ctx, ensure_ascii=False),
        status="planning",
    )

    return {"status": "planning", "actions": actions}


def continue_orchestrator_agent(story_key: str):
    """用户确认规划后，执行 action list。

    遍历 action list，逐个执行：
    - launch: 启动 CLI，轮询 done file
    - skip: 记录跳过

    执行在后台线程中运行。
    """
    from ..db import models as db
    from ..adapters import get_adapter
    from .nodes.profile_loader import resolve_profile
    from .nodes.json_helpers import robust_json_parse
    from ..terminal.pty import ensure_agent_pty

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

    # 解析 profile 用于生成 prompt
    profile_stages = {}
    try:
        rp = resolve_profile(profile_name)
        profile_stages = {name: cfg for name, cfg in rp.stages.items()}
    except Exception:
        pass

    # 逐个执行 action
    for idx, action in enumerate(actions):
        if action.get("action") == "skip":
            stage = action.get("stage", f"stage_{idx}")
            reason = action.get("reason", "")
            db.log_event(story_key, stage, "skipped", {"reason": reason})
            log.info(f"[{story_key}] Skipped stage {stage}: {reason}")
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

            while elapsed < poll_timeout:
                # 检查 done file
                if done_path.exists():
                    try:
                        raw = done_path.read_text(encoding="utf-8")
                        done_data = robust_json_parse(raw) or {}
                        db.log_event(story_key, stage, "completed", done_data)
                        log.info(
                            f"[{story_key}] Stage {stage} completed: "
                            f"{done_data.get('summary', '')[:100]}"
                        )
                        # 清理 done file
                        try:
                            done_path.unlink()
                        except OSError:
                            pass
                        break
                    except Exception as exc:
                        log.error(f"[{story_key}] Error parsing done file: {exc}")

                time.sleep(poll_interval)
                elapsed += poll_interval
            else:
                # 超时
                log.warning(
                    f"[{story_key}] Stage {stage} timed out after {poll_timeout}s"
                )
                db.update_story(
                    story_key,
                    status="failed",
                    last_error=f"Stage {stage} timed out",
                )
                return

    # 所有 action 执行完毕
    db.update_story(story_key, status="completed")
    log.info(f"[{story_key}] All stages completed")


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
) -> str:
    """构建给 CLI 的执行 prompt。"""
    from ..story_paths import story_evidence_dir

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
### 关键要点
{focus}
{worktree_section}
### 完成协议
完成后必须写入文件 `{done_file}`，内容为 JSON:
{{"stage": "{stage}", "status": "done", "summary": "完成摘要", "files_changed": []}}

注意：JSON 必须是纯 JSON，不要包裹在 markdown 代码块中。"""


def run_orchestrator_agent_async(story_key: str, *, on_action=None) -> dict:
    """同步版本的 Agent 规划（直接调用，不进线程池）。

    用于 SSE 端点：规划在 generator 中执行，SSE 流式推送每个 action。
    """
    return run_orchestrator_agent(story_key, on_action=on_action)
