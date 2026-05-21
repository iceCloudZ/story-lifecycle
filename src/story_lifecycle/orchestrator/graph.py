"""LangGraph StateGraph — the orchestration engine."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver

from .nodes import (
    StoryState,
    execute_stage_node, poll_completion_node, router_node,
    advance_node, retry_node, skip_node, fail_node, wait_confirm_node,
)
from ..db import models as db


STORY_HOME = Path.home() / ".story-lifecycle"
checkpoint_db = STORY_HOME / "checkpoint.db"

# Thread pool for parallel story execution
_executor = ThreadPoolExecutor(max_workers=4)


def build_graph() -> StateGraph:
    """Build and return the Story Lifecycle StateGraph."""
    graph = StateGraph(StoryState)

    # Nodes
    graph.add_node("execute_stage", execute_stage_node)
    graph.add_node("poll_completion", poll_completion_node)
    graph.add_node("router", router_node)
    graph.add_node("advance", advance_node)
    graph.add_node("retry", retry_node)
    graph.add_node("skip_stage", skip_node)
    graph.add_node("fail_stage", fail_node)
    graph.add_node("wait_confirm", wait_confirm_node)

    # Edges
    graph.add_edge(START, "execute_stage")
    graph.add_edge("execute_stage", "poll_completion")

    # Conditional: router decides next step
    graph.add_conditional_edges(
        "poll_completion",
        router_node,
        {
            "advance": "advance",
            "retry": "retry",
            "skip": "skip_stage",
            "fail": "fail_stage",
            "wait_confirm": "wait_confirm",
        }
    )

    # After action, loop back or end
    graph.add_edge("advance", "execute_stage")     # next stage
    graph.add_edge("retry", "execute_stage")        # redo current
    graph.add_edge("skip_stage", "advance")         # skip → advance
    graph.add_edge("fail_stage", END)               # blocked
    graph.add_edge("wait_confirm", END)             # paused, resume manually

    return graph


def run_story(story_key: str):
    """Run a story's lifecycle in a dedicated thread. Blocks until completion."""
    story = db.get_story(story_key)
    if not story:
        return

    graph = build_graph()

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
    }

    config = {"configurable": {"thread_id": story_key}}

    checkpoint_db.parent.mkdir(parents=True, exist_ok=True)
    with SqliteSaver.from_conn_string(str(checkpoint_db)) as saver:
        graph.compile(checkpointer=saver).invoke(initial_state, config)


def start_story_async(story_key: str):
    """Submit a story for execution. Non-blocking."""
    _executor.submit(run_story, story_key)


def recover_orphan_stories():
    """On startup, re-submit all active stories that lost their thread."""
    stories = db.list_active_stories()
    for s in stories:
        start_story_async(s["story_key"])
    return len(stories)
