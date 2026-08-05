"""PromptBuilder 子类（设计 13 Step 2）— stage prompt 构建统一入口。

从 planner.py（_build_cli_prompt）+ api.py（_build_interactive_stage_prompt /
_build_stage_launch_prompt）抽出。编排线程 / executors 只通过 PromptBuilder 接口
构建 prompt，不再直接调 planner/api 的私有函数。

两个 builder：
- StagePromptBuilder: 全量 stage prompt（交互式终端路径语义，interactive=True，
  与 api._build_interactive_stage_prompt 同源）。按 stage 有 design/build/verify
  子类（语义标注 + 默认 stage），prompt 内容本身由共享渲染逻辑保证一致。
- LaunchSeedBuilder: 短 read-file seed（写 .story/context/<key>/prompt_<stage>.md
  + 返回「读取并执行」指令），替 api._build_stage_launch_prompt。
"""

from __future__ import annotations

import logging

from .abc import PromptBuilder

log = logging.getLogger("story-lifecycle.prompts")


class StagePromptBuilder(PromptBuilder):
    """全量 stage CLI prompt（交互式路径）。

    build() 的渲染逻辑 = planner._build_cli_prompt（同一份段落拼装，含
    design 维度段 / verify 质量清单 / worktree 段 / task_actions 约束等），
    由 _render_stage_prompt 统一调用 —— 输出与老 _build_interactive_stage_prompt
    完全一致（回归保护：prompt 内容不变，只是调用入口换了）。
    """

    #: 本 builder 固定的 stage（None = 用 build() 传入的 stage）
    default_stage: str | None = None

    def build(
        self,
        story_key: str,
        stage: str,
        workspace: str,
        ctx: dict,
        action: dict,
    ) -> str:
        return _render_stage_prompt(
            story_key=story_key,
            stage=self.default_stage or stage,
            workspace=workspace,
            ctx=ctx,
            action=action,
        )


class DesignPromptBuilder(StagePromptBuilder):
    """design stage prompt（设计维度 checklist + 逐问澄清协议）。"""

    default_stage = "design"


class BuildPromptBuilder(StagePromptBuilder):
    """build stage prompt（worktree 分支隔离指令 + 代码约束）。"""

    default_stage = "build"


class VerifyPromptBuilder(StagePromptBuilder):
    """verify stage prompt（质量清单 + 测试环境注入）。"""

    default_stage = "verify"


class LaunchSeedBuilder(PromptBuilder):
    """短 read-file seed：写全量 prompt 到 .story/context/<key>/prompt_<stage>.md，
    返回一行「读取该文件并执行」指令（喂给 claude "query" / kimi PTY seed）。
    文件写失败返回空串（spawn 继续，无 seed 兜底）。"""

    def build(
        self,
        story_key: str,
        stage: str,
        workspace: str,
        ctx: dict,
        action: dict,
    ) -> str:
        return _render_launch_seed(
            story_key=story_key, stage=stage, workspace=workspace, ctx=ctx, action=action
        )


# ---------------------------------------------------------------------------
# 共享渲染（与 planner._build_cli_prompt / api._build_stage_launch_prompt 同源）
# ---------------------------------------------------------------------------


def _resolve_profile_stages(story_key: str, profile_name: str) -> dict:
    from .engine.profile_loader import resolve_profile

    try:
        rp = resolve_profile(profile_name)
        return {n: c for n, c in rp.stages.items()}
    except Exception:
        log.debug("resolve_profile failed for %s", profile_name, exc_info=True)
        return {}


def _project_lines(story_key: str) -> str:
    from ..infra.db import models as db

    lines = []
    try:
        for sp in db.get_story_projects(story_key):
            proj = db.get_project(sp["project_id"])
            if not proj:
                continue
            wt = sp.get("worktree_path", "")
            if wt:
                lines.append(
                    f"- 仓库 `{proj['repo_path']}` → worktree `{wt}` "
                    f"(分支 `{sp['branch']}`, 基线 `{sp.get('base_branch', 'main')}`)"
                )
            else:
                lines.append(
                    f"- 仓库 `{proj['repo_path']}`: 分支 `{sp['branch']}`, "
                    f"基线 `{sp.get('base_branch', 'main')}`"
                )
    except Exception:
        pass
    return "\n".join(lines)


def _render_stage_prompt(
    *,
    story_key: str,
    stage: str,
    workspace: str,
    ctx: dict,
    action: dict,
) -> str:
    """渲染全量 stage prompt（交互式路径）。

    参数拼装与 api._build_interactive_stage_prompt 一致；正文渲染委托
    planner._build_cli_prompt（段落拼装不动，避免 prompt 漂移）。
    """
    from ..infra.db import models as db
    from ..infra.paths import stage_done_file_rel
    from .engine import planner

    story = db.get_story(story_key) or {}
    title = story.get("title", "") or ""
    profile_name = story.get("profile", "minimal")
    profile_stages = _resolve_profile_stages(story_key, profile_name)
    stage_cfg = profile_stages.get(stage)
    focus = (
        stage_cfg.description if stage_cfg and hasattr(stage_cfg, "description") else ""
    ) or ""
    focus = (action or {}).get("focus") or focus

    transcript_section = ""
    try:
        from ..knowledge.context_providers import get_transcript_context

        transcript_section = get_transcript_context(story_key, workspace, stage) or ""
    except Exception:
        pass

    from .engine.task_actions import get_default_task_actions

    is_single = len(profile_stages) <= 1
    task_actions = (action or {}).get("task_actions") or get_default_task_actions(
        stage, is_single
    )

    return planner._build_cli_prompt(
        story_key=story_key,
        title=title,
        stage=stage,
        focus=focus,
        done_file=stage_done_file_rel(story_key, stage),
        profile_stages=profile_stages,
        prd_path=planner._resolve_prd_for_exec(
            story_key, workspace, title, ctx.get("prd_path", "")
        ),
        project_section=_project_lines(story_key),
        workspace=workspace,
        workspace_path=ctx.get("workspace_path", ""),
        transcript_section=transcript_section,
        interactive=True,  # 交互式 claude("query",无 MCP):逐问澄清改「终端问人」
        task_actions=task_actions,
        grill=True if is_single else False,  # single-pass 默认 grill(对齐 fallback)
        is_single_stage=is_single,
        seed_context=ctx.get("seed_context", ""),
    )


def _render_launch_seed(
    *,
    story_key: str,
    stage: str,
    workspace: str,
    ctx: dict,
    action: dict,
) -> str:
    """渲染 read-file seed（写 prompt 文件 + 返回读取指令）。"""
    try:
        from ..infra.story_paths import safe_story_path

        full = _render_stage_prompt(
            story_key=story_key, stage=stage, workspace=workspace, ctx=ctx, action=action
        )
        pdir = safe_story_path(workspace, ".story", "context", story_key)
        pdir.mkdir(parents=True, exist_ok=True)
        pfile = pdir / f"prompt_{stage}.md"
        pfile.write_text(full, encoding="utf-8")
        return (
            f"请读取 `{pfile}` 并严格按其中的说明执行本阶段({stage})任务,"
            f"完成后按其完成协议写入 done 文件。"
        )
    except Exception:
        log.exception("render launch seed failed for %s/%s", story_key, stage)
        return ""


def get_stage_prompt_builder(stage: str) -> StagePromptBuilder:
    """Factory:按 stage 返回对应 PromptBuilder 子类。"""
    if stage == "design":
        return DesignPromptBuilder()
    if stage == "build":
        return BuildPromptBuilder()
    if stage == "verify":
        return VerifyPromptBuilder()
    return StagePromptBuilder()
