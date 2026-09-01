"""outbox — notification_outbox 读写(管家系统 WP1,DESIGN-story-butler §3.1)。

事件出口(``infra/notification/emitter.py``)同步写一行(pending);投递线程
(``infra/notification/delivery.py``)drain 后按通道结果改状态:

- ``pending`` → ``sent``:全部目标通道送达(sent_at 落时刻)
- ``pending`` → ``skipped``:通道不可用跳过(永不重试一个发不出去的通道)
- ``pending`` → ``failed``:attempts ≥ 5,行保留(可审计,不删)

至少一次语义:失败退避重试(1m/5m/30m);已送达通道记在 payload_json.delivered,
重试不重复发同一条弹窗。raw SQL,零 ORM。DDL 在 schema._create_notification_tables。
"""

from __future__ import annotations

from datetime import datetime, timezone

from .connection import _db


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def enqueue_notification(
    *,
    event_type: str,
    story_key: str = "",
    project: str = "",
    tier: str = "batch",
    title: str = "",
    message: str = "",
    payload_json: str = "{}",
) -> int:
    """事件出口入口:写一行 pending,返回行 id。"""
    with _db() as conn:
        cur = conn.execute(
            """INSERT INTO notification_outbox
               (event_type, story_key, project, tier, title, message,
                payload_json, status, attempts)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0)""",
            (
                event_type,
                story_key,
                project,
                tier,
                title,
                message,
                payload_json or "{}",
            ),
        )
        return int(cur.lastrowid)


def get_notification(nid: int) -> dict | None:
    """按 id 读一行(测试/排障用)。"""
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM notification_outbox WHERE id = ?", (nid,)
        ).fetchone()
    return dict(row) if row else None


def list_pending_notifications(limit: int = 200) -> list[dict]:
    """全部 pending 行(id 升序,先到先投)。到期过滤在投递线程做
    (_last_attempt_ts 在 payload_json 里,SQL 过滤不了,表小无所谓)。"""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM notification_outbox WHERE status = 'pending' "
            "ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_notifications(status: str | None = None, limit: int = 100) -> list[dict]:
    """按状态(可选)倒序读 outbox 行(测试/排障/晨报聚合用)。"""
    sql = "SELECT * FROM notification_outbox"
    args: list = []
    if status:
        sql += " WHERE status = ?"
        args.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with _db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def mark_notification_sent(nid: int, payload_json: str | None = None) -> None:
    """全部通道送达 → sent(至少一次语义的终点)。"""
    with _db() as conn:
        if payload_json is None:
            conn.execute(
                "UPDATE notification_outbox SET status = 'sent', sent_at = ? "
                "WHERE id = ?",
                (_now(), nid),
            )
        else:
            conn.execute(
                "UPDATE notification_outbox SET status = 'sent', sent_at = ?, "
                "payload_json = ? WHERE id = ?",
                (_now(), payload_json, nid),
            )


def mark_notification_skipped(nid: int, payload_json: str | None = None) -> None:
    """通道不可用全跳过 → skipped(不算失败,不重试)。"""
    with _db() as conn:
        if payload_json is None:
            conn.execute(
                "UPDATE notification_outbox SET status = 'skipped', sent_at = ? "
                "WHERE id = ?",
                (_now(), nid),
            )
        else:
            conn.execute(
                "UPDATE notification_outbox SET status = 'skipped', sent_at = ?, "
                "payload_json = ? WHERE id = ?",
                (_now(), payload_json, nid),
            )


def mark_notification_attempt(
    nid: int,
    *,
    attempts: int,
    last_error: str = "",
    payload_json: str | None = None,
) -> None:
    """一轮投递有通道失败 → 计数 + 记错,行保持 pending 等退避到期重试。"""
    with _db() as conn:
        if payload_json is None:
            conn.execute(
                "UPDATE notification_outbox SET attempts = ?, last_error = ? "
                "WHERE id = ?",
                (attempts, last_error, nid),
            )
        else:
            conn.execute(
                "UPDATE notification_outbox SET attempts = ?, last_error = ?, "
                "payload_json = ? WHERE id = ?",
                (attempts, last_error, payload_json, nid),
            )


def mark_notification_failed(
    nid: int,
    *,
    attempts: int | None = None,
    last_error: str = "",
    payload_json: str | None = None,
) -> None:
    """attempts ≥ 5 → failed。行保留(降级矩阵 §5:丢失 0,迟到可审计)。

    ``attempts`` 传最终尝试次数(failed 是终点,计数也要落库可查)。
    """
    sets = ["status = 'failed'"]
    args: list = []
    if attempts is not None:
        sets.append("attempts = ?")
        args.append(attempts)
    if payload_json is not None:
        sets.append("payload_json = ?")
        args.append(payload_json)
    sets.append("last_error = ?")
    args.append(last_error)
    args.append(nid)
    with _db() as conn:
        conn.execute(
            f"UPDATE notification_outbox SET {', '.join(sets)} WHERE id = ?", args
        )
