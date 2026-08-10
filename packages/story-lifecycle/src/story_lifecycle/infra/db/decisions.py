"""decisions — 编排决策审计（设计15 阶段B 拆分自 models.py）。"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone

from .connection import _db

log = logging.getLogger("story-lifecycle.db.decisions")


def _insert_with_retry(conn, sql, args) -> int:
    """执行 INSERT，遇瞬时写锁(sqlite3.OperationalError locked/busy)重试。

    2026-08-06 real-run 1068018:外部 `story tool declare` 进程与 serve 并发写,
    approve 决策行的 INSERT 撞 "database is locked" → log_decision 抛异常 →
    调用方 best-effort 吞掉 → 审计链断裂(approve 行丢失)。审计写入必须可靠。
    """
    for attempt in range(3):
        try:
            cur = conn.execute(sql, args)
            return int(cur.lastrowid)
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                if attempt < 2:
                    time.sleep(0.2 * (attempt + 1))
                    continue
            raise
    raise RuntimeError("unreachable")


def log_decision(
    story_key: str,
    stage: str,
    trigger: str,
    decision: str,
    *,
    reason: str = "",
    context_ref: str = "",
    action_taken: str = "",
    action_payload: dict | None = None,
    llm_model: str = "",
) -> int:
    """记录一次编排 LLM 决策到 orchestrator_decision 表。

    Args:
        story_key / stage: 决策归属的 story+stage。
        trigger: 决策触发源("boundary_judge" / "stuck_summary" / "stuck_agentic")。
        decision: 决策结果("approve" / "reject" / "escalate" / "restart" / "wait")。
        reason: 决策理由(reject 上限去重时用)。
        context_ref: 上下文引用(组装的上下文摘要 hash 或路径,审计追溯用)。
        action_taken: Handler 实际执行的副作用("insert_retry_action" / "paused" / "killed+respawn")。
        action_payload: 副作用参数(如 retry 的 seed / restart 的 adapter),JSON 序列化存。
        llm_model: 决策用的 LLM 模型名(fallback 时为空)。

    Returns: 新插入行的 id。
    """
    import json as _json

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _db() as conn:
        rid = _insert_with_retry(
            conn,
            """INSERT INTO orchestrator_decision
               (story_key, stage, trigger, context_ref, decision, reason,
                action_taken, action_payload, llm_model, decided_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                story_key,
                stage,
                trigger,
                context_ref,
                decision,
                reason,
                action_taken,
                _json.dumps(action_payload, ensure_ascii=False)
                if action_payload
                else None,
                llm_model,
                now,
            ),
        )
    return rid


def get_decisions(
    story_key: str,
    stage: str | None = None,
    trigger: str | None = None,
    *,
    limit: int = 100,
) -> list[dict]:
    """读编排 LLM 决策历史(无状态编排 §4.6 的上下文来源 + reject 上限 §4.9 查询)。

    按 decided_at 降序(最近在前)。stage/trigger 可选过滤。
    """
    sql = "SELECT * FROM orchestrator_decision WHERE story_key = ?"
    args: list = [story_key]
    if stage:
        sql += " AND stage = ?"
        args.append(stage)
    if trigger:
        sql += " AND trigger = ?"
        args.append(trigger)
    sql += " ORDER BY decided_at DESC, id DESC LIMIT ?"
    args.append(limit)
    with _db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def count_decisions(
    story_key: str,
    stage: str,
    decision: str,
    trigger: str | None = None,
) -> int:
    """数某 stage 某 decision 的次数(reject 上限 §4.9 用)。"""
    sql = (
        "SELECT COUNT(*) FROM orchestrator_decision "
        "WHERE story_key = ? AND stage = ? AND decision = ?"
    )
    args: list = [story_key, stage, decision]
    if trigger:
        sql += " AND trigger = ?"
        args.append(trigger)
    with _db() as conn:
        return int(conn.execute(sql, args).fetchone()[0])
