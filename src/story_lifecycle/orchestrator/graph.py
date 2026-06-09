"""LangGraph StateGraph — the 5-node orchestration engine.

Nodes: plan_stage, execute_and_wait, review_stage, router, advance.
retry/skip/fail/wait_confirm are handled inside router_node.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from filelock import FileLock, Timeout
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver

from .nodes import (
    StoryState,
    plan_stage_node,
    execute_and_wait_node,
    review_stage_node,
    router_node,
    advance_node,
    route_after_plan,
    route_after_execute,
    route_from_router,
    route_after_advance,
)
from ..db import models as db


STORY_HOME = Path.home() / ".story-lifecycle"
checkpoint_db = STORY_HOME / "checkpoint.db"

_executor = ThreadPoolExecutor(max_workers=4)


# Execution guard — prevent double submission.
_running_stories: dict[str, int] = {}
_running_lock = threading.Lock()

# Workspace mutex — cross-process safe via filelock + in-process threading.Lock.
_workspace_locks_dir = STORY_HOME / "workspace-locks"
_workspace_locks_dir.mkdir(parents=True, exist_ok=True)
# In-process dict: each workspace gets a threading.Lock for same-process mutual exclusion
_ws_inproc_locks: dict[str, threading.Lock] = {}
# FileLock instances held by current process (to keep them locked across acquire/release cycles)
_ws_file_locks: dict[str, FileLock] = {}

# Run epoch — bumped on start/force-stop so stale threads detect cancellation
_story_epochs: dict[str, int] = {}


def _workspace_lock_path(workspace: str) -> Path:
    """Return filelock path for a workspace."""
    import hashlib

    h = hashlib.sha256(workspace.encode()).hexdigest()[:16]
    return _workspace_locks_dir / f"{h}.lock"


def acquire_workspace(workspace: str, story_key: str, exclude_story: str = "") -> bool:
    """Try to acquire workspace lock. Returns True if successful.

    Uses a hybrid approach:
    - threading.Lock for in-process mutual exclusion (same workspace can't be
      acquired by two threads in the same process)
    - filelock.FileLock for cross-process mutual exclusion
    """
    ws = str(workspace)
    # Step 1: In-process lock (non-blocking)
    inproc = _ws_inproc_locks.setdefault(ws, threading.Lock())
    if not inproc.acquire(blocking=False):
        return False

    # Step 2: Cross-process file lock
    lock_path = _workspace_lock_path(workspace)
    flock = FileLock(str(lock_path), timeout=0)
    try:
        flock.acquire()
    except Timeout:
        inproc.release()  # Release in-process lock since we failed
        if exclude_story:
            owner_file = lock_path.with_suffix(".owner")
            if (
                owner_file.exists()
                and owner_file.read_text(encoding="utf-8") == exclude_story
            ):
                return True
        return False

    # Both locks acquired — store references and owner info
    _ws_file_locks[ws] = flock
    lock_path.with_suffix(".owner").write_text(story_key, encoding="utf-8")
    return True


def _set_workspace_owner(workspace: str, story_key: str, epoch: int):
    """Update workspace lock owner info."""
    lock_path = _workspace_lock_path(workspace)
    owner_file = lock_path.with_suffix(".owner")
    owner_file.write_text(f"{story_key}:{epoch}", encoding="utf-8")


def release_workspace(workspace: str, story_key: str = "", epoch: int = 0):
    """Release workspace file lock."""
    ws = str(workspace)
    flock = _ws_file_locks.pop(ws, None)
    if flock:
        try:
            flock.release()
        except (Timeout, OSError):
            pass
    # Clean up owner file
    lock_path = _workspace_lock_path(workspace)
    owner_file = lock_path.with_suffix(".owner")
    if owner_file.exists():
        try:
            owner_file.unlink(missing_ok=True)
        except OSError:
            pass
    # Release in-process lock
    inproc = _ws_inproc_locks.get(ws)
    if inproc and inproc.locked():
        inproc.release()


def is_story_running(story_key: str) -> bool:
    with _running_lock:
        return story_key in _running_stories


def _running_epoch(story_key: str) -> int | None:
    with _running_lock:
        return _running_stories.get(story_key)


def force_stop_story(story_key: str) -> bool:
    import logging

    log = logging.getLogger("story-lifecycle.graph")
    with _running_lock:
        was_running = story_key in _running_stories
        _running_stories.pop(story_key, None)
        _story_epochs[story_key] = _story_epochs.get(story_key, 0) + 1
        log.warning(
            f"Force-stopped story {story_key} (guard released, epoch={_story_epochs[story_key]})"
        )
    return was_running


def is_workspace_locked(workspace: str, exclude_story: str = "") -> bool:
    """Check if workspace is locked by any process."""
    ws = str(workspace)
    # Check in-process lock first
    inproc = _ws_inproc_locks.get(ws)
    if inproc and inproc.locked():
        return True
    # Check cross-process file lock
    lock_path = _workspace_lock_path(workspace)
    try:
        flock = FileLock(str(lock_path), timeout=0)
        if not flock.is_locked:
            return False
        if exclude_story:
            owner_file = lock_path.with_suffix(".owner")
            if (
                owner_file.exists()
                and owner_file.read_text(encoding="utf-8") == exclude_story
            ):
                return False
        return True
    except (Timeout, OSError):
        return True


def get_epoch(story_key: str) -> int:
    with _running_lock:
        return _story_epochs.get(story_key, 0)


def is_epoch_current(story_key: str, epoch: int) -> bool:
    if not epoch:
        return True
    with _running_lock:
        return _story_epochs.get(story_key, 0) == epoch


def build_graph() -> StateGraph:
    """Build and return the 5-node Story Lifecycle StateGraph."""
    graph = StateGraph(StoryState)

    graph.add_node("plan_stage", plan_stage_node)
    graph.add_node("execute_and_wait", execute_and_wait_node)
    graph.add_node("review_stage", review_stage_node)
    graph.add_node("router", router_node)
    graph.add_node("advance", advance_node)

    graph.add_edge(START, "plan_stage")

    graph.add_conditional_edges(
        "plan_stage",
        route_after_plan,
        {
            "execute_and_wait": "execute_and_wait",
            "router": "router",
            "__end__": END,
        },
    )

    graph.add_conditional_edges(
        "execute_and_wait",
        route_after_execute,
        {"review_stage": "review_stage", "router": "router", "__end__": END},
    )

    graph.add_edge("review_stage", "router")

    graph.add_conditional_edges(
        "router",
        route_from_router,
        {
            "plan_stage": "plan_stage",
            "advance": "advance",
            "__end__": END,
        },
    )

    graph.add_conditional_edges(
        "advance",
        route_after_advance,
        {"plan_stage": "plan_stage", "router": "router", "__end__": END},
    )

    return graph


_compiled_graph = None
_compiled_graph_lock = threading.Lock()


def get_compiled_graph():
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
    import traceback
    import logging

    log = logging.getLogger("story-lifecycle.graph")
    story = db.get_story(story_key)
    workspace = story["workspace"] if story else ""

    acquired = False
    try:
        if workspace:
            acquired = acquire_workspace(workspace, story_key)

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
            if _running_stories.get(story_key) == epoch:
                _running_stories.pop(story_key, None)


def _run_story_impl(story_key: str, epoch: int = 0):
    story = db.get_story(story_key)
    if not story:
        return

    if epoch and not is_epoch_current(story_key, epoch):
        import logging

        logging.getLogger("story-lifecycle.graph").warning(
            f"Story {story_key} epoch {epoch} is stale (current {get_epoch(story_key)}), aborting"
        )
        return

    import json

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
        "_epoch": epoch,
        "_cancelled": False,
        "_resolved_profile": None,
    }

    # Resolve profile once at start
    try:
        from .nodes.profile_loader import resolve_profile

        rp = resolve_profile(story.get("profile", "minimal"))
        initial_state["_resolved_profile"] = rp.to_dict()
    except Exception:
        pass

    config = {"configurable": {"thread_id": story_key}}
    compiled = get_compiled_graph()
    compiled.invoke(initial_state, config)

    # Start any pending sub-stories
    try:
        snapshot = compiled.get_state(config)
        if snapshot and snapshot.values:
            pending = snapshot.values.get("_pending_sub_keys") or []
            for sub_key in pending:
                start_story_async(sub_key)
    except Exception:
        pass


def _restore_epoch_from_checkpoint(story_key: str) -> int:
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
    import logging

    log = logging.getLogger("story-lifecycle.graph")

    checkpoint_epoch = _restore_epoch_from_checkpoint(story_key)

    with _running_lock:
        if story_key in _running_stories:
            log.info(f"resume_story: {story_key} already running, skipping")
            return
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
            acquired = acquire_workspace(workspace, story_key)

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
    _executor.submit(resume_story, story_key)


def recover_orphan_stories():
    stories = db.list_active_stories()
    for s in stories:
        resume_story_async(s["story_key"])
    return len(stories)
