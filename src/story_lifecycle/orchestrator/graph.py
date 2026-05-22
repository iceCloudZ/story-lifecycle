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
        {"skip_stage": "skip_stage", "execute_stage": "execute_stage", "end": END},
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
        router_node,
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
    checkpoint_db.parent.mkdir(parents=True, exist_ok=True)
    saver = SqliteSaver.from_conn_string(str(checkpoint_db))
    return build_graph().compile(checkpointer=saver)


def run_story(story_key: str):
    """Run a story's lifecycle. Blocks until interrupt or END."""
    story = db.get_story(story_key)
    if not story:
        return

    initial_state: StoryState = {
        "story_key": story["story_key"],
        "title": story["title"] or "",
        "workspace": story["workspace"],
        "profile": story.get("profile", "minimal"),
        "current_stage": story["current_stage"],
        "status": story["status"],
        "complexity": story.get("complexity", ""),
        "context": {},
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
    compiled.invoke(initial_state, config)


def resume_story(story_key: str):
    """Resume a story from interrupt. Non-blocking in TUI (called by Watchdog)."""
    config = {"configurable": {"thread_id": story_key}}
    compiled = get_compiled_graph()
    compiled.invoke(None, config)


def start_story_async(story_key: str):
    """Submit a story for execution. Non-blocking."""
    _executor.submit(run_story, story_key)


def recover_orphan_stories():
    """On startup, re-submit all active stories that lost their thread."""
    stories = db.list_active_stories()
    for s in stories:
        start_story_async(s["story_key"])
    return len(stories)
