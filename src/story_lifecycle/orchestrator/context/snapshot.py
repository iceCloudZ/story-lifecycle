"""Context Snapshot — generate versioned Markdown snapshots for AI sessions.

Writes to .story/context/<story_key>/story-context-r<revision>.md
"""

from __future__ import annotations

from pathlib import Path

from .resolver import ContextResolver, ContextBundle


def generate_snapshot(story_key: str) -> dict:
    """Generate a versioned context snapshot for a story.

    Returns a dict with:
        snapshot_path: absolute path to the snapshot file
        revision: the context_revision used
        story_key: the story key
    """
    from ...db import models as db

    resolver = ContextResolver()
    bundle = resolver.resolve(story_key)

    # Determine snapshot directory
    story = bundle.story
    workspace = story.get("workspace", "") if story else ""
    if workspace:
        snapshot_dir = Path(workspace) / ".story" / "context" / story_key
    else:
        snapshot_dir = Path(".story") / "context" / story_key

    snapshot_dir.mkdir(parents=True, exist_ok=True)

    revision = story.get("context_revision", 0) if story else 0
    snapshot_path = snapshot_dir / f"story-context-r{revision}.md"

    content = _render_snapshot(story_key, bundle, revision)
    snapshot_path.write_text(content, encoding="utf-8")

    # Log the event
    db.log_event(
        story_key,
        stage=story.get("current_stage", "") if story else "",
        event_type="context_snapshot_created",
        payload={
            "revision": revision,
            "snapshot_path": str(snapshot_path),
            "project_count": len(bundle.projects),
            "document_count": len(bundle.documents),
            "change_item_count": len(bundle.change_items),
            "delivery_artifact_count": len(bundle.delivery_artifacts),
        },
    )

    return {
        "snapshot_path": str(snapshot_path),
        "revision": revision,
        "story_key": story_key,
    }


def _render_snapshot(story_key: str, bundle: ContextBundle, revision: int) -> str:
    """Render a ContextBundle to a Markdown snapshot."""
    lines: list[str] = []

    story = bundle.story or {}
    profile = bundle.profile or {}
    current_stage = story.get("current_stage", "")

    # Find current stage definition
    stage_def: dict = {}
    for s in profile.get("stages", []):
        if isinstance(s, dict):
            if s.get("name") == current_stage:
                stage_def = s
                break
        elif isinstance(s, str) and s == current_stage:
            stage_def = {"name": s}
            break

    # Header
    lines.append("## Story 长期上下文")
    lines.append("")
    lines.append(f"- Story: {story_key}")
    lines.append(f"- Context Revision: {revision}")
    lines.append(f"- 标题: {story.get('title', '')}")

    tapd_url = story.get("tapd_url", "")
    if tapd_url:
        lines.append(f"- TAPD: {tapd_url}")

    profile_name = story.get("profile", "")
    lines.append(f"- Profile / Stage: {profile_name} / {current_stage}")
    lines.append(f"- Stage Goal: {stage_def.get('goal', '')}")
    lines.append(f"- Expected Outputs: {stage_def.get('expected_outputs', '')}")

    quality_gates = stage_def.get("quality_gates", [])
    if quality_gates:
        lines.append(f"- Quality Gates: {', '.join(quality_gates)}")
    lines.append("")

    # Projects
    for sp in bundle.story_projects:
        project = _find_project(bundle.projects, sp.get("project_id"))
        if not project:
            continue
        lines.append(f"### 项目：{project.get('name', 'unknown')}")
        lines.append("")
        lines.append(f"- 主仓库：{project.get('repo_path', '')}")
        lines.append(f"- 执行目录：{sp.get('worktree_path', '(未准备)')}")
        lines.append(f"- 分支：{sp.get('branch', '')}")
        lines.append(
            f"- 基线：{sp.get('base_branch', '')}@{sp.get('base_commit', 'HEAD')}"
        )
        summary = sp.get("summary", "")
        if summary:
            lines.append(f"- 影响摘要：{summary}")
        lines.append("")

    # Documents
    for doc in bundle.documents:
        lines.append("文档：")
        lines.append(f"- {doc.get('kind', '')}：{doc.get('ref', '')}")
        summary = doc.get("summary", "")
        if summary:
            lines.append(f"  摘要：{summary}")
        lines.append("")

    # Change items (DDL/Nacos)
    for ci in bundle.change_items:
        lines.append(f"{ci.get('kind', '').upper()}：")
        lines.append(f"- {ci.get('ref', '')}")
        lines.append(
            f"  状态：{ci.get('lifecycle_state', '')} / {ci.get('verification_state', '')}"
        )
        evidence = ci.get("evidence_ref", "")
        if evidence:
            lines.append(f"  证据：{evidence}")
        lines.append("")

    # Runtime facts
    for rf in bundle.runtime_facts:
        lines.append("运行时：")
        lines.append(
            f"- {rf.get('runtime_type', '')} {rf.get('runtime_version', '')}：{rf.get('availability', '')}"
        )
        dep = rf.get("dependency_ref", "")
        if dep:
            lines.append(f"  依赖：{dep}")
        lines.append("")

    # Delivery artifacts
    for da in bundle.delivery_artifacts:
        lines.append("交付：")
        lines.append(f"- {da.get('kind', '')} {da.get('external_id', '')}")
        lines.append(
            f"  状态：{da.get('delivery_state', '')} / {da.get('review_state', '')}"
        )
        target = da.get("target_branch", "")
        if target:
            lines.append(f"  目标分支：{target}")
        url = da.get("url", "")
        if url:
            lines.append(f"  URL：{url}")
        lines.append("")

    return "\n".join(lines)


def _find_project(projects: list[dict], project_id: int | None) -> dict | None:
    """Find a project by id in a list."""
    for p in projects:
        if p.get("id") == project_id:
            return p
    return None
