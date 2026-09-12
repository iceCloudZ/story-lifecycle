"""routers/digest — 日清 digest(DESIGN-v1-work-agent WP-C §5.3)。

``POST /api/digest/daily``:纯读聚合(库 + 配置)→ 四节 Markdown 简报 →
``emit_event("daily_digest", tier=digest)`` 一行 outbox,投递归通知线程。

四节:
- ① 今日/近 3 日到期 story(deadline 落 [today, today+2] 窗口)
- ② 超龄 bug 排行(挂龄 ≥ 阈值,age_days 降序 top 10;挂龄算法唯一来源
  ``sourcing/aging``,时间字段读 context_json 的 bug_* 键 —— 同步时落)
- ③ active story 清单(lifecycle_state / current_stage / 最近 gate 状态)
- ④ 昨日 patrol FAIL 摘要(读 patrol_run + patrol_run_item,任一 FAIL → FAIL)

聚合函数 :func:`build_daily_digest` 是共享实现:`story daily --push`(CLI)与
本路由都调它,markdown 构建不复制。CLI 选直连 emit 而非 POST serve:emit 走
同一 outbox(至少一次投递),不引入「serve 必须在线」的网络依赖。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter

from ....infra.db import models as db
from ....sourcing.aging import (
    bug_active_days,
    bug_aging_warn_days,
    bug_time_fields,
)

log = logging.getLogger("story-lifecycle.api-digest")

router = APIRouter(tags=["digest"])

_DIGEST_TITLE = "每日清结简报"
# ② 节排行榜长度上限
_TOP_AGING_BUGS = 10


def _to_day(value: str) -> date | None:
    """story 列里的日期字面量(deadline "YYYY-MM-DD[ ...]")→ date。"""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _due_within_days(pool: list[dict], today: date, window: int) -> list[dict]:
    """① 节:deadline 落 [today, today+window-1] 的 story,按 deadline 稳序。"""
    horizon = today + timedelta(days=window - 1)
    due = [
        s
        for s in pool
        if (d := _to_day(s.get("deadline") or "")) is not None
        and today <= d <= horizon
    ]
    due.sort(key=lambda s: s.get("deadline") or "")
    return due


def _aging_bugs(today: date, threshold: int) -> list[dict]:
    """② 节:超龄 bug(item_type=bug,挂龄 ≥ 阈值),age_days 降序 top N。

    挂龄用 sourcing/aging 唯一算法;时间字段读 context_json(同步时落),
    没落过字段的存量 bug(created 缺失)→ age None,自然不进榜 —— 不回填。
    """
    bugs = db.list_visible_stories(item_type="bug")
    aged: list[tuple[int, dict]] = []
    for b in bugs:
        age = bug_active_days(bug_time_fields(b), today=today)
        if age is None or age < threshold:
            continue
        aged.append((age, b))
    aged.sort(key=lambda pair: pair[0], reverse=True)
    return [{"age_days": age, "bug": b} for age, b in aged[:_TOP_AGING_BUGS]]


def _active_stories() -> list[dict]:
    """③ 节:engine status=active 的 story,带 lifecycle_state/stage/最近 gate。

    最近 gate 取 gate_result 最新一行(stage/gate_name/result);无记录 →
    "无记录"(诚实展示,不编造)。
    """
    out = []
    for s in db.list_active_stories():
        gates = db.get_gate_results(s["story_key"], limit=1)
        latest_gate = "无记录"
        if gates:
            g = gates[0]
            latest_gate = f"{g.get('stage','')}/{g.get('gate_name','')}:{g.get('result','')}"
        out.append(
            {
                "story_key": s["story_key"],
                "title": s["title"],
                "lifecycle_state": s.get("lifecycle_state") or "",
                "current_stage": s.get("current_stage") or "",
                "latest_gate": latest_gate,
                "tapd_url": s.get("tapd_url") or "",
            }
        )
    out.sort(key=lambda s: s["story_key"])
    return out


def _yesterday_patrol_fails(today: date) -> list[dict]:
    """④ 节:昨日(相对 today)rollup FAIL 的 patrol run。

    结论与列表徽标同源:任一 item FAIL → FAIL(SUM>0)。train 解析同
    routers/patrol.py:run_scope "train:x" 前缀优先,fallback story.release_train。
    """
    yesterday = (today - timedelta(days=1)).isoformat()
    with db._db() as conn:
        rows = conn.execute(
            """
            SELECT r.id, r.story_key, r.run_scope, r.started_at,
                   SUM(CASE WHEN ri.result = 'FAIL' THEN 1 ELSE 0 END) AS fail_count
            FROM patrol_run r
            LEFT JOIN patrol_run_item ri ON ri.run_id = r.id
            WHERE date(r.started_at) = ?
            GROUP BY r.id, r.story_key, r.run_scope, r.started_at
            """,
            (yesterday,),
        ).fetchall()
    fails = [dict(r) for r in rows if (r["fail_count"] or 0) > 0]
    for f in fails:
        scope = (f.get("run_scope") or "").strip()
        if scope.lower().startswith("train:"):
            f["train"] = scope[len("train:"):].strip()
        else:
            story = db.get_story(f["story_key"]) or {}
            f["train"] = story.get("release_train") or ""
        f["story"] = (db.get_story(f["story_key"]) or {}).get("title", "")
    fails.sort(key=lambda f: f.get("started_at") or "")
    return fails


def build_daily_digest(*, today: date | None = None) -> dict:
    """聚合四节,返回 ``{"markdown", "sections", "date"}``。纯读无写副作用。

    共享实现:POST /api/digest/daily 与 ``story daily --push`` 都走这里,
    markdown 构建只有这一份。
    """
    today = today or datetime.now(timezone.utc).date()
    threshold = bug_aging_warn_days()

    pool = db.list_visible_stories()
    due = _due_within_days(pool, today, window=3)
    aged = _aging_bugs(today, threshold)
    active = _active_stories()
    patrol_fails = _yesterday_patrol_fails(today)

    lines = [f"# {_DIGEST_TITLE} · {today.isoformat()}", ""]

    # ① 近 3 日到期 story
    lines.append(f"## ① 今日/近 3 日到期 story — {len(due)} 条")
    lines.append("")
    if due:
        for s in due:
            url = s.get("tapd_url") or ""
            link = f" · [TAPD]({url})" if url else ""
            lines.append(
                f"- **{s['story_key']}** {s['title'][:50]} · 截止 {s['deadline']}{link}"
            )
    else:
        lines.append("无")
    lines.append("")

    # ② 超龄 bug 排行
    lines.append(f"## ② 超龄 bug 排行(≥{threshold} 天,最多 {_TOP_AGING_BUGS} 条)— {len(aged)} 条")
    lines.append("")
    if aged:
        for entry in aged:
            b = entry["bug"]
            url = b.get("tapd_url") or ""
            link = f" · [TAPD]({url})" if url else ""
            lines.append(
                f"- {entry['age_days']} 天 · **{b['story_key']}** {b['title'][:50]}{link}"
            )
    else:
        lines.append("无")
    lines.append("")

    # ③ active story 清单
    lines.append(f"## ③ 进行中 story — {len(active)} 条")
    lines.append("")
    if active:
        for s in active:
            state = s["lifecycle_state"] or "?"
            stage = s["current_stage"] or "?"
            lines.append(
                f"- **{s['story_key']}** {s['title'][:50]} · {state}/{stage}"
                f" · 最近 gate:{s['latest_gate']}"
            )
    else:
        lines.append("无")
    lines.append("")

    # ④ 昨日 patrol FAIL 摘要
    lines.append(f"## ④ 昨日巡检 FAIL — {len(patrol_fails)} 轮")
    lines.append("")
    if patrol_fails:
        for f in patrol_fails:
            lines.append(
                f"- run #{f['id']} · **{f['story_key']}** {f.get('story','')[:50]}"
                f" · 包 {f.get('train') or '未挂包'} · 失败 {f['fail_count']} 项"
            )
    else:
        lines.append("无(昨日无 FAIL 轮次)")
    lines.append("")

    markdown = "\n".join(lines).rstrip()
    sections = {
        "due_stories": [
            {
                "story_key": s["story_key"],
                "title": s["title"],
                "deadline": s.get("deadline") or "",
                "url": s.get("tapd_url") or "",
            }
            for s in due
        ],
        "aging_bugs": [
            {
                "story_key": e["bug"]["story_key"],
                "title": e["bug"]["title"],
                "age_days": e["age_days"],
                "url": e["bug"].get("tapd_url") or "",
            }
            for e in aged
        ],
        "active_stories": active,
        "patrol_fails": [
            {
                "run_id": f["id"],
                "story_key": f["story_key"],
                "train": f.get("train") or "",
                "failed_count": f["fail_count"],
            }
            for f in patrol_fails
        ],
    }
    return {"markdown": markdown, "sections": sections, "date": today.isoformat()}


@router.post("/api/digest/daily")
def api_digest_daily():
    """日清 digest:聚合四节 → emit_event("daily_digest", tier=digest)。

    纯读 + 一次 emit;emit 自吞异常(emitter 契约),这里只透传是否落行。
    """
    built = build_daily_digest()
    nid = None
    try:
        from ....infra.notification.emitter import emit_event

        nid = emit_event(
            "daily_digest",
            title=f"{_DIGEST_TITLE} · {built['date']}",
            message=built["markdown"],
            payload={
                "date": built["date"],
                "counts": {k: len(v) for k, v in built["sections"].items()},
                "sections": built["sections"],
            },
        )
    except Exception:  # noqa: BLE001 — 观察性推送,绝不影响响应本体
        log.debug("daily_digest emit failed (non-fatal)", exc_info=True)
    return {"ok": True, "markdown": built["markdown"], "emitted": nid is not None}
