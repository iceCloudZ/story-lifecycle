"""story sync — 拉取 TAPD 需求/缺陷同步为本地 story。"""

import click
from rich.console import Console
from rich.table import Table

console = Console()


@click.command("sync")
@click.option("--dry-run", is_flag=True, help="只显示会创建/更新哪些，不实际执行")
@click.option("--status-only", is_flag=True, help="只更新现有 story 的 TAPD 状态")
@click.option("--workspace", "-w", default=None, help="新 story 的工作区目录")
@click.option(
    "--all", "-a", "fetch_all", is_flag=True, help="拉取全部需求/缺陷（忽略状态过滤）"
)
@click.option(
    "--id",
    "story_id",
    default=None,
    help="按 TAPD id 拉单个需求/缺陷，绕过 owner/状态过滤（fetch_pending 漏拉时用）",
)
def sync_cmd(dry_run, status_only, workspace, fetch_all, story_id):
    """拉取 TAPD 待处理需求/缺陷，同步为本地 story。"""
    from ..db.models import init_db
    from ..sources.tapd_source import TapdSource

    init_db()

    config = _load_tapd_config()
    if not config:
        console.print(
            "[red]TAPD 未配置。请先在 ~/.story-lifecycle/config.yaml 中添加 tapd 段。[/]"
        )
        console.print(
            '[dim]示例:\n  tapd:\n    workspace_id: "12345"\n    owner: "zhangsan"[/]'
        )
        raise SystemExit(1)

    source = TapdSource(config)

    if story_id:
        # Pull one by id, bypassing fetch_pending's owner (custom_field_25) /
        # parent_id filters — covers stories those filters skip.
        try:
            item = source.get_detail(story_id)
        except Exception as e:
            console.print(f"[red]TAPD 拉取失败: {e}[/]")
            raise SystemExit(1)
        if not item:
            console.print(f"[red]TAPD 未找到 id={story_id}[/]")
            raise SystemExit(1)
        items = [item]
        console.print(f"[cyan]按 id 拉取: {item.id} | {item.title}[/]")
    else:
        console.print("[bold cyan]正在拉取 TAPD 数据...[/]")
        try:
            items = source.fetch_pending(fetch_all=fetch_all)
        except Exception as e:
            console.print(f"[red]TAPD 拉取失败: {e}[/]")
            raise SystemExit(1)
        if not items:
            console.print("[green]没有待处理的需求或缺陷。[/]")
            return
        label = "全量" if fetch_all else "待处理"
        console.print(f"  拉取到 [cyan]{len(items)}[/] 个{label}项")

    if dry_run:
        _show_dry_run(items)
        return

    from ..orchestrator.sync_service import sync_tapd

    result = sync_tapd(
        items,
        workspace=workspace or ".",
        dry_run=dry_run,
        status_only=status_only,
    )

    console.print(
        f"\n[green]同步完成[/]: "
        f"新建 [cyan]{result['created']}[/] | "
        f"更新 [cyan]{result['updated']}[/] | "
        f"跳过 [dim]{result['skipped']}[/]"
    )


def _load_tapd_config() -> dict:
    from pathlib import Path
    import yaml

    config_file = Path.home() / ".story-lifecycle" / "config.yaml"
    if not config_file.exists():
        return {}
    with open(config_file, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("tapd", {})


def _show_dry_run(items):
    from ..db import models as db

    table = Table(title="Dry Run 预览")
    table.add_column("ID", style="cyan")
    table.add_column("类型")
    table.add_column("标题")
    table.add_column("优先级")
    table.add_column("截止日期")
    table.add_column("操作", style="green")

    for item in items:
        existing = db.find_by_source_id(item.source, item.id)
        action = "更新" if existing else "新建"
        item_type = "缺陷" if item.item_type == "bug" else "需求"
        table.add_row(
            item.id[:20],
            item_type,
            item.title[:40],
            item.priority,
            item.deadline,
            action,
        )

    console.print(table)
