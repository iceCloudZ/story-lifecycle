"""1.6 — 迁移脚本:扫存量 story 的 done.json → story_doc 版本化记录。

DESIGN-artifact-driven-stage-completion §1.6 / STEP 1 子任务 1.6。

旧 story 用 done.json 自报协议(已废),新协议成果物驱动。本脚本把存量 story 的
`.story/done/<key>/<stage>.json` 读出来 → upsert_story_doc 落版本化记录,让旧 story
的产出也进 story_doc(可检索/可版本化/可确认)。

- 旧 done.json 不删(兼容期,miner 还在读 —— 1.5 双写)。
- 旧 story 跑完为止,不强制迁移(脚本提供,用户按需跑)。
- 挂 `story migrate-done` 子命令。提供 --dry-run。
"""

from __future__ import annotations

import logging
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

console = Console()
log = logging.getLogger("story-lifecycle.migrate_done")


def _scan_done_files(workspace: str) -> list[tuple[str, str, Path]]:
    """扫 workspace 下所有 .story/done/<key>/<stage>.json。

    Returns: [(story_key, stage, path), ...]
    """
    results = []
    done_root = Path(workspace) / ".story" / "done"
    if not done_root.exists():
        return results
    for story_dir in done_root.iterdir():
        if not story_dir.is_dir():
            continue
        story_key = story_dir.name
        for done_file in story_dir.glob("*.json"):
            stage = done_file.stem
            results.append((story_key, stage, done_file))
    return results


def _stage_to_doc_type(stage: str) -> str:
    """stage 名 → story_doc doc_type 映射。"""
    return {
        "design": "spec",
        "build": "plan",
        "verify": "test_report",
        "implement": "plan",
        "review": "review_verdict",
    }.get(stage, stage)


def migrate_done_to_artifact(
    workspace: str, *, dry_run: bool = False, db_module=None
) -> dict:
    """扫 workspace 下 done.json → upsert_story_doc 版本化记录。

    Returns: {scanned, migrated, skipped, details: [{story_key, stage, action, ...}]}
    """
    if db_module is None:
        from ...infra.db import models as db_module

    from ...infra.json_helpers import robust_json_parse

    done_files = _scan_done_files(workspace)
    details = []
    migrated = 0
    skipped = 0

    for story_key, stage, done_path in done_files:
        try:
            payload = robust_json_parse(done_path) or {}
        except Exception:  # noqa: BLE001
            payload = {}

        # 从 payload 取内容(优先 spec_path/test_report_path 指向的文件;否则用 summary)。
        content = ""
        files_changed = payload.get("files_changed") or []
        # 优先读成果物文件内容进 story_doc
        for ref in files_changed:
            ref_path = Path(ref)
            if not ref_path.is_absolute():
                ref_path = Path(workspace) / ref_path
            if ref_path.exists() and ref_path.stat().st_size > 0:
                try:
                    content = ref_path.read_text(encoding="utf-8", errors="replace")
                    break
                except OSError:
                    pass
        # 兜底:用 done.json 的 summary 作 content
        if not content:
            content = (
                payload.get("summary", "") or f"{stage} 阶段产出(迁移自 done.json)"
            )

        doc_type = _stage_to_doc_type(stage)

        if dry_run:
            details.append(
                {
                    "story_key": story_key,
                    "stage": stage,
                    "doc_type": doc_type,
                    "action": "would_migrate",
                    "done_file": str(done_path),
                }
            )
            continue

        try:
            version = db_module.upsert_story_doc(
                story_key,
                doc_type,
                content,
                change_reason=f"迁移自 done.json({stage})",
                author="migration",
            )
            migrated += 1
            details.append(
                {
                    "story_key": story_key,
                    "stage": stage,
                    "doc_type": doc_type,
                    "version": version,
                    "action": "migrated",
                    "done_file": str(done_path),
                }
            )
        except Exception as exc:  # noqa: BLE001
            skipped += 1
            details.append(
                {
                    "story_key": story_key,
                    "stage": stage,
                    "action": "failed",
                    "error": str(exc),
                    "done_file": str(done_path),
                }
            )

    return {
        "scanned": len(done_files),
        "migrated": migrated,
        "skipped": skipped + (len(done_files) if dry_run else 0),
        "dry_run": dry_run,
        "details": details,
    }


@click.command("migrate-done")
@click.option(
    "--workspace",
    "-w",
    default=None,
    help="工作区根(默认 cwd)。扫该工作区下 .story/done/<key>/*.json",
)
@click.option("--dry-run", is_flag=True, help="只打印会迁移什么,不真写 story_doc")
def migrate_done_cmd(workspace, dry_run):
    """扫存量 story 的 done.json → story_doc 版本化记录(STEP 1.6)。

    旧 story 用 done.json 自报(已废),本脚本把产出落进 story_doc(可检索/可版本化)。
    旧 done.json 不删(miner 兼容期)。按需跑,不强制。
    """
    from ...infra.db import models as db

    db.init_db()
    ws = workspace or str(Path.cwd())
    console.print(f"[cyan]扫描工作区:[/] {ws}")
    console.print(
        f"[cyan]模式:[/] {'dry-run(不写)' if dry_run else '迁移(写 story_doc)'}"
    )
    console.print()

    result = migrate_done_to_artifact(ws, dry_run=dry_run, db_module=db)

    if not result["details"]:
        console.print("[yellow]未找到任何 done.json 文件(无旧 story 需迁移)。[/]")
        return

    table = Table(title="done.json → story_doc 迁移")
    table.add_column("Story Key", style="cyan")
    table.add_column("Stage")
    table.add_column("doc_type")
    table.add_column("Action", style="green")
    for d in result["details"]:
        table.add_row(
            d["story_key"],
            d["stage"],
            d.get("doc_type", "-"),
            d["action"],
        )
    console.print(table)
    console.print()
    console.print(
        f"[green]扫描 {result['scanned']}[/] / "
        f"[green]迁移 {result['migrated']}[/] / "
        f"[yellow]跳过 {result['skipped']}[/]"
    )
    console.print("[dim]旧 done.json 保留(miner 兼容期,直到 P7 切换)。[/]")
