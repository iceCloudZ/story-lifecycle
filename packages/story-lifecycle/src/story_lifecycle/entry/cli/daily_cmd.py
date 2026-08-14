"""story daily — 每日简报:聚合今日该关注的 story(过期/到期/新落/受阻/进行中)。

纯读聚合,无副作用:只 SELECT,不写库、不拉远端。两种输出:
- 默认 Markdown(人读,分节 + 每条一行带 tapd_url)
- ``--json`` 结构化 JSON(脚本消费 — 定时任务系统的 story-daily-sync wrapper,
  见 PLAN-proactive-cadence.md Part A)
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone

import click

# 受阻判定:escalate 在编排侧落 status='paused'(handlers.handle_escalate),
# 引擎错误落 'failed' —— 两者都等人介入。stuck 是运行时检测,不落库,此处不判。
BLOCKED_STATUSES = frozenset({"paused", "failed"})


@click.command("daily")
@click.option("--md", "as_md", is_flag=True, help="输出 Markdown(默认行为,显式传无害)")
@click.option("--json", "as_json", is_flag=True, help="输出结构化 JSON(脚本消费)")
@click.option("--days", default=2, show_default=True, help="前瞻天数(含今日,2=今日+明日)")
def daily_cmd(as_md, as_json, days):
    """每日简报 — 聚合过期/到期/新落/受阻/进行中的 story。纯读,无副作用。"""
    # GBK 控制台下 emoji/中文不炸(errors=replace);被管道消费时由调用方设
    # PYTHONIOENCODING=utf-8 拿到完整 utf-8(见 story-daily-sync wrapper)。
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

    from ...infra.db import models as db

    db.init_db()
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    horizon = (now + timedelta(days=days - 1)).strftime("%Y-%m-%d")

    sections = _aggregate(_load_pool(), today, horizon)

    if as_json:
        print(
            json.dumps(
                {
                    "date": today,
                    "generated_at": now.isoformat(),
                    "days": days,
                    "counts": {k: len(v) for k, v in sections.items()},
                    **{k: [_brief_item(s) for s in v] for k, v in sections.items()},
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(_render_md(today, days, sections))


def _brief_item(s: dict) -> dict:
    """JSON 输出投影 — 只带简报字段,不泄漏 context_json 等整行大字段。"""
    return {
        "story_key": s.get("story_key", ""),
        "title": s.get("title", ""),
        "deadline": s.get("deadline") or "",
        "tapd_status": s.get("tapd_status") or "",
        "lifecycle_state": s.get("lifecycle_state") or "",
        "current_stage": s.get("current_stage") or "",
        "status": s.get("status") or "",
        "intake_state": s.get("intake_state") or "",
        "tapd_url": s.get("tapd_url") or "",
    }


def _load_pool() -> list[dict]:
    """简报聚合池:active + candidate,去重、滤 is_test、滤终态。

    candidate 必须扫 —— sync 新建的 story 落 intake_state='candidate'
    (sync_service.sync_tapd),不扫会漏掉「今日新落」(主动接单的价值所在)。
    终态过滤(真实数据验证):candidate 池里有大量 TAPD 远端已关闭、同步时映射
    lifecycle_state='结项' 的 story(deadline 早过),不过滤会把它们全灌进
    「已过期未完成」变成噪音。TAPD 侧已关闭(resolved/rejected/closed)的
    非结项 story 同理隐藏(与 list 视图默认行为一致)。
    """
    from ...infra.db import models as db
    from ...infra.db.stories import COMPLETED_STATES

    stories = db.list_active_stories() + db.list_candidate_stories()
    seen: set[str] = set()
    pool: list[dict] = []
    for s in stories:
        k = s.get("story_key", "")
        if not k or k in seen:
            continue
        seen.add(k)
        if s.get("is_test"):
            continue
        if s.get("lifecycle_state") == "结项":
            continue
        if s.get("tapd_status") in COMPLETED_STATES:
            continue
        pool.append(s)
    return pool


def _aggregate(pool: list[dict], today: str, horizon: str) -> dict[str, list[dict]]:
    """五个视图各自独立(一条 story 可同时出现在多个视图)。"""
    sections: dict[str, list[dict]] = {
        "overdue": [],
        "due_soon": [],
        "new_today": [],
        "blocked": [],
        "in_progress": [],
    }
    for s in pool:
        dl = (s.get("deadline") or "")[:10]
        created = (s.get("created_at") or "")[:10]
        if dl and dl < today:
            sections["overdue"].append(s)
        elif dl and dl <= horizon:
            sections["due_soon"].append(s)
        if created == today:
            sections["new_today"].append(s)
        if s.get("status") in BLOCKED_STATUSES:
            sections["blocked"].append(s)
        elif s.get("intake_state") == "ready" and s.get("status") == "active":
            sections["in_progress"].append(s)

    sections["overdue"].sort(key=lambda s: s.get("deadline") or "")
    sections["due_soon"].sort(key=lambda s: s.get("deadline") or "")
    sections["new_today"].sort(key=lambda s: s.get("created_at") or "")
    return sections


MD_SECTIONS = [
    ("overdue", "🔴 已过期未完成"),
    ("due_soon", "🟠 到期临近"),
    ("new_today", "🆕 今日新落"),
    ("blocked", "⛔ 受阻/卡住"),
    ("in_progress", "▶️ 进行中"),
]


def _render_md(today: str, days: int, sections: dict[str, list[dict]]) -> str:
    total = sum(len(v) for v in sections.values())
    lines = [f"# Story 每日简报 · {today}", ""]
    if total == 0:
        lines.append("✅ 没有需要关注的 story(活跃池为空)。")
        return "\n".join(lines)
    lines.append(f"> 前瞻 {days} 天 · 五类视图共 {total} 条次(一条 story 可属多类)")
    lines.append("")
    for key, label in MD_SECTIONS:
        suffix = f"今日起 {days} 天内" if key == "due_soon" else ""
        header = f"{label}({suffix})" if suffix else f"{label}"
        items = sections[key]
        lines.append(f"## {header} — {len(items)} 条")
        lines.append("")
        if not items:
            lines.append("无")
            lines.append("")
            continue
        for s in items:
            lines.append(f"- {_fmt_item(s, today)}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _fmt_item(s: dict, today: str) -> str:
    key = s.get("story_key", "")
    title = s.get("title", "")[:60]
    parts = [f"**{key}** {title}".rstrip()]

    dl = (s.get("deadline") or "")[:10]
    if dl:
        overdue_days = (date.fromisoformat(today) - date.fromisoformat(dl)).days
        parts.append(
            f"截止 {dl}(逾期 {overdue_days} 天)" if overdue_days > 0 else f"截止 {dl}"
        )
    state = s.get("lifecycle_state") or ""
    stage = s.get("current_stage") or ""
    if state or stage:
        parts.append("/".join(x for x in (state, stage) if x))
    if s.get("tapd_status"):
        parts.append(f"tapd:{s['tapd_status']}")
    url = s.get("tapd_url") or ""
    if url:
        parts.append(f"[TAPD]({url})")
    return " · ".join(parts)
