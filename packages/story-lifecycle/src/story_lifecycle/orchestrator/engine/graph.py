"""Story execution engine — workspace locking, thread pool, story lifecycle.

Previously built on LangGraph StateGraph. Now delegates to Agent-driven
execution (continue_orchestrator_agent) via Function Calling.
"""

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from filelock import FileLock, Timeout

from . import planner
from ...infra.db import models as db

log = logging.getLogger("story-lifecycle.graph")

STORY_HOME = Path.home() / ".story-lifecycle"

_executor = ThreadPoolExecutor(max_workers=4)

# Execution guard — prevent double submission.
_running_stories: dict[str, int] = {}
_running_lock = threading.Lock()

# 层3 rescue:单次 run_story 内换 adapter 重跑的次数上限(与 rescue_story/decide_recovery 协同)。
_MAX_RECOVERY = 3

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
    """Run a story through the Agent execution loop.

    Replaces the old LangGraph StateGraph invocation. 层3 rescue Handler:
    失败 → ``decide_recovery`` → ``rescue_story`` 换 adapter → 有界重跑(上限 ``_MAX_RECOVERY``)。

    ``claim_token`` is the driver_claim won by ``start_story_async``; it is
    released in ``finally`` so the story becomes drivable again on exit/crash
    of this run (only released if still ours). Callers that invoke ``run_story``
    directly (e.g. swebench) pass no token → no claim lifecycle.
    """
    import json as _json
    import traceback

    story = db.get_story(story_key)
    workspace = story["workspace"] if story else ""

    acquired = False
    try:
        if workspace:
            acquired = acquire_workspace(workspace, story_key)

        # 有界重试循环:可恢复失败 → 换 adapter 重跑;不可恢复 / 超上限 / 无 _agent_actions → 停。
        while True:
            try:
                planner.continue_orchestrator_agent(story_key)
                return  # 成功
            except Exception as exc:
                log.error(
                    f"run_story attempt failed for {story_key}:\n{traceback.format_exc()}"
                )
                # 异常回写(0d-D):不标 failed 则崩溃 story 永远卡 active。
                try:
                    db.update_story(
                        story_key, status="failed", last_error=str(exc)[:500]
                    )
                except Exception:
                    log.exception("failed to mark story %s as failed", story_key)
                # 层3 recovery:决策救法 + 落 recovery_action 事件(审计 + 层5 反思数据源)
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
                    adapter="claude",  # 兜底;rescue_story 会按 ctx action 的 adapter 换
                    attempt_count=attempt_count,
                )
                db.log_event(story_key, stage_rec, "recovery_action", recovery)
                if recovery.get("action") != "retry_new_adapter":
                    break  # 不可恢复(escalate/skip/downgrade)→ 停
                # rescue:换失败 stage 的 adapter + bump 计数
                rescue = rescue_story(
                    story_key=story_key,
                    recovery_decision=recovery,
                    ctx=prior_ctx,
                    current_stage=stage_rec,
                    max_attempts=_MAX_RECOVERY,
                )
                if not rescue.get("scheduled"):
                    break  # 超 _agent_actions 不匹配 / 超上限 → 停(story 已 failed)
                try:
                    db.update_story(
                        story_key,
                        context_json=_json.dumps(prior_ctx, ensure_ascii=False),
                        status="implementing",
                    )
                except Exception:
                    db.update_story(story_key, status="implementing")
                log.info(
                    "[%s] rescue: retry stage %s with %s (attempt %d/%d)",
                    story_key,
                    stage_rec,
                    rescue.get("new_adapter"),
                    rescue["attempt"],
                    _MAX_RECOVERY,
                )
                # loop → 重新调 planner(读更新后的 ctx,用新 adapter)
        # 终态失败:写 graph_error.log
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
    """Submit a story for execution in the thread pool.

    If the story has _agent_actions in context, it runs through
    continue_orchestrator_agent. Otherwise it auto-generates a plan first.
    """
    story = db.get_story(story_key)
    if story and story.get("intake_state") == "candidate":
        log.info(
            f"start_story_async: {story_key} is candidate, skipping (must promote to ready)"
        )
        return

    # Cross-process driver mutual exclusion (optimistic CAS, real-run 2026-07-06).
    # The in-process _running_stories dict below can't see OTHER processes — each
    # python process has its own — so two drivers (e.g. tmp_drive_minimal + a
    # serve worker) both passed the old guard and double-drove the same story
    # (event_log events appeared ×2, interleaved per stage). Claim atomically in
    # the SHARED DB: only one caller's UPDATE (driver_claim NULL -> token)
    # succeeds; the loser returns here. See db.claim_story_driver.
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


def find_ready_interactive_stories() -> list[str]:
    """Return active interactive stories whose done file is ready."""
    from ...infra.paths import stage_done_file

    ready = []
    for story in db.list_active_stories():
        if story.get("status") != "active":
            continue
        try:
            context = json.loads(story.get("context_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        marker = context.get("_active_execution")
        if not isinstance(marker, dict):
            continue
        if marker.get("mode") != "interactive_pty":
            continue
        stage = story.get("current_stage", "")
        if marker.get("stage") != stage:
            continue
        if is_story_running(story["story_key"]):
            continue
        if stage_done_file(
            story.get("workspace", ""),
            story["story_key"],
            stage,
        ).exists():
            ready.append(story["story_key"])
    return ready


def order_ready_stories(story_keys: list[str]) -> list[str]:
    """层5 scheduler:把 ready story 按 decide_schedule(优先级+就绪+FIFO)排序。

    替 ``resume_ready_interactive_stories`` 原本的 FIFO 提交序。查不到行的 key(已删)丢弃。
    """
    from .scheduler import decide_schedule

    stories: list[dict] = []
    for k in story_keys:
        row = db.get_story(k)
        if not row:
            continue
        row["ready"] = True  # 都已就绪(done file 在)
        stories.append(row)
    return decide_schedule(stories=stories)


def resume_ready_interactive_stories() -> list[str]:
    """Submit interactive stories that have produced a done file.

    层5 scheduler:按优先级排序提交(替 FIFO)。
    """
    ready = find_ready_interactive_stories()
    ordered = order_ready_stories(ready)
    for story_key in ordered:
        resume_story_async(story_key)
    return ordered


def recover_orphan_stories():
    """Recover stories left 'active' after a server restart.

    We do NOT auto-resume execution: relaunching the AI CLI on every restart was
    surprising and heavy (it silently re-spawned codex for each active story).
    Instead, mark such stories 'paused' so they surface in the UI with a manual
    '继续执行' action. Candidates are already excluded by list_active_stories.
    """
    stories = [
        story
        for story in db.list_active_stories()
        if story.get("status") == "active" and story.get("intake_state") == "ready"
    ]
    for s in stories:
        db.update_story(s["story_key"], status="paused")
    return len(stories)
