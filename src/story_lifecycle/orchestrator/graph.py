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

# Execution guard — prevent double submission.
# Maps story_key → running_epoch. Only the owning epoch may clear the guard.
_running_stories: dict[str, int] = {}
_running_lock = threading.Lock()

# Workspace mutex — same workspace can only have one executing story.
# Each entry is {"lock": threading.Lock, "owner_token": (story_key, epoch)}.
# release_workspace is only honored when the caller's (story_key, epoch) matches owner_token.
_workspace_locks: dict[str, dict] = {}

# Run epoch — bumped on start/force-stop so stale threads detect cancellation
_story_epochs: dict[str, int] = {}


def acquire_workspace(workspace: str, story_key: str) -> bool:
    """Try to acquire workspace execution lock. Returns True if acquired."""
    ws = str(workspace)
    if ws not in _workspace_locks:
        _workspace_locks[ws] = {"lock": threading.Lock(), "owner_token": None}
    return _workspace_locks[ws]["lock"].acquire(blocking=False)


def _set_workspace_owner(workspace: str, story_key: str, epoch: int):
    """Record which (story_key, epoch) owns the workspace lock."""
    ws = str(workspace)
    if ws in _workspace_locks:
        _workspace_locks[ws]["owner_token"] = (story_key, epoch)


def release_workspace(workspace: str, story_key: str = "", epoch: int = 0):
    """Release workspace execution lock. Only honored if (story_key, epoch) matches
    the owner_token. If story_key is empty, backward-compat: skip owner check."""
    ws = str(workspace)
    entry = _workspace_locks.get(ws)
    if not entry:
        return
    owner = entry.get("owner_token")
    if story_key and owner:
        if owner != (story_key, epoch):
            return  # stale thread or wrong story
    if entry["lock"].locked():
        entry["lock"].release()


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


def _running_epoch(story_key: str) -> int | None:
    """Get the epoch of the currently-running instance, or None."""
    with _running_lock:
        return _running_stories.get(story_key)


def force_stop_story(story_key: str) -> bool:
    """Force-remove a story from the running guard.

    Returns True if the story was running and was stopped.
    Bumps the run epoch so any stale thread detects the cancellation.
    Does NOT release the workspace lock — the owning worker's finally block handles that.
    """
    import logging

    log = logging.getLogger("story-lifecycle.graph")
    with _running_lock:
        was_running = story_key in _running_stories
        # Force-remove regardless of epoch — this is an explicit user action
        _running_stories.pop(story_key, None)
        _story_epochs[story_key] = _story_epochs.get(story_key, 0) + 1
        log.warning(
            f"Force-stopped story {story_key} (guard released, epoch={_story_epochs[story_key]})"
        )

    return was_running


def is_workspace_locked(workspace: str, exclude_story: str = "") -> bool:
    """Check if a workspace lock is held by a *different* story.

    If *exclude_story* is given and it is the current owner, returns False
    (same story holding its own lock is not a conflict).
    """
    ws = str(workspace)
    entry = _workspace_locks.get(ws)
    if entry is None or not entry["lock"].locked():
        return False
    if exclude_story:
        owner = entry.get("owner_token")
        if owner and owner[0] == exclude_story:
            return False
    return True


def get_epoch(story_key: str) -> int:
    """Get current epoch for a story. Returns 0 if never tracked."""
    with _running_lock:
        return _story_epochs.get(story_key, 0)


def is_epoch_current(story_key: str, epoch: int) -> bool:
    """Check if the given epoch is still the current one."""
    if not epoch:
        return True
    with _running_lock:
        return _story_epochs.get(story_key, 0) == epoch


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
        {"review_stage": "review_stage", "router": "router", "__end__": END},
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
            "__end__": END,
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


_compiled_graph = None
_compiled_graph_lock = threading.Lock()


def get_compiled_graph():
    """Return a cached compiled graph with SQLite checkpointer. Thread-safe."""
    global _compiled_graph
    if _compiled_graph is not None:
        return _compiled_graph
    with _compiled_graph_lock:
        if _compiled_graph is not None:
            return _compiled_graph
        import sqlite3

        checkpoint_db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(checkpoint_db), check_same_thread=False)
        saver = SqliteSaver(conn)
        _compiled_graph = build_graph().compile(checkpointer=saver)
        return _compiled_graph


def run_story(story_key: str, epoch: int = 0):
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
            entry = _workspace_locks.setdefault(
                workspace, {"lock": threading.Lock(), "owner_token": None}
            )
            entry["lock"].acquire()
            entry["owner_token"] = (story_key, epoch)
            acquired = True

        _run_story_impl(story_key, epoch)
    except Exception:
        log.error(f"run_story failed for {story_key}:\n{traceback.format_exc()}")
        err_file = STORY_HOME / "graph_error.log"
        err_file.write_text(
            f"run_story failed for {story_key}:\n{traceback.format_exc()}",
            encoding="utf-8",
        )
    finally:
        if acquired and workspace:
            release_workspace(workspace, story_key, epoch)
        with _running_lock:
            # Only clear guard if our epoch is still the active one
            if _running_stories.get(story_key) == epoch:
                _running_stories.pop(story_key, None)


def _run_story_impl(story_key: str, epoch: int = 0):
    story = db.get_story(story_key)
    if not story:
        return

    # Stale-epoch guard: if force_stop_story bumped epoch, abort early.
    if epoch and not is_epoch_current(story_key, epoch):
        import logging

        logging.getLogger("story-lifecycle.graph").warning(
            f"Story {story_key} epoch {epoch} is stale (current {get_epoch(story_key)}), aborting"
        )
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
        "_epoch": epoch,
    }

    config = {"configurable": {"thread_id": story_key}}
    compiled = get_compiled_graph()
    result = compiled.invoke(initial_state, config)

    # Launch sub-stories if plan_stage delegated
    # Use start_story_async so every story goes through running guard + epoch
    if result and result.get("_pending_sub_keys"):
        if is_epoch_current(story_key, epoch):
            for sub_key in result["_pending_sub_keys"]:
                start_story_async(sub_key)


def _restore_epoch_from_checkpoint(story_key: str) -> int:
    """Read _epoch from the LangGraph checkpoint for story_key.

    Returns the checkpoint epoch, or 0 if unavailable (no checkpoint, error).
    This survives process restart — _story_epochs is memory-only but the
    checkpoint persists in SQLite.
    """
    try:
        config = {"configurable": {"thread_id": story_key}}
        compiled = get_compiled_graph()
        snapshot = compiled.get_state(config)
        if snapshot and snapshot.values:
            return snapshot.values.get("_epoch", 0) or 0
    except Exception:
        pass
    return 0


def resume_story(story_key: str):
    """Resume a story from interrupt. Non-blocking in TUI (called by Watchdog).

    Resume continues the SAME run — it does NOT bump the epoch. The checkpoint
    state holds the original _epoch; bumping _story_epochs would make
    _is_cancelled() fire on the very next node, causing self-cancellation.

    On process restart, _story_epochs is empty but the LangGraph checkpoint
    still holds the original _epoch. We read the checkpoint first and restore
    the in-memory epoch to prevent self-cancellation.
    """
    import logging

    log = logging.getLogger("story-lifecycle.graph")

    # Recover epoch from checkpoint (survives process restart)
    checkpoint_epoch = _restore_epoch_from_checkpoint(story_key)

    with _running_lock:
        if story_key in _running_stories:
            log.info(f"resume_story: {story_key} already running, skipping")
            return
        # Restore epoch from checkpoint if it's higher than memory
        mem_epoch = _story_epochs.get(story_key, 0)
        if checkpoint_epoch and checkpoint_epoch > mem_epoch:
            _story_epochs[story_key] = checkpoint_epoch
            epoch = checkpoint_epoch
        elif mem_epoch > 0:
            epoch = mem_epoch
        else:
            epoch = 1
            _story_epochs[story_key] = 1
        _running_stories[story_key] = epoch

    story = db.get_story(story_key)
    workspace = story["workspace"] if story else ""

    acquired = False
    try:
        if workspace:
            entry = _workspace_locks.setdefault(
                workspace, {"lock": threading.Lock(), "owner_token": None}
            )
            entry["lock"].acquire()
            entry["owner_token"] = (story_key, epoch)
            acquired = True

        config = {"configurable": {"thread_id": story_key}}
        compiled = get_compiled_graph()
        compiled.invoke(None, config)
    except Exception:
        log.exception(f"resume_story failed for {story_key}")
    finally:
        if acquired and workspace:
            release_workspace(workspace, story_key, epoch)
        with _running_lock:
            if _running_stories.get(story_key) == epoch:
                _running_stories.pop(story_key, None)


def start_story_async(story_key: str):
    """Submit a story for execution. Non-blocking. Skips if already running."""
    import logging

    with _running_lock:
        if story_key in _running_stories:
            return
        _story_epochs[story_key] = _story_epochs.get(story_key, 0) + 1
        epoch = _story_epochs[story_key]
        _running_stories[story_key] = epoch

    log = logging.getLogger("story-lifecycle.graph")
    try:
        STORY_HOME.mkdir(parents=True, exist_ok=True)
        err_file = STORY_HOME / "graph_error.log"
        err_file.write_text(
            f"start_story_async called for {story_key}\n", encoding="utf-8"
        )
    except Exception:
        pass
    log.info(f"Submitting story {story_key} to executor (epoch={epoch})")
    _executor.submit(run_story, story_key, epoch)


def resume_story_async(story_key: str):
    """Async wrapper: submits resume_story to the executor thread pool.

    Unlike resume_story() which blocks on compiled.invoke(), this returns
    immediately. Use for startup recovery to avoid blocking server init.
    """
    _executor.submit(resume_story, story_key)


def recover_orphan_stories():
    """On startup, resume all active stories via resume_story_async.

    Uses resume_story (not start_story_async) because orphaned stories have
    an existing checkpoint with a stored _epoch. resume_story reads the
    checkpoint epoch and restores it, preventing self-cancellation.

    Submits via executor so orphan recovery doesn't block server startup.
    """
    stories = db.list_active_stories()
    for s in stories:
        resume_story_async(s["story_key"])
    return len(stories)
