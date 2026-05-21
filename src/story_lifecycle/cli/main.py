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
from ..terminal import ttyd
from .setup import is_configured, load_config_to_env, run_setup
from .doctor import run_doctor


console = Console()


def _get_client():
    """Lazy import httpx to avoid dependency on CLI startup."""
    import httpx

    server = os.environ.get("STORY_SERVER", "http://127.0.0.1:8180")
    return httpx.Client(base_url=server)


@click.group()
@click.version_option(version="0.1.0")
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
    ws = workspace or os.getcwd()
    prd_content = ""

    # Read PRD from --content flag
    if content:
        prd_content = Path(content).read_text(encoding="utf-8")

    # If no PRD provided, ask user interactively
    if not prd_content:
        console.print("\n[bold yellow]No PRD provided.[/]")
        console.print("The AI needs requirements to work with.\n")
        console.print("  [1] Paste PRD content directly (type or paste, then Ctrl+D)")
        console.print("  [2] Provide path to PRD markdown file")
        console.print("  [3] Skip for now (AI will ask you in the terminal)\n")

        choice = click.prompt("Choice", type=int, default=3)
        if choice == 1:
            console.print("[dim]Paste PRD content (Ctrl+D or Ctrl+Z when done):[/]")
            lines = []
            try:
                while True:
                    line = input()
                    lines.append(line)
            except EOFError:
                pass
            prd_content = "\n".join(lines)
        elif choice == 2:
            path = click.prompt("PRD file path", type=str)
            prd_content = Path(path).read_text(encoding="utf-8")

    try:
        client = _get_client()
        resp = client.post(
            "/api/story",
            json={
                "key": story_key,
                "title": title,
                "content": prd_content,
                "profile": profile,
                "workspace": ws,
            },
        )
        if resp.status_code != 200:
            console.print(f"[red]Error: {resp.json().get('detail', 'Unknown')}[/]")
            return
        data = resp.json()
        console.print(f"\n[green]Story created: {data['storyKey']}[/]")
        console.print(f"  Stage: {data['currentStage']}")
        console.print(f"  Workspace: {data['workspace']}")
        console.print(f"\n  Open terminal: [bold]story enter {data['storyKey']}[/]")
    except Exception as e:
        console.print(f"[red]Failed to connect to orchestrator: {e}[/]")
        console.print(
            "[yellow]Is the orchestrator running? ('story serve' in another terminal)[/]"
        )


# -------- story board --------


@cli.command()
def board():
    """Show all active stories in a dashboard."""
    try:
        client = _get_client()
        resp = client.get("/api/story")
        stories = resp.json() if resp.status_code == 200 else []
    except Exception:
        # Fallback to direct DB read
        stories = db.list_active_stories()

    if not stories:
        console.print("[dim]No active stories. Create one with: story new <KEY>[/]")
        return

    table = Table(title="Story Board", show_lines=False)
    table.add_column("Story", style="cyan", no_wrap=True)
    table.add_column("Title", style="white")
    table.add_column("Stage", style="green")
    table.add_column("Status")
    table.add_column("Retries", justify="center")
    table.add_column("Workspace", style="dim")

    for s in stories:
        status_str = {
            "active": "[bold green]> active[/]",
            "paused": "[bold yellow]|| paused[/]",
            "blocked": "[bold red]X blocked[/]",
            "completed": "[bold blue]OK completed[/]",
        }.get(s.get("status", ""), s.get("status", ""))

        table.add_row(
            s.get("story_key", ""),
            (s.get("title") or "")[:40],
            s.get("current_stage", ""),
            status_str,
            str(s.get("execution_count", 0)),
            s.get("workspace", ""),
        )

    console.print(table)
    console.print(
        "\n[dim]Commands: story new | story enter <key> | story skip <key> --stage <name> | story fail <key>[/]"
    )


# -------- story enter --------


@cli.command()
@click.argument("story_key")
def enter(story_key):
    """Open the ttyd terminal for a story to interact with the AI."""
    try:
        client = _get_client()
        resp = client.get(f"/api/session/terminal/{story_key}")
        if resp.status_code != 200:
            console.print(f"[red]Story not found: {story_key}[/]")
            return
        data = resp.json()
    except Exception:
        # Fallback: read workspace from local DB
        s = db.get_story(story_key)
        if not s:
            console.print(f"[red]Story not found: {story_key}[/]")
            return
        url = ttyd.ensure_ttyd(story_key, s["workspace"])
        data = {"url": url}

    console.print(f"[green]Opening terminal for {story_key}...[/]")
    console.print(f"  URL: {data['url']}")
    console.print(f"\n[bold]Open in browser:[/] http://localhost:8180{data['url']}")
    console.print(f"  Or direct: [bold]tmux attach -t s-{story_key}[/]")


# -------- story status --------


@cli.command()
@click.argument("story_key")
def status(story_key):
    """Show detailed status for a story."""
    try:
        client = _get_client()
        resp = client.get(f"/api/story/{story_key}")
        s = resp.json() if resp.status_code == 200 else None
    except Exception:
        s = db.get_story(story_key)

    if not s:
        console.print(f"[red]Story not found: {story_key}[/]")
        return

    console.print(f"[bold]{s['storyKey']}[/] — {s.get('title', '')}")
    console.print(f"  Stage:    {s.get('currentStage', '')}")
    console.print(f"  Status:   {s.get('status', '')}")
    console.print(f"  Workspace:{s.get('workspace', '')}")
    console.print(f"  Profile:  {s.get('profile', 'minimal')}")
    console.print(f"  Retries:  {s.get('executionCount', 0)}")
    if s.get("lastError"):
        console.print(f"  [red]Error:   {s['lastError']}[/]")

    # Show LLM router status
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
    try:
        client = _get_client()
        resp = client.put(
            f"/api/story/{story_key}/skip/{stage}", json={"reason": reason}
        )
        if resp.status_code == 200:
            console.print(f"[green]Skipped {stage} for {story_key}[/]")
        else:
            console.print(f"[red]Failed: {resp.json()}[/]")
    except Exception as e:
        console.print(f"[red]Failed: {e}[/]")


# -------- story fail --------


@cli.command()
@click.argument("story_key")
@click.option("--reason", "-r", default="Manual fail", help="Reason for failing")
def fail(story_key, reason):
    """Mark a story as failed/blocked."""
    try:
        client = _get_client()
        resp = client.put(f"/api/story/{story_key}/fail", json={"reason": reason})
        if resp.status_code == 200:
            console.print(f"[yellow]Marked {story_key} as blocked[/]")
        else:
            console.print(f"[red]Failed: {resp.json()}[/]")
    except Exception as e:
        console.print(f"[red]Failed: {e}[/]")


# -------- story resume --------


@cli.command()
@click.argument("story_key")
def resume(story_key):
    """Resume a paused or blocked story."""
    try:
        client = _get_client()
        resp = client.put(
            f"/api/story/{story_key}/advance", json={"description": "Resumed by user"}
        )
        if resp.status_code == 200:
            console.print(f"[green]Resumed {story_key}[/]")
        else:
            console.print(f"[red]Failed: {resp.json()}[/]")
    except Exception as e:
        console.print(f"[red]Failed: {e}[/]")


# -------- story serve --------


@cli.command()
@click.option("--host", default="127.0.0.1", help="Bind address")
@click.option("--port", default=8180, help="Bind port")
def serve(host, port):
    """Start the orchestrator server."""
    import uvicorn

    if not is_configured():
        console.print("\n[yellow]No LLM API key configured.[/]")
        console.print("Run [bold]story setup[/] to configure your API key.\n")

    console.print(f"[green]Starting Story Lifecycle orchestrator on {host}:{port}[/]")
    console.print(f"[dim]Data directory: {db.get_db_path().parent}[/]")
    uvicorn.run(
        "story_lifecycle.orchestrator.api:app", host=host, port=port, reload=False
    )


if __name__ == "__main__":
    cli()
