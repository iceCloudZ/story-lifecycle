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


@click.group(invoke_without_command=True)
@click.version_option(
    version=__import__("importlib.metadata").metadata.version("story-lifecycle")
)
@click.option("--serve", is_flag=True, help="Start API server instead of board")
@click.option("--host", default="127.0.0.1", help="Server bind address")
@click.option("--port", default=8180, help="Server bind port")
@click.option(
    "--fix", "fix_deps", is_flag=True, help="Auto-install missing dependencies"
)
@click.pass_context
def cli(ctx, serve, host, port, fix_deps):
    """Story Lifecycle Manager — AI-powered development workflow orchestrator."""
    if ctx.invoked_subcommand is not None:
        return
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


@cli.command()
@click.argument("key")
@click.option("--title", "-t", default="", help="Story title")
@click.option("--prd", "-p", default=None, help="Path to PRD markdown file")
@click.option("--profile", default="minimal", help="Profile name (default: minimal)")
@click.option("--workspace", "-w", default=None, help="Workspace directory")
@click.option(
    "--no-start", is_flag=True, help="Create story without starting execution"
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    help="Print rendered prompts for each stage, do not execute",
)
def create(key, title, prd, profile, workspace, no_start, dry_run):
    """Create a new story and start its lifecycle.

    \b
    Examples:
      story create FEAT-001 -t "Add login"
      story create BUG-042 -p prd/bug-042.md
      story create FEAT-001 --dry-run
    """
    from ..orchestrator.service import create_and_start_story
    from ..orchestrator.graph import start_story_async
    from ..orchestrator.nodes import load_profile, _render_prompt

    init_db()
    ws = workspace or str(Path.cwd())

    key = create_and_start_story(
        story_key=key,
        title=title,
        profile=profile,
        workspace=ws,
        prd_path=prd or None,
    )

    console.print(f"\n[green]Story created:[/] [bold cyan]{key}[/]")
    if title:
        console.print(f"  Title: {title}")
    console.print(f"  Profile: [dim]{profile}[/]")
    console.print(f"  Workspace: [dim]{ws}[/]")

    if dry_run:
        console.print("\n[bold]Dry Run — stage prompts:[/]\n")
        profile_data = load_profile(profile)
        stages = profile_data.get("stages", {})
        # Build a minimal state for prompt rendering
        state = {
            "story_key": key,
            "title": title,
            "workspace": ws,
            "profile": profile,
            "current_stage": "",
            "context": {},
        }
        if prd:
            state["context"]["prd_path"] = prd
        for stage_name, stage_cfg in stages.items():
            state["current_stage"] = stage_name
            prompt = _render_prompt(stage_name, state)
            adapter = stage_cfg.get("cli", profile_data.get("cli", "claude"))
            model = stage_cfg.get("model", "sonnet")
            console.print(f"  [bold cyan]Stage: {stage_name}[/]")
            console.print(f"    Adapter: {adapter}  Model: {model}")
            preview = prompt[:500].replace("\n", "\n    ")
            console.print(f"    Prompt preview:\n    {preview}")
            if len(prompt) > 500:
                console.print(f"    [dim]... ({len(prompt)} chars total)[/]")
            console.print()
        return

    if not no_start:
        start_story_async(key)
        console.print("\n[dim]Graph started. Run [bold]story[/] to open the board.[/]")
    else:
        console.print(
            "\n[dim]Story created but not started. "
            "Run [bold]story[/] to open the board and press [[r]] to resume.[/]"
        )


@cli.command()
def demo():
    """Run a simulated lifecycle — no LLM, no AI CLI needed."""
    from .demo import run_demo

    run_demo()


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
