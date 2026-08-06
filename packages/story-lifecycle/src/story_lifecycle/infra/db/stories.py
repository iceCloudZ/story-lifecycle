"""stories — story CRUD + driver claim（设计15 阶段B 拆分自 models.py）。"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .connection import _db, _validate_columns


def create_story(
    story_key: str,
    title: str,
    workspace: str,
    profile: str = "minimal",
    current_stage: str = "design",
    parent_key: str | None = None,
    subtask_index: int = 0,
) -> dict:
    with _db() as conn:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            """INSERT INTO story (story_key, title, workspace, profile, current_stage, status, created_at, updated_at, parent_key, subtask_index, intake_state)
               VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, 'ready')""",
            (
                story_key,
                title,
                str(workspace),
                profile,
                current_stage,
                now,
                now,
                parent_key,
                subtask_index,
            ),
        )
        row = conn.execute(
            "SELECT * FROM story WHERE story_key = ?", (story_key,)
        ).fetchone()
    return dict(row) if row else {}


def get_story(story_key: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM story WHERE story_key = ?", (story_key,)
        ).fetchone()
    return dict(row) if row else None


def find_by_source_id(source_type: str, source_id: str) -> dict | None:
    """Find a story by its external source type and ID (e.g. tapd, 1001234)."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM story WHERE source_type = ? AND source_id = ?",
            (source_type, source_id),
        ).fetchall()
    return dict(rows[0]) if rows else None


def list_active_stories() -> list[dict]:
    # TABS-LIFECYCLE-STATE: 改 lifecycle_state 驱动(原按 status IN (...) 过滤有双重
    # 过滤 bug — failed/paused 的开发态 story 被后端 SQL 丢掉,前端 tab 看不到)。
    # 非结项状态(待启动/开发/测试/上线)且已激活(ready)的 story 都算 active。
    with _db() as conn:
        rows = conn.execute(
            """SELECT * FROM story
               WHERE lifecycle_state IN ('待启动', '开发', '测试', '上线')
               AND intake_state = 'ready'
               AND deleted_at IS NULL
               ORDER BY updated_at DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


def list_candidate_stories() -> list[dict]:
    """Return candidate stories that need project binding before activation."""
    with _db() as conn:
        rows = conn.execute(
            """SELECT * FROM story WHERE intake_state = 'candidate'
               AND deleted_at IS NULL
               ORDER BY updated_at DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


def list_completed_stories(limit: int | None = None) -> list[dict]:
    # TABS-LIFECYCLE-STATE: 改 lifecycle_state 驱动。结项 = lifecycle_state='结项'
    # (原按 status IN ('completed','failed','aborted','archived') 过滤)。
    # limit 默认 None=全量(前端 DonePage 做分页,后端不再截断;旧的 limit=20/100 会
    # 把结项 story 砍到看不到 — 已结项 tab 只显示 ~21 个的根因之一)。
    sql = (
        "SELECT * FROM story WHERE lifecycle_state = '结项' "
        "AND deleted_at IS NULL ORDER BY updated_at DESC"
    )
    params: tuple = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    with _db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def list_visible_stories(
    show_all: bool = False,
    status: str = "",
    item_type: str = "",
    show_completed: bool = False,
    overdue: bool = False,
    show_test: bool = False,
) -> list[dict]:
    """Gather + filter stories for list views.

    Shared by the REST ``/api/story`` endpoint and the ``story list`` CLI so the
    two can't drift apart — the CLI previously omitted candidate stories that the
    API included, and COMPLETED_STATES was hardcoded in both places.

    show_all: include failed/aborted/archived stories (completed shows by default).
    status: filter by engine status (active/paused/completed/failed).
    item_type: filter by tapd_type (story/bug/subtask).
    show_completed: keep resolved/rejected/closed TAPD stories in the active/candidate
        pool (hidden by default). 结项 story (lifecycle_state='结项') 不受此过滤 —
        tapd closed 是结项的正常来源,不再被隐藏。
    overdue: only stories past their deadline.
    show_test: keep is_test=1 stories (hidden by default to keep worklist clean).
    """
    stories = list_active_stories() + list_candidate_stories()
    # TABS-LIFECYCLE-STATE: 结项 story 全量拉(原 limit=100 会截断,220 条只回 100 条)。
    stories = stories + list_completed_stories()
    # 三档(active/candidate/completed)按不同维度过滤,会重叠 — 例如一条 intake=candidate
    # 且 lifecycle=结项 的 story 会同时进 candidate 档和 completed 档。去重保首次出现顺序
    # (active > candidate > completed),避免列表里出现重复卡片。
    seen: set[str] = set()
    deduped: list[dict] = []
    for s in stories:
        k = s.get("story_key", "")
        if k and k not in seen:
            seen.add(k)
            deduped.append(s)
    stories = deduped

    if status:
        stories = [s for s in stories if s["status"] == status]
    if item_type:
        stories = [s for s in stories if s.get("tapd_type") == item_type]
    if not show_completed:
        # COMPLETED_STATES(tapd closed/resolved/rejected)过滤只作用于活跃/候选池 —
        # 隐藏「TAPD 已关闭但还没结项」的噪音。结项 story 不受此过滤:tapd closed 是
        # 结项的正常来源(CQRS 后结项判据已是 lifecycle_state='结项'),原无差别过滤会
        # 把合法结项 story 删掉(已结项 tab 只显示 ~21 个的根因之二)。
        stories = [
            s
            for s in stories
            if s.get("lifecycle_state") == "结项"
            or s.get("tapd_status") not in COMPLETED_STATES
        ]
    if overdue:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        stories = [s for s in stories if s.get("deadline") and s["deadline"][:10] < now]
    if not show_test:
        # is_test:0/1/NULL 都按「非测试」处理(not None / not 0 = keep)。
        # 老行迁移后 DEFAULT 0,新建真实数据 0,仅测试/demo 造的置 1。
        stories = [s for s in stories if not s.get("is_test")]
    return stories


def get_sub_stories(parent_key: str) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM story WHERE parent_key = ? ORDER BY subtask_index",
            (parent_key,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_stories_by_parent(parent_key: str, item_type: str = "") -> list[dict]:
    """List stories linked to a parent, optionally filtered by tapd_type."""
    with _db() as conn:
        if item_type:
            rows = conn.execute(
                "SELECT * FROM story WHERE parent_key = ? AND tapd_type = ? ORDER BY updated_at DESC",
                (parent_key, item_type),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM story WHERE parent_key = ? ORDER BY updated_at DESC",
                (parent_key,),
            ).fetchall()
    return [dict(r) for r in rows]


def list_unlinked_bugs() -> list[dict]:
    """List bugs that are not linked to any story."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM story WHERE tapd_type = 'bug' AND (parent_key IS NULL OR parent_key = '') ORDER BY updated_at DESC LIMIT 200",
        ).fetchall()
    return [dict(r) for r in rows]


def get_pending_parents() -> list[dict]:
    # waiting_subtasks 合并进 paused:用 status='paused' + ctx._pause_reason 鉴别。
    # SQL LIKE 粗筛(避免逐行 json.loads),调用方 resume_parent 会精确校验 ctx。
    with _db() as conn:
        rows = conn.execute(
            """SELECT * FROM story
               WHERE status = 'paused'
               AND context_json LIKE '%_pause_reason%'
               AND context_json LIKE '%waiting_subtasks%'"""
        ).fetchall()
    return [dict(r) for r in rows]


def update_story(story_key: str, **kwargs):
    """Update story fields. Always bumps updated_at."""
    if not kwargs:
        return
    _validate_columns(kwargs.keys())
    kwargs["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [story_key]
    with _db() as conn:
        conn.execute(f"UPDATE story SET {sets} WHERE story_key = ?", values)


def update_context(story_key: str, field: str, value: str):
    """Merge a single field into context_json."""
    with _db() as conn:
        row = conn.execute(
            "SELECT context_json FROM story WHERE story_key = ?", (story_key,)
        ).fetchone()
        if not row:
            return
        ctx = json.loads(row["context_json"] or "{}")
        ctx[field] = value
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE story SET context_json = ?, updated_at = ? WHERE story_key = ?",
            (json.dumps(ctx, ensure_ascii=False), now, story_key),
        )


def log_stage(story_key: str, stage: str, action: str, detail: str = ""):
    with _db() as conn:
        row = conn.execute(
            "SELECT id FROM story WHERE story_key = ?", (story_key,)
        ).fetchone()
        if not row:
            return
        conn.execute(
            "INSERT INTO stage_log (story_id, stage, action, detail) VALUES (?, ?, ?, ?)",
            (row["id"], stage, action, detail),
        )


def get_stage_logs(story_key: str, limit: int = 50) -> list[dict]:
    """Return recent stage_log rows for a story, newest first."""
    with _db() as conn:
        rows = conn.execute(
            """SELECT sl.* FROM stage_log sl
               JOIN story s ON s.id = sl.story_id
               WHERE s.story_key = ?
               ORDER BY sl.id DESC LIMIT ?""",
            (story_key, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_gate_results(story_key: str, limit: int = 20) -> list[dict]:
    """Return recent gate_result rows for a story, newest first."""
    with _db() as conn:
        rows = conn.execute(
            """SELECT gr.* FROM gate_result gr
               JOIN story s ON s.id = gr.story_id
               WHERE s.story_key = ?
               ORDER BY gr.id DESC LIMIT ?""",
            (story_key, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_story(story_key: str):
    with _db() as conn:
        row = conn.execute(
            "SELECT id FROM story WHERE story_key = ?", (story_key,)
        ).fetchone()
        if row:
            conn.execute("DELETE FROM stage_log WHERE story_id = ?", (row["id"],))
            conn.execute("DELETE FROM gate_result WHERE story_id = ?", (row["id"],))
            conn.execute("DELETE FROM story WHERE id = ?", (row["id"],))


def soft_delete_story(story_key: str) -> bool:
    """软删除:置 deleted_at 时间戳,行保留可 restore 恢复。

    卡片「删除」走这条(可恢复),区别于 delete_story()(物理删除,一次性脚本用)。
    返回是否命中(行不存在或已软删返回 False)。
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _db() as conn:
        cur = conn.execute(
            "UPDATE story SET deleted_at = ? "
            "WHERE story_key = ? AND deleted_at IS NULL",
            (now, story_key),
        )
        return cur.rowcount == 1


def restore_story(story_key: str) -> bool:
    """恢复软删除:清空 deleted_at。行不存在或未软删返回 False。"""
    with _db() as conn:
        cur = conn.execute(
            "UPDATE story SET deleted_at = NULL "
            "WHERE story_key = ? AND deleted_at IS NOT NULL",
            (story_key,),
        )
        return cur.rowcount == 1


def claim_story_driver(story_key: str, token: str) -> bool:
    """Optimistic CAS claim for cross-process driver mutual exclusion.

    ``NULL`` driver_claim = free; the first caller's conditional UPDATE wins and
    returns True; concurrent callers (other processes) see rowcount 0 and return
    False. SQLite serializes the UPDATE so exactly one caller wins. The in-process
    ``_running_stories`` dict can't see other processes (each python process has
    its own), so without this a second driver double-drives the same story
    (real-run 2026-07-06: event_log events appeared ×2).

    Dead-PID recovery: if the existing claim's PID is no longer alive (process
    crashed / was emergency-stopped / machine rebooted), the claim is stale and
    a new caller is allowed to seize it. Without this, a crashed driver's claim
    locks the story forever (real-run 2026-07-20: emergency_stop killed driver
    but driver_claim stayed → next confirm failed CAS → story stuck).
    Token format ``<pid>:<epoch>`` (see graph.start_story_async).
    """
    with _db() as conn:
        # fast path: free claim
        cur = conn.execute(
            "UPDATE story SET driver_claim = ? WHERE story_key = ? "
            "AND driver_claim IS NULL",
            (token, story_key),
        )
        if cur.rowcount == 1:
            return True
        # slow path: existing claim — is its PID dead? If so, seize.
        row = conn.execute(
            "SELECT driver_claim FROM story WHERE story_key = ?",
            (story_key,),
        ).fetchone()
        existing = (row or [None])[0]
        if not existing:
            return False  # row vanished (story deleted between calls)
        if not _driver_pid_alive(existing):
            cur = conn.execute(
                "UPDATE story SET driver_claim = ? "
                "WHERE story_key = ? AND driver_claim = ?",
                (token, story_key, existing),
            )
            return cur.rowcount == 1
        return False


def _driver_pid_alive(token: str) -> bool:
    """Check if the PID encoded in a driver_claim token is still running.

    Token format: ``<pid>:<epoch>``. Returns False if the PID is gone, the
    token is malformed, or the check is unsupported on this platform.

    Platform notes:
      - POSIX: ``os.kill(pid, 0)`` raises ``ProcessLookupError`` when the PID
        doesn't exist (and ``PermissionError`` when it exists but is not ours
        — treat as alive).
      - Windows: ``os.kill`` with signal 0 throws ``OSError: WinError 87``
        regardless of liveness, so use ``OpenProcess`` via ctypes — succeeds
        for live PIDs, fails (returns NULL) for dead ones.
    """

    try:
        pid = int(str(token).split(":", 1)[0])
    except (ValueError, AttributeError):
        return True  # malformed → don't seize (safer than guessing)
    if pid <= 0:
        return True  # sentinel / missing → don't seize

    if os.name == "nt":
        # Windows: OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION=0x1000, FALSE, pid).
        # Returns NULL (0) if the process doesn't exist; nonzero handle if alive.
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.OpenProcess.argtypes = (
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            )
            SYNCHRONIZE = 0x00100000
            handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if not handle:
                return False  # dead — GetLastError() usually ERROR_INVALID_PARAMETER
            kernel32.CloseHandle(handle)
            return True
        except Exception:
            # ctypes failure → can't tell, don't seize (safe default)
            return True

    # POSIX: use signal-0 probe.
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # PID exists but not ours — still alive
    except OSError:
        return True  # unsupported / unknown — don't seize


def release_story_driver(story_key: str, token: str) -> None:
    """Release the driver claim — only if it is still ours (token matches).

    Guards against releasing a claim a newer driver force-claimed after a crash.
    """
    with _db() as conn:
        conn.execute(
            "UPDATE story SET driver_claim = NULL "
            "WHERE story_key = ? AND driver_claim = ?",
            (story_key, token),
        )


def upsert_story(
    story_key: str,
    title: str = "",
    workspace: str = "",
    profile: str = "minimal",
    current_stage: str = "design",
    status: str = "active",
    **kwargs,
):
    """Insert or update a story. Used by service layer."""
    _validate_columns(kwargs.keys())
    with _db() as conn:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        existing = conn.execute(
            "SELECT id FROM story WHERE story_key = ?", (story_key,)
        ).fetchone()
        if existing:
            kwargs["updated_at"] = now
            # 复活软删除的 story:重新 create/upsert 同一个 key 视为恢复,
            # 清除 deleted_at(否则列表 SQL 的 deleted_at IS NULL 会过滤掉它)。
            kwargs["deleted_at"] = None
            if title:
                kwargs["title"] = title
            if status:
                kwargs["status"] = status
            if current_stage:
                kwargs["current_stage"] = current_stage
            if kwargs:
                sets = ", ".join(f"{k} = ?" for k in kwargs)
                values = list(kwargs.values()) + [story_key]
                conn.execute(f"UPDATE story SET {sets} WHERE story_key = ?", values)
        else:
            parent_key = kwargs.pop("parent_key", None)
            subtask_index = kwargs.pop("subtask_index", 0)
            conn.execute(
                """INSERT INTO story (story_key, title, workspace, profile, current_stage, status, intake_state, created_at, updated_at, parent_key, subtask_index)
                   VALUES (?, ?, ?, ?, ?, ?, 'ready', ?, ?, ?, ?)""",
                (
                    story_key,
                    title,
                    str(workspace),
                    profile,
                    current_stage,
                    status,
                    now,
                    now,
                    parent_key,
                    subtask_index,
                ),
            )


def upsert_story_from_source(
    source_type: str,
    source_id: str,
    title: str = "",
    workspace: str = "",
    profile: str = "minimal",
    current_stage: str = "design",
    # idle 移出 status:candidate 的"未启动"由 intake_state=candidate 表达,
    # status 用 active(DB DEFAULT 一致)。candidate 被 list_visible_stories 的
    # intake_state 过滤挡在四 tab 外(在 /tapd 页),status 值不参与 tab 判据。
    status: str = "active",
    intake_state: str = "candidate",
    deadline: str = "",
    priority: str = "",
    owner: str = "",
    tapd_status: str = "",
    tapd_url: str = "",
    tapd_type: str = "story",
    parent_key: str = "",
) -> tuple[dict, bool]:
    """Insert or update a story from an external source.

    For new stories, defaults to intake_state="candidate" and status="idle"
    to reflect the intake lifecycle. Existing stories are updated in place.

    Returns (story_dict, was_created).
    """
    existing = find_by_source_id(source_type, source_id)
    if not existing:
        # story_key may already exist without source_id linked (hand-created,
        # historical, or duplicate items in one sync batch). Fall back to a key
        # lookup so upsert stays idempotent instead of crashing on UNIQUE
        # story_key in create_story().
        key = f"{source_type}-{source_id}"
        existing_by_key = get_story(key)
        if existing_by_key:
            update_story(key, source_type=source_type, source_id=source_id)
            existing = existing_by_key
    if existing:
        updates = {}
        if title:
            updates["title"] = title
        if deadline:
            updates["deadline"] = deadline
        if priority:
            updates["priority"] = priority
        if owner:
            updates["owner"] = owner
        if tapd_status:
            updates["tapd_status"] = tapd_status
        if tapd_url:
            updates["tapd_url"] = tapd_url
        if tapd_type:
            updates["tapd_type"] = tapd_type
        if parent_key:
            updates["parent_key"] = parent_key
        # intake_state is a local lifecycle field, NOT TAPD-authoritative.
        # Never overwrite it on update — a user may have promoted the story to ready.
        if updates:
            update_story(existing["story_key"], **updates)
        return get_story(existing["story_key"]), False
    else:
        key = f"{source_type}-{source_id}"
        create_story(
            story_key=key,
            title=title,
            workspace=workspace or str(Path.cwd()),
            profile=profile,
            current_stage=current_stage,
        )
        update_story(
            key,
            source_type=source_type,
            source_id=source_id,
            status=status,
            intake_state=intake_state,
            deadline=deadline,
            priority=priority,
            owner=owner,
            tapd_status=tapd_status,
            tapd_url=tapd_url,
            tapd_type=tapd_type,
            parent_key=parent_key,
        )
        return get_story(key), True


COMPLETED_STATES = frozenset({"resolved", "rejected", "closed"})
