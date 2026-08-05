"""Story execution engine 鈥?workspace locking, thread pool, story lifecycle.

Previously built on LangGraph StateGraph. Now delegates to Agent-driven
execution (continue_orchestrator_agent) via Function Calling.
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from filelock import FileLock, Timeout

from ...infra.db import models as db
from ...sourcing.state_machine import (
    activate as sm_activate,
    mark_failed as sm_mark_failed,
    pause as sm_pause,
)

log = logging.getLogger("story-lifecycle.graph")

STORY_HOME = Path.home() / ".story-lifecycle"

_executor = ThreadPoolExecutor(max_workers=4)

# Execution guard 鈥?prevent double submission.
_running_stories: dict[str, int] = {}
_running_lock = threading.Lock()

# 灞? rescue:鍗曟 run_story 鍐呮崲 adapter 閲嶈窇鐨勬鏁颁笂闄?涓?rescue_story/decide_recovery 鍗忓悓)銆?
_MAX_RECOVERY = 3

# Workspace mutex 鈥?cross-process safe via filelock + in-process threading.Lock.
_workspace_locks_dir = STORY_HOME / "workspace-locks"
_workspace_locks_dir.mkdir(parents=True, exist_ok=True)
# In-process dict: each workspace gets a threading.Lock for same-process mutual exclusion
_ws_inproc_locks: dict[str, threading.Lock] = {}
# FileLock instances held by current process (to keep them locked across acquire/release cycles)
_ws_file_locks: dict[str, FileLock] = {}

# Run epoch 鈥?bumped on start/force-stop so stale threads detect cancellation
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

    # Both locks acquired 鈥?store references and owner info
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


def run_story(story_key: str, epoch: int = 0, claim_token: str = ""):
    """Run a story through the global orchestrator machinery (璁捐 13).

    璁捐13 鍓?driver 绾跨▼姹?submit 鈫?continue_orchestrator_agent 鍏ㄩ噺杞銆?
    璁捐13 鍚?鍏ㄥ眬缂栨帓绾跨▼(OrchestratorThread)绠＄悊鎵€鏈?story 鐨?PTY;杩欓噷淇濈暀涓?
    **鍚屾椹卞姩鍏ュ彛**(swebench CLI / 鏃?serve 鍦烘櫙),鍐呴儴璋?``drive_story_sync``
    鈥斺€?鍚屼竴濂?executors/handlers/judge,涓嶆槸绗簩濂楄皟搴︽満鍒躲€?

    灞? rescue Handler 淇濈暀:澶辫触 鈫?``decide_recovery`` 鈫?``rescue_story``
    鎹?adapter 鈫?鏈夌晫閲嶈窇(涓婇檺 ``_MAX_RECOVERY``)銆?

    ``claim_token`` is the driver_claim won by ``start_story_async``; it is
    released in ``finally`` so the story becomes drivable again on exit/crash
    of this run (only released if still ours). Callers that invoke ``run_story``
    directly (e.g. swebench) pass no token 鈫?no claim lifecycle.
    """
    import json as _json
    import traceback

    from ..scheduler import drive_story_sync

    story = db.get_story(story_key)
    workspace = story["workspace"] if story else ""

    acquired = False
    try:
        if workspace:
            acquired = acquire_workspace(workspace, story_key)

        # 鏈夌晫閲嶈瘯寰幆:鍙仮澶嶅け璐?鈫?鎹?adapter 閲嶈窇;涓嶅彲鎭㈠ / 瓒呬笂闄?鈫?鍋溿€?
        while True:
            try:
                drive_story_sync(story_key)
                return  # 鎴愬姛
            except Exception as exc:
                log.error(
                    f"run_story attempt failed for {story_key}:\n{traceback.format_exc()}"
                )
                # 寮傚父鍥炲啓(0d-D):涓嶆爣 failed 鍒欏穿婧?story 姘歌繙鍗?active銆?
                try:
                    sm_mark_failed(story_key, str(exc))
                except Exception:
                    log.exception("failed to mark story %s as failed", story_key)
                # 灞? recovery:鍐崇瓥鏁戞硶 + 钀?recovery_action 浜嬩欢(瀹¤ + 灞? 鍙嶆€濇暟鎹簮)
                from .recovery import decide_recovery, rescue_story

                story_rec = db.get_story(story_key) or {}
                stage_rec = story_rec.get("current_stage") or ""
                try:
                    prior_ctx = _json.loads(story_rec.get("context_json") or "{}")
                except Exception:
                    prior_ctx = {}
                attempt_count = int(prior_ctx.get("_recovery_attempt", 0)) + 1
                recovery = decide_recovery(
                    exc=exc,
                    story_facts={
                        "story_key": story_key,
                        "stage": stage_rec,
                        "priority": story_rec.get("priority") or "P2",
                        "workspace": story_rec.get("workspace") or "",
                    },
                    adapter="claude",  # 鍏滃簳;rescue_story 浼氭寜 ctx action 鐨?adapter 鎹?
                    attempt_count=attempt_count,
                )
                db.log_event(story_key, stage_rec, "recovery_action", recovery)
                if recovery.get("action") != "retry_new_adapter":
                    break  # 涓嶅彲鎭㈠(escalate/skip/downgrade)鈫?鍋?
                # rescue:鎹㈠け璐?stage 鐨?adapter + bump 璁℃暟
                rescue = rescue_story(
                    story_key=story_key,
                    recovery_decision=recovery,
                    ctx=prior_ctx,
                    current_stage=stage_rec,
                    max_attempts=_MAX_RECOVERY,
                )
                if not rescue.get("scheduled"):
                    break  # 瓒?_agent_actions 涓嶅尮閰?/ 瓒呬笂闄?鈫?鍋?story 宸?failed)
                try:
                    sm_activate(story_key, ctx_updates=prior_ctx)
                except Exception:
                    sm_activate(story_key)
                log.info(
                    "[%s] rescue: retry stage %s with %s (attempt %d/%d)",
                    story_key,
                    stage_rec,
                    rescue.get("new_adapter"),
                    rescue["attempt"],
                    _MAX_RECOVERY,
                )
                # loop 鈫?閲嶆柊 drive(璇绘洿鏂板悗鐨?ctx,鐢ㄦ柊 adapter)
        # 缁堟€佸け璐?鍐?graph_error.log
        err_file = STORY_HOME / "graph_error.log"
        try:
            err_file.write_text(
                f"run_story failed for {story_key}:\n{traceback.format_exc()}",
                encoding="utf-8",
            )
        except Exception:
            pass
    finally:
        if acquired and workspace:
            release_workspace(workspace, story_key, epoch)
        if claim_token:
            try:
                db.release_story_driver(story_key, claim_token)
            except Exception:
                log.exception("failed to release driver claim for %s", story_key)
        with _running_lock:
            if _running_stories.get(story_key) == epoch:
                _running_stories.pop(story_key, None)


def start_story_async(story_key: str):
    """Notify the global orchestrator that this story should start (璁捐 13).

    璁捐13 鍓?driver CAS 璁ら + 绾跨▼姹?submit run_story銆?
    璁捐13 鍚?缂栨帓绾跨▼(serve 閲屽父椹?姣忚疆鎵?active story,鍙戠幇鍗虫帴绠?鈥斺€?杩欓噷
    鍙渶纭繚 story 鏄?active(CAS 璁ら淇濈暀,闃?CLI 涓?serve 鍙岄┍鍔?缂栨帓绾跨▼
    鐨?tick 鏈韩涓嶈棰?鍙湁鍚屾 drive 璺緞鐢?銆?
    """
    story = db.get_story(story_key)
    if story and story.get("intake_state") == "candidate":
        log.info(
            f"start_story_async: {story_key} is candidate, skipping (must promote to ready)"
        )
        return

    # 缂栨帓绾跨▼鍦?serve 閲屽父椹?CLI(鏃?serve)璧板悓姝ラ┍鍔ㄣ€?
    try:
        from ..scheduler import is_orchestrator_running

        if is_orchestrator_running():
            # serve 鍦烘櫙:鍙繚璇?active,缂栨帓绾跨▼涓嬩竴杞彂鐜板苟鎺ョ銆?
            if story and story.get("status") != "active":
                sm_activate(story_key)
            return
    except Exception:
        pass

    # CLI/鏃?serve:淇濈暀鏃х殑鍚屾椹卞姩(CAS 璁ら + 绾跨▼姹?submit)銆?
    # Cross-process driver mutual exclusion (optimistic CAS, real-run 2026-07-06)銆?
    import os as _os
    import time as _time

    claim_token = f"{_os.getpid()}:{int(_time.time())}"
    if not db.claim_story_driver(story_key, claim_token):
        cur = db.get_story(story_key) or {}
        log.info(
            f"start_story_async: {story_key} already driven by another process "
            f"(driver_claim={cur.get('driver_claim')!r}); skipping (CAS lost)"
        )
        return

    with _running_lock:
        if story_key in _running_stories:
            # Re-entrant call within this process: another in-process path is
            # already driving. Release the DB claim we just won so we don't
            # strand it; defer to the existing in-process run.
            db.release_story_driver(story_key, claim_token)
            return
        _story_epochs[story_key] = _story_epochs.get(story_key, 0) + 1
        epoch = _story_epochs[story_key]
        _running_stories[story_key] = epoch

    log.info(
        f"Submitting story {story_key} to executor (epoch={epoch}) [claim={claim_token}]"
    )
    _executor.submit(run_story, story_key, epoch, claim_token)


def resume_story_async(story_key: str):
    """Resume a story (e.g. after server restart)."""
    start_story_async(story_key)


def recover_orphan_stories():
    """Recover stories left 'active' after a server restart.

    We do NOT auto-resume execution: relaunching the AI CLI on every restart was
    surprising and heavy (it silently re-spawned codex for each active story).
    Instead, mark such stories 'paused' so they surface in the UI with a manual
    '缁х画鎵ц' action. Candidates are already excluded by list_active_stories.
    """
    stories = [
        story
        for story in db.list_active_stories()
        if story.get("status") == "active" and story.get("intake_state") == "ready"
    ]
    for s in stories:
        sm_pause(s["story_key"])
    return len(stories)
