"""CLI entry point — `story` command (launches board directly)."""

import os
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
    init_db()
    load_config_to_env()

    if ctx.invoked_subcommand is not None:
        if ctx.invoked_subcommand not in (
            "setup",
            "serve",
            "doctor",
            "demo",
            "upgrade",
            "swebench",
        ):
            if not is_configured():
                console.print(
                    "[yellow]LLM API key not configured — launching setup wizard.[/]\n"
                )
                run_setup()
                load_config_to_env()
                if not is_configured():
                    console.print(
                        "[red]Setup incomplete. Run 'story' again to retry.[/]"
                    )
                    raise SystemExit(1)
        return

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
            prompt, _ = _render_prompt(stage_name, state)
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
def setup():
    """Configure LLM provider and API key."""
    run_setup()
    load_config_to_env()


@cli.command()
@click.option("--host", default="127.0.0.1", help="Server bind address")
@click.option("--port", default=8180, help="Server bind port")
def serve(host, port):
    """Start the API server."""
    _run_server(host, port)


@cli.command()
def demo():
    """Run a simulated lifecycle — no LLM, no AI CLI needed."""
    from .demo import run_demo

    run_demo()


def _run_upgrade():
    """Run pip upgrade. On Windows, spawn a detached child to avoid exe lock."""
    import subprocess
    import tempfile

    pip_cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "story-lifecycle"]

    if sys.platform == "win32":
        # story.exe is locked by this process — write a bat script that waits for
        # this PID to exit, then runs pip.  We use `tasklist` polling because
        # `wait /b <pid>` only works for child processes.
        pid = os.getpid()
        bat = f"""@echo off
:wait
tasklist /fi "PID eq {pid}" /nh 2>nul | find "{pid}" >nul
if %errorlevel%==0 (
    timeout /t 2 /nobreak >nul
    goto wait
)
{sys.executable} -m pip install --upgrade story-lifecycle
del "%~f0"
"""
        bat_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".bat", delete=False, encoding="utf-8"
        )
        bat_file.write(bat)
        bat_file.close()
        subprocess.Popen(
            [bat_file.name],
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS,
            close_fds=True,
            shell=True,
        )
        console.print("  [dim]Upgrade will run after this process exits.[/]")
        raise SystemExit(0)

    return subprocess.run(pip_cmd, capture_output=True, text=True)


@cli.command()
def upgrade():
    """Upgrade story-lifecycle to the latest version."""
    from importlib.metadata import version as _pkg_version

    current = _pkg_version("story-lifecycle")
    console.print(f"  Current version: [cyan]{current}[/]")

    # Clean up broken ~-prefixed distributions left by interrupted pip installs
    import shutil
    import site

    sp = site.getsitepackages()[0]
    broken = [d for d in os.listdir(sp) if d.startswith("~")]
    if broken:
        console.print(f"  Cleaning {len(broken)} broken distribution(s)...")
        for d in broken:
            shutil.rmtree(os.path.join(sp, d), ignore_errors=True)

    console.print("  Upgrading...")

    result = _run_upgrade()
    if result.returncode == 0:
        new = _pkg_version("story-lifecycle")
        console.print(f"  [green]Upgraded to {new}[/]")
    else:
        console.print(f"  [red]Upgrade failed:[/]\n{result.stderr[:500]}")
        raise SystemExit(1)


from .seed_quality import seed_quality_group  # noqa: E402

cli.add_command(seed_quality_group)

from .review_feedback import review_feedback_group, approvals_group, findings_cmd  # noqa: E402

cli.add_command(review_feedback_group)
cli.add_command(approvals_group)
cli.add_command(findings_cmd)

from .swebench import swebench_group  # noqa: E402

cli.add_command(swebench_group)


@cli.group(invoke_without_command=True, no_args_is_help=False)
@click.pass_context
def doctor(ctx):
    """System diagnostics and maintenance."""
    if ctx.invoked_subcommand is None:
        run_doctor()


@doctor.command()
def paths():
    """Scan for legacy .story-done, .story-context, .story-runs directories."""
    from ..orchestrator.doctor_paths import run_doctor_paths

    run_doctor_paths()


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
            console.print("[red]LLM API key is required to run the server.[/]")
            console.print("Run 'story setup' to configure, or set STORY_LLM_API_KEY.")
            raise SystemExit(1)

    from ..db import models as db

    console.print(f"[green]Starting Story Lifecycle orchestrator on {host}:{port}[/]")
    console.print(f"[dim]Data directory: {db.get_db_path().parent}[/]")
    uvicorn.run(
        "story_lifecycle.orchestrator.api:app", host=host, port=port, reload=False
    )


if __name__ == "__main__":
    cli()
