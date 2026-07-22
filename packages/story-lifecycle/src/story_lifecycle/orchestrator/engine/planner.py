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


def _sanitize_workspace_slug(slug: str) -> str:
    """Normalize an LLM-produced workspace slug to a safe directory name.

    Forces kebab-case-ish lowercase ASCII + digits + ``-_.``. Strips path
    separators (no traversal: the slug becomes ONE path segment under
    worktrees_root, never a nested path). Empty → "".
    """
    import re

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

    return f"""你是开发任务编排 Agent。根据需求信息，规划开发流程。

## 你的职责
- 根据需求决定需要执行哪些阶段(skip 不需要的)
- 给每个阶段指定 2-3 个关键要点（focus）
- 为每个阶段选 task_actions（该干什么活）
- 给本 story 起一个独立的 workspace_slug(工作空间目录名)
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

## 规则
1. 对每个阶段,决定 skip(true)还是执行(false)
2. 对执行的阶段,给 2-3 个 focus 要点(不要写详细设计)
3. 为每个执行的阶段选 task_actions(从上面的动作库选,不能自己编)
4. 标 grill=true 表示该阶段需要人澄清关键歧义(如复杂设计决策);简单明确的标 false
5. CLI（claude/codex/kimi）会自己理解需求并设计方案，你不需要代劳
6. adapter 由 profile 路由,你不需要选
7. **workspace_slug**：为这个 story 起一个独立的隔离工作空间目录名。
   - 从标题提炼：小写英文 + 数字 + 连字符(kebab-case)，10-40 字符
   - 例：「MGM活动限制用户当前的app版本」→ `mgm-app-version-limit`
   - 例：「优化订单导出查询性能」→ `order-export-perf`
   - 后端会建空目录 <worktrees_root>/<slug>/ 作为 code agent 的 cwd
   - agent 自己把要改的项目 `git worktree add` 进去,在这里干活
   - 纯调研/不改代码的 story 才留空字符串 ""

## 输出格式（关键）
必须**只**输出一个 JSON 对象，不要任何 markdown、表格、解释文字、代码块标记。
schema:
{{"stages":[{{"stage":"<阶段名>","skip":<true|false>,"focus":"<要点，多条用分号>","task_actions":["<动作1>","<动作2>"],"grill":<true|false>}}],"workspace_slug":"<kebab-case 目录名>"}}
- 每个 profile 里的阶段都要出现在 stages 里（skip 的也要列出，skip=true）
- focus/task_actions 用中文
- workspace_slug 用 kebab-case 英文(标题简写)
- 直接输出 JSON，第一个字符必须是 {{，不要有 ```json 或任何前缀"""


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
        """单阶段规划:skip 哪些阶段 + 每阶段 focus + task_actions + grill。adapter 不让模型选。"""

        stage: str
        skip: bool = False
        focus: str = ""
        task_actions: list[str] = []
        grill: bool = False

    class PlanResult(BaseModel):
        """规划结果:阶段列表 + 工作空间 slug。

        workspace_slug(标题简写,kebab-case):规划 LLM 决定的 per-story 隔离
        工作空间目录名,后端会在 <worktrees_root>/<slug>/ 建空目录,作为 code
        agent 的 cwd。agent 自己把要改的项目 git worktree add 进来。空字符串
        = 不需要独立工作空间(如纯调研 story),code agent 用主 workspace。
        """

        stages: list[StagePlan]
        workspace_slug: str = ""

    story = db.get_story(story_key)
    if not story:
        raise ValueError(f"Story not found: {story_key}")

    title = story.get("title", "")
    content = story.get("content", "")
    workspace = story.get("workspace", "")
    profile_name = story.get("profile", "minimal")

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
            # 规划 LLM 决定的 per-story 工作空间 slug → mkdir + 存 ctx。
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
    # 落规划期建好的工作空间路径(无则保留原值/清空),供后续 spawn + prompt 用。
    if workspace_path:
        ctx["workspace_path"] = workspace_path
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

    替旧 build_repair_action(transition_decision → action)。
    字段映射:kind(action→kind) / reason / new_adapter(新增,替硬编码轮转) / rescue_stage。
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
        # Unified dual-write: legacy story_document (ref) + new story_doc
        # (full content versioned). Both are best-effort; going through the
        # shared helper keeps the two tables in sync (see doc_sync).
        _story_row = db.get_story(story_key) or {}
        _ws = _story_row.get("workspace") or ""
        try:
            from ...infra.doc_sync import register_doc_dual_write

            register_doc_dual_write(
                story_key,
                kind,
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
    rp = None
    try:
        rp = resolve_profile(profile_name)
        profile_stages = {name: cfg for name, cfg in rp.stages.items()}
        quality_cfg = rp.quality or {}
    except Exception:
        pass
    # SOURCE-DRIVEN-MODEL: Story 业务状态机(开发/测试/...)按 source_type 解析,不再从
    # profile 读。source 定义状态拓扑,profile 定义阶段执行。无 source / 未注册 →
    # default.yaml(通用四状态机);source 配置 story_states 为空 → driver 退化扁平阶段。
    story_states = {}
    try:
        from ...sourcing.source_loader import resolve_source_profile

        story_states = (
            resolve_source_profile(story.get("source_type")).story_states or {}
        )
    except Exception:
        pass
    # STORY-STATE-MODEL: 初始化 lifecycle_state(Story 业务状态,独立第一公民)。
    # 优先级:ctx._lifecycle_state(resume 续用)> DB lifecycle_state > 默认"待启动"。
    # 写回 DB + ctx 保证一致。无 story_states 的 profile → lifecycle_state 仍存但
    # driver 不按状态机跑(退化扁平,向后兼容)。
    lifecycle_state = (
        ctx.get("_lifecycle_state") or story.get("lifecycle_state") or "待启动"
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
            # adapter 来源:action("adapter") > profile stage cli > profile cli > claude。
            # 用户在 plan UI 改 adapter(PATCH /plan/actions/{stage})直接写 _agent_actions,
            # 那里是权威 —— profile cli 不再覆盖用户选择(老逻辑会静默盖回去,导致
            # UI 选 kimi 执行还是 claude)。resolve_stage_adapter 是唯一权威 resolver,
            # _ensure_story_agent_pty 也调它,两条 spawn 路径保持一致。
            adapter_name = resolve_stage_adapter(
                story, stage, profile=rp, action=action
            )
            if adapter_name != (action.get("adapter") or ""):
                # resolver 兜底了(profile cli)→ 回写保持下游一致
                action["adapter"] = adapter_name
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
                prd_path=_resolve_prd_for_exec(
                    story_key, workspace, title, ctx.get("prd_path", "")
                ),
                project_section=project_section,
                workspace=workspace,
                workspace_path=ctx.get("workspace_path", ""),
                transcript_section=transcript_ctx or "",
                interactive=not headless,  # BUG #9:交互式路径走"终端直接问人"
                task_actions=action.get("task_actions", []),
                grill=action.get("grill", False),
                is_single_stage=len(profile_stages) <= 1,
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
                    # headless 路径不走 PTY 注入(走 done-file 轮询);spec 用不上。
                    _session_spec = None
                else:
                    # 交互式:走 adapter.start_session 拿统一的 SessionSpec。
                    # prompt/resume/session_id 在 spec 里,下游 ensure_agent_pty
                    # 按 spec.pty_prompt + spec.readiness_marker 注入,不再分支。
                    #
                    # prompt 投递策略:不把完整多行 cli_prompt 直接塞进 claude "query"
                    # —— claude CLI 只接收命令行的首行,多行 prompt 会被截断到
                    # `## 任务: <stage>` 一行(实测:tapd-1144381896001067642 的 verify
                    # stage 只剩首行,agent 无从下手)。cli_prompt 已在上方写入
                    # prompt_file(1049 行),这里只传一条「读该文件并执行」的 seed,
                    # 与 _spawn_story_agent_pty(api.py)的投递路径对齐:两条 spawn
                    # 入口落到同一个 prompt_<stage>.md,claude 收到的也都是读文件指令。
                    # 对 kimi/codex(PTY paste)同样安全:seed 短,完整内容在文件里。
                    _seed = (
                        f"请读取 `{prompt_file}` 并严格按其中的说明执行本阶段"
                        f"({stage})任务,完成后按其完成协议写入 done 文件。"
                    )
                    _session_spec = adapter.start_session(model=model, prompt=_seed)
                    launch_cmd = _session_spec.command

                # grill-me:LLM 决定 + mode 兜底。
                # 当 action.grill=True 且 task_actions 里有 interactive 动作时,接 MCP clarify。
                # 替原 stage=="design" 硬编码 —— grill 不再绑 design stage。
                # 仅 headless 路径走 MCP clarify;交互式路径(interactive_pty)走"终端直接问人"
                # (BUG #9,见 handoff-design-hitl §11)。
                # 见 orchestrator/mcp/clarify_server.py + memory story-lifecycle-design-hitl。
                from .task_actions import TASK_ACTIONS as _TA

                _has_interactive = any(
                    _TA.get(a, {}).get("mode") == "interactive"
                    for a in action.get("task_actions", [])
                )
                _wants_grill = action.get("grill", False) and _has_interactive

                story_env = None
                # consult (DESIGN-consult-tool §5.8):env 注入提升到所有 headless spawn
                # —— STORY_KEY/STAGE/WORKSPACE/ADAPTER 对 claude/kimi caller 都可用,
                # 这样 code agent 用 Bash 跑 `story consult` 时能读到。**不注入**
                # STORY_CONSULT_DEPTH(caller depth 是未设/0;只有外援 spawn 时注入 1)。
                if headless:
                    import os as _os

                    story_env = {
                        **_os.environ,
                        "STORY_KEY": story_key,
                        "STORY_STAGE": stage,
                        "STORY_WORKSPACE": workspace,  # consult spawn 外援的工作区
                        "STORY_ADAPTER": adapter_name,  # consult 的 decorrelation 决策
                    }
                if _wants_grill and adapter_name == "claude" and headless:
                    import sys as _sys

                    try:
                        from ..mcp.clarify_server import write_mcp_config

                        _mcp_cfg = (
                            safe_story_path(workspace, ".story", "context", story_key)
                            / "clarify_mcp.json"
                        )
                        write_mcp_config(_mcp_cfg, _sys.executable)
                        launch_cmd = list(launch_cmd) + ["--mcp-config", str(_mcp_cfg)]
                        # story_env 已在 headless 分支注入;这里只补 MCP config,
                        # 不再重复设 STORY_KEY/STAGE(避免双源真相漂移)。
                        log.info(
                            "[%s] stage %s grill clarify MCP wired: --mcp-config=%s",
                            story_key,
                            stage,
                            _mcp_cfg,
                        )
                    except Exception:
                        log.exception(
                            "[%s] stage %s grill clarify MCP wiring failed (clarify unavailable)",
                            story_key,
                            stage,
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
                # spawn cwd:ctx.workspace_path(规划 LLM 决定的隔离空间)优先,
                # 退回主 workspace。code agent 在隔离空间里 worktree add 项目进来。
                _spawn_cwd = ctx.get("workspace_path") or workspace
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
                            cwd=_spawn_cwd,
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
                    # spec 驱动:prompt 和 readiness_marker 都从 adapter.start_session
                    # 的返回值拿(adapter 自己声明 prompt 怎么传,见 SessionSpec)。
                    # cwd 用 ctx.workspace_path(规划 LLM 决定的隔离空间),没有则退回主 ws。
                    _spawn_cwd = ctx.get("workspace_path") or workspace
                    _pty_session, _agent_pty = ensure_agent_pty(
                        story_key,
                        launch_cmd,
                        _spawn_cwd,
                        _session_spec.pty_prompt if _session_spec else "",
                        readiness_marker=(
                            _session_spec.readiness_marker if _session_spec else None
                        ),
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
                        from .execution import auto_confirm_from_profile
                        from .supervisor import supervise_pty_session

                        _sup_llm = get_llm().invoke
                        _sup_sf = {
                            "story_key": story_key,
                            "stage": stage,
                            "summary": focus,
                            # supervision 模式:默认 False(人工盯,supervisor 不调 LLM、不写 PTY,
                            # 仅落 awaiting_confirm 事件 + 桌面通知);仅 profile 显式 auto_confirm=True
                            # 的全自动场景(benchmark/CI)才走 LLM 决策 + 自动回写。
                            "auto_confirm": auto_confirm_from_profile(rp, stage),
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
                                cwd=_spawn_cwd,
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
                # PTY（interactive_pty 路径）：kimi/codex 若已退出却没写 done file，
                # 同样提前失败——否则进程死后 poll 循环只能傻等满 45min（且若残留
                # 输出被误判为 pending clarification，elapsed 会被反复重置，永不超时，
                # story 僵尸在 active）。对称 headless 的 1230 检查，但不重试（PTY
                # 重启重，交给 decide_recovery/rescue_story 层统一换 adapter 恢复）。
                # 容错：进程刚 spawn 时 alive 短暂为 False（启动握手期），给 30s 宽限。
                if (
                    _agent_pty is not None
                    and elapsed > 30
                    and not _agent_pty.alive
                    and not done_path.exists()
                ):
                    log.warning(
                        "[%s] PTY %s exited without done file for stage=%s; "
                        "marking failed (rescue layer will retry)",
                        story_key,
                        adapter_name,
                        stage,
                    )
                    db.update_story(
                        story_key,
                        status="failed",
                        last_error=(
                            f"Stage {stage}: {adapter_name} PTY process exited "
                            f"without writing done file"
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
                                    _persist_playbook_for_story(
                                        workspace, story_key, db
                                    )
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
                stage_cfg = profile_stages.get(stage)
                max_retries = (
                    stage_cfg.max_retries if hasattr(stage_cfg, "max_retries") else 2
                )
                ctx["last_verify_summary"] = done_data.get("summary", "")
                ctx["last_done_data"] = done_data  # §4.2:喂给 unified gate 作 context
                # REFACTOR §5.3:统一 gate(一次 LLM:质量判断 + finding + decision + repair)
                # 替原 run_verify_gate + decide_transition + build_repair_action 三步。
                from ..evaluation.unified_gate import run_unified_verify_gate

                gate_result = run_unified_verify_gate(
                    story_key=story_key,
                    stage=stage,
                    workspace=workspace,
                    context=ctx,
                    quality_cfg=quality_cfg,
                    max_retries=max_retries,
                    done_data=done_data,
                    adapter_name=adapter_name,
                    retry_count=ctx.get("_verify_round", 1),
                )
                if gate_result["decision"] == "retry":
                    # unified_gate 的 repair_action 已包含 kind/reason/new_adapter/rescue_stage
                    repair_spec = gate_result.get("repair_action") or {}
                    repair_action = _repair_spec_to_action(
                        repair_spec=repair_spec,
                        story_key=story_key,
                        adapter_name=adapter_name,
                        round_n=gate_result.get("round", 1),
                        reason=gate_result.get("reason", ""),
                    )
                    if repair_action is None:
                        # escalate/skip → 不插 action,标失败
                        db.update_story(
                            story_key,
                            status="failed",
                            last_error=str(gate_spec_reason(repair_spec))[:500],
                        )
                        return
                    actions.insert(idx + 1, repair_action)
                    ctx["_agent_actions"] = actions
                    db.update_story(
                        story_key,
                        context_json=json.dumps(ctx, ensure_ascii=False),
                    )
                    log.info(
                        "[%s] Verify gate blocked (round %d/%d); repair=%s adapter=%s",
                        story_key,
                        gate_result.get("round", 1),
                        gate_result.get("retry_limit", max_retries),
                        (repair_spec or {}).get("kind", "?"),
                        repair_action.get("adapter", "-"),
                    )
                elif gate_result["decision"] == "fail":
                    db.update_story(
                        story_key,
                        status="failed",
                        last_error=gate_result.get("reason", "verify gate fail"),
                    )
                    return

        idx += 1

    # 所有 action 执行完毕
    db.update_story(story_key, status="completed")
    log.info(f"[{story_key}] All stages completed")
    # story 完成时生成 retrospect.md（聚合各 stage done 摘要）
    _write_retrospect(workspace, story_key, actions)
    # 飞轮回写:reflect → 按 task_type/dimension 落盘(REFACTOR §5.1.3)
    _persist_playbook_for_story(workspace, story_key, db)


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
    elif workspace_path:
        # 规划 LLM 决定的 per-story 隔离工作空间(无项目绑定场景):后端建了空目录,
        # agent 自己把要改的项目 worktree add 进来。主 workspace(ws,如 D:/hc-all)下
        # 有多个独立项目仓库,agent 凭需求自己判断要改哪个 → 把那个项目 worktree 进来。
        worktree_section = f"""
### 工作空间

本 story 的隔离工作空间已建好(空目录)：`{workspace_path}`

**这是你的工作目录(cwd)**。请把本次改动涉及的项目仓库 `git worktree add` 进来,在隔离分支上改代码：

```bash
# 例:判断要改 hc-config,基于 main 切 feature 分支并加进工作空间
cd {workspace_path}
git -C {workspace or "<主工作区>"}/hc-config worktree add -b feature/{story_key} ./hc-config main
cd ./hc-config
```

**判断方法**：先读 PRD 了解需求,扫主工作区下的项目目录(每个子目录都是独立 git 仓库),凭需求决定要改哪个。可以 worktree add 多个(跨服务改动)。

**不要**直接在主工作区的项目里改 —— 必须先 worktree add 到 `{workspace_path}` 下,在 feature 分支上改。

**硬约束**：若 git worktree add 失败(分支冲突、仓库不可写),立即停止,把错误写入完成协议的 `summary` + `status="error"`,不要在主分支继续。
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
    # 完成协议:动态字段(选了 write_test_report → test_report_path;write_design_doc
    # → spec_path)。一鱼两吃:task_actions 既驱动任务清单,又驱动 done 协议字段,
    # 让 CLI 提前知道要交什么(否则 done 校验无源失败)。
    done_protocol_section = build_done_protocol(stage, done_file, _task_actions)

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
