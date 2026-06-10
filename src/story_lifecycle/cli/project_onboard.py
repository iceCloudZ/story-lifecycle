"""Workspace Onboarding CLI commands — inspect, onboard, confirm, refresh, probe.

These commands are registered on the ``project`` Click group from cli/project.py.
They implement the Workspace Onboarding & Project Profile design.
"""

from __future__ import annotations

import click
from pathlib import Path
from rich.console import Console

from .project import project  # register on existing group

console = Console()


@project.command("inspect")
@click.option("-w", "--workspace", default=None, help="工作区目录（默认当前目录）")
@click.option("--json", "as_json", is_flag=True, help="输出原始 JSON")
def inspect(workspace, as_json):
    """Deterministic scan — 输出 observed facts，不写 confirmed profile。"""
    from ..orchestrator.project_scan import scan_workspace
    from ..orchestrator.project_profile import _to_dict

    ws = Path(workspace or Path.cwd()).resolve()
    profile = scan_workspace(ws)

    if as_json:
        import json as _json

        console.print(_json.dumps(_to_dict(profile), ensure_ascii=False, indent=2))
        return

    console.print("\n[bold cyan]Project Inspection[/]")
    console.print(f"  Workspace: [dim]{ws}[/]")
    console.print(f"  Type: [cyan]{profile.workspace_type}[/]")
    console.print(f"  Confidence: [cyan]{profile.confidence}[/]")

    if profile.repos:
        console.print(f"\n  Repos ([bold]{len(profile.repos)}[/]):")
        for repo in profile.repos:
            dirty_mark = "[red]*[/]" if repo.dirty else " "
            console.print(
                f"    {dirty_mark} {repo.id} ({repo.repo_type}) "
                f"[dim]{', '.join(repo.languages) or 'unknown lang'}[/]"
            )

    if profile.test_sources:
        console.print(f"\n  Test candidates ([bold]{len(profile.test_sources)}[/]):")
        for ts in profile.test_sources:
            console.print(f"    {ts.command} [dim]({ts.repo_id})[/]")

    if profile.release_profile.scale != "unknown":
        console.print(f"\n  Release scale: [cyan]{profile.release_profile.scale}[/]")

    if profile.facts:
        console.print(f"\n  Facts: [bold]{len(profile.facts)}[/]")


@project.command("onboard")
@click.option("-w", "--workspace", default=None, help="工作区目录（默认当前目录）")
@click.option("--force", is_flag=True, help="强制重新扫描（覆盖已有 profile）")
@click.option("--yes", "-y", is_flag=True, help="非交互模式，自动接受扫描结果")
def onboard(workspace, force, yes):
    """执行 scan -> 确认流程 -> 写 Project Profile。"""
    from ..orchestrator.project_scan import scan_workspace
    from ..orchestrator.project_profile import (
        load_profile,
        save_profile,
        profile_path,
    )

    ws = Path(workspace or Path.cwd()).resolve()

    existing = load_profile(ws)
    if existing and not force:
        console.print("\n[yellow]Project Profile already exists:[/]")
        console.print(f"  Path: [dim]{profile_path(ws)}[/]")
        console.print(
            f"  Type: {existing.workspace_type}  Repos: {len(existing.repos)}"
        )
        console.print(
            "\n  Use [bold]--force[/] to re-scan, or [bold]story project confirm[/] to edit."
        )
        return

    console.print("\n[bold cyan]Workspace Onboarding[/]")
    console.print(f"  Workspace: [dim]{ws}[/]")
    console.print("\n  Scanning...")

    profile = scan_workspace(ws)

    # Display summary
    console.print("\n  Detected:")
    console.print(f"    workspace type: [cyan]{profile.workspace_type}[/]")
    if profile.repos:
        console.print(f"    git repos: [bold]{len(profile.repos)}[/]")
        backend = sum(1 for r in profile.repos if r.repo_type == "backend")
        frontend = sum(1 for r in profile.repos if r.repo_type == "frontend")
        if backend:
            console.print(f"    likely backend repos: {backend}")
        if frontend:
            console.print(f"    likely frontend repos: {frontend}")
    if profile.test_sources:
        console.print(f"    test command candidates: {len(profile.test_sources)}")
    if profile.doc_assets:
        console.print(f"    doc assets: {len(profile.doc_assets)}")

    if yes:
        console.print("\n  [dim]--yes mode, accepting scan results.[/]")
    else:
        console.print("\n  Actions:")
        console.print("    [a] accept and save")
        console.print("    [e] edit before save")
        console.print("    [s] skip for now")
        choice = click.prompt("  Choose", type=str, default="a").strip().lower()

        if choice == "s":
            console.print("[yellow]Skipped. Run `story project onboard` to retry.[/]")
            return
        if choice == "e":
            console.print(
                "[dim]Editing not yet supported in CLI. Use --json output and edit profile.json directly.[/]"
            )

    saved = save_profile(ws, profile)
    console.print(f"\n  [green]Profile saved:[/] [dim]{saved}[/]")


@project.command("confirm")
@click.option("-w", "--workspace", default=None, help="工作区目录（默认当前目录）")
def confirm_profile(workspace):
    """对已有 observed facts 做确认/编辑。"""
    from ..orchestrator.project_profile import load_profile, save_profile

    ws = Path(workspace or Path.cwd()).resolve()
    profile = load_profile(ws)

    if profile is None:
        console.print(
            "[yellow]No Project Profile found. Run `story project onboard` first.[/]"
        )
        raise SystemExit(1)

    console.print("\n[bold cyan]Confirm Project Facts[/]")
    console.print(f"  Workspace: [dim]{ws}[/]")

    unconfirmed = [f for f in profile.facts if not f.confirmed]
    if unconfirmed:
        console.print(f"\n  Unconfirmed facts: [bold]{len(unconfirmed)}[/]")
        for fact in unconfirmed:
            console.print(
                f"    [{fact.confidence}] {fact.type}: {fact.value} "
                f"[dim]({fact.scope})[/]"
            )
    else:
        console.print("\n  [green]All facts confirmed.[/]")

    console.print("\n  [a] accept all  [s] skip")
    choice = click.prompt("  Choose", type=str, default="a").strip().lower()

    if choice == "a":
        for f in profile.facts:
            f.confirmed = True
        for r in profile.repos:
            r.confirmed = True
        saved = save_profile(ws, profile)
        console.print(
            f"  [green]All facts confirmed. Profile saved:[/] [dim]{saved}[/]"
        )
    else:
        console.print("  [yellow]Skipped.[/]")


@project.command("refresh")
@click.option("-w", "--workspace", default=None, help="工作区目录（默认当前目录）")
def refresh(workspace):
    """对现有 Project Profile 做轻量漂移检查。"""
    from ..orchestrator.project_profile import refresh_profile

    ws = Path(workspace or Path.cwd()).resolve()
    console.print("\n[bold cyan]Story Start Refresh[/]")
    console.print(f"  Workspace: [dim]{ws}[/]")

    report = refresh_profile(ws)

    if report.status == "missing_profile":
        console.print("\n  [yellow]No Project Profile found.[/]")
        console.print("  Run [bold]story project onboard[/] to create one.")
        raise SystemExit(1)

    if report.status == "ok":
        console.print("\n  [green]Profile is up-to-date.[/]")
        return

    console.print("\n  [yellow]Drift detected:[/]")
    for item in report.drift:
        icon = "[red]X[/]" if item.severity == "error" else "[yellow]![/]"
        console.print(f"    {icon} {item.type}: {item.detail}")

    console.print("\n  [u] update profile  [c] continue once  [b] block story")
    choice = click.prompt("  Choose", type=str, default="c").strip().lower()

    if choice == "u":
        from ..orchestrator.project_scan import scan_workspace
        from ..orchestrator.project_profile import save_profile

        profile = scan_workspace(ws)
        saved = save_profile(ws, profile)
        console.print(f"  [green]Profile updated:[/] [dim]{saved}[/]")
    elif choice == "b":
        console.print("  [red]Story blocked due to profile drift.[/]")
        raise SystemExit(1)
    else:
        console.print("  [dim]Continuing with drift warnings.[/]")


@project.command("probe")
@click.option("-w", "--workspace", default=None, help="工作区目录（默认当前目录）")
@click.option("--question", "-q", default=None, help="Probe 问题")
def probe(workspace, question):
    """受控调用 code agent 只读探查（需要 LLM 配置）。"""
    import os

    if not os.environ.get("STORY_LLM_API_KEY"):
        console.print("[red]LLM not configured. Run `story setup` first.[/]")
        console.print("[dim]inspect and onboard commands work without LLM.[/]")
        raise SystemExit(1)

    ws = Path(workspace or Path.cwd()).resolve()
    q = question or "Find test commands, startup commands, and release signals"

    console.print("\n[bold cyan]Project Intelligence Probe[/]")
    console.print(f"  Workspace: [dim]{ws}[/]")
    console.print(f"  Question: {q}")

    from ..orchestrator.project_probe import run_probe

    console.print("\n  Running probe (read-only, no file modifications)...")
    result = run_probe(ws, q)

    if result.get("error"):
        console.print(f"  [red]Probe failed: {result['error']}[/]")
        raise SystemExit(1)

    facts = result.get("facts", [])
    hypotheses = result.get("hypotheses", [])
    open_questions = result.get("open_questions", [])

    if facts:
        console.print(f"\n  [bold]Facts ({len(facts)}):[/]")
        for f in facts:
            console.print(f"    {f.get('type', '?')}: {f.get('value', '?')}")

    if hypotheses:
        console.print(f"\n  [bold]Hypotheses ({len(hypotheses)}):[/]")
        for h in hypotheses:
            conf = h.get("confidence", 0)
            console.print(
                f"    [{conf:.0%}] {h.get('type', '?')}: {h.get('value', '?')}"
            )

    if open_questions:
        console.print(f"\n  [bold]Open Questions ({len(open_questions)}):[/]")
        for oq in open_questions:
            console.print(f"    - {oq}")

    if result.get("rejected"):
        console.print(
            f"\n  [dim]Rejected {len(result['rejected'])} items (validation failed).[/]"
        )
