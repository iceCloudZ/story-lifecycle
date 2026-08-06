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
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

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
            story_key=story_key,
            stage=stage,
            workspace=workspace,
            ctx=ctx,
            action=action,
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

    return _render_cli_prompt(
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


def _render_cli_prompt(
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
    """构建给 CLI 的执行 prompt（原 planner._build_cli_prompt，设计 14 迁入）。

    ``interactive``:交互式终端路径(``claude "query"``,无 MCP)传 True —— design 维度
    协议的逐问澄清改为「在终端直接问人」(见 prompt_sections.build_design_dimensions_section)。
    """
    req = CliPromptRequest(
        story_key=story_key,
        title=title,
        stage=stage,
        focus=focus,
        done_file=done_file,
        profile_stages=profile_stages,
        prd_path=prd_path,
        project_section=project_section,
        workspace=workspace,
        workspace_path=workspace_path,
        transcript_section=transcript_section,
        interactive=interactive,
        task_actions=task_actions,
        grill=grill,
        is_single_stage=is_single_stage,
        seed_context=seed_context,
    )
    return _render_cli_prompt_req(req)


@dataclass
class CliPromptRequest:
    """_render_cli_prompt 的入参打包（设计 14 F2：16 参数 → 1 参数对象）。

    字段 = 原 _render_cli_prompt 参数，类型照搬。
    """

    story_key: str
    title: str
    stage: str
    focus: str
    done_file: str
    profile_stages: dict
    prd_path: str = ""
    project_section: str = ""
    workspace: str = ""
    workspace_path: str = ""
    transcript_section: str = ""
    interactive: bool = False
    task_actions: Optional[list[str]] = None
    grill: bool = False
    is_single_stage: bool = False
    seed_context: str = ""


def _render_cli_prompt_req(req: CliPromptRequest) -> str:
    """渲染 CLI 执行 prompt 主体（原 _build_cli_prompt 正文，设计 14 迁入）。

    ``interactive``:交互式终端路径(``claude "query"``,无 MCP)传 True —— design 维度
    协议的逐问澄清改为「在终端直接问人」(见 prompt_sections.build_design_dimensions_section)。
    """
    story_key = req.story_key
    title = req.title
    stage = req.stage
    focus = req.focus
    done_file = req.done_file
    profile_stages = req.profile_stages
    prd_path = req.prd_path
    project_section = req.project_section
    workspace = req.workspace
    workspace_path = req.workspace_path
    transcript_section = req.transcript_section
    interactive = req.interactive
    task_actions = req.task_actions
    grill = req.grill
    is_single_stage = req.is_single_stage
    seed_context = req.seed_context
    from ..infra.story_paths import story_evidence_dir
    from .engine.prompt_sections import (
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
    from .engine.task_actions import _build_exec_constraint as _build_constraint
    from .engine.task_actions import _build_task_list
    from .engine.task_actions import build_done_protocol

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
    artifacts_obligation_section = _render_artifacts_obligation(
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


def _render_artifacts_obligation(
    stage: str, profile_stages: dict, story_dir
) -> str:
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
    from .engine.artifact_check import _ARTIFACT_TO_DOC_TYPE
    from ..infra.story_paths import doc_filename

    lines = [
        "",
        "### ⚠️ 本阶段必须 declare 的成果物(完成判据 —— 不 declare stage 不算完成)",
        "",
        "编排器**只认 `story tool declare` 信号**(不看文件存在)。写完文件后**必须**调",
        "`story tool declare <doc_type> <相对路径>` 落地，编排器才会判定 stage 完成、推进 judge。",
        "只写文件不 declare → stage 永远卡住。declare 会原子写文件 + 版本化 + 自动落正确位置。",
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
            lines.append(f"- **必 declare**: `{art}`(对应文件 `{abs_path}`,非空)")
            if doc_type:
                lines.append(
                    f"  - 写完文件后调: `story tool declare {doc_type} {art}`"
                )
                lines.append(
                    "  - 或直接用 declare 的 --content 参数原子写(推荐,免去路径错位)"
                )
        else:
            lines.append(f"- **必落地**: `{art}`")
    if has_git:
        lines.append("- **必须有未提交的代码改动**(`git status` 非空)")

    lines.append("")
    lines.append(
        "**关键**:全部写完后,逐个调 declare。可以先用 Write 写草稿、确认内容完整,"
        "再用 `story tool declare <doc_type> <相对路径> --content \"$(cat 文件)\"` 落地。"
        "没 declare = stage 没完成。"
    )
    lines.append("")
    return "\n".join(lines)


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
            story_key=story_key,
            stage=stage,
            workspace=workspace,
            ctx=ctx,
            action=action,
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
