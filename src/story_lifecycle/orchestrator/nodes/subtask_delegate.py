"""Sub-story delegation — split parent story into sub-stories."""

from pathlib import Path

from langgraph.types import interrupt

from ...db import models as db
from ..paths import context_dir
from .state import StoryState


def _delegate_subtasks(state: StoryState, plan: dict) -> StoryState:
    """Split a parent story into sub-stories. Stores sub_keys in state for
    the graph runner to launch. Uses interrupt() to pause the parent.
    """
    import shutil

    parent_key = state["story_key"]
    workspace = state["workspace"]
    profile = state.get("profile", "minimal")
    stage = state["current_stage"]
    subtasks = plan["subtasks"]

    active_sub_keys = []
    for i, sub in enumerate(subtasks):
        sub_key = f"{parent_key}-{sub['key_suffix']}"
        has_deps = bool(sub.get("depends_on"))
        sub_status = "blocked" if has_deps else "active"

        db.upsert_story(
            sub_key,
            title=sub.get("title", ""),
            workspace=workspace,
            profile=profile,
            current_stage=stage,
            status=sub_status,
            parent_key=parent_key,
            subtask_index=i,
        )

        # Copy parent knowledge to sub-story (Windows-safe, no symlinks)
        parent_knowledge = Path(workspace) / ".story-knowledge" / parent_key
        sub_knowledge = Path(workspace) / ".story-knowledge" / sub_key
        if parent_knowledge.exists():
            sub_knowledge.mkdir(parents=True, exist_ok=True)
            for f in parent_knowledge.glob("*.md"):
                shutil.copy2(str(f), str(sub_knowledge / f.name))

        # Write per-subtask plan file
        plan_dir = context_dir(workspace, sub_key)
        plan_dir.mkdir(parents=True, exist_ok=True)
        plan_file = plan_dir / f"plan_{stage}.md"
        plan_file.write_text(
            f"# 子任务: {sub.get('title', '')}\n\n"
            f"## 所属 Story\n{parent_key} 的子任务 ({i + 1}/{len(subtasks)})\n\n"
            f"## 执行指令\n{sub.get('summary', '')}\n\n"
            f"## 约束\n这是子任务，只负责本模块的实现，不要修改其他模块。\n",
            encoding="utf-8",
        )

        if sub_status == "active":
            active_sub_keys.append(sub_key)

        db.log_event(
            parent_key,
            stage,
            "delegate",
            {
                "sub_key": sub_key,
                "title": sub.get("title", ""),
                "depends_on": sub.get("depends_on", []),
                "status": sub_status,
            },
        )

    # Store keys for graph runner to launch (no circular import)
    state["_pending_sub_keys"] = active_sub_keys
    state["status"] = "waiting_subtasks"
    state["plan_summary"] = f"拆分为 {len(subtasks)} 个子任务"
    db.update_story(parent_key, status="waiting_subtasks")
    db.log_event(
        parent_key,
        stage,
        "split",
        {
            "subtask_count": len(subtasks),
            "sub_keys": [f"{parent_key}-{s['key_suffix']}" for s in subtasks],
        },
    )

    # Pause parent via interrupt — resume_story will wake it up
    interrupt({"reason": "waiting_for_subtasks", "sub_count": len(subtasks)})
    return state
