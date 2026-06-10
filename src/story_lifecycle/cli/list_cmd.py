"""story list / show / advance / done — 基础 story 管理 CLI 命令。"""

from __future__ import annotations

import json

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()


@click.command("list")
@click.option(
    "--status", "-s", default=None, help="按状态筛选 (active/paused/completed/failed)"
)
@click.option("--overdue", is_flag=True, help="只显示已逾期的 story")
@click.option(
    "--all", "show_all", is_flag=True, help="显示所有状态（含 completed/failed）"
)
@click.option(
    "--type", "-t", "story_type", default=None, help="按类型筛选 (story/bug/subtask)"
)
@click.option(
    "--completed", "show_completed", is_flag=True, help="显示已完成的 story（默认隐藏）"
)
def list_cmd(status, overdue, show_all, story_type, show_completed):
    """列出所有 story。"""
    from ..db import models as db

    db.init_db()

    if show_all:
        stories = db.list_active_stories() + db.list_completed_stories(limit=100)
    else:
        stories = db.list_active_stories()

    if status:
        stories = [s for s in stories if s["status"] == status]

    if story_type:
        stories = [s for s in stories if s.get("tapd_type") == story_type]

    # Hide completed (resolved/rejected/closed) by default
    if not show_completed:
        COMPLETED_STATES = {"resolved", "rejected", "closed"}
        stories = [s for s in stories if s.get("tapd_status") not in COMPLETED_STATES]

    if overdue:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        stories = [s for s in stories if s.get("deadline") and s["deadline"][:10] < now]

    if not stories:
        console.print("[dim]没有 story。运行 [bold]story sync[/] 从 TAPD 拉取需求。[/]")
        return

    table = Table()
    table.add_column("类型", max_width=6)
    table.add_column("KEY", style="cyan", max_width=20)
    table.add_column("标题", max_width=35)
    table.add_column("优先级", max_width=6)
    table.add_column("截止", max_width=10)
    table.add_column("阶段", max_width=10)
    table.add_column("状态", max_width=8)
    table.add_column("TAPD", max_width=10)

    from datetime import datetime, timezone

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for s in stories:
        deadline = s.get("deadline", "") or ""
        deadline_display = deadline[:10] if deadline else ""

        stage = s["current_stage"]
        st = s["status"]
        tapd_st = s.get("tapd_status", "") or ""

        deadline_style = ""
        if deadline and deadline[:10] < now_str:
            deadline_style = "bold red"
        elif deadline:
            try:
                dl = datetime.fromisoformat(deadline[:10]).replace(tzinfo=timezone.utc)
                delta = (dl - datetime.now(timezone.utc)).days
                if delta <= 3:
                    deadline_style = "yellow"
            except ValueError:
                pass

        TYPE_LABELS = {"bug": "缺陷", "story": "需求", "subtask": "子任务"}
        tapd_type = s.get("tapd_type", "")
        table.add_row(
            TYPE_LABELS.get(tapd_type, tapd_type),
            s["story_key"],
            s.get("title", "")[:35],
            s.get("priority", "")[:6],
            f"[{deadline_style}]{deadline_display}[/]"
            if deadline_style
            else deadline_display,
            stage,
            st,
            tapd_st[:10],
        )

    console.print(table)
    console.print(f"[dim]共 {len(stories)} 个 story[/]")


@click.command("show")
@click.argument("key")
def show_cmd(key):
    """查看 story 详情。"""
    from ..db import models as db

    db.init_db()
    s = db.get_story(key)
    if not s:
        console.print(f"[red]Story {key} 不存在[/]")
        raise SystemExit(1)

    lines = []
    lines.append(f"[bold cyan]{s['story_key']}[/]")
    lines.append(f"  标题: {s.get('title', '')}")
    lines.append(f"  状态: {s['status']}")
    lines.append(f"  阶段: {s['current_stage']}")
    lines.append(f"  Profile: {s.get('profile', '')}")
    lines.append(f"  工作区: {s.get('workspace', '')}")

    if s.get("deadline"):
        lines.append(f"  截止日期: {s['deadline']}")
    if s.get("priority"):
        lines.append(f"  优先级: {s['priority']}")
    if s.get("owner"):
        lines.append(f"  处理人: {s['owner']}")
    if s.get("tapd_status"):
        lines.append(f"  TAPD 状态: {s['tapd_status']}")
    if s.get("tapd_url"):
        lines.append(f"  TAPD 链接: {s['tapd_url']}")

    branches_raw = s.get("branches_json", "[]")
    if isinstance(branches_raw, str):
        try:
            branches = json.loads(branches_raw)
        except (json.JSONDecodeError, TypeError):
            branches = []
    else:
        branches = branches_raw or []
    if branches:
        lines.append("  关联分支:")
        for b in branches:
            lines.append(
                f"    - {b.get('repo', '')}/{b.get('branch', '')} ({b.get('status', '')})"
            )

    if s.get("last_error"):
        lines.append(f"  [red]最后错误: {s['last_error'][:100]}[/]")

    console.print(Panel("\n".join(lines)))

    logs = db.get_stage_logs(key, limit=10)
    if logs:
        console.print("\n[bold]最近操作:[/]")
        for log_entry in logs:
            console.print(
                f"  [{log_entry.get('created_at', '')[:16]}] "
                f"{log_entry['stage']} — {log_entry['action']}"
                + (f" ({log_entry['detail'][:50]})" if log_entry.get("detail") else "")
            )


@click.command("advance")
@click.argument("key")
def advance_cmd(key):
    """手动推进 story 到下一阶段。"""
    from ..db import models as db

    db.init_db()
    s = db.get_story(key)
    if not s:
        console.print(f"[red]Story {key} 不存在[/]")
        raise SystemExit(1)

    STAGE_ORDER = ["design", "implement", "test", "done"]
    current = s["current_stage"]

    if current == "done":
        console.print("[yellow]Story 已完成，无法继续推进。[/]")
        return

    try:
        idx = STAGE_ORDER.index(current)
    except ValueError:
        console.print(f"[red]未知阶段: {current}[/]")
        return

    next_stage = STAGE_ORDER[idx + 1] if idx + 1 < len(STAGE_ORDER) else "done"

    db.update_story(key, current_stage=next_stage)
    db.log_stage(key, next_stage, "advance", f"手动推进: {current} → {next_stage}")

    if next_stage == "done":
        db.update_story(key, status="completed")

    console.print(f"[green]{current} → {next_stage}[/]")


@click.command("done")
@click.argument("key")
def done_cmd(key):
    """标记 story 完成。"""
    from ..db import models as db

    db.init_db()
    s = db.get_story(key)
    if not s:
        console.print(f"[red]Story {key} 不存在[/]")
        raise SystemExit(1)

    db.update_story(key, current_stage="done", status="completed")
    db.log_stage(key, "done", "complete", "手动标记完成")

    console.print(f"[green]Story {key} 已标记完成[/]")

    if s.get("source_type") == "tapd" and s.get("source_id"):
        console.print(
            f"[dim]提示: TAPD 状态未自动同步。"
            f"可手动到 {s.get('tapd_url', 'TAPD')} 更新状态。[/]"
        )
