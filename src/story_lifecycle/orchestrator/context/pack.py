"""Context Pack — render a neutral, mixed-density markdown for injecting into any AI agent.

Mixed density: local files (PRD/spec/plan/DDL) given as paths the agent reads in its
worktree; non-local content (Nacos config, TAPD summary) inlined.
Neutral: states facts only, never issues "please implement" instructions.
"""

from __future__ import annotations

from .resolver import ContextResolver, ContextBundle


def generate_pack(story_key: str) -> dict:
    """Render a context pack for manual injection into an AI agent session.

    Returns {"content": <markdown>, "revision": N, "story_key": story_key}.
    Raises ValueError if story not found.
    """
    from ...db import models as db

    bundle = ContextResolver().resolve(story_key)
    content = _render_pack(story_key, bundle)
    db.log_event(
        story_key,
        stage=bundle.story.get("current_stage", "") if bundle.story else "",
        event_type="context_pack_generated",
        payload={"revision": bundle.revision},
    )
    return {"content": content, "revision": bundle.revision, "story_key": story_key}


def _render_pack(story_key: str, bundle: ContextBundle) -> str:
    story = bundle.story or {}
    lines: list[str] = []

    lines.append(f"# Story 上下文资料包：{story_key}")
    lines.append("")
    lines.append(f"- 标题：{story.get('title', '')}")
    tapd_url = story.get("tapd_url", "")
    if tapd_url:
        lines.append(f"- TAPD：{tapd_url}")
    lines.append(f"- Profile / Stage：{story.get('profile', '')} / {story.get('current_stage', '')}")
    lines.append(f"- Context Revision：{bundle.revision}")
    lines.append("")

    # 绑定项目与分支
    if bundle.story_projects:
        lines.append("## 绑定项目与分支")
        for sp in bundle.story_projects:
            proj = _find_project(bundle.projects, sp.get("project_id"))
            name = proj.get("name", "") if proj else "(未知项目)"
            lines.append(f"- **{name}**：分支 `{sp.get('branch', '')}`")
            wt = sp.get("worktree_path", "")
            if wt and not str(wt).startswith("_pending"):
                lines.append(f"  - worktree：`{wt}`")
            if sp.get("base_branch"):
                lines.append(f"  - 基线：`{sp.get('base_branch', '')}`")
            if sp.get("summary"):
                lines.append(f"  - 影响摘要：{sp.get('summary', '')}")
        lines.append("")

    # 文档（本地文件，给路径）
    if bundle.documents:
        lines.append("## 文档（在 worktree 内可读）")
        for doc in bundle.documents:
            ref = doc.get("ref", "") or "(无路径)"
            lines.append(f"- **{doc.get('kind', '')}**：{ref}")
            if doc.get("summary"):
                lines.append(f"  - 摘要：{doc.get('summary', '')}")
        lines.append("")

    # 变更项：DDL 给路径，Nacos 内联
    ddl = [ci for ci in bundle.change_items if ci.get("kind") == "ddl"]
    nacos = [ci for ci in bundle.change_items if ci.get("kind") == "nacos"]
    others = [ci for ci in bundle.change_items if ci.get("kind") not in ("ddl", "nacos")]
    if ddl:
        lines.append("## DDL（在 worktree 内可读）")
        for ci in ddl:
            lines.append(f"- {ci.get('ref', '') or '(无路径)'}")
            if ci.get("summary"):
                lines.append(f"  - 摘要：{ci.get('summary', '')}")
        lines.append("")
    if nacos:
        lines.append("## Nacos 配置变更（内联）")
        for ci in nacos:
            lines.append(f"- **{ci.get('ref', '') or '(未命名配置)'}**")
            if ci.get("summary"):
                lines.append(f"  - 变更摘要：{ci.get('summary', '')}")
            if ci.get("evidence_ref"):
                lines.append("  ```")
                lines.append(str(ci.get("evidence_ref", "")))
                lines.append("  ```")
        lines.append("")
    if others:
        lines.append("## 其他变更")
        for ci in others:
            lines.append(f"- **{ci.get('kind', '')}**：{ci.get('ref', '')}")
        lines.append("")

    # 交付产物
    if bundle.delivery_artifacts:
        lines.append("## 交付产物")
        for da in bundle.delivery_artifacts:
            url = da.get("url", "")
            lines.append(f"- **{da.get('kind', '')}**：{url or da.get('external_id', '')}")
            if da.get("target_branch"):
                lines.append(f"  - 目标分支：`{da.get('target_branch', '')}`")
        lines.append("")

    return "\n".join(lines)


def _find_project(projects: list[dict], project_id: int | None) -> dict | None:
    for p in projects:
        if p.get("id") == project_id:
            return p
    return None