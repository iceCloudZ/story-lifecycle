"""patrol — 生产巡检读写（docs/design-prod-patrol-integration.md Phase 2）。

巡检不是 lifecycle stage：story 上线后的观察期内，由外部 skill/cron 触发，
服务端只做登记（items）、回写（runs）与聚合（train overview），不驱动推进。

返回值为 snake_case 原始 dict（params 已解码为 dict），路由层负责 camelCase 化。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .connection import _db


def _now_ts() -> str:
    """UTC 时间戳，格式与 SQLite CURRENT_TIMESTAMP 一致（YYYY-MM-DD HH:MM:SS）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _decode_params(params_json: str | None) -> dict:
    if not params_json:
        return {}
    try:
        decoded = json.loads(params_json)
        return decoded if isinstance(decoded, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def rollup_run_result(item_results: list[str]) -> str:
    """一轮的汇总结论：任一 FAIL → FAIL，否则 PASS（SKIP/WAIVED 不翻红）。"""
    return "FAIL" if any(r == "FAIL" for r in item_results) else "PASS"


def replace_patrol_items(story_key: str, items: list[dict]) -> list[dict]:
    """全量替换该 story 的巡检项（幂等）。seq 按传入顺序 1..N 重新分配。

    items 每项: {name, type, params, baseline, pass_criteria, rollback_ref, enabled}
    """
    now = _now_ts()
    with _db() as conn:
        conn.execute("DELETE FROM patrol_item WHERE story_key = ?", (story_key,))
        for seq, it in enumerate(items, start=1):
            conn.execute(
                "INSERT INTO patrol_item (story_key, seq, name, type, params_json, "
                "baseline, pass_criteria, rollback_ref, enabled, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    story_key,
                    seq,
                    it.get("name", ""),
                    it.get("type") or "manual",
                    json.dumps(it.get("params") or {}, ensure_ascii=False),
                    it.get("baseline"),
                    it.get("pass_criteria") or "",
                    it.get("rollback_ref"),
                    1 if it.get("enabled", True) else 0,
                    now,
                    now,
                ),
            )
    return list_patrol_items(story_key)


def list_patrol_items(story_key: str, enabled_only: bool = False) -> list[dict]:
    """按 seq 稳序返回巡检项；params 解码为 dict。"""
    sql = "SELECT * FROM patrol_item WHERE story_key = ?"
    if enabled_only:
        sql += " AND enabled = 1"
    sql += " ORDER BY seq"
    with _db() as conn:
        rows = conn.execute(sql, (story_key,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["params"] = _decode_params(d.pop("params_json", None))
        d["enabled"] = bool(d.get("enabled"))
        out.append(d)
    return out


def create_patrol_run(
    story_key: str,
    run_scope: str,
    executor: str,
    summary: str,
    items: list[dict],
) -> dict:
    """回写一轮巡检（items 结果内联），返回带 rollup 结论的 run dict。

    items 每项: {item_seq, result, observed, evidence_ref}
    """
    started_at = _now_ts()
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO patrol_run (story_key, run_scope, started_at, executor, summary) "
            "VALUES (?, ?, ?, ?, ?)",
            (story_key, run_scope, started_at, executor, summary),
        )
        run_id = cur.lastrowid
        for it in items:
            conn.execute(
                "INSERT INTO patrol_run_item (run_id, item_seq, result, observed, evidence_ref) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    run_id,
                    it.get("item_seq", 0),
                    (it.get("result") or "").upper(),
                    it.get("observed") or "",
                    it.get("evidence_ref") or "",
                ),
            )
    run = {
        "id": run_id,
        "story_key": story_key,
        "run_scope": run_scope,
        "started_at": started_at,
        "executor": executor,
        "summary": summary,
        "result": rollup_run_result([it.get("result", "") for it in items]),
    }
    run["items"] = _attach_item_names(story_key, [
        {
            "item_seq": it.get("item_seq", 0),
            "result": (it.get("result") or "").upper(),
            "observed": it.get("observed") or "",
            "evidence_ref": it.get("evidence_ref") or "",
        }
        for it in items
    ])
    return run


def _attach_item_names(story_key: str, run_items: list[dict]) -> list[dict]:
    """软引用补名：run_item 按 (story_key, item_seq) LEFT JOIN 当前 item 名。

    items 全量替换后旧轮次的 seq 可能指向不存在/已变的 item——join 不到就
    name=None，前端仍可用 observed/evidence 渲染，不阻塞历史轮次展示。
    """
    if not run_items:
        return run_items
    names = {it["seq"]: it["name"] for it in list_patrol_items(story_key)}
    for ri in run_items:
        ri["name"] = names.get(ri.get("item_seq"))
    return run_items


def list_patrol_runs(story_key: str, limit: int = 20) -> list[dict]:
    """历史轮次（新→旧），items 内联（含 rollup result 与逐项 name）。"""
    with _db() as conn:
        runs = conn.execute(
            "SELECT * FROM patrol_run WHERE story_key = ? ORDER BY id DESC LIMIT ?",
            (story_key, limit),
        ).fetchall()
        if not runs:
            return []
        placeholders = ",".join("?" * len(runs))
        ri_rows = conn.execute(
            f"SELECT run_id, item_seq, result, observed, evidence_ref "
            f"FROM patrol_run_item WHERE run_id IN ({placeholders}) ORDER BY id",
            [r["id"] for r in runs],
        ).fetchall()
    by_run: dict[int, list[dict]] = {}
    for ri in ri_rows:
        by_run.setdefault(ri["run_id"], []).append(
            {
                "item_seq": ri["item_seq"],
                "result": ri["result"],
                "observed": ri["observed"],
                "evidence_ref": ri["evidence_ref"],
            }
        )
    out = []
    for r in runs:
        items = _attach_item_names(story_key, by_run.get(r["id"], []))
        out.append(
            {
                "id": r["id"],
                "story_key": r["story_key"],
                "run_scope": r["run_scope"],
                "started_at": r["started_at"],
                "executor": r["executor"],
                "summary": r["summary"],
                "result": rollup_run_result([it["result"] for it in items]),
                "items": items,
            }
        )
    return out


def get_patrol_summaries() -> dict[str, dict]:
    """全量 story 的巡检摘要（列表徽标用）：itemsCount / 最新轮时间与结论。

    两条小查询一次拉全表聚合，避免列表端点 N+1。无巡检数据的 story 不出现在
    返回 dict 里（序列化层落 patrolSummary=null）。
    """
    with _db() as conn:
        item_counts = {
            r["story_key"]: r["c"]
            for r in conn.execute(
                "SELECT story_key, COUNT(*) AS c FROM patrol_item "
                "WHERE enabled = 1 GROUP BY story_key"
            )
        }
        latest = {
            r["story_key"]: r
            for r in conn.execute(
                "SELECT r.story_key, r.id, r.started_at, "
                "SUM(CASE WHEN ri.result = 'FAIL' THEN 1 ELSE 0 END) AS fails "
                "FROM patrol_run r "
                "LEFT JOIN patrol_run_item ri ON ri.run_id = r.id "
                "WHERE r.id IN (SELECT MAX(id) FROM patrol_run GROUP BY story_key) "
                "GROUP BY r.id, r.story_key, r.started_at"
            )
        }
    out: dict[str, dict] = {}
    for key in set(item_counts) | set(latest):
        lr = latest.get(key)
        out[key] = {
            "itemsCount": item_counts.get(key, 0),
            "latestRunAt": lr["started_at"] if lr else None,
            "latestResult": rollup_run_result(["FAIL"] * (lr["fails"] or 0)) if lr else None,
        }
    return out


def get_train_patrol_overview(train: str) -> dict:
    """包维度聚合：train 下全部 story 的 items 数、最新轮结论、FAIL 明细、
    从未巡检清单。巡检 skill 每轮开始 GET 一次即得本轮范围。"""
    with _db() as conn:
        stories = conn.execute(
            "SELECT story_key, title, lifecycle_state, status FROM story "
            "WHERE release_train = ? AND deleted_at IS NULL ORDER BY story_key",
            (train,),
        ).fetchall()
        if not stories:
            return {
                "train": train,
                "total": 0,
                "patrolled": 0,
                "failed": 0,
                "neverPatrolled": [],
                "stories": [],
            }
        keys = [s["story_key"] for s in stories]
        placeholders = ",".join("?" * len(keys))

        item_counts = {
            r["story_key"]: r["c"]
            for r in conn.execute(
                f"SELECT story_key, COUNT(*) AS c FROM patrol_item "
                f"WHERE story_key IN ({placeholders}) AND enabled = 1 GROUP BY story_key",
                keys,
            )
        }
        latest_runs = {
            r["story_key"]: r
            for r in conn.execute(
                f"SELECT r.id, r.story_key, r.run_scope, r.started_at, r.executor, r.summary "
                f"FROM patrol_run r "
                f"WHERE r.story_key IN ({placeholders}) "
                f"AND r.id IN (SELECT MAX(id) FROM patrol_run WHERE story_key IN "
                f"({placeholders}) GROUP BY story_key)",
                keys + keys,
            )
        }
        latest_ids = [r["id"] for r in latest_runs.values()]
        # 巡检项名表（seq → name）：只给有 items 的 story 取，FAIL 明细展示用。
        names: dict[str, dict[int, str]] = {}
        for k in keys:
            if item_counts.get(k, 0) > 0:
                names[k] = {i["seq"]: i["name"] for i in list_patrol_items(k)}
        latest_items: dict[int, list[dict]] = {}
        if latest_ids:
            ph = ",".join("?" * len(latest_ids))
            for ri in conn.execute(
                f"SELECT run_id, item_seq, result, observed, evidence_ref "
                f"FROM patrol_run_item WHERE run_id IN ({ph}) ORDER BY id",
                latest_ids,
            ):
                latest_items.setdefault(ri["run_id"], []).append(
                    {
                        "item_seq": ri["item_seq"],
                        "result": ri["result"],
                        "observed": ri["observed"],
                        "evidence_ref": ri["evidence_ref"],
                    }
                )

    story_entries = []
    never_patrolled: list[str] = []
    failed_count = 0
    for s in stories:
        key = s["story_key"]
        lr = latest_runs.get(key)
        entry = {
            "story_key": key,
            "title": s["title"],
            "lifecycle_state": s["lifecycle_state"],
            "status": s["status"],
            "items_count": item_counts.get(key, 0),
            "latest_run": None,
        }
        if lr:
            items = latest_items.get(lr["id"], [])
            for it in items:
                it["name"] = names.get(key, {}).get(it.get("item_seq"))
            result = rollup_run_result([it["result"] for it in items])
            entry["latest_run"] = {
                "id": lr["id"],
                "run_scope": lr["run_scope"],
                "started_at": lr["started_at"],
                "executor": lr["executor"],
                "summary": lr["summary"],
                "result": result,
                "fail_items": [
                    {
                        "item_seq": it["item_seq"],
                        "name": it.get("name"),
                        "observed": it["observed"],
                        "evidence_ref": it["evidence_ref"],
                    }
                    for it in items
                    if it["result"] == "FAIL"
                ],
            }
            if result == "FAIL":
                failed_count += 1
        else:
            never_patrolled.append(key)
        story_entries.append(entry)

    return {
        "train": train,
        "total": len(story_entries),
        "patrolled": len(story_entries) - len(never_patrolled),
        "failed": failed_count,
        "neverPatrolled": never_patrolled,
        "stories": story_entries,
    }
