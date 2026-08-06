"""Smart Orchestrator — plan and review via LLM.

Agent mode (Function Calling): run_orchestrator_agent plans a structured
action list via plan_step/skip_stage tool calls; continue_orchestrator_agent
executes them with verify-gate. The legacy text-JSON planning path
(plan_stage / build_plan_prompt / /plan/generate) has been removed.
All LLM calls delegate to LLMClient.
"""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from ...infra.llm_client import get_llm, with_story_key
from ...infra.story_paths import safe_story_path
from ...infra.paths import stage_done_file_rel, story_home
from ...sourcing.state_machine import (
    activate as sm_activate,
    mark_failed as sm_mark_failed,
)

log = logging.getLogger("story-lifecycle.planner")

STORY_HOME = story_home()


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


def _sanitize_workspace_slug(slug: str) -> str:
    """Normalize an LLM-produced workspace slug to a safe directory name.

    Forces kebab-case-ish lowercase ASCII + digits + ``-_.``. Strips path
    separators (no traversal: the slug becomes ONE path segment under
    worktrees_root, never a nested path). Empty → "".
    """
    s = (slug or "").strip()
    if not s:
        return ""
    # Drop anything that isn't a-z 0-9 - _ . (one path segment, no slashes)
    s = re.sub(r"[^a-z0-9\-_.]+", "-", s.lower())
    s = re.sub(r"-{2,}", "-", s).strip("-_.")
    # Cap length so we don't blow Windows MAX_PATH in deep nested scenarios.
    return s[:60]


def _prepare_story_workspace(story_key: str, slug: str) -> str:
    """mkdir the per-story workspace at <worktrees_root>/<slug>/.

    Called after the planning LLM returns ``workspace_slug``. Idempotent:
    existing dir is fine. Returns the absolute path as str, or "" if slug
    is empty/invalid (story has no isolated workspace → spawn uses main
    workspace). Failures (permission, disk) are logged + return "" so
    planning never blocks on workspace prep.
    """
    safe = _sanitize_workspace_slug(slug)
    if not safe:
        return ""
    try:
        from ...infra.config import get_worktrees_root

        root = get_worktrees_root()
        ws_path = root / safe
        ws_path.mkdir(parents=True, exist_ok=True)
        return str(ws_path)
    except Exception as exc:  # noqa: BLE001 — planning must not block on fs
        log.warning("[%s] workspace prep failed for slug=%r: %s", story_key, slug, exc)
        return ""


def _build_agent_system_prompt(
    *,
    profile_stages: dict | None = None,
    story_title: str = "",
    story_key: str = "",
    workspace: str = "",
) -> str:
    """构建 Agent 的 system prompt。

    REFACTOR §5.2.1:接通死代码 _load_team_knowledge / _load_story_knowledge,
    让编排器-模型用上跨阶段 + 历史经验的特权视角(信息差护城河)。
    """
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

    # REFACTOR §5.2.1:接通死代码——团队级 + story 级知识
    team_kb = _load_team_knowledge()
    story_kb = (
        _load_story_knowledge(workspace, story_key)
        if workspace
        else "（无 Story 知识）"
    )

    # task_actions:动作目录(帮 LLM 选每个 stage 该干什么)
    from .task_actions import get_action_catalog_for_prompt

    action_catalog = get_action_catalog_for_prompt()
    # 候选测试场景(设计 10 改动 2):knowledge 的 scenario 条目渲染给规划 LLM 选,
    # LLM 输出 selected_scenarios 到每个 stage,verify 阶段经 R8 接线传给外部
    # verify provider。容错:无 scenario 知识 → 空串,不注入,规划零影响。
    from .prompt_sections import build_scenario_catalog_section

    scenario_catalog = build_scenario_catalog_section(story_key, workspace, "planning")

    return f"""你是开发任务编排 Agent。根据需求信息，规划开发流程。

## 你的职责
- 根据需求决定需要执行哪些阶段(skip 不需要的)
- 给每个阶段指定 2-3 个关键要点（focus）
- 为每个阶段选 task_actions（该干什么活）
- 给本 story 起一个独立的 sandbox_slug(隔离沙箱目录名)
- 从候选测试场景里选本 story 需要验证的（selected_scenarios）
- 规划完成后暂停，等待用户确认

## 当前 Story
- Key: {story_key}
- 标题: {story_title}

## 团队记忆（跨 story 经验，参考但不盲从）
{team_kb}

## 本 Story 已有知识
{story_kb}

## 可用阶段
{stages_hint}

{action_catalog}

{scenario_catalog}

## 规则
1. 对每个阶段,决定 skip(true)还是执行(false)
2. 对执行的阶段,给 2-3 个 focus 要点(不要写详细设计)
3. 为每个执行的阶段选 task_actions(从上面的动作库选,不能自己编)
4. 标 grill=true 表示该阶段需要人澄清关键歧义(如复杂设计决策);简单明确的标 false
5. CLI（claude/codex/kimi）会自己理解需求并设计方案，你不需要代劳
6. adapter 由 profile 路由,你不需要选
7. **selected_scenarios**：从「候选测试场景」里选本 story 需要验证的场景，
   填 scenario id（如 `scenario:borrow-flow`）；没有候选或无需外部验证时填空数组 []
8. **workspace_slug**：为这个 story 起一个独立的隔离沙箱目录名(Sandbox)。
   - 从标题提炼：小写英文 + 数字 + 连字符(kebab-case)，10-40 字符
   - 例：「MGM活动限制用户当前的app版本」→ `mgm-app-version-limit`
   - 例：「优化订单导出查询性能」→ `order-export-perf`
   - 后端会建空目录 <worktrees_root>/<slug>/ 作为 code agent 的 cwd(Sandbox)
   - agent 自己把要改的项目 `git worktree add` 进去,在这里干活
   - 纯调研/不改代码的 story 才留空字符串 ""

## 输出格式（关键）
必须**只**输出一个 JSON 对象，不要任何 markdown、表格、解释文字、代码块标记。
schema:
{{"stages":[{{"stage":"<阶段名>","skip":<true|false>,"focus":"<要点，多条用分号>","task_actions":["<动作1>","<动作2>"],"grill":<true|false>,"selected_scenarios":["scenario:xxx"]}}],"workspace_slug":"<kebab-case 目录名>"}}
- 每个 profile 里的阶段都要出现在 stages 里（skip 的也要列出，skip=true）
- focus/task_actions 用中文
- workspace_slug 用 kebab-case 英文(标题简写)
- 直接输出 JSON，第一个字符必须是 {{，不要有 ```json 或任何前缀"""


def _read_prd_snippet(prd_path: str, limit: int = 3000) -> str:
    """读取 PRD 文件前 ``limit`` 字给规划 LLM 看。

    best-effort:任何失败(路径空/文件不存在/编码错误)都返空串,不抛——
    规划 LLM 没 PRD 摘录也能基于 title+seed_context 工作。

    这是为了让 PRD 正文进入规划 LLM 的 user_msg(BUG FIX 2026-07-27:
    原来读不存在的 story.content 列,永远空串)。
    """
    if not prd_path:
        return ""
    try:
        from pathlib import Path

        text = Path(prd_path).read_text(encoding="utf-8", errors="ignore")
        return text[:limit]
    except Exception:  # noqa: BLE001 — best-effort,绝不阻塞规划
        return ""


def _build_agent_user_message(
    *,
    story_key: str,
    title: str,
    content: str,
    workspace: str = "",
    profile_stages: dict | None = None,
    seed_context: str = "",
) -> str:
    """构建 Agent 的初始 user message。

    ``content``:PRD 正文摘录(前 3000 字),让规划 LLM 看到真实需求内容而非只看标题。
    ``seed_context``:接手中途需求的"已有工作说明"——做到哪了、还差什么。
    两者都来自 context_json(见 run_orchestrator_agent 的读取逻辑)。
    """
    parts = [
        "## 需求信息",
        f"标题: {title}",
    ]
    if content:
        parts.append(f"内容:\n{content[:3000]}")
    if seed_context:
        parts.append(
            f"接手说明(已有工作 / 做到哪了 / 还差什么):\n{seed_context[:3000]}"
        )
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
    """Supervisor Agent 规划:单次 LLM 决定需要哪些阶段 + 各阶段 focus。

    REFACTOR §5.4.1:从 10 轮 FC 循环(plan_step/skip_stage)改为单次 invoke_structured。
    阶段序列由 profile 定义(接力拓扑不动);模型只决定 skip 哪些 + 每阶段 focus。

    **边界(§5.4.2,护城河不动)**:
    - adapter 由 profile 的 stage→cli 决定(continue_orchestrator_agent:793 兜底覆盖)
    - 阶段序列由 profile 定义(design→build→verify)
    - 人确认闸(api_confirm_plan)保留
    - 模型只决定"skip 哪些阶段 + 每阶段 focus 要点"

    Args:
        story_key: Story 唯一标识
        on_action: 回调函数(SSE 推送,保留兼容)

    Returns:
        {"status": "planning", "actions": [...]}
    """
    from ...infra.db import models as db
    from pydantic import BaseModel

    class StagePlan(BaseModel):
        """单阶段规划:skip 哪些阶段 + 每阶段 focus + task_actions + grill + selected_scenarios。adapter 不让模型选。"""

        stage: str
        skip: bool = False
        focus: str = ""
        task_actions: list[str] = []
        grill: bool = False
        # 设计 10 改动 2.3:规划 LLM 从候选测试场景里选的 scenario id 列表
        # (如 ["scenario:borrow-flow"])。随 action 持久化进 ctx["_agent_actions"],
        # verify 阶段经 R8 接线传给外部 verify provider。
        selected_scenarios: list[str] = []

    class PlanResult(BaseModel):
        """规划结果:阶段列表 + 沙箱 slug。

        workspace_slug(标题简写,kebab-case):规划 LLM 决定的 per-story 隔离
        沙箱(Sandbox)目录名,后端会在 <worktrees_root>/<slug>/ 建空目录,作为
        code agent 的 cwd。agent 自己把要改的项目 git worktree add 进来。空字符串
        = 不需要独立沙箱(如纯调研 story),code agent 用主 workspace。
        """

        stages: list[StagePlan]
        workspace_slug: str = ""

    story = db.get_story(story_key)
    if not story:
        raise ValueError(f"Story not found: {story_key}")

    title = story.get("title", "")
    workspace = story.get("workspace", "")
    profile_name = story.get("profile", "minimal")

    # 读 context_json 取 seed_context(接手说明)和 prd_path(→ PRD 正文摘录)。
    # BUG FIX(2026-07-27):原代码 `content = story.get("content","")` 读的是不存在的
    # DB 列(story 表无 content 列,VALID_COLUMNS 不含它)→ 永远空串 → 规划 LLM 从来看
    # 不到任何 intake material,只凭 title 一个字面量做规划。改成从 context_json 读真实值。
    ctx = {}
    try:
        ctx_raw = story.get("context_json", "{}") or "{}"
        ctx = json.loads(ctx_raw) if isinstance(ctx_raw, str) else (ctx_raw or {})
    except (ValueError, TypeError):
        ctx = {}
    seed_context = ctx.get("seed_context", "") or ""
    content = _read_prd_snippet(ctx.get("prd_path", ""))

    # 解析 profile 获取阶段列表(adapter 路由来源,模型不参与)
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

    system_prompt = _build_agent_system_prompt(
        profile_stages=profile_stages,
        story_title=title,
        story_key=story_key,
        workspace=workspace,
    )
    user_msg = _build_agent_user_message(
        story_key=story_key,
        title=title,
        content=content,
        workspace=workspace,
        profile_stages=profile_stages,
        seed_context=seed_context,
    )

    # 单次 LLM 调用(替 10 轮 FC 循环)
    llm = get_llm()
    actions: list[dict] = []
    workspace_path = ""  # LLM 路径会填,fallback 路径留空(用主 workspace)

    if llm.api_key and profile_stages:
        prompt = f"{system_prompt}\n\n{user_msg}"
        try:
            result = llm.invoke_structured(
                prompt, PlanResult, temperature=0.1, timeout=90
            )
            # 规划 LLM 决定的 per-story 沙箱(Sandbox) slug → mkdir + 存 ctx。
            # LLM 只决定 slug(标题简写),建目录是后端的事(无副作用、可重放)。
            # 失败/无 slug → workspace_path 留空,后续 spawn 退回主 workspace。
            workspace_slug = (getattr(result, "workspace_slug", "") or "").strip()
            workspace_path = _prepare_story_workspace(story_key, workspace_slug)
            if workspace_path:
                log.info(
                    "[%s] workspace prepared: %s (slug=%r)",
                    story_key,
                    workspace_path,
                    workspace_slug,
                )
            # 把 PlanResult 转 action list(adapter 由 profile 决定,不用模型选的)
            stage_to_cli = {name: cfg["cli"] for name, cfg in profile_stages.items()}
            # single-pass 保底:LLM 路径下也要保证全干语义(见 _default_planning_actions
            # 对 single-pass 的处理)。LLM 漏选 run_tests / 没给 grill=True 时补上,
            # 否则 single-pass verify 会拿到禁测试约束 + 无任务清单 + 无 grill 段。
            is_single = len(profile_stages) <= 1
            # Defensive: invoke_structured's fallback coerce may leave fields
            # unset (e.g. LLM returned 'name' instead of 'stage'), so access
            # via getattr with sane defaults rather than letting AttributeError
            # sink the whole structured plan to _default_planning_actions.
            profile_stage_names = list(profile_stages.keys())
            for idx, sp in enumerate(result.stages):
                stage_name = getattr(sp, "stage", None) or (
                    profile_stage_names[idx]
                    if idx < len(profile_stage_names)
                    else f"stage{idx}"
                )
                skip = bool(getattr(sp, "skip", False))
                focus = getattr(sp, "focus", "") or ""
                if skip:
                    actions.append(
                        {
                            "action": "skip",
                            "stage": stage_name,
                            "reason": focus or "skipped",
                        }
                    )
                else:
                    adapter = stage_to_cli.get(stage_name, "claude")
                    actions.append(
                        {
                            "action": "launch",
                            "adapter": adapter,
                            "stage": stage_name,
                            "focus": focus,
                            "task_actions": _ensure_single_pass_actions(
                                getattr(sp, "task_actions", None), is_single
                            ),
                            "grill": _resolve_single_pass_grill(
                                getattr(sp, "grill", None), is_single
                            ),
                            # 设计 10 改动 2.3:selected_scenarios 随 action 持久化,
                            # verify 阶段 gate 的 R8 接线把它合进 done_data 给外部
                            # verify provider 读。
                            "selected_scenarios": list(
                                getattr(sp, "selected_scenarios", None) or []
                            ),
                            "done_file": stage_done_file_rel(story_key, stage_name),
                        }
                    )
                    if on_action:
                        on_action({"type": "action", "action": actions[-1]})
        except Exception as exc:
            log.warning(
                "[%s] structured plan failed, using default actions: %s", story_key, exc
            )
            actions = _default_planning_actions(story_key, profile_stages)
    else:
        # 无 api_key 或无 profile → fallback:全跑 profile 默认阶段
        actions = _default_planning_actions(story_key, profile_stages)

    # 写入 DB:暂停等用户确认
    ctx = {}
    try:
        ctx = json.loads(story.get("context_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        pass
    ctx["_agent_actions"] = actions
    ctx["_plan_confirmed"] = False
    # 落规划期建好的沙箱(Sandbox)路径(无则保留原值/清空),供后续 spawn + prompt 用。
    if workspace_path:
        ctx["workspace_path"] = workspace_path
    ctx["plan_summary"] = "; ".join(
        f"{a.get('stage', '')}: {a.get('focus', '')}"
        for a in actions
        if a.get("action") == "launch"
    )
    # planning 移出 status:规划期间 lifecycle_state 在「待启动」(未确认规划),
    # 引擎在跑规划 LLM 算 active。返回值的 "status":"planning" 是给 SSE 调用方
    # 的信号(表示规划完成),不是 DB status — 保留。
    sm_activate(story_key, ctx_updates=ctx)

    return {"status": "planning", "actions": actions}


def _ensure_single_pass_actions(
    task_actions: list[str] | None, is_single: bool
) -> list[str]:
    """LLM 规划路径的 single-pass 保底:动作清单缺关键动作时补上。

    fallback 路径(_default_planning_actions)对 single-pass 直接用
    _DEFAULT_SINGLE_STAGE_ACTIONS(含 run_tests),但 LLM 路径完全放权给模型——
    模型可能漏选 run_tests,导致 _build_exec_constraint 产出"禁测试"约束、
    _build_task_list 不出现任务清单段。single-pass 是单 CLI 全干,没有后续阶段
    替它兜底测试,所以必须保底 run_tests。

    多阶段(is_single=False)直通,不动 LLM 的选择。
    """
    actions = list(task_actions or [])
    if not is_single:
        return actions
    if "run_tests" not in actions:
        actions.append("run_tests")
    return actions


def _resolve_single_pass_grill(grill: bool | None, is_single: bool) -> bool:
    """LLM 规划路径的 single-pass 保底:grill 缺省(None)时保底 True。

    single-pass verify 是单 CLI 全干(含设计),PRD 岔路(信息缺失/多选)需澄清协议
    兜底——fallback 路径对 single-pass 默认 grill=True(:367),LLM 路径也应对齐。
    LLM 显式给 False 时尊重(它判断无岔路);给 None(没想清楚)时保底 True。

    多阶段(is_single=False)直通 LLM 的选择(None 视为 False)。
    """
    if not is_single:
        return bool(grill)
    return True if grill is None else bool(grill)


def _default_planning_actions(
    story_key: str, profile_stages: dict | None
) -> list[dict]:
    """Fallback:LLM 不可用时,全跑 profile 默认阶段(adapter 由 profile cli 决定)。"""
    from .task_actions import get_default_task_actions

    if not profile_stages:
        return []
    is_single = len(profile_stages) <= 1
    # 默认 grill:design/单阶段 → True(设计决策需拉扯);build/verify → False
    _DEFAULT_GRILL = {"design": True, "build": False, "verify": False}
    actions = []
    for name, cfg in profile_stages.items():
        cli = cfg["cli"] if isinstance(cfg, dict) else getattr(cfg, "cli", "claude")
        actions.append(
            {
                "action": "launch",
                "adapter": cli,
                "stage": name,
                "focus": cfg.get("description", "") if isinstance(cfg, dict) else "",
                "task_actions": get_default_task_actions(name, is_single),
                "grill": True if is_single else _DEFAULT_GRILL.get(name, False),
                "done_file": stage_done_file_rel(story_key, name),
            }
        )
    return actions


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


def _persist_playbook_for_story(workspace: str, story_key: str, db) -> None:
    """story 完成时飞轮回写(REFACTOR §5.1.3)。

    查全局决策事件 → reflect → 按 task_type/dimension 分文件落盘。
    与 ``_write_retrospect`` 并列,best-effort,只在 completed 路径触发。
    task_type 为空时跳过(冷启动期可能未分类)。
    """
    try:
        # 从 context_json 取 task_type
        story = db.get_story(story_key) or {}
        ctx = json.loads(story.get("context_json") or "{}")
        task_type = ctx.get("task_type")
        if not task_type:
            return

        # 复用 _build_verify_history_facts 的事件查询逻辑
        raw = db.get_recent_events_by_type(
            ["recovery_action", "judge_verdict", "transition_decision"], limit=100
        )
        events = []
        for r in raw:
            try:
                payload = json.loads(r.get("payload") or "{}")
            except Exception:
                payload = {}
            events.append(
                {
                    "story_key": r.get("story_key", ""),
                    "event_type": r.get("event_type", ""),
                    "payload": payload,
                }
            )

        from ..learning.reflection import persist_playbook

        persist_playbook(
            workspace=workspace, story_key=story_key, events=events, task_type=task_type
        )
    except Exception as exc:
        log.warning("[%s] _persist_playbook_for_story failed: %s", story_key, exc)


# stage → story_document.kind 映射(对齐 auto_discovery.py 的约定:
# design→spec, build→plan, verify→test_report)。不在映射里的 stage 跳过。
_STAGE_DOC_KIND = {
    "design": "spec",
    "build": "plan",
    "verify": "test_report",
}


def _repair_spec_to_action(
    *, repair_spec: dict, story_key: str, adapter_name: str, round_n: int, reason: str
) -> dict | None:
    """REFACTOR §5.3.4:把 unified_gate 的 repair_action spec 转 planner 可 insert 的 action dict。

    字段映射:kind(action→kind) / reason / new_adapter / rescue_stage。
    """
    from ...infra.story_paths import safe_segment

    kind = repair_spec.get("kind", "retry")
    seg = safe_segment(story_key)

    if kind in ("escalate", "proceed", "skip", None):
        return None  # caller 标失败

    if kind == "insert_rescue_stage":
        rescue = repair_spec.get("rescue_stage", "setup_dependency")
        return {
            "action": "launch",
            "stage": rescue,
            "adapter": adapter_name,
            "focus": f"rescue stage — {repair_spec.get('reason', reason)}",
            "done_file": f".story/done/{seg}/{rescue}.json",
        }

    # retry 或 swap_approach → verify 修复 action
    if kind == "swap_approach":
        # 模型指定 new_adapter(基于 playbook),fallback 到硬编码轮转
        repair_adapter = repair_spec.get("new_adapter") or _next_adapter_fallback(
            adapter_name
        )
    else:
        repair_adapter = adapter_name
    return {
        "action": "launch",
        "stage": "verify",
        "adapter": repair_adapter,
        "focus": f"repair round {round_n} — {repair_spec.get('reason', reason)}",
        "done_file": f".story/done/{seg}/verify-round{round_n}.json",
    }


def _next_adapter_fallback(current: str) -> str:
    """模型未指定 new_adapter 时的兜底轮转(与原 _SWAP_ADAPTER_ORDER 一致)。"""
    order = ("codex", "claude", "kimi")
    if current not in order:
        return order[0]
    return order[(order.index(current) + 1) % len(order)]


def gate_spec_reason(repair_spec: dict) -> str:
    """从 repair_spec 取 reason(给标失败用)。"""
    return (repair_spec or {}).get("reason", "verify gate escalate")


def _register_stage_outputs(story_key: str, stage: str, done_data: dict) -> None:
    """把 stage done 产出的文件登记进 story_document(BUG #17)。

    纯确定性:读 done_data["files_changed"],按文件名反查 doc_type(查不到
    才回退 stage 默认 kind),调 db.create_document(幂等)。让前端「文档」
    卡片可追溯 design/plan/test_report。

    - 过滤 .story/done/*.json(done 握手文件本身不算文档)。
    - 文件名能反查出已知 doc_type 的(delivery.md→delivery 等)按真实类型
      登记 — 同阶段多 doc_type 时,若一律按 stage kind 登记,后落地的文件会
      覆盖前者的内容(真实事件 2026-07-27:verify 的 delivery.md 被登记成
      test_report,local-amountraise-rerun 的测试报告内容被交付文档覆盖)。
    - files_changed 为空时,也读 done_data 的显式路径字段(spec_path 等)兜底。
    - stage 不在 _STAGE_DOC_KIND 里则跳过(防御)。
    """
    kind = _STAGE_DOC_KIND.get(stage)
    if not kind:
        return

    from ...infra.db import models as db  # 延迟 import(避免循环)
    from ...infra.story_paths import doc_type_for_filename

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
        ref_kind = doc_type_for_filename(Path(ref).name) or kind
        # Unified dual-write: legacy story_document (ref) + new story_doc
        # (full content versioned). Both are best-effort; going through the
        # shared helper keeps the two tables in sync (see doc_sync).
        _story_row = db.get_story(story_key) or {}
        _ws = _story_row.get("workspace") or ""
        try:
            from ...infra.doc_sync import register_doc_dual_write

            register_doc_dual_write(
                story_key,
                ref_kind,
                ref,
                change_reason=f"AI {stage} 阶段产出",
                author="ai",
                workspace=_ws,
                source="ai",
                verification_state="unverified",
            )
        except Exception:  # noqa: BLE001 — 单个文件登记失败不影响其他
            log.debug(
                "[%s] doc dual-write failed for stage=%s ref=%s",
                story_key,
                stage,
                ref,
            )


def _auto_commit_worktrees(story_key: str, summary: str) -> None:
    """build 完成后自动 commit 所有 story_project 的 worktree 改动。

    agent 只写代码不提交(untracked + modified),编排器负责 git add -A &&
    commit,让分支有 commit 可推送、代码变更 tab diff 完整。

    对每个 story_project 的 worktree_path 跑:
      git add -A && git commit -m "<message>"
    无改动(worktree 干净)时跳过。失败不抛(调用方 try/except 兜底)。
    """
    import subprocess as _sp
    from ...infra.db import models as db

    sps = db.get_story_projects(story_key)
    if not sps:
        return
    # commit message 规范:对齐项目约定
    short_summary = (summary or "build")[:80].replace('"', "'")
    message = f"feat({story_key}): {short_summary}"
    for sp in sps:
        wt = sp.get("worktree_path") or ""
        if not wt:
            continue
        try:
            # 检查有无改动
            status = _sp.run(
                ["git", "-C", wt, "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if not status.stdout.strip():
                continue  # 无改动跳过
            # add + commit
            _sp.run(
                ["git", "-C", wt, "add", "-A"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            result = _sp.run(
                ["git", "-C", wt, "commit", "-m", message],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                log.info(
                    "[%s] auto-commit worktree %s: %s",
                    story_key,
                    wt,
                    message[:60],
                )
            else:
                log.warning(
                    "[%s] auto-commit failed for %s: %s",
                    story_key,
                    wt,
                    result.stderr[:200],
                )
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] auto-commit exception for %s: %s", story_key, wt, e)


def _now_utc_iso() -> str:
    """UTC ISO 时间戳(秒精度),作文件扫描捕获 sid 的时间窗口下界。

    对齐 adapter.write_anchor 与 transcript 里的 UTC ts;文件扫描用它过滤
    ``time.created >= since`` 的会话,避免抓到本次 spawn 之前的旧会话。
    """
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@with_story_key()
def continue_orchestrator_agent(story_key: str, headless: bool = False):
    """执行 action list（设计13：driver 已删，这里是对外同步入口）。

    设计13 前:1813 行 driver 线程(poll 循环 + judge + gates + spawn 全量逻辑)。
    设计13 后:全局编排线程(OrchestratorThread)在 serve 里管所有 story;本函数
    保留为**同步驱动入口**(CLI / swebench / 旧测试),内部驱动同一套
    executors/handlers/judge 机制 —— 不是第二套调度逻辑。

    契约(AGENTS.md「规划在前，执行在后」):
    - story 不存在 → ValueError
    - 无 _agent_actions → sm_mark_failed("No actions to execute")
    - 有 actions → 置 _plan_confirmed + sm_activate → drive_story_sync 跑到
      paused/completed/failed。
    """
    from ...infra.db import models as db
    from ..scheduler import drive_story_sync

    story = db.get_story(story_key)
    if not story:
        raise ValueError(f"Story not found: {story_key}")

    ctx = {}
    try:
        ctx = json.loads(story.get("context_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        pass

    actions = ctx.get("_agent_actions", [])
    if not actions:
        log.warning(f"No actions found for {story_key}")
        sm_mark_failed(story_key, "No actions to execute")
        return

    # 进入执行即清除确认闸标记(对齐 driver 入口语义)。
    ctx["_plan_confirmed"] = True
    ctx.pop("_stage_gate", None)
    ctx.pop("_story_state_gate", None)
    sm_activate(story_key, ctx_updates=ctx)

    drive_story_sync(story_key)


def _resolve_prd_for_exec(
    story_key: str, workspace: str, title: str, legacy_path: str = ""
) -> str:
    """Resolve the PRD path for execution through the versioned-doc cache layer.

    If the doc exists in story_doc (versioned), verify/rebuild the local .md
    cache; else fall back to the legacy ctx['prd_path']. Any error falls back
    to legacy_path — execution must never break on doc-cache issues.
    """
    try:
        from ...infra.doc_sync import get_doc_for_execution

        return get_doc_for_execution(
            story_key, "prd", workspace, title, legacy_path=legacy_path
        )
    except Exception:
        return legacy_path


def resolve_stage_adapter(
    story: dict, stage: str, profile=None, action: dict | None = None
) -> str:
    """Canonical resolver: which adapter (claude/codex/kimi) runs this stage?

    Precedence (user intent wins over profile defaults):
      1. ``action["adapter"]`` if non-empty — the LLM-planned or user-override
         value (user changes it via the plan UI PATCH endpoint, which writes
         straight into ``context_json._agent_actions[stage].adapter``). This
         is the authoritative source once a plan exists.
      2. ``profile.stages[stage].cli`` — the profile's static default. Only
         used as a fallback when the action has no adapter set (e.g. legacy
         stories predating _agent_actions, or a freshly regenerated plan
         before the LLM has filled it in).
      3. ``profile.cli`` — profile-level default.
      4. ``"claude"`` — last-resort fallback.

    The profile is NO LONGER allowed to override ``_agent_actions``: the old
    behavior (profile cli silently overwrote the user's UI choice at spawn
    time) made the plan UI dropdown a no-op. Profile cli is now strictly a
    fallback. ``profile`` may be None (caller doesn't have it); we load it
    from the story row in that case.
    """
    if profile is None:
        try:
            from .profile_loader import resolve_profile

            profile = resolve_profile(story.get("profile", "minimal"))
        except Exception:  # noqa: BLE001 — profile resolve must not block spawn
            profile = None

    # 1. action adapter (authoritative post-plan / post-user-edit)
    if action is not None:
        _a = (action.get("adapter") or "").strip()
        if _a:
            return _a

    # 2-3. profile fallbacks
    if profile is not None:
        try:
            stage_cfg = profile.stage(stage) if hasattr(profile, "stage") else None
        except Exception:  # noqa: BLE001
            stage_cfg = None
        if stage_cfg is not None:
            _cli = (getattr(stage_cfg, "cli", "") or "").strip()
            if _cli:
                return _cli
        _pcli = (getattr(profile, "cli", "") or "").strip()
        if _pcli:
            return _pcli

    # 4. last resort
    return "claude"


def _build_artifacts_obligation(stage: str, profile_stages: dict, story_dir) -> str:
    """STEP 1.4 强化:把本 stage 必须产出的文件(绝对路径)放 prompt 最显眼处。

    code agent(claude)验证发现:即使文末有 declare 协议段,也可能不调 declare 也不
    写约定路径的文件(实测把 design 写成 design.md 而非 spec.md,或写到别的目录)。
    本段在 Story 信息紧下方用**绝对路径 + 红色警告语气**列清楚:必须产出哪些文件、
    写到哪里、两条等价落地方式(declare / 直接 Write)。

    artifacts 来自 profile stage.artifacts(1.1 schema 契约)。文件类 artifact 给绝对
    路径(story_dir 下的 canonical 文件名);git 类给"必须有未提交改动"。
    """
    cfg = profile_stages.get(stage) if profile_stages else None
    artifacts = []
    if cfg:
        # profile_stages 可能是 StageConfig dataclass 或 dict(两路调用)
        arts = getattr(cfg, "artifacts", None)
        if arts is None and isinstance(cfg, dict):
            arts = cfg.get("artifacts")
        artifacts = list(arts or [])
    if not artifacts:
        return ""

    # artifact 路径 → canonical 文件名(story_dir 下的绝对路径)
    from .artifact_check import _ARTIFACT_TO_DOC_TYPE
    from ...infra.story_paths import doc_filename

    lines = [
        "",
        "### ⚠️ 本阶段必须产出的文件(完成判据 —— 不产出这些 stage 不算完成)",
        "",
        "编排器**只看下列文件是否落地**(不看你是否说「完成了」)。任一缺失,stage 永远卡住。",
        "两条等价落地方式(任选其一,推荐第 1 条):",
        "  1. `story tool declare <doc_type> <相对路径>`(原子写 + 版本化 + 自动落正确位置)",
        "  2. 直接 Write 到下列**绝对路径**(code agent 不调 declare 时用这条)",
        "",
    ]
    file_artifacts = []
    has_git = False
    for art in artifacts:
        if art == "git":
            has_git = True
            continue
        doc_type = _ARTIFACT_TO_DOC_TYPE.get(art)
        if doc_type:
            fname = doc_filename(doc_type)
            abs_path = f"{story_dir}/{fname}"
            file_artifacts.append((art, doc_type, abs_path))
        else:
            file_artifacts.append((art, None, None))

    for art, doc_type, abs_path in file_artifacts:
        if abs_path:
            lines.append(f"- **必写文件**: `{abs_path}`(非空)")
            if doc_type:
                lines.append(f"  - declare 方式: `story tool declare {doc_type} {art}`")
                lines.append("  - 或直接 Write 到上面的绝对路径(内容非空)")
        else:
            lines.append(f"- **必落地**: `{art}`")
    if has_git:
        lines.append("- **必须有未提交的代码改动**(`git status` 非空)")

    lines.append("")
    lines.append(
        "**不要**用别的文件名(如 design.md 而非 spec.md)或别的目录 —— "
        "编排器只查上面列的绝对路径。写完确认文件存在且非空再退出。"
    )
    lines.append("")
    return "\n".join(lines)


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
    workspace_path: str = "",
    transcript_section: str = "",
    interactive: bool = False,
    task_actions: list[str] | None = None,
    grill: bool = False,
    is_single_stage: bool = False,
    seed_context: str = "",
) -> str:
    """构建给 CLI 的执行 prompt。

    ``interactive``:交互式终端路径(``claude "query"``,无 MCP)传 True —— design 维度
    协议的逐问澄清改为「在终端直接问人」(见 prompt_sections.build_design_dimensions_section)。
    """
    from ...infra.story_paths import story_evidence_dir
    from .prompt_sections import (
        build_consult_protocol_section,
        build_design_dimensions_section,
        build_grill_protocol_section,
        build_kb_tool_section,
        build_quality_section,
        build_test_env_section,
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

    # 接手中途需求:已有工作说明(纯文字,内联)。与 PRD 的区别——PRD 是"需求是什么",
    # seed_context 是"已经做到哪了、还差什么"。镜像 prd_section 的条件渲染模式:
    # 字段空就不出 section,新建 story 零影响。
    seed_section = ""
    if seed_context:
        seed_section = f"\n### 已有工作(接手)\n{seed_context}\n"

    # Quality checklist injection for verify stage (uses existing quality_checklist slot
    # semantics without touching prompt_renderer vars_map).
    # section 内容走共享 helper（与 _render_prompt 同一份），verify 门控留在本调用点。
    quality_section = ""
    if stage == "verify":
        checklist = build_quality_section(story_key, stage)
        if checklist.strip():
            quality_section = f"\n{checklist}\n"

    # 测试环境配置注入（verify-only）：从 workspace entity 读 confirmed 的 test_env，
    # 让 CLI agent 知道连哪个 gateway、用哪个测试用户。failsafe（无 workspace/未确认→""）。
    test_env_section = ""
    if stage == "verify":
        env_text = build_test_env_section(story_key, stage)
        if env_text.strip():
            test_env_section = f"\n{env_text}\n"

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

    # grill-me:非 design stage 但 grill=True 时,注入通用澄清协议。
    # design stage 的澄清协议已在 dimensions_section 里(不重复注入)。
    # single-pass 的 stage 名虽叫 verify,但本质含设计、没有后续阶段兜底澄清,
    # 所以 is_single_stage 时也允许 grill 段(此时 dimensions_section 为空,
    # 不会与 design 维度段内的澄清协议重复)。
    grill_section = ""
    if grill and (stage != "design" or is_single_stage):
        grill_section = build_grill_protocol_section(interactive=interactive)

    # consult (DESIGN-consult-tool §5.3): 所有 headless 路径注入 consult 协议段
    # (claude/kimi caller 都能用,无 claude-only 限制 —— 与 grill 不同)。
    # interactive 路径 code agent 在终端可直接问人,不注入。
    consult_section = ""
    if not interactive:
        consult_section = build_consult_protocol_section(interactive=interactive)

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

**硬约束**：若发现 worktree 路径不存在或分支异常，**立即停止**，不要尝试在主分支或其他分支上继续。不要 `story tool declare` 成果物(本阶段没产出有效结果)。
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

**硬约束**：若 git 操作失败（分支已存在且冲突、无权限、仓库不可写等），**立即停止后续工作**，不要尝试在错误的分支或主分支上继续。不要 `story tool declare` 成果物(本阶段没产出有效结果)。
"""
    elif workspace_path:
        # 规划 LLM 决定的 per-story 隔离沙箱(无项目绑定场景):后端建了空目录,
        # agent 自己把要改的项目 worktree add 进来。主 workspace(ws,如 D:/hc-all)下
        # 有多个独立项目仓库,agent 凭需求自己判断要改哪个 → 把那个项目 worktree 进来。
        worktree_section = f"""
### 工作沙箱 (Sandbox)

本 story 的隔离沙箱已建好(空目录)：`{workspace_path}`

**这是你的工作目录(cwd)**。请把本次改动涉及的项目仓库 `git worktree add` 进来,在隔离分支上改代码：

```bash
# 例:判断要改 hc-config,基于 main 切 feature 分支并加进沙箱
cd {workspace_path}
git -C {workspace or "<主工作区>"}/hc-config worktree add -b feature/{story_key} ./hc-config main
cd ./hc-config
```

**判断方法**：先读 PRD 了解需求,扫主工作区下的项目目录(每个子目录都是独立 git 仓库),凭需求决定要改哪个。可以 worktree add 多个(跨服务改动)。

**不要**直接在主工作区的项目里改 —— 必须先 worktree add 到 `{workspace_path}` 下,在 feature 分支上改。

**硬约束**：若 git worktree add 失败(分支冲突、仓库不可写),立即停止,不要在主分支继续。不要 `story tool declare` 成果物(本阶段没产出有效结果)。
"""

    # 执行约束:由 task_actions 内容决定(替 _is_single_stage 硬编码)。
    # 选了 run_tests → 允许轻量测试;没选 → 禁测试。都禁重构建。
    from .task_actions import _build_exec_constraint as _build_constraint
    from .task_actions import _build_task_list
    from .task_actions import build_done_protocol

    _task_actions = task_actions or []
    exec_constraint_section = _build_constraint(_task_actions)
    # 任务清单:LLM 选的动作 → prompt 里的有序步骤(按 order 排序)
    task_list_section = _build_task_list(_task_actions)
    # 成果物落地协议(STEP 1.4:替旧 done.json 自报协议):一鱼两吃 —— task_actions 既
    # 驱动任务清单,又驱动该 declare 哪种 doc_type(选了 write_test_report → declare
    # test_report;write_design_doc → declare spec)。让 CLI 提前知道要交什么成果物。
    done_protocol_section = build_done_protocol(stage, done_file, _task_actions)

    # STEP 1.4 强化(验证发现):code agent 不一定调 story tool declare,也不一定写到
    # 约定路径。把"本阶段必须产出的文件"绝对路径提到 prompt 最显眼位置(Story 信息
    # 紧下方),并给两条等价落地方式(declare / 直接 Write),让 code agent 必落其一。
    # artifacts_obligation_section 用绝对路径消除歧义(evidence 目录 vs workspace)。
    artifacts_obligation_section = _build_artifacts_obligation(
        stage, profile_stages, story_dir
    )

    return f"""## 任务: {stage}

### Story 信息
- Key: {story_key}
- 标题: {title}
- Story 证据目录: {story_dir}
{artifacts_obligation_section}
### 阶段说明
{stage_desc}
{prd_section}
{seed_section}
{transcript_section}
{knowledge_section}
{dimensions_section}
{quality_section}
{test_env_section}
{grill_section}
{consult_section}
{task_list_section}
### 关键要点
{focus}
{worktree_section}
{exec_constraint_section}
{done_protocol_section}"""


@with_story_key()
def run_orchestrator_agent_async(story_key: str, *, on_action=None) -> dict:
    """同步版本的 Agent 规划（直接调用，不进线程池）。

    用于 SSE 端点：规划在 generator 中执行，SSE 流式推送每个 action。
    """
    return run_orchestrator_agent(story_key, on_action=on_action)
