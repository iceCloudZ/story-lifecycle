"""LangGraph StateGraph — the orchestration engine."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver

from .nodes import (
    StoryState,
    plan_stage_node,
    execute_stage_node,
    poll_completion_node,
    review_stage_node,
    route_after_plan,
    route_after_poll,
    router_node,
    route_from_router,
    advance_node,
    retry_node,
    skip_node,
    fail_node,
    wait_confirm_node,
)
from ..db import models as db


STORY_HOME = Path.home() / ".story-lifecycle"
checkpoint_db = STORY_HOME / "checkpoint.db"

_executor = ThreadPoolExecutor(max_workers=4)


def build_graph() -> StateGraph:
    """Build and return the Story Lifecycle StateGraph."""
    graph = StateGraph(StoryState)

    graph.add_node("plan_stage", plan_stage_node)
    graph.add_node("execute_stage", execute_stage_node)
    graph.add_node("poll_completion", poll_completion_node)
    graph.add_node("review_stage", review_stage_node)
    graph.add_node("router", router_node)
    graph.add_node("advance", advance_node)
    graph.add_node("retry", retry_node)
    graph.add_node("skip_stage", skip_node)
    graph.add_node("fail_stage", fail_node)
    graph.add_node("wait_confirm", wait_confirm_node)

    graph.add_edge(START, "plan_stage")

    graph.add_conditional_edges(
        "plan_stage",
        route_after_plan,
        {"skip_stage": "skip_stage", "execute_stage": "execute_stage"},
    )

    graph.add_edge("execute_stage", "poll_completion")

    graph.add_conditional_edges(
        "poll_completion",
        route_after_poll,
        {"review_stage": "review_stage", "router": "router"},
    )

    graph.add_edge("review_stage", "router")

    graph.add_conditional_edges(
        "router",
        route_from_router,
        {
            "advance": "advance",
            "retry": "retry",
            "skip": "skip_stage",
            "fail": "fail_stage",
            "wait_confirm": "wait_confirm",
        },
    )

    graph.add_edge("advance", "plan_stage")
    graph.add_edge("retry", "plan_stage")
    graph.add_edge("skip_stage", "advance")
    graph.add_edge("fail_stage", END)
    graph.add_edge("wait_confirm", "plan_stage")

    return graph


def get_compiled_graph():
    """Return a compiled graph with SQLite checkpointer. Thread-safe."""
    import sqlite3

    checkpoint_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(checkpoint_db), check_same_thread=False)
    saver = SqliteSaver(conn)
    return build_graph().compile(checkpointer=saver)


def run_story(story_key: str):
    """Run a story's lifecycle. Blocks until interrupt or END."""
    import traceback
    import logging

    log = logging.getLogger("story-lifecycle.graph")

    try:
        _run_story_impl(story_key)
    except Exception:
        log.error(f"run_story failed for {story_key}:\n{traceback.format_exc()}")
        # Also write to a known file for debugging
        err_file = STORY_HOME / "graph_error.log"
        err_file.write_text(
            f"run_story failed for {story_key}:\n{traceback.format_exc()}",
            encoding="utf-8",
        )


def _run_story_impl(story_key: str):
    story = db.get_story(story_key)
    if not story:
        return

    import json

    # Load context from DB (includes prd_path, etc.)
    try:
        ctx = json.loads(story.get("context_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        ctx = {}

    initial_state: StoryState = {
        "story_key": story["story_key"],
        "title": story["title"] or "",
        "workspace": story["workspace"],
        "profile": story.get("profile", "minimal"),
        "current_stage": story["current_stage"],
        "status": story["status"],
        "complexity": story.get("complexity", ""),
        "context": ctx,
        "execution_count": story.get("execution_count", 0),
        "last_error": None,
        "stage_start_time": 0.0,
        "plan_summary": None,
        "review_summary": None,
        "trajectory_score": None,
        "plan": None,
    }

    config = {"configurable": {"thread_id": story_key}}
    compiled = get_compiled_graph()
    result = compiled.invoke(initial_state, config)

    # Launch sub-stories if plan_stage delegated (no circular import)
    if result and result.get("_pending_sub_keys"):
        for sub_key in result["_pending_sub_keys"]:
            _executor.submit(run_story, sub_key)


def resume_story(story_key: str):
    """Resume a story from interrupt. Non-blocking in TUI (called by Watchdog)."""
    config = {"configurable": {"thread_id": story_key}}
    compiled = get_compiled_graph()
    compiled.invoke(None, config)


def start_story_async(story_key: str):
    """Submit a story for execution. Non-blocking."""
    import logging

    log = logging.getLogger("story-lifecycle.graph")
    err_file = STORY_HOME / "graph_error.log"
    err_file.write_text(f"start_story_async called for {story_key}\n", encoding="utf-8")
    log.info(f"Submitting story {story_key} to executor")
    _executor.submit(run_story, story_key)


def recover_orphan_stories():
    """On startup, re-submit all active stories that lost their thread."""
    stories = db.list_active_stories()
    for s in stories:
        start_story_async(s["story_key"])
    return len(stories)
