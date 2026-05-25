"""LangGraph StateGraph — the orchestration engine."""

import threading
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
    route_after_advance,
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

# TUI reference for cross-thread notifications (set by StoryBoardApp.on_mount)
_tui_app: object | None = None

# In-memory status bus — thread-safe, no file I/O
_status_lock = threading.Lock()
_plan_done: dict[str, tuple[str, bool]] = {}
_terminal_opened: set[str] = set()
_terminal_requests: dict[str, list[str]] = {}

# Execution guard — prevent double submission
_running_stories: set[str] = set()
_running_lock = threading.Lock()

# Workspace mutex — same workspace can only have one executing story
_workspace_locks: dict[str, threading.Lock] = {}


def acquire_workspace(workspace: str, story_key: str) -> bool:
    """Try to acquire workspace execution lock. Returns True if acquired."""
    ws = str(workspace)
    if ws not in _workspace_locks:
        _workspace_locks[ws] = threading.Lock()
    return _workspace_locks[ws].acquire(blocking=False)


def release_workspace(workspace: str):
    """Release workspace execution lock."""
    ws = str(workspace)
    lock = _workspace_locks.get(ws)
    if lock and lock.locked():
        lock.release()


def set_tui_app(app: object) -> None:
    global _tui_app
    _tui_app = app


def emit_plan_stream(story_key: str, chunk: str) -> None:
    pass


def emit_terminal_opened(story_key: str) -> None:
    """Signal that the CLI terminal has been opened."""
    with _status_lock:
        _terminal_opened.add(story_key)


def emit_terminal_request(story_key: str, args: list[str]) -> None:
    """Request TUI to hand over the real terminal for foreground execution."""
    with _status_lock:
        _terminal_requests[story_key] = args


def take_terminal_request(story_key: str) -> list[str] | None:
    """Atomically read and clear a terminal execution request."""
    with _status_lock:
        return _terminal_requests.pop(story_key, None)


def emit_plan_done(story_key: str, summary: str, ok: bool = True) -> None:
    """Signal that planning is complete."""
    with _status_lock:
        _plan_done[story_key] = (summary, ok)


def take_plan_done(story_key: str) -> tuple[str, bool] | None:
    """Atomically read and clear plan_done status."""
    with _status_lock:
        return _plan_done.pop(story_key, None)


def take_terminal_opened(story_key: str) -> bool:
    """Atomically read and clear terminal_opened status."""
    with _status_lock:
        if story_key in _terminal_opened:
            _terminal_opened.discard(story_key)
            return True
        return False


def is_story_running(story_key: str) -> bool:
    """Check if a story is currently being executed."""
    with _running_lock:
        return story_key in _running_stories


def force_stop_story(story_key: str) -> bool:
    """Force-remove a story from the running guard and release its workspace lock.

    Returns True if the story was running and was stopped.
    WARNING: The background thread may still be executing — this only releases
    the guard so a new execution can start. Use only for user-confirmed overrides.
    """
    import logging

    log = logging.getLogger("story-lifecycle.graph")
    with _running_lock:
        was_running = story_key in _running_stories
        if was_running:
            _running_stories.discard(story_key)
            log.warning(f"Force-stopped story {story_key} (guard released)")

    # Also release workspace lock if held
    story = db.get_story(story_key)
    if story and story.get("workspace"):
        release_workspace(story["workspace"])

    return was_running


def is_workspace_locked(workspace: str) -> bool:
    """Check if a workspace lock is currently held."""
    ws = str(workspace)
    lock = _workspace_locks.get(ws)
    return lock is not None and lock.locked()


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
        {
            "skip_stage": "skip_stage",
            "execute_stage": "execute_stage",
            "__end__": END,
            "router": "router",
        },
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

    graph.add_conditional_edges(
        "advance",
        route_after_advance,
        {"plan_stage": "plan_stage", "router": "router", "__end__": END},
    )
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
    story = db.get_story(story_key)
    workspace = story["workspace"] if story else ""

    acquired = False
    try:
        # Workspace mutex: block until workspace is free
        if workspace:
            ws_lock = _workspace_locks.setdefault(workspace, threading.Lock())
            ws_lock.acquire()
            acquired = True

        _run_story_impl(story_key)
    except Exception:
        log.error(f"run_story failed for {story_key}:\n{traceback.format_exc()}")
        err_file = STORY_HOME / "graph_error.log"
        err_file.write_text(
            f"run_story failed for {story_key}:\n{traceback.format_exc()}",
            encoding="utf-8",
        )
    finally:
        if acquired and workspace:
            ws_lock = _workspace_locks.get(workspace)
            if ws_lock:
                ws_lock.release()
        with _running_lock:
            _running_stories.discard(story_key)


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
        "_next_action": None,
        "_pending_sub_keys": None,
        "_router_decision": None,
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
    """Submit a story for execution. Non-blocking. Skips if already running."""
    import logging

    with _running_lock:
        if story_key in _running_stories:
            return
        _running_stories.add(story_key)

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
