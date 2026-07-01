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
from .doctor import run_doctor, run_doctor_fix, has_missing_tools, run_linkage_health

console = Console()

# Track first-run state
_FIRST_RUN_MARKER = Path.home() / ".story-lifecycle" / ".initialized"


def _cleanup_broken_dists():
    """Remove ~-prefixed broken distributions left by interrupted pip installs."""
    try:
        import shutil
        import site

        sp = site.getsitepackages()[0]
        broken = [d for d in os.listdir(sp) if d.startswith("~")]
        if broken:
            for d in broken:
                shutil.rmtree(os.path.join(sp, d), ignore_errors=True)
    except Exception:
        pass


def _protect_config():
    """Auto-backup config.yaml on startup; restore if missing."""
    config_dir = Path.home() / ".story-lifecycle"
    config_file = config_dir / "config.yaml"
    backup_file = config_dir / "config.yaml.bak"

    if config_file.exists():
        try:
            import shutil as _shutil

            _shutil.copy2(config_file, backup_file)
        except Exception:
            pass
    elif backup_file.exists():
        try:
            import shutil as _shutil

            _shutil.copy2(backup_file, config_file)
        except Exception:
            pass


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


def _get_version():
    try:
        return __import__("importlib.metadata").metadata.version("story-lifecycle")
    except Exception:
        pass
    # Fallback: read __version__ from the package (works for editable installs
    # where importlib.metadata may not be populated)
    try:
        from .. import __version__

        if __version__ and __version__ != "0.1.0":
            return __version__
    except Exception:
        pass
    return "unknown"


@click.group(invoke_without_command=True)
@click.version_option(version=_get_version(), message="%(prog)s %(version)s")
@click.option("--serve", is_flag=True, help="Start API server without opening browser")
@click.option("--host", default="127.0.0.1", help="Server bind address")
@click.option("--port", default=8180, help="Server bind port")
@click.option(
    "--fix", "fix_deps", is_flag=True, help="Auto-install missing dependencies"
)
@click.pass_context
def cli(ctx, serve, host, port, fix_deps):
    """Story Lifecycle Manager — AI-powered development workflow orchestrator."""
    _cleanup_broken_dists()
    _protect_config()
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
            "diagnostics",
            "project",
            "sync",
            "list",
            "show",
            "advance",
            "done",
            "calendar",
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

    # Default: launch web board (open browser)
    if not _first_run_check():
        return

    if not is_configured():
        console.print("[yellow]LLM API key not configured.[/]\n")
        if console.input("[bold]Run setup wizard now? [Y/n][/] ").strip().lower() in (
            "",
            "y",
            "yes",
        ):
            run_setup()
            load_config_to_env()

    _run_web_board(host, port)


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
    from ..orchestrator.service.story_service import create_and_start_story
    from ..orchestrator.engine.graph import start_story_async
    from ..orchestrator.nodes import load_profile
    from ..orchestrator.engine.prompt_renderer import _render_prompt

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

    # 检查知识包是否存在
    from ..knowledge.paths import manifest_path as _km

    if not _km(ws).exists():
        console.print(
            "[yellow]当前项目尚未初始化项目知识包。建议先运行：[/]\n"
            "  [bold]story project init-knowledge[/]\n"
            "[dim]继续创建 story 也可以，但 AI 将缺少项目级业务/代码上下文。[/]\n"
        )

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

    pip_cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "story-lifecycle"]

    if sys.platform == "win32":
        pid = os.getpid()
        python_exe = sys.executable
        bat = f"""@echo off
:wait
timeout /t 2 /nobreak >nul
tasklist /fi "PID eq {pid}" 2>nul | findstr /c:"{pid}" >nul 2>&1
if not errorlevel 1 goto wait
"{python_exe}" -m pip install --upgrade story-lifecycle
del "%~f0" 2>nul
"""
        _ensure_dir = Path.home() / ".story-lifecycle"
        _ensure_dir.mkdir(parents=True, exist_ok=True)
        bat_path = _ensure_dir / "upgrade.bat"
        bat_path.write_text(bat, encoding="ascii")
        subprocess.Popen(
            f'start "" "{bat_path}"',
            shell=True,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
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

from .sync_cmd import sync_cmd  # noqa: E402

cli.add_command(sync_cmd)

from .list_cmd import list_cmd, show_cmd, advance_cmd, done_cmd  # noqa: E402

cli.add_command(list_cmd)
cli.add_command(show_cmd)
cli.add_command(advance_cmd)
cli.add_command(done_cmd)

from .calendar_cmd import calendar_cmd  # noqa: E402

cli.add_command(calendar_cmd)

from .project import project  # noqa: E402

cli.add_command(project)


@cli.group(invoke_without_command=True, no_args_is_help=False)
@click.pass_context
def doctor(ctx):
    """System diagnostics and maintenance."""
    if ctx.invoked_subcommand is None:
        run_doctor()


@doctor.command()
def paths():
    """Scan for legacy .story-done, .story-context, .story-runs directories."""
    from ..orchestrator.workspace.doctor_paths import run_doctor_paths

    run_doctor_paths()


@doctor.command()
def linkage():
    """Check story↔git hard-linkage health (% hard / orphan branches)."""
    run_linkage_health()


def _run_web_board(host, port):
    """Start FastAPI server with web frontend and open browser."""
    import uvicorn
    import webbrowser
    import threading

    from ..db import models as db

    web_dir = Path(__file__).parent.parent / "web"
    if not (web_dir / "index.html").exists():
        console.print(
            "[red]Web frontend not found. Build it first or install from PyPI.[/]"
        )
        raise SystemExit(1)

    url = f"http://{host}:{port}"
    console.print(f"[green]Starting Story Lifecycle web board on {url}[/]")
    console.print(f"[dim]Data directory: {db.get_db_path().parent}[/]")
    console.print("[dim]Press Ctrl+C to stop.[/]")

    def open_browser():
        import time

        time.sleep(1.5)
        webbrowser.open(url)

    threading.Thread(target=open_browser, daemon=True).start()
    uvicorn.run(
        "story_lifecycle.orchestrator.service.api:app", host=host, port=port, reload=False
    )


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

    # Surface app-level logs (planner / api) at INFO alongside uvicorn's access logs.
    import logging

    _sl = logging.getLogger("story-lifecycle")
    if not _sl.handlers:
        _h = logging.StreamHandler()
        _h.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
        )
        _sl.addHandler(_h)
    _sl.setLevel(logging.INFO)

    uvicorn.run(
        "story_lifecycle.orchestrator.service.api:app", host=host, port=port, reload=False
    )


from .diagnostics import diagnostics  # noqa: E402

cli.add_command(diagnostics)

from .plan_cmd import plan  # noqa: E402

cli.add_command(plan)

if __name__ == "__main__":
    cli()
