"""events — event_log / gate_result 读写（设计15 阶段B 拆分自 models.py）。"""

from __future__ import annotations

import json

from .connection import _db
def log_event(story_key: str, stage: str, event_type: str, payload: dict | None = None):
    """Record an event to event_log. Structured replacement for log_stage."""
    with _db() as conn:
        conn.execute(
            "INSERT INTO event_log (story_key, stage, event_type, payload) VALUES (?, ?, ?, ?)",
            (
                story_key,
                stage,
                event_type,
                json.dumps(payload or {}, ensure_ascii=False),
            ),
        )


def record_gate_result(
    story_key: str, stage: str, gate_name: str, result: str, detail: str = ""
):
    """Record a compact gate result in the existing gate_result table."""
    with _db() as conn:
        row = conn.execute(
            "SELECT id FROM story WHERE story_key = ?", (story_key,)
        ).fetchone()
        if not row:
            return
        conn.execute(
            "INSERT INTO gate_result (story_id, stage, gate_name, result, detail) "
            "VALUES (?, ?, ?, ?, ?)",
            (row["id"], stage, gate_name, result, detail),
        )


def get_story_events(story_key: str) -> list[dict]:
    """Return all events for a story, ordered by id."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM event_log WHERE story_key = ? ORDER BY id",
            (story_key,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_latest_declare(
    story_key: str, stage: str, *, since_version: int = 0
) -> dict | None:
    """归一化的"成果物完成"读接口（单一真相源）。

    code agent 调 ``story tool declare`` 时往 event_log 写一条
    ``artifact_declared`` event（payload 含 doc_type/version/summary/files_changed）。
    本函数返回**本轮**（version > since_version）最新的 declare event payload，
    无则 None。``since_version`` 是 spawn 时快照的 base，用来过滤上轮残留 event。

    这是 stage 完成判定的唯一真相源 —— is_artifacts_ready / judge payload /
    story tool todo 都从它读，不再各自看文件/done.json（归一化 1068018 事故修复）。
    """
    with _db() as conn:
        row = conn.execute(
            "SELECT payload FROM event_log "
            "WHERE story_key = ? AND stage = ? AND event_type = 'artifact_declared' "
            "ORDER BY id DESC LIMIT 1",
            (story_key, stage),
        ).fetchone()
    if row is None:
        return None
    payload = parse_event_payload({"payload": row["payload"]})
    if payload.get("version", 0) <= since_version:
        return None  # 旧残留（version 未超过本轮 base）
    return payload


def parse_event_payload(event: dict) -> dict:
    """Decode an event's payload (stored as JSON str or dict) into a dict.

    Centralized so failure semantics don't drift across the many endpoints
    that read event payloads (stats / loop-trace / timeline / gate-history).
    Returns {} on missing or unparseable payload, so callers can uniformly
    do dict operations — a failed parse simply yields no matches.
    """
    payload = event.get("payload")
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        try:
            decoded = json.loads(payload)
            return decoded if isinstance(decoded, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def is_adversarial_loop_event(event: dict) -> bool:
    """True if this event records one adversarial plan↔review / code↔review round."""
    payload = parse_event_payload(event)
    return bool(payload.get("adversarial_loop")) and event.get("event_type") in (
        "plan",
        "review",
    )


def get_recent_quality_events(
    story_key: str, event_types: list[str], limit: int = 50
) -> list[dict]:
    """Get recent events of specified types from event_log."""
    placeholders = ",".join("?" * len(event_types))
    with _db() as conn:
        rows = conn.execute(
            f"SELECT * FROM event_log WHERE story_key = ? AND event_type IN ({placeholders}) ORDER BY id DESC LIMIT ?",
            [story_key] + event_types + [limit],
        ).fetchall()
    return [dict(r) for r in rows]


def get_recent_events_by_type(event_types: list[str], limit: int = 100) -> list[dict]:
    """跨所有 story 取近期事件(无 story_key 过滤)。

    供层5 reflection 的全局 playbook 用(飞轮知识是跨 story 的)。
    """
    placeholders = ",".join("?" * len(event_types))
    with _db() as conn:
        rows = conn.execute(
            f"SELECT * FROM event_log WHERE event_type IN ({placeholders}) ORDER BY id DESC LIMIT ?",
            list(event_types) + [limit],
        ).fetchall()
    return [dict(r) for r in rows]


