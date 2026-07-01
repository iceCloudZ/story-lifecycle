"""Release-prompt generator — produce a pre-release checklist prompt for a code AI."""

from __future__ import annotations

from .resolver import ContextResolver
from ..engine.prompt_sections import build_knowledge_section, build_transcript_section


def generate_release_prompt(story_key: str) -> dict:
    """Render a release-preparation prompt that can be pasted into a code AI session.

    Returns {"content": <markdown>, "story_key": story_key}.
    Raises ValueError if story not found.
    """
    from ...infra.db import models as db

    bundle = ContextResolver().resolve(story_key)
    content = _render_release_prompt(story_key, bundle)
    db.log_event(
        story_key,
        stage=bundle.story.get("current_stage", "") if bundle.story else "",
        event_type="release_prompt_generated",
        payload={"revision": bundle.revision},
    )
    return {"content": content, "story_key": story_key}


def _render_release_prompt(story_key: str, bundle) -> str:
    story = bundle.story or {}
    lines: list[str] = []

    lines.append("# 上线前准备")
    lines.append("")
    lines.append(f"Story：{story_key} - {story.get('title', '')}")
    tapd_url = story.get("tapd_url", "")
    if tapd_url:
        lines.append(f"TAPD：{tapd_url}")
    lines.append(f"工作区：{story.get('workspace', '')}")
    lines.append(
        f"Profile / 当前阶段：{story.get('profile', '')} / {story.get('current_stage', '')}"
    )
    lines.append("")

    lines.append("请基于以上 Story 的完整上下文，做上线前准备。具体任务如下：")
    lines.append("")
    lines.append("1. **代码最终检查**：")
    lines.append("   - 确认所有变更已提交到正确分支，且 PR/MR 已通过代码审查。")
    lines.append("   - 检查是否有未提交的临时代码、调试日志或测试开关。")
    lines.append("   - 确认分支版本号、依赖版本与需求一致。")
    lines.append("")
    lines.append("2. **回归与验证**：")
    lines.append("   - 运行核心单元测试、集成测试，确保无失败用例。")
    lines.append("   - 验证需求中的验收标准是否全部满足。")
    lines.append("   - 若涉及数据库 DDL，确认已在预发环境执行并验证。")
    lines.append("")
    lines.append("3. **配置与依赖**：")
    lines.append("   - 检查 Nacos / 配置中心变更是否已同步到线上环境。")
    lines.append("   - 确认外部依赖、第三方服务、开关状态已就绪。")
    lines.append("")
    lines.append("4. **发布清单**：")
    lines.append("   - 列出本次上线涉及的服务、接口、数据库变更、配置项。")
    lines.append("   - 明确发布顺序、回滚方案、灰度策略。")
    lines.append("   - 标注需要人工复核或监控观察的关键点。")
    lines.append("")
    lines.append("5. **风险与回滚**：")
    lines.append("   - 识别潜在风险（性能、兼容性、数据一致性）。")
    lines.append("   - 给出可执行的回滚步骤和止损方案。")
    lines.append("")
    lines.append(
        "请直接输出一份结构化的《上线前检查清单》（Markdown 格式），并给出明确的通过/阻塞结论。"
    )

    if bundle.story_projects:
        lines.append("")
        lines.append("## 绑定项目与分支")
        for sp in bundle.story_projects:
            proj = _find_project(bundle.projects, sp.get("project_id"))
            name = proj.get("name", "") if proj else "(未知项目)"
            lines.append(f"- **{name}**：分支 `{sp.get('branch', '')}`")
            if sp.get("base_branch"):
                lines.append(f"  - 基线：`{sp.get('base_branch', '')}`")
            if sp.get("summary"):
                lines.append(f"  - 影响摘要：{sp.get('summary', '')}")

    if bundle.documents:
        lines.append("")
        lines.append("## 相关文档")
        for doc in bundle.documents:
            ref = doc.get("ref", "") or "(无路径)"
            lines.append(f"- **{doc.get('kind', '')}**：{ref}")
            if doc.get("summary"):
                lines.append(f"  - 摘要：{doc.get('summary', '')}")

    ddl = [ci for ci in bundle.change_items if ci.get("kind") == "ddl"]
    nacos = [ci for ci in bundle.change_items if ci.get("kind") == "nacos"]
    if ddl:
        lines.append("")
        lines.append("## DDL 变更")
        for ci in ddl:
            lines.append(f"- {ci.get('ref', '') or '(无路径)'}")
            if ci.get("summary"):
                lines.append(f"  - 摘要：{ci.get('summary', '')}")
    if nacos:
        lines.append("")
        lines.append("## Nacos 配置变更")
        for ci in nacos:
            lines.append(f"- **{ci.get('ref', '') or '(未命名配置)'}**")
            if ci.get("summary"):
                lines.append(f"  - 变更摘要：{ci.get('summary', '')}")

    _render_delivery_artifacts(lines, bundle.delivery_artifacts)

    _inject_flywheel_sections(lines, story_key, story.get("workspace", ""), story.get("current_stage", ""))

    return "\n".join(lines)


def generate_post_release_prompt(story_key: str) -> dict:
    """Render a post-release auto-verification prompt that can be pasted into a code AI session.

    Returns {"content": <markdown>, "story_key": story_key}.
    Raises ValueError if story not found.
    """
    from ...infra.db import models as db

    bundle = ContextResolver().resolve(story_key)
    content = _render_post_release_prompt(story_key, bundle)
    db.log_event(
        story_key,
        stage=bundle.story.get("current_stage", "") if bundle.story else "",
        event_type="post_release_prompt_generated",
        payload={"revision": bundle.revision},
    )
    return {"content": content, "story_key": story_key}


def _render_post_release_prompt(story_key: str, bundle) -> str:
    story = bundle.story or {}
    lines: list[str] = []

    lines.append("# 上线完成 - 自动验证")
    lines.append("")
    lines.append(f"Story：{story_key} - {story.get('title', '')}")
    tapd_url = story.get("tapd_url", "")
    if tapd_url:
        lines.append(f"TAPD：{tapd_url}")
    lines.append(f"工作区：{story.get('workspace', '')}")
    lines.append(
        f"Profile / 当前阶段：{story.get('profile', '')} / {story.get('current_stage', '')}"
    )
    lines.append("")

    lines.append(
        "本次上线已完成。请基于 Story 上下文，自动执行上线后验证并输出结论。具体任务如下："
    )
    lines.append("")
    lines.append("1. **服务健康检查**：")
    lines.append("   - 检查相关服务是否已正常启动，进程、端口、健康检查接口是否可用。")
    lines.append("   - 确认无异常重启、OOM、连接池耗尽等告警。")
    lines.append("")
    lines.append("2. **功能验证**：")
    lines.append("   - 根据 PRD / spec 中的验收标准，验证核心功能是否在线上正常生效。")
    lines.append("   - 优先验证本次变更涉及的关键接口、页面、定时任务或消息链路。")
    lines.append("   - 若涉及灰度/开关，确认灰度比例、命中规则符合预期。")
    lines.append("")
    lines.append("3. **配置与数据**：")
    lines.append("   - 确认 Nacos / 配置中心的变更已在线上生效。")
    lines.append("   - 若涉及 DDL/DML，确认表结构、索引、初始数据正确。")
    lines.append("   - 检查关键业务数据是否符合预期，无脏数据或遗漏。")
    lines.append("")
    lines.append("4. **监控与日志**：")
    lines.append("   - 查看上线后一段时间的 ERROR/WARN 日志，确认无新增异常。")
    lines.append("   - 检查核心指标（QPS、RT、成功率、错误率）是否平稳。")
    lines.append("   - 确认关键告警已消除或未误报。")
    lines.append("")
    lines.append("5. **回滚可达性**：")
    lines.append("   - 确认回滚方案仍可用（如版本包、数据库备份、配置历史）。")
    lines.append("   - 若验证失败，给出明确的回滚指令和止损步骤。")
    lines.append("")
    lines.append("请直接输出一份结构化的《上线后验证报告》（Markdown 格式），包括：")
    lines.append("- 验证项与结果（通过/失败/待观察）")
    lines.append("- 发现的问题及严重等级")
    lines.append("- 最终结论：上线成功 / 需要回滚 / 需要人工复核")
    lines.append("- 如需回滚，给出具体回滚命令或操作步骤")

    if bundle.story_projects:
        lines.append("")
        lines.append("## 绑定项目与分支")
        for sp in bundle.story_projects:
            proj = _find_project(bundle.projects, sp.get("project_id"))
            name = proj.get("name", "") if proj else "(未知项目)"
            lines.append(f"- **{name}**：分支 `{sp.get('branch', '')}`")
            if sp.get("base_branch"):
                lines.append(f"  - 基线：`{sp.get('base_branch', '')}`")
            if sp.get("summary"):
                lines.append(f"  - 影响摘要：{sp.get('summary', '')}")

    if bundle.documents:
        lines.append("")
        lines.append("## 相关文档")
        for doc in bundle.documents:
            ref = doc.get("ref", "") or "(无路径)"
            lines.append(f"- **{doc.get('kind', '')}**：{ref}")
            if doc.get("summary"):
                lines.append(f"  - 摘要：{doc.get('summary', '')}")

    ddl = [ci for ci in bundle.change_items if ci.get("kind") == "ddl"]
    nacos = [ci for ci in bundle.change_items if ci.get("kind") == "nacos"]
    if ddl:
        lines.append("")
        lines.append("## DDL 变更")
        for ci in ddl:
            lines.append(f"- {ci.get('ref', '') or '(无路径)'}")
            if ci.get("summary"):
                lines.append(f"  - 摘要：{ci.get('summary', '')}")
    if nacos:
        lines.append("")
        lines.append("## Nacos 配置变更")
        for ci in nacos:
            lines.append(f"- **{ci.get('ref', '') or '(未命名配置)'}**")
            if ci.get("summary"):
                lines.append(f"  - 变更摘要：{ci.get('summary', '')}")

    _render_delivery_artifacts(lines, bundle.delivery_artifacts)

    _inject_flywheel_sections(lines, story_key, story.get("workspace", ""), story.get("current_stage", ""))

    return "\n".join(lines)


def generate_bugfix_prompt(story_key: str, bug_key: str) -> dict:
    """Render a bug-fix prompt for a code AI based on the parent story context + bug details.

    Returns {"content": <markdown>, "story_key": story_key, "bug_key": bug_key}.
    Raises ValueError if story or bug not found.
    """
    from ...infra.db import models as db

    bundle = ContextResolver().resolve(story_key)
    bug = db.get_story(bug_key)
    if not bug:
        raise ValueError(f"bug not found: {bug_key}")
    content = _render_bugfix_prompt(story_key, bundle, bug)
    db.log_event(
        bug_key,
        stage=bug.get("current_stage", ""),
        event_type="bugfix_prompt_generated",
        payload={"story_key": story_key, "revision": bundle.revision},
    )
    return {"content": content, "story_key": story_key, "bug_key": bug_key}


def _render_bugfix_prompt(story_key: str, bundle, bug: dict) -> str:
    story = bundle.story or {}
    lines: list[str] = []

    lines.append("# 缺陷修复")
    lines.append("")
    lines.append(f"Story：{story_key} - {story.get('title', '')}")
    lines.append(f"缺陷：{bug.get('story_key', '')} - {bug.get('title', '')}")
    if bug.get("tapd_url"):
        lines.append(f"缺陷 TAPD：{bug.get('tapd_url')}")
    if story.get("tapd_url"):
        lines.append(f"Story TAPD：{story.get('tapd_url')}")
    lines.append(f"工作区：{story.get('workspace', '')}")
    lines.append(
        f"缺陷状态：本地 {bug.get('status', '')} / TAPD {bug.get('tapd_status', '')}"
    )
    lines.append(f"优先级：{bug.get('priority', '')}")
    if bug.get("owner"):
        lines.append(f"负责人：{bug.get('owner', '')}")
    lines.append("")

    lines.append("请基于以上 Story 的完整上下文修复这个缺陷。具体任务如下：")
    lines.append("")
    lines.append("1. **定位根因**：")
    lines.append("   - 复现缺陷现象，确认触发条件和影响范围。")
    lines.append("   - 结合 Story 上下文、代码变更历史、日志和监控，定位根因。")
    lines.append("   - 区分是本次 Story 引入的回归问题，还是历史遗留问题。")
    lines.append("")
    lines.append("2. **设计修复方案**：")
    lines.append("   - 给出至少两个可选方案，并说明选择理由。")
    lines.append("   - 评估修复对现有功能、接口、数据的影响。")
    lines.append("   - 如涉及配置/开关/灰度，说明如何兼容。")
    lines.append("")
    lines.append("3. **实施修复**：")
    lines.append("   - 在正确的分支上修改代码，保持最小改动。")
    lines.append("   - 补充或更新单元测试、集成测试用例覆盖该缺陷。")
    lines.append("   - 确保代码风格和现有代码一致。")
    lines.append("")
    lines.append("4. **验证修复**：")
    lines.append("   - 在本地或预发环境复现并验证缺陷已修复。")
    lines.append("   - 运行相关测试套件，确保无回归。")
    lines.append("   - 如涉及数据修复，给出数据订正脚本或步骤。")
    lines.append("")
    lines.append("5. **输出修复报告**：")
    lines.append("   - 根因分析")
    lines.append("   - 修复内容摘要")
    lines.append("   - 验证结果")
    lines.append("   - 是否需要回滚/灰度观察")
    lines.append("")
    lines.append(
        "请直接输出结构化的《缺陷修复报告》（Markdown 格式），并给出明确的修复完成/需要人工复核结论。"
    )

    if bundle.story_projects:
        lines.append("")
        lines.append("## 绑定项目与分支")
        for sp in bundle.story_projects:
            proj = _find_project(bundle.projects, sp.get("project_id"))
            name = proj.get("name", "") if proj else "(未知项目)"
            lines.append(f"- **{name}**：分支 `{sp.get('branch', '')}`")
            if sp.get("base_branch"):
                lines.append(f"  - 基线：`{sp.get('base_branch', '')}`")
            if sp.get("summary"):
                lines.append(f"  - 影响摘要：{sp.get('summary', '')}")

    if bundle.documents:
        lines.append("")
        lines.append("## 相关文档")
        for doc in bundle.documents:
            ref = doc.get("ref", "") or "(无路径)"
            lines.append(f"- **{doc.get('kind', '')}**：{ref}")
            if doc.get("summary"):
                lines.append(f"  - 摘要：{doc.get('summary', '')}")

    ddl = [ci for ci in bundle.change_items if ci.get("kind") == "ddl"]
    nacos = [ci for ci in bundle.change_items if ci.get("kind") == "nacos"]
    if ddl:
        lines.append("")
        lines.append("## DDL 变更")
        for ci in ddl:
            lines.append(f"- {ci.get('ref', '') or '(无路径)'}")
            if ci.get("summary"):
                lines.append(f"  - 摘要：{ci.get('summary', '')}")
    if nacos:
        lines.append("")
        lines.append("## Nacos 配置变更")
        for ci in nacos:
            lines.append(f"- **{ci.get('ref', '') or '(未命名配置)'}**")
            if ci.get("summary"):
                lines.append(f"  - 变更摘要：{ci.get('summary', '')}")

    _render_delivery_artifacts(lines, bundle.delivery_artifacts)

    _inject_flywheel_sections(
        lines,
        story_key,
        story.get("workspace", ""),
        story.get("current_stage", "") or bug.get("current_stage", "") or "verify",
    )

    return "\n".join(lines)


def generate_batch_bugfix_prompt(story_key: str, bug_keys: list[str]) -> dict:
    """Render a combined bug-fix prompt for multiple bugs under the same story.

    Shares the story context once, then lists each bug's specifics, avoiding
    redundant project/branch/document sections.
    """
    from ...infra.db import models as db

    bundle = ContextResolver().resolve(story_key)
    bugs: list[dict] = []
    for bug_key in bug_keys:
        bug = db.get_story(bug_key)
        if not bug:
            raise ValueError(f"bug not found: {bug_key}")
        if bug.get("tapd_type") != "bug":
            raise ValueError(f"not a bug: {bug_key}")
        bugs.append(bug)

    content = _render_batch_bugfix_prompt(story_key, bundle, bugs)
    db.log_event(
        story_key,
        stage=bundle.story.get("current_stage", "") if bundle.story else "",
        event_type="batch_bugfix_prompt_generated",
        payload={"bug_keys": bug_keys, "revision": bundle.revision},
    )
    return {"content": content, "story_key": story_key, "bug_keys": bug_keys}


def _render_batch_bugfix_prompt(story_key: str, bundle, bugs: list[dict]) -> str:
    story = bundle.story or {}
    lines: list[str] = []

    lines.append("# 批量缺陷修复")
    lines.append("")
    lines.append(f"Story：{story_key} - {story.get('title', '')}")
    if story.get("tapd_url"):
        lines.append(f"Story TAPD：{story.get('tapd_url')}")
    lines.append(f"工作区：{story.get('workspace', '')}")
    lines.append("")
    lines.append("本次需要一起修复以下缺陷：")
    lines.append("")

    for idx, bug in enumerate(bugs, 1):
        lines.append(f"## 缺陷 {idx}")
        lines.append(f"- **Key**：{bug.get('story_key', '')}")
        lines.append(f"- **标题**：{bug.get('title', '')}")
        if bug.get("tapd_url"):
            lines.append(f"- **TAPD**：{bug.get('tapd_url')}")
        lines.append(
            f"- **状态**：本地 {bug.get('status', '')} / TAPD {bug.get('tapd_status', '')}"
        )
        lines.append(f"- **优先级**：{bug.get('priority', '')}")
        if bug.get("owner"):
            lines.append(f"- **负责人**：{bug.get('owner', '')}")
        lines.append("")

    lines.append("请基于以上 Story 上下文和缺陷列表，按以下步骤批量修复：")
    lines.append("")
    lines.append("1. **分别定位每个缺陷的根因**：")
    lines.append("   - 逐个复现缺陷现象，确认触发条件和影响范围。")
    lines.append("   - 结合 Story 上下文、代码变更历史、日志和监控，分别定位根因。")
    lines.append("   - 判断这些缺陷是否由同一处代码/逻辑问题引起，能否统一修复。")
    lines.append("")
    lines.append("2. **设计整体修复方案**：")
    lines.append("   - 给出整体修复思路，以及每个缺陷的独立处理点。")
    lines.append("   - 评估修复对现有功能、接口、数据的影响。")
    lines.append("   - 如涉及配置/开关/灰度，说明如何兼容。")
    lines.append("")
    lines.append("3. **实施修复**：")
    lines.append("   - 在正确的分支上修改代码，保持最小改动。")
    lines.append("   - 优先尝试统一修复；如必须分别修复，说明原因。")
    lines.append("   - 补充或更新单元测试、集成测试用例覆盖这些缺陷。")
    lines.append("   - 确保代码风格和现有代码一致。")
    lines.append("")
    lines.append("4. **验证修复**：")
    lines.append("   - 在本地或预发环境逐个复现并验证缺陷已修复。")
    lines.append("   - 运行相关测试套件，确保无回归。")
    lines.append("   - 如涉及数据修复，给出数据订正脚本或步骤。")
    lines.append("")
    lines.append("5. **输出修复报告**：")
    lines.append("   - 每个缺陷的根因分析")
    lines.append("   - 修复内容摘要（区分统一修复/分别修复）")
    lines.append("   - 验证结果")
    lines.append("   - 是否需要回滚/灰度观察")
    lines.append("")
    lines.append(
        "请直接输出结构化的《批量缺陷修复报告》（Markdown 格式），并给出明确的修复完成/需要人工复核结论。"
    )

    if bundle.story_projects:
        lines.append("")
        lines.append("## 绑定项目与分支")
        for sp in bundle.story_projects:
            proj = _find_project(bundle.projects, sp.get("project_id"))
            name = proj.get("name", "") if proj else "(未知项目)"
            lines.append(f"- **{name}**：分支 `{sp.get('branch', '')}`")
            if sp.get("base_branch"):
                lines.append(f"  - 基线：`{sp.get('base_branch', '')}`")
            if sp.get("summary"):
                lines.append(f"  - 影响摘要：{sp.get('summary', '')}")

    if bundle.documents:
        lines.append("")
        lines.append("## 相关文档")
        for doc in bundle.documents:
            ref = doc.get("ref", "") or "(无路径)"
            lines.append(f"- **{doc.get('kind', '')}**：{ref}")
            if doc.get("summary"):
                lines.append(f"  - 摘要：{doc.get('summary', '')}")

    ddl = [ci for ci in bundle.change_items if ci.get("kind") == "ddl"]
    nacos = [ci for ci in bundle.change_items if ci.get("kind") == "nacos"]
    if ddl:
        lines.append("")
        lines.append("## DDL 变更")
        for ci in ddl:
            lines.append(f"- {ci.get('ref', '') or '(无路径)'}")
            if ci.get("summary"):
                lines.append(f"  - 摘要：{ci.get('summary', '')}")
    if nacos:
        lines.append("")
        lines.append("## Nacos 配置变更")
        for ci in nacos:
            lines.append(f"- **{ci.get('ref', '') or '(未命名配置)'}**")
            if ci.get("summary"):
                lines.append(f"  - 变更摘要：{ci.get('summary', '')}")

    _render_delivery_artifacts(lines, bundle.delivery_artifacts)

    _inject_flywheel_sections(lines, story_key, story.get("workspace", ""), story.get("current_stage", "") or "verify")

    return "\n".join(lines)


def _inject_flywheel_sections(
    lines: list[str], story_key: str, workspace: str, stage: str
) -> None:
    """Append mined knowledge + historical transcript sections to a prompt.

    Failsafe: both ``build_knowledge_section`` / ``build_transcript_section``
    never raise and return ``""`` when there is nothing to inject or the
    underlying provider/DB errors — in that case nothing is appended and the
    prompt is unchanged. The returned text already carries its own ``##`` header
    (e.g. ``## 飞轮知识上下文``), so we only prepend a blank separator line,
    mirroring the prompt-renderer path (see prompt_renderer.py).
    """
    kctx = build_knowledge_section(story_key, workspace, stage)
    if kctx and kctx.strip():
        lines.append("")
        lines.append(kctx.strip())

    tctx = build_transcript_section(story_key, workspace, stage)
    if tctx and tctx.strip():
        lines.append("")
        lines.append(tctx.strip())


def _render_delivery_artifacts(lines: list[str], artifacts: list[dict]) -> None:
    active = [a for a in artifacts if a.get("delivery_state") != "abandoned"]
    if not active:
        return
    lines.append("")
    lines.append("## 交付产物")
    for da in active:
        provider = da.get("provider", "") or ""
        kind = da.get("kind", "") or "other"
        label = provider if kind == "other" and provider else kind
        source = da.get("source_branch", "") or ""
        target = da.get("target_branch", "") or ""
        state = da.get("delivery_state", "") or ""
        evidence = (da.get("evidence_ref", "") or "").strip()
        ext = da.get("external_id", "") or da.get("url", "") or "(无标识)"
        lines.append(f"- **{label}**：{ext}")
        if source:
            lines.append(f"  - 构建分支：`{source}`")
        if target:
            lines.append(f"  - 部署环境：`{target}`")
        if state:
            lines.append(f"  - 状态：{state}")
        if evidence:
            lines.append(
                f"  - 备注：{evidence[:160]}{'...' if len(evidence) > 160 else ''}"
            )


def _find_project(projects: list[dict], project_id: int | None) -> dict | None:
    for p in projects:
        if p.get("id") == project_id:
            return p
    return None
