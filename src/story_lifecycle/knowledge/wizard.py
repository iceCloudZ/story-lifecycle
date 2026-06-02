"""Interactive wizard for init-knowledge.

Implements Steps 2-4 of the design: show overview, show recommended scope,
accept/edit flow.
"""

from __future__ import annotations


import click
from rich.console import Console

from .detector import DetectionResult
from .scope import ScopeRecommendation

console = Console()


def show_project_overview(detection: DetectionResult) -> None:
    """Step 1: Show detected project structure."""
    console.print(f"\n[bold cyan]Project: {_display_name(detection.product_guess)}[/]")
    console.print(f"  Root: [dim]{detection.root}[/]")
    console.print()

    if detection.existing_knowledge:
        console.print("  [yellow]Existing knowledge pack: yes[/]")
    if detection.codegraph_cache:
        console.print("  [dim]CodeGraph cache: yes[/]")

    console.print()
    console.print("[bold]Detected:[/]")
    if detection.services:
        java_svcs = [s for s in detection.services if "java" in s.type]
        other_svcs = [s for s in detection.services if "java" not in s.type]
        if java_svcs:
            console.print(f"  Java services: [green]{len(java_svcs)}[/]")
        if other_svcs:
            console.print(f"  Other services: [green]{len(other_svcs)}[/]")
    if detection.frontends:
        console.print(f"  Frontend apps: [green]{len(detection.frontends)}[/]")
    if detection.doc_dirs:
        console.print(f"  Doc directories: [dim]{', '.join(detection.doc_dirs)}[/]")
    if detection.spec_dirs:
        console.print(
            f"  PRD/spec directories: [dim]{', '.join(detection.spec_dirs)}[/]"
        )
    if detection.bug_dirs:
        console.print(
            f"  Bug record directories: [dim]{', '.join(detection.bug_dirs)}[/]"
        )


def show_file_stats(detection: DetectionResult) -> None:
    """Step 2: Show language and file statistics."""
    if not detection.file_stats:
        return

    console.print()
    console.print("[bold]Files by language:[/]")
    for lang, count in sorted(detection.file_stats.items(), key=lambda x: -x[1]):
        console.print(f"  {lang:<16} {count}")

    if detection.ignored_or_generated:
        console.print()
        console.print("[dim]Generated/dependency folders excluded:[/]")
        for ig in detection.ignored_or_generated[:6]:
            console.print(f"  [dim]{ig}[/]")
        if len(detection.ignored_or_generated) > 6:
            console.print(
                f"  [dim]... and {len(detection.ignored_or_generated) - 6} more[/]"
            )

    if detection.warnings:
        console.print()
        for w in detection.warnings:
            console.print(f"  [yellow]Warning: {w}[/]")


def show_recommended_scope(scope: ScopeRecommendation) -> None:
    """Step 3: Show recommended P0 scope."""
    console.print()
    console.print("[bold]Recommended P0 bootstrap scope:[/]")
    for svc in scope.included:
        console.print(f"  [green][x][/green] {svc.id}  [dim]({svc.reason})[/]")
    for svc in scope.excluded:
        console.print(f"  [dim][ ] {svc.id}  ({svc.reason})[/]")


def show_candidate_domains(scope: ScopeRecommendation) -> None:
    """Show candidate domains."""
    if not scope.candidate_domains:
        return
    console.print()
    console.print("[bold]Candidate domains:[/]")
    for cd in scope.candidate_domains:
        scenarios = ", ".join(cd.candidate_scenarios[:4])
        if len(cd.candidate_scenarios) > 4:
            scenarios += ", ..."
        console.print(f"  {cd.domain:<18} {scenarios}")


def interactive_confirm(
    detection: DetectionResult,
    scope: ScopeRecommendation,
) -> ScopeRecommendation | None:
    """Step 4: Interactive scope confirmation.

    Returns the confirmed ScopeRecommendation, or None if user quit.
    """
    while True:
        console.print()
        console.print("[bold]Actions:[/]")
        console.print("  a  accept recommended scope")
        console.print("  f  include frontend app(s)")
        console.print("  x  exclude a service")
        console.print("  i  include an excluded service")
        console.print("  d  dry-run only")
        console.print("  q  quit")

        choice = click.prompt("Choose", type=str, default="a").strip().lower()

        if choice == "a":
            return scope
        elif choice == "q":
            return None
        elif choice == "d":
            return scope  # caller checks dry_run flag
        elif choice == "f":
            _include_frontends(scope)
        elif choice == "x":
            _exclude_service(scope)
        elif choice == "i":
            _include_service(scope)
        else:
            console.print(f"[yellow]Unknown option: {choice}[/]")


def _include_frontends(scope: ScopeRecommendation) -> None:
    """Move all frontends from excluded to included."""
    fe_to_move = [s for s in scope.excluded if "frontend" in s.type]
    if not fe_to_move:
        console.print("[dim]No frontends to include.[/]")
        return
    for svc in fe_to_move:
        svc.included = True
        svc.reason = "user included"
        scope.included.append(svc)
        scope.excluded.remove(svc)
    console.print(f"[green]Included {len(fe_to_move)} frontend app(s).[/]")


def _exclude_service(scope: ScopeRecommendation) -> None:
    """Let user pick a service to exclude."""
    if not scope.included:
        console.print("[dim]No included services to exclude.[/]")
        return
    console.print("[bold]Select service to exclude:[/]")
    for i, svc in enumerate(scope.included, 1):
        console.print(f"  {i}) {svc.id}")
    idx = click.prompt("Number", type=int, default=0)
    if 1 <= idx <= len(scope.included):
        svc = scope.included.pop(idx - 1)
        svc.included = False
        svc.reason = "user excluded"
        scope.excluded.append(svc)
        console.print(f"[yellow]Excluded {svc.id}[/]")
    else:
        console.print("[dim]Cancelled.[/]")


def _include_service(scope: ScopeRecommendation) -> None:
    """Let user pick an excluded service to include."""
    excludable = [s for s in scope.excluded if "frontend" not in s.type]
    if not excludable:
        console.print("[dim]No excluded services to include.[/]")
        return
    console.print("[bold]Select service to include:[/]")
    for i, svc in enumerate(excludable, 1):
        console.print(f"  {i}) {svc.id}")
    idx = click.prompt("Number", type=int, default=0)
    if 1 <= idx <= len(excludable):
        svc = excludable[idx - 1]
        svc.included = True
        svc.reason = "user included"
        scope.included.append(svc)
        scope.excluded.remove(svc)
        console.print(f"[green]Included {svc.id}[/]")
    else:
        console.print("[dim]Cancelled.[/]")


def show_next_steps(scope: ScopeRecommendation) -> None:
    """Step 7: Show recommended next commands."""
    console.print()
    console.print("[bold green]Knowledge bootstrap completed.[/]")
    console.print()
    console.print("[bold]Recommended next commands:[/]")
    console.print("  story project scenarios review")
    for cd in scope.candidate_domains[:3]:
        console.print(f"  story project scenario scan {cd.domain}")

    excluded = [s for s in scope.excluded if "frontend" in s.type]
    if excluded:
        console.print()
        console.print("[bold]Optional:[/]")
        for fe in excluded:
            console.print(f"  story project init-knowledge --include {fe.id}")


def _display_name(guess: str) -> str:
    return guess.replace("-", " ").title()
