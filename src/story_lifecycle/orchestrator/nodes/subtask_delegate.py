"""Sub-story delegation — split parent story into sub-stories via LangGraph Send."""

import shutil
from pathlib import Path

from langgraph.types import Send

from ...db import models as db
from ..paths import context_dir
from .state import StoryState


def _create_subtask_records(state: StoryState, plan: dict) -> list[dict]:
    """Create DB records, knowledge copies, and plan files for each subtask.

    Returns a list of dicts with sub_key, sub_status, and sub info for each
    active subtask (ready for Send dispatch).
    """
    parent_key = state["story_key"]
    workspace = state["workspace"]
    profile = state.get("profile", "minimal")
    stage = state["current_stage"]
    subtasks = plan["subtasks"]

    active_subs = []
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
            active_subs.append(
                {
                    "sub_key": sub_key,
                    "title": sub.get("title", ""),
                    "summary": sub.get("summary", ""),
                    "sub_status": sub_status,
                }
            )

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

    return active_subs


def build_subtask_sends(state: StoryState, plan: dict) -> list[Send]:
    """Build Send objects for fan-out subtask delegation via LangGraph.

    Creates DB records for each subtask, then returns a list of Send
    objects targeting "plan_stage" with sub-state for each active subtask.
    """
    parent_key = state["story_key"]
    stage = state["current_stage"]
    subtasks = plan["subtasks"]

    active_subs = _create_subtask_records(state, plan)

    # Mark parent as waiting and log the split event
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

    sends = []
    for sub_info in active_subs:
        # Build sub-state: copy parent state with overridden fields
        sub_state: StoryState = {
            "story_key": sub_info["sub_key"],
            "title": sub_info["title"],
            "workspace": state["workspace"],
            "profile": state.get("profile", "minimal"),
            "current_stage": stage,
            "status": "active",
            "complexity": state.get("complexity", ""),
            "context": dict(state.get("context", {})),
            "execution_count": 0,
            "last_error": None,
            "stage_start_time": 0.0,
            "plan_summary": sub_info["summary"],
            "review_summary": None,
            "trajectory_score": None,
            "plan": None,
            "_next_action": None,
            "_epoch": 0,
            "_cancelled": False,
        }
        sends.append(Send("plan_stage", sub_state))

    return sends


def merge_subtask_results(states: list[StoryState]) -> dict:
    """Merge results from fanned-in subtask states into a parent state update.

    Called after all subtasks complete to produce a single state update dict
    for the parent story.
    """
    if not states:
        return {}

    merged = {
        "status": "active",
        "plan_summary": None,
        "last_error": None,
        "context": {},
    }

    summaries = []
    errors = []
    for s in states:
        if s.get("plan_summary"):
            summaries.append(s["plan_summary"])
        if s.get("last_error"):
            errors.append(f"{s.get('story_key', '?')}: {s['last_error']}")
        # Merge context from each subtask
        for k, v in s.get("context", {}).items():
            merged["context"][k] = v

    if summaries:
        merged["plan_summary"] = "; ".join(summaries)
    if errors:
        merged["last_error"] = "; ".join(errors)

    return merged


def _delegate_subtasks(state: StoryState, plan: dict) -> StoryState:
    """Split a parent story into sub-stories. Updates state to reflect
    delegation. The graph_nodes layer should use build_subtask_sends() for
    the actual Send-based fan-out; this function is kept for backward
    compatibility and test support.
    """
    parent_key = state["story_key"]
    stage = state["current_stage"]
    subtasks = plan["subtasks"]

    active_subs = _create_subtask_records(state, plan)

    active_sub_keys = [s["sub_key"] for s in active_subs]
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

    # Store keys for test inspection (no longer used by graph runner)
    state["_pending_sub_keys"] = active_sub_keys
    return state
