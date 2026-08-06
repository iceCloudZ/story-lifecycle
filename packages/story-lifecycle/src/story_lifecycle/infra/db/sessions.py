"""sessions — agent 会话 CRUD + sid（设计15 阶段B 拆分自 models.py）。"""

from __future__ import annotations

from datetime import datetime, timezone

from .connection import _db


def compute_session_id(story_key: str, stage: str, adapter: str) -> str:
    """Deterministic session id for a (story_key, stage, adapter) triple.

    全仓库唯一一处算 claude 的 session-id uuid5 —— 保证无论走哪条 spawn 路径
    (api 交互式 / planner 自动循环),同一个 (story, stage, adapter) 算出
    **同一个** session id,这样 resume 时 claude ``--resume <sid>`` 能对上历史。

    DESIGN-session-pty-id-model.md §3.5 / 问题 4:此前 api.py 用
    ``f"{story}:{stage}"``(2 字段)、planner.py 用 ``f"{story}:{stage}:{adapter}"``
    (3 字段),同 stage 算出不同 uuid → resume 续不上历史。统一为 3 字段。

    只用于 claude(kimi 不支持预指定 id,见 §2.5.3),但签名带 adapter 以与
    story_session 的 ``UNIQUE(story_key, stage, adapter)`` 约束对齐。
    """
    import uuid as _uuid

    return str(_uuid.uuid5(_uuid.NAMESPACE_DNS, f"{story_key}:{stage}:{adapter}"))


def get_session(story_key: str, stage: str, adapter: str) -> dict | None:
    """Return the persisted session row for (story_key, stage, adapter), or None.

    用于 spawn 前判断该阶段是否已有会话可 resume。session_id 非空即代表已建过会话。
    """
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM story_session "
            "WHERE story_key = ? AND stage = ? AND adapter = ?",
            (story_key, stage, adapter),
        ).fetchone()
    return dict(row) if row else None


def list_sessions_for_story(story_key: str) -> list[dict]:
    """Return all session rows for a story, ordered by id (insertion order).

    GET /sessions 用:返回每个 (stage, adapter) 的会话记录,带真实的 stage 字段
    (内存 PTY 注册表的 stage 是硬编码空的,这里从 DB 读真实值)。
    """
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM story_session WHERE story_key = ? ORDER BY id",
            (story_key,),
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_session(
    story_key: str,
    stage: str,
    adapter: str,
    session_id: str | None = None,
    status: str = "active",
) -> None:
    """Insert or update the session row for (story_key, stage, adapter).

    session_id=None 时仅建占位行(kimi 在捕获到 banner 的 id 后再 set_session_id 回填)。
    新插入 status 默认 active;重试/崩溃恢复时复用同一行(ON CONFLICT 更新 updated_at)。
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _db() as conn:
        conn.execute(
            "INSERT INTO story_session "
            "(story_key, stage, adapter, session_id, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(story_key, stage, adapter) DO UPDATE SET "
            "session_id = COALESCE(excluded.session_id, story_session.session_id), "
            "updated_at = excluded.updated_at",
            (story_key, stage, adapter, session_id, status, now, now),
        )


def set_session_id(story_key: str, stage: str, adapter: str, session_id: str) -> None:
    """回填 kimi 捕获到的 session_id(banner 解析成功后调用)。

    专给 kimi 用:claude 在 spawn 前就给 uuid5(upsert_session 带 session_id),
    kimi 必须先 spawn 再从输出捕获,故拆出这个单字段更新。
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _db() as conn:
        conn.execute(
            "UPDATE story_session SET session_id = ?, updated_at = ? "
            "WHERE story_key = ? AND stage = ? AND adapter = ?",
            (session_id, now, story_key, stage, adapter),
        )


def complete_session(story_key: str, stage: str, adapter: str) -> None:
    """标记阶段会话完成(stage done 后调用)。语义:该阶段任务结束;不影响 resume
    (同 stage 仍可 resume 续上历史,只是跨 stage 不共享)。"""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _db() as conn:
        conn.execute(
            "UPDATE story_session SET status = 'completed', updated_at = ? "
            "WHERE story_key = ? AND stage = ? AND adapter = ?",
            (now, story_key, stage, adapter),
        )


def set_session_completion_summary(
    story_key: str, stage: str, adapter: str, summary: str
) -> None:
    """存 stage 完成摘要(设计12 改动3:judge_stage_completion 的 summary)。

    供前端 TerminalTab 展示「本轮完成:...」。无对应 session 行(headless 路径没建
    session)时静默 no-op —— 摘要缺失不阻断主流程。
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _db() as conn:
        conn.execute(
            "UPDATE story_session SET completion_summary = ?, updated_at = ? "
            "WHERE story_key = ? AND stage = ? AND adapter = ?",
            (summary, now, story_key, stage, adapter),
        )


def delete_session(story_key: str, stage: str = "", adapter: str = "") -> None:
    """删除 story_session 记录,供 kill_pty 杀进程后清悬空记录用。

    防御「kill 进程不清 DB → spawn resume 拿死 sid 崩」(real-run 2026-07-28
    tapd-1144381896001066735 事件:claude 被紧急停止,story_session 行残留
    status=active,下次 confirm spawn 走 resume 传死 sid,claude 立报
    "No conversation found" 秒退)。违反 driver 不变式「CLI 生命周期 ⊆ driver
    生命周期」,此处补上缺失的「杀进程 = 清记录」环节。

    给定 (story_key, stage, adapter) 删单条;只给 story_key 删该 story 全部行;
    对齐 kill_pty 的「单 sid / 全 story」两种签名。无匹配行静默 no-op。
    """
    with _db() as conn:
        if stage and adapter:
            conn.execute(
                "DELETE FROM story_session "
                "WHERE story_key = ? AND stage = ? AND adapter = ?",
                (story_key, stage, adapter),
            )
        else:
            conn.execute(
                "DELETE FROM story_session WHERE story_key = ?",
                (story_key,),
            )


def update_session_trace(
    story_key: str,
    stage: str,
    adapter: str,
    *,
    attempt: int | None = None,
    outcome: str | None = None,
    failure_reason: str | None = None,
    artifacts_prod: list[str] | None = None,
    pty_log_ref: str | None = None,
) -> None:
    """更新 story_session 的执行轨迹字段(STEP 1.7a,DESIGN §4.10)。

    所有参数可选 —— 只更新给定的字段,None 的不动。artifacts_prod 传 list(序列化成
    JSON)。无对应 session 行时静默 no-op(防御:某些路径 session 未建)。
    """
    import json as _json

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sets: list[str] = []
    vals: list = []
    if attempt is not None:
        sets.append("attempt = ?")
        vals.append(int(attempt))
    if outcome is not None:
        sets.append("outcome = ?")
        vals.append(outcome)
    if failure_reason is not None:
        sets.append("failure_reason = ?")
        vals.append(failure_reason)
    if artifacts_prod is not None:
        sets.append("artifacts_prod = ?")
        vals.append(_json.dumps(artifacts_prod, ensure_ascii=False))
    if pty_log_ref is not None:
        sets.append("pty_log_ref = ?")
        vals.append(pty_log_ref)
    if not sets:
        return  # 没字段要更新
    sets.append("updated_at = ?")
    vals.append(now)
    vals.extend([story_key, stage, adapter])
    with _db() as conn:
        conn.execute(
            f"UPDATE story_session SET {', '.join(sets)} "
            "WHERE story_key = ? AND stage = ? AND adapter = ?",
            vals,
        )
