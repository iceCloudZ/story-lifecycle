"""CLI entry point — `story` command (launches board directly)."""

import sys

# Force UTF-8 on Windows (GBK can't encode Chinese + Unicode arrows)
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import click
from pathlib import Path

from rich.console import Console

from ..db.models import init_db
from .setup import is_configured, load_config_to_env, run_setup
from .doctor import run_doctor, run_doctor_fix, has_missing_tools

console = Console()

# Track first-run state
_FIRST_RUN_MARKER = Path.home() / ".story-lifecycle" / ".initialized"


def _first_run_check():
    """On first run: doctor check + setup wizard. Skip on subsequent runs."""
    if _FIRST_RUN_MARKER.exists():
        return True

    console.print("\n[bold cyan]First run — checking environment...[/]\n")
    run_doctor()
    console.print()

    if has_missing_tools():
        answer = (
            console.input("[bold]Install missing tools now? [Y/n][/] ").strip().lower()
        )
        if answer in ("", "y", "yes"):
            run_doctor_fix(interactive=True)
            console.print()

    console.input("[bold]Press Enter to continue...[/]")
    console.print()

    if not is_configured():
        console.print("[yellow]LLM API key not configured.[/]\n")
        run_setup()
        load_config_to_env()
        if not is_configured():
            console.print("[red]Setup incomplete. Run `story` again to retry.[/]\n")
            return False

    _FIRST_RUN_MARKER.parent.mkdir(parents=True, exist_ok=True)
    _FIRST_RUN_MARKER.write_text("ok", encoding="utf-8")
    console.print("[green]Setup complete. Launching board...[/]\n")
    return True


@click.command()
@click.version_option(
    version=__import__("importlib.metadata").metadata.version("story-lifecycle")
)
@click.option("--serve", is_flag=True, help="Start API server instead of board")
@click.option("--host", default="127.0.0.1", help="Server bind address")
@click.option("--port", default=8180, help="Server bind port")
@click.option(
    "--fix", "fix_deps", is_flag=True, help="Auto-install missing dependencies"
)
def cli(serve, host, port, fix_deps):
    """Story Lifecycle Manager — AI-powered development workflow orchestrator."""
    init_db()
    load_config_to_env()

    if fix_deps:
        run_doctor_fix(interactive=True)
        return

    if serve:
        _run_server(host, port)
        return

    if not _first_run_check():
        return

    _run_board()


def _run_board():
    """Launch the interactive TUI board."""
    try:
        from .tui import run_tui

        run_tui()
    except ImportError:
        console.print("[yellow]textual not installed. Falling back to static mode.[/]")
        from ..db import models as db

        stories = db.list_active_stories()
        if not stories:
            console.print("[dim]No active stories. Press [[n]] to create one.[/]")
        else:
            from rich.table import Table

            table = Table(title="Story Board")
            table.add_column("Story", style="cyan")
            table.add_column("Title", style="white")
            table.add_column("Stage", style="green")
            table.add_column("Status")
            for s in stories:
                table.add_row(
                    s.get("story_key", ""),
                    (s.get("title") or "")[:40],
                    s.get("current_stage", ""),
                    s.get("status", ""),
                )
            console.print(table)


def _run_server(host, port):
    """Start the API server."""
    import uvicorn

    if not is_configured():
        console.print("[yellow]LLM API key not configured. Starting setup...[/]\n")
        run_setup()
        load_config_to_env()
        if not is_configured():
            return

    from ..db import models as db

    console.print(f"[green]Starting Story Lifecycle orchestrator on {host}:{port}[/]")
    console.print(f"[dim]Data directory: {db.get_db_path().parent}[/]")
    uvicorn.run(
        "story_lifecycle.orchestrator.api:app", host=host, port=port, reload=False
    )


if __name__ == "__main__":
    cli()
