"""story calendar — 按 deadline 分组的日历视图。"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

import click
from rich.console import Console
from rich.text import Text

console = Console()

WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


@click.command("calendar")
@click.option("--days", "-d", default=14, help="显示未来 N 天的 story（默认 14）")
@click.option(
    "--type", "-t", "story_type", default=None, help="按类型筛选 (story/bug/subtask)"
)
@click.option(
    "--completed", "show_completed", is_flag=True, help="显示已完成的 story（默认隐藏）"
)
def calendar_cmd(days, story_type, show_completed):
    """日历视图 — 按 deadline 展示近期 story。"""
    from ...infra.db import models as db

    db.init_db()
    stories = _load_stories_with_deadlines(story_type, show_completed)

    if not stories:
        console.print("[dim]没有带截止日期的 story。[/]")
        return

    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")
    cutoff_str = (now + timedelta(days=days)).strftime("%Y-%m-%d")

    # 分组
    overdue: list[dict] = []
    by_date: dict[str, list[dict]] = defaultdict(list)

    for s in stories:
        dl = s["deadline"]
        if not dl:
            continue
        dl_date = dl[:10]
        if dl_date < today_str:
            overdue.append(s)
        elif dl_date <= cutoff_str:
            by_date[dl_date].append(s)

    # 渲染 —— 逾期组
    if overdue:
        console.print()
        console.print("[bold red]▸ 已逾期[/]")
        console.print("─" * 60)
        for s in overdue:
            _print_story_row(s, now, is_overdue=True)
        console.print()

    # 渲染 —— 按日期
    if not by_date:
        console.print(f"[dim]未来 {days} 天内没有到期的 story。[/]")
        return

    sorted_dates = sorted(by_date.keys())
    for dl_date in sorted_dates:
        dl_dt = datetime.fromisoformat(dl_date).replace(tzinfo=timezone.utc)
        weekday = WEEKDAY_NAMES[dl_dt.weekday()]
        is_today = dl_date == today_str
        delta_days = (dl_dt - now).days

        if is_today:
            header = f"[bold yellow]▸ {dl_date} (今天 {weekday})[/]"
        else:
            suffix = f"({delta_days}天后)" if delta_days <= 7 else ""
            header = f"[bold]{dl_date} ({weekday})[/] {suffix}"

        console.print()
        console.print(header)
        console.print("─" * 60)

        for s in by_date[dl_date]:
            _print_story_row(s, now, is_overdue=False)

        console.print()


def _load_stories_with_deadlines(
    story_type: str = "", show_completed: bool = False
) -> list[dict]:
    from ...infra.db import models as db

    active = db.list_active_stories()

    if story_type:
        active = [s for s in active if s.get("tapd_type") == story_type]

    if not show_completed:
        COMPLETED_STATES = {"resolved", "rejected", "closed"}
        active = [s for s in active if s.get("tapd_status") not in COMPLETED_STATES]

    return [s for s in active if s.get("deadline")]


def _print_story_row(s: dict, now: datetime, *, is_overdue: bool = False):
    key = s["story_key"]
    title = s.get("title", "")[:40]
    priority = s.get("priority", "")[:4]
    tapd_status = s.get("tapd_status", "")[:8]
    deadline = s.get("deadline", "") or ""

    if is_overdue:
        dl_dt = datetime.fromisoformat(deadline[:10]).replace(tzinfo=timezone.utc)
        overdue_days = (now - dl_dt).days
        prefix = "🔴"
        suffix = f"(逾期 {overdue_days} 天)"
        style = "bold red"
    else:
        dl_dt = datetime.fromisoformat(deadline[:10]).replace(tzinfo=timezone.utc)
        delta = (dl_dt - now).days
        if delta == 0:
            prefix = "🟡"
            suffix = "(今天到期)"
            style = "yellow"
        elif delta <= 3:
            prefix = "  "
            suffix = f"({delta}天后到期)"
            style = "yellow"
        else:
            prefix = "  "
            suffix = ""
            style = ""

    text = Text()
    text.append(f" {prefix} ", style=style)
    text.append(f"{key:<22}", style="cyan")
    text.append(f"{title:<42}")
    if priority:
        text.append(f"{priority:<6}")
    if tapd_status:
        text.append(f"[{tapd_status}] ", style="dim")
    if suffix:
        text.append(suffix, style=style)
    console.print(text)
