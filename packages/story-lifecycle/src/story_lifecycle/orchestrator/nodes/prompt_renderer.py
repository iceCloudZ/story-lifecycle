"""Prompt rendering — template loading, variable substitution, stage contracts."""

import re
from pathlib import Path

from ... import context_providers
from ...db import models as db
from ...story_paths import story_evidence_dir
from .state import StoryState, STORY_HOME
from .profile_loader import get_stage_config


def _strip_planner_contract_duplicates(plan_content: str) -> str:
    """Keep planner guidance, remove fixed stage-contract sections.

    Planner output is allowed to describe what to do. The stage template owns
    done-file schema, completion contract, adapter config, and lifecycle bounds.
    """
    blocked = {
        "完成后",
        "边界",
        "配置",
        "决策理由",
        "路径评分",
        "完成标准",
        "输出要求",
    }
    lines = plan_content.splitlines()
    kept: list[str] = []
    skipping = False
    for line in lines:
        if re.match(r"^#\s+.*任务书.*", line):
            continue
        heading = re.match(r"^(#{2,6})\s+(.+?)\s*$", line)
        if heading:
            title = heading.group(2).strip()
            if "执行指令" in title:
                skipping = False
                continue
            skipping = any(key in title for key in blocked)
            if skipping:
                continue
        if not skipping:
            kept.append(line)
    return "\n".join(kept).strip()


def _build_stage_contract(stage: str, state: StoryState) -> str:
    key = state["story_key"]
    rp = state.get("_resolved_profile")
    if rp:
        cfg = rp.get("stages", {}).get(stage, {})
    else:
        cfg = get_stage_config(state.get("profile", "minimal"), stage)
    expected = cfg.get("expected_outputs", [])
    expected_lines = "\n".join(f"- {name}" for name in expected) or "- none"
    done_path = f".story/done/{key}/{stage}.json"

    return (
        "## Stage Contract\n\n"
        f"- Story Key: {key}\n"
        f"- Stage: {stage}\n"
        f"- Done file: `{done_path}`\n"
        "- The done file must contain raw JSON only. Do not wrap it in markdown.\n"
        "- Do not continue into later stages after writing the done file.\n\n"
        "### Required output fields\n\n"
        f"{expected_lines}\n"
    )


def _build_plan_executor_prompt(
    stage: str, state: StoryState, plan_content: str
) -> tuple[str, dict]:
    """Build executor prompt when planner produced a task packet.

    In this mode the static stage prompt is not injected. It is a fallback only.
    """
    _, metadata = _render_prompt(stage, state)
    skill_instruction = metadata.get("skill_instruction", "")
    planner_packet = _strip_planner_contract_duplicates(plan_content)
    contract = _build_stage_contract(stage, state)

    support_sections: list[str] = []
    if skill_instruction:
        support_sections.insert(0, skill_instruction)
    if metadata.get("transcript_context"):
        support_sections.append(
            "## 历史上下文（来自既往 transcript）\n\n" + metadata["transcript_context"]
        )
    if metadata.get("quality_packet_text"):
        support_sections.append(metadata["quality_packet_text"])
    if metadata.get("checklist_text"):
        support_sections.append(
            f"## Executor Checklist\n\n{metadata['checklist_text']}"
        )

    ctx = state.get("context", {})
    repair_packet_path = ctx.get("repair_packet_path")
    if repair_packet_path:
        rp_file = Path(state["workspace"]) / repair_packet_path
        if rp_file.exists():
            support_sections.append(
                f"## Repair Packet\n\n{rp_file.read_text(encoding='utf-8')}"
            )

    prompt = f"{planner_packet}\n\n---\n\n{contract}"
    if support_sections:
        prompt = f"{prompt}\n\n---\n\n" + "\n\n".join(support_sections)
    metadata["has_plan_file"] = True
    return prompt, metadata


def _derive_relevance_tags(state: StoryState, stage: str) -> list[str]:
    """Derive relevance tags from story context for pattern matching."""
    tags = [stage]
    ctx = state.get("context", {})

    # Affected modules
    modules = ctx.get("affected_modules", [])
    if isinstance(modules, list):
        tags.extend(modules)
    elif isinstance(modules, str):
        tags.append(modules)

    # Touched file paths → derive module tags
    paths = ctx.get("touched_paths", [])
    if isinstance(paths, list):
        for p in paths:
            if isinstance(p, str) and "/" in p:
                tags.append(p.split("/")[0])
            elif isinstance(p, str):
                tags.append(p)

    category = ctx.get("category")
    if category:
        tags.append(category)
    profile = state.get("profile", "")
    if profile:
        tags.append(profile)

    # Source type & sub-type from DB story record
    try:
        story = db.get_story(state["story_key"])
        if story:
            source_type = story.get("source_type")
            if source_type:
                tags.append(source_type)
            sub_type = story.get("sub_type")
            if sub_type:
                tags.append(sub_type)
    except Exception:
        pass

    return tags


def _build_prd_task_section(state: StoryState, stage: str, has_prd: bool) -> str:
    """Deprecated: PRD generation belongs to Intake, before AI stages start."""
    return ""


def _render_prompt(stage: str, state: StoryState) -> tuple[str, dict]:
    """Render a prompt for the given stage. Returns (prompt_text, metadata_dict).

    Reads built-in templates or falls back to defaults.
    """
    template_paths = [
        STORY_HOME / "prompts" / f"{stage}.md",
    ]
    template = None
    for p in template_paths:
        if p.exists():
            template = p.read_text(encoding="utf-8")
            break

    # Package built-in via importlib.resources
    if not template:
        try:
            import importlib.resources as _ir

            ref = _ir.files("story_lifecycle.prompts").joinpath(f"{stage}.md")
            template = ref.read_text(encoding="utf-8")
        except (FileNotFoundError, TypeError):
            pass

    if not template:
        # Default prompt
        template = f"""执行阶段: {stage}
Story: {state["story_key"]}
标题: {state["title"]}

完成后将结果写入项目根目录下的 `.story/done/{state["story_key"]}/{stage}.json`。
文件必须只包含纯 JSON，不要用 markdown 代码块包裹。"""

    # Variable substitution
    ctx = state.get("context", {})

    # Sub-story context injection (type-aware)
    parent_key = None
    current_story = db.get_story(state["story_key"])
    if current_story:
        parent_key = current_story.get("parent_key")
    if parent_key:
        parent_story = db.get_story(parent_key)
        parent_title = parent_story.get("title", "") if parent_story else ""
        sub_desc = ctx.get("sub_description", "")
        sub_type = current_story.get("sub_type") or ""

        type_emphasis = {
            "bug-fix": "修复以下问题",
            "integration": "前后端联调修改",
            "refinement": "需求补充/调整",
            "redo": "重做",
        }
        emphasis = type_emphasis.get(sub_type, "子任务")

        context_hints = ""
        if sub_type == "bug-fix":
            review_path = ctx.get("review_path")
            if review_path:
                context_hints += (
                    f"\n- 上次评审: {review_path}\n  请关注评审中提到的问题。"
                )
        elif sub_type == "integration":
            spec_path = ctx.get("spec_path")
            if spec_path:
                context_hints += (
                    f"\n- 接口文档: {spec_path}\n  请参考设计文档中的接口定义。"
                )
        elif sub_type == "refinement":
            spec_path = ctx.get("spec_path")
            if spec_path:
                context_hints += (
                    f"\n- 现有设计文档: {spec_path}\n  在此基础上进行补充和调整。"
                )
        elif sub_type == "redo":
            review_path = ctx.get("review_path")
            review_summary = ctx.get("review_summary", "")
            if review_path:
                context_hints += f"\n- 被否决的方案评审: {review_path}"
            if review_summary:
                context_hints += f"\n- 评审摘要: {review_summary}"
            context_hints += "\n  请推翻旧方案，重新设计和实现。"

        sub_header = (
            f"## 子任务上下文\n\n"
            f"- **父故事**: {parent_key} — {parent_title}\n"
            f"- **类型**: {sub_type} — {emphasis}\n"
            f"- **任务描述**: {sub_desc}\n"
            f"{context_hints}\n"
        )
        template = sub_header + template

    # Quality Packet injection
    quality_section = ""
    checklist = ""
    quality_packet_injected = False
    quality_checklist_injected = False
    open_findings_count = 0
    learned_patterns_count = 0
    relevance_tags: list[str] = []
    try:
        from ..quality import build_quality_packet, build_quality_checklist

        relevance_tags = _derive_relevance_tags(state, stage)
        quality_packet = build_quality_packet(
            state["story_key"], stage, relevant_tags=relevance_tags
        )
        empty_marker = (
            f"Quality Packet for {state['story_key']}\n\nOpen Findings: none\n"
        )
        if quality_packet.strip() != empty_marker.strip():
            quality_section = f"## Quality Packet\n\n{quality_packet}"
            quality_packet_injected = True
        checklist = build_quality_checklist(state["story_key"], stage)
        if checklist.strip():
            quality_checklist_injected = True
        # Count findings and patterns from the packet for metadata
        try:
            from ...db import models as _qdb

            findings = _qdb.get_open_findings(state["story_key"])
            open_findings_count = len(findings)
            patterns = _qdb.find_relevant_patterns(relevance_tags, limit=5)
            learned_patterns_count = len(patterns)
        except Exception:
            pass
    except Exception:
        pass

    # Repair packet injection
    repair_section = ""
    repair_packet_path = ctx.get("repair_packet_path")
    if repair_packet_path:
        rp_file = Path(state["workspace"]) / repair_packet_path
        if rp_file.exists():
            repair_content = rp_file.read_text(encoding="utf-8")
            repair_section = f"## Repair Packet（修复上下文）\n\n{repair_content}"

    has_prd = bool(ctx.get("prd_path"))
    story_dir = str(
        story_evidence_dir(
            state.get("workspace", "") or str(Path.cwd()),
            state["story_key"],
            state.get("title", ""),
        )
    )

    # Get stage skill from profile
    rp = state.get("_resolved_profile")
    if rp:
        stage_cfg = rp.get("stages", {}).get(stage, {})
    else:
        stage_cfg = get_stage_config(state.get("profile", "minimal"), stage)
    skill = stage_cfg.get("skill", "")

    transcript_context = context_providers.get_transcript_context(
        state["story_key"], state.get("workspace", ""), stage
    )
    knowledge_context = context_providers.get_knowledge_context(
        state["story_key"], state.get("workspace", ""), stage
    )

    vars_map = {
        "{story_key}": state["story_key"],
        "{title}": state.get("title", ""),
        "{story_dir}": story_dir,
        "{prd_path}": ctx.get("prd_path", ""),
        "{prd_path_section}": (
            f"- PRD 文件: {ctx['prd_path']}\n  请读取该文件了解需求详情。"
            if has_prd
            else ""
        ),
        "{no_prd_section}": (
            ""
            if has_prd
            else "**没有提供 PRD 文件。请先回到 story-lifecycle Intake 准备 PRD.md。**"
        ),
        # AI-enhanced PRD injection
        "{prd_task_section}": _build_prd_task_section(state, stage, has_prd),
        "{requirement_source}": (
            "阅读 PRD 文件" if has_prd else "停止并要求先准备 PRD.md"
        ),
        "{spec_path_section}": (
            f"- Spec 路径: {ctx['spec_path']}" if ctx.get("spec_path") else ""
        ),
        "{skill}": skill,
        "{skill_instruction}": (
            f"在开始本阶段任务前，请先使用 Skill 工具调用 `{skill}`，"
            f"基于 skill 的分析结果来完成后续工作。"
            if skill
            else ""
        ),
        "{quality_packet_section}": quality_section,
        "{quality_checklist}": checklist,
        "{repair_packet_section}": repair_section,
        "{transcript_context}": (transcript_context + "\n") if transcript_context else "",
        "{knowledge_context}": (knowledge_context + "\n") if knowledge_context else "",
    }
    _had_repair_placeholder = "{repair_packet_section}" in template
    for key, value in vars_map.items():
        template = template.replace(key, value)

    # Append repair packet directly if template had no placeholder
    if repair_section and not _had_repair_placeholder:
        template = f"{template}\n\n{repair_section}"

    metadata = {
        "transcript_context": transcript_context or "",
        "knowledge_context": knowledge_context or "",
        "quality_packet_injected": quality_packet_injected,
        "quality_checklist_injected": quality_checklist_injected,
        "quality_packet_text": quality_section,
        "checklist_text": checklist,
        "open_findings_count": open_findings_count,
        "learned_patterns_count": learned_patterns_count,
        "relevance_tags": relevance_tags,
        "has_prd": has_prd,
        "has_plan_file": False,  # set by caller when plan file prepended
        "skill_instruction": (
            f"在开始本阶段任务前，请先使用 Skill 工具调用 `{skill}`，"
            f"基于 skill 的分析结果来完成后续工作。"
            if skill
            else ""
        ),
    }
    return template, metadata
