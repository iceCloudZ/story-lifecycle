"""feedback — 人判 vs 机判校准（迭代 4 C 线）。

judge_feedback 表只记人判侧；机判在 orchestrator_decision。
混淆矩阵口径（设计 §5）：
- 机判 approve + 人判 disagree = 漏拦（false negative）
- 机判 reject/escalate + 人判 disagree = 误拦（false positive）
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .connection import _db

log = logging.getLogger("story-lifecycle.db.feedback")


def record_feedback(
    story_key: str,
    decision_id: int,
    human_decision: str,
    note: str = "",
) -> dict:
    """记录一次人判反馈（agree / disagree）。

    冗余快照 machine_decision / decided_at 从 orchestrator_decision 读，
    join 时校验一致性（防决策表改动后口径漂移）。
    重复提交同一 (story_key, decision_id)：覆盖更新（最近人判为准）。
    """
    if human_decision not in ("agree", "disagree"):
        raise ValueError(f"human_decision 必须是 agree/disagree，收到: {human_decision!r}")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _db() as conn:
        row = conn.execute(
            "SELECT decision, decided_at FROM orchestrator_decision WHERE id = ?",
            (decision_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"orchestrator_decision id={decision_id} 不存在")
        machine_decision = row["decision"]
        decided_at = row["decided_at"] or ""
        conn.execute(
            """INSERT INTO judge_feedback
               (story_key, decision_id, machine_decision, human_decision, note, decided_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(story_key, decision_id) DO UPDATE SET
                 machine_decision = excluded.machine_decision,
                 human_decision = excluded.human_decision,
                 note = excluded.note,
                 decided_at = excluded.decided_at,
                 created_at = excluded.created_at""",
            (story_key, decision_id, machine_decision, human_decision, note, decided_at, now),
        )
    return {
        "story_key": story_key,
        "decision_id": decision_id,
        "machine_decision": machine_decision,
        "human_decision": human_decision,
        "note": note,
    }


def get_feedbacks(story_key: str | None = None, limit: int = 500) -> list[dict]:
    """读反馈记录（可选按 story 过滤，按 created_at 降序）。"""
    sql = "SELECT * FROM judge_feedback"
    args: list = []
    if story_key:
        sql += " WHERE story_key = ?"
        args.append(story_key)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    args.append(limit)
    with _db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def confusion_matrix() -> dict:
    """机判 vs 人判混淆矩阵（join orchestrator_decision 校验快照一致性）。

    返回:
        {
            "total": n,
            "agree": n, "disagree": n,
            "missed_block": n,   # 机判 approve + 人判 disagree（漏拦）
            "false_block": n,    # 机判 reject/escalate + 人判 disagree（误拦）
            "rows": [...],
        }
    """
    rows = get_feedbacks(limit=10_000)
    agree = sum(1 for r in rows if r["human_decision"] == "agree")
    disagree = sum(1 for r in rows if r["human_decision"] == "disagree")
    missed = 0
    false_block = 0
    for r in rows:
        if r["human_decision"] != "disagree":
            continue
        md = r["machine_decision"]
        if md == "approve":
            missed += 1
        elif md in ("reject", "escalate"):
            false_block += 1
    return {
        "total": len(rows),
        "agree": agree,
        "disagree": disagree,
        "missed_block": missed,
        "false_block": false_block,
        "rows": rows,
    }
