"""CLI entry point — `story` command."""

import os
import sys

# Force UTF-8 on Windows (GBK can't encode Chinese + Unicode arrows)
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import click
from pathlib import Path

from rich.console import Console
from rich.table import Table

from ..db import models as db
from ..db.models import init_db
from ..orchestrator.service import (
    create_and_start_story,
    fail_story,
    skip_stage,
)
from .setup import is_configured, load_config_to_env, run_setup
from .doctor import run_doctor


console = Console()


@click.group()
@click.version_option(version="0.2.0")
def cli():
    """Story Lifecycle Manager — AI-powered development workflow orchestrator."""
    init_db()
    load_config_to_env()


# -------- story setup --------


@cli.command()
def setup():
    """Configure LLM API key and model (first-run wizard)."""
    run_setup()


# -------- story doctor --------


@cli.command()
def doctor():
    """Check system environment and available CLI tools."""
    run_doctor()


# -------- story new --------


@cli.command()
@click.argument("story_key")
@click.option("--title", "-t", default="", help="Story title")
@click.option(
    "--profile",
    "-p",
    default="minimal",
    help="Profile name (minimal, standard, custom)",
)
@click.option(
    "--workspace", "-w", default=None, help="Project workspace path (default: CWD)"
)
@click.option("--content", "-c", default=None, help="Path to PRD markdown file")
def new(story_key, title, profile, workspace, content):
    """Create a new story and start the first stage."""
    if not is_configured():
        console.print("[yellow]LLM API key not configured. Starting setup...[/]\n")
        run_setup()
        load_config_to_env()
        if not is_configured():
            return

    ws = workspace or os.getcwd()
    prd_content = ""

    # Read PRD from --content flag
    if content:
        prd_content = Path(content).read_text(encoding="utf-8")

    # If no PRD provided, ask user interactively
    if not prd_content:
        console.print("\n[bold yellow]No PRD provided.[/]")
        console.print("Paste PRD content, a file path, or press Enter to skip (AI will ask you).\n")

        raw = click.prompt("PRD / file path", default="", show_default=False)
        if raw.strip():
            p = Path(raw.strip())
            if p.exists() and p.is_file():
                prd_content = p.read_text(encoding="utf-8")
            else:
                # Treat as pasted content (may be single line)
                prd_content = raw
                console.print("[dim]Paste more lines (Ctrl+D or Ctrl+Z when done):[/]")
                try:
                    while True:
                        line = input()
                        prd_content += "\n" + line
                except EOFError:
                    pass

    prd_path = None
    if prd_content:
        prd_dir = Path(ws) / "prd"
        prd_dir.mkdir(exist_ok=True)
        prd_file = prd_dir / f"{story_key}.md"
        prd_file.write_text(prd_content, encoding="utf-8")
        prd_path = str(prd_file)

    create_and_start_story(
        story_key=story_key,
        title=title,
        profile=profile,
        workspace=ws,
        prd_path=prd_path,
    )
    s = db.get_story(story_key)

    console.print(f"\n[green]Story created: {s['story_key']}[/]")
    console.print(f"  Stage: {s['current_stage']}")
    console.print(f"  Workspace: {s['workspace']}")
    console.print("\n  [bold]story board[/] to manage and launch")


# -------- story board --------


@cli.command()
@click.option(
    "--no-tui", is_flag=True, help="Static table mode (for non-interactive terminals)"
)
def board(no_tui):
    """Show all active stories in a dashboard."""
    if not is_configured():
        console.print("[yellow]LLM API key not configured. Starting setup...[/]\n")
        run_setup()
        load_config_to_env()
        if not is_configured():
            return

    if no_tui or not sys.stdout.isatty():
        _board_static()
        return

    try:
        from .tui import run_tui

        run_tui()
    except ImportError:
        console.print("[yellow]textual not installed. Falling back to static mode.[/]")
        _board_static()


def _board_static():
    """Static table fallback."""
    stories = db.list_active_stories()

    if not stories:
        console.print("[dim]No active stories. Create one with: story new <KEY>[/]")
        return

    # Sort: parents first, then children grouped under parent
    parents = [s for s in stories if not s.get("parent_key")]
    children = [s for s in stories if s.get("parent_key")]

    display_list = []
    for p in parents:
        display_list.append(p)
        display_list.extend(sorted(
            [c for c in children if c["parent_key"] == p["story_key"]],
            key=lambda c: c.get("subtask_index", 0),
        ))
    # Orphans (children whose parent not in list)
    parent_keys = {p["story_key"] for p in parents}
    display_list.extend(c for c in children if c["parent_key"] not in parent_keys)

    table = Table(title="Story Board", show_lines=False)
    table.add_column("Story", style="cyan", no_wrap=True)
    table.add_column("Title", style="white")
    table.add_column("Stage", style="green")
    table.add_column("Status")
    table.add_column("Retries", justify="center")
    table.add_column("Workspace", style="dim")

    for s in display_list:
        status_str = {
            "active": "[bold green]> active[/]",
            "paused": "[bold yellow]|| paused[/]",
            "blocked": "[bold red]X blocked[/]",
            "completed": "[dim green]OK done[/]",
            "waiting_subtasks": "[bold magenta]≡ waiting subs[/]",
        }.get(s.get("status", ""), s.get("status", ""))

        is_child = bool(s.get("parent_key"))
        key_str = f"  └─ {s['story_key']}" if is_child else s.get("story_key", "")
        title_str = f"  {s.get('title', '')}" if is_child else (s.get("title") or "")[:40]

        table.add_row(
            key_str,
            title_str[:42],
            s.get("current_stage", ""),
            status_str,
            str(s.get("execution_count", 0)),
            s.get("workspace", ""),
        )

    console.print(table)
    console.print(
        "\n[dim]Commands: story new | story board | story skip <key> --stage <name> | story fail <key>[/]"
    )


# -------- story status --------


@cli.command()
@click.argument("story_key")
def status(story_key):
    """Show detailed status for a story."""
    s = db.get_story(story_key)
    if not s:
        console.print(f"[red]Story not found: {story_key}[/]")
        return

    console.print(f"[bold]{s['story_key']}[/] — {s.get('title', '')}")
    console.print(f"  Stage:    {s.get('current_stage', '')}")
    console.print(f"  Status:   {s.get('status', '')}")
    console.print(f"  Workspace:{s.get('workspace', '')}")
    console.print(f"  Profile:  {s.get('profile', 'minimal')}")
    console.print(f"  Retries:  {s.get('execution_count', 0)}")
    if s.get("last_error"):
        console.print(f"  [red]Error:   {s['last_error']}[/]")

    from ..orchestrator.router import llm_is_available

    if llm_is_available():
        console.print("  [dim]LLM Router: enabled[/]")
    else:
        console.print(
            "  [dim]LLM Router: disabled (set STORY_LLM_API_KEY to enable)[/]"
        )


# -------- story skip --------


@cli.command()
@click.argument("story_key")
@click.option("--stage", "-s", required=True, help="Stage to skip")
@click.option("--reason", "-r", default="Manual skip", help="Reason for skipping")
def skip(story_key, stage, reason):
    """Skip a stage and continue to the next one."""
    skip_stage(story_key, stage, reason)
    console.print(f"[green]Skipped {stage} for {story_key}[/]")


# -------- story fail --------


@cli.command()
@click.argument("story_key")
@click.option("--reason", "-r", default="Manual fail", help="Reason for failing")
def fail(story_key, reason):
    """Mark a story as failed/blocked."""
    fail_story(story_key, reason)
    console.print(f"[yellow]Marked {story_key} as blocked[/]")


# -------- story resume --------


@cli.command()
@click.argument("story_key")
def resume(story_key):
    """Resume a paused or blocked story."""
    db.update_story(story_key, status="active")
    console.print(f"[green]Resumed {story_key}[/]")


# -------- story log --------


@cli.command("log")
@click.argument("story_key")
def log_cmd(story_key):
    """Show event log for a story."""
    events = db.get_story_events(story_key)
    if not events:
        console.print(f"[dim]No events found for {story_key}[/]")
        return

    table = Table(title=f"Event Log: {story_key}", show_lines=False)
    table.add_column("Time", style="dim", width=19)
    table.add_column("Stage", style="cyan")
    table.add_column("Event", style="green")
    table.add_column("Detail", style="white")

    for e in events:
        payload = e.get("payload") or ""
        detail = ""
        if payload:
            try:
                import json
                data = json.loads(payload)
                detail = ", ".join(f"{k}={v}" for k, v in data.items())[:120]
            except (json.JSONDecodeError, TypeError):
                detail = str(payload)[:120]
        table.add_row(
            e.get("created_at", "")[:19],
            e.get("stage", ""),
            e.get("event_type", ""),
            detail,
        )

    console.print(table)


# -------- story serve --------


@cli.command()
@click.option("--host", default="127.0.0.1", help="Bind address")
@click.option("--port", default=8180, help="Bind port")
def serve(host, port):
    """Start the orchestrator server (for remote access)."""
    import uvicorn

    if not is_configured():
        console.print("[yellow]LLM API key not configured. Starting setup...[/]\n")
        run_setup()
        load_config_to_env()
        if not is_configured():
            return

    console.print(f"[green]Starting Story Lifecycle orchestrator on {host}:{port}[/]")
    console.print(f"[dim]Data directory: {db.get_db_path().parent}[/]")
    uvicorn.run(
        "story_lifecycle.orchestrator.api:app", host=host, port=port, reload=False
    )


if __name__ == "__main__":
    cli()
