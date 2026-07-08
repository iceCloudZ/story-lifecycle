"""story diagnostics -- generate diagnostic bundles for stories and system."""

from __future__ import annotations

import click
from rich.console import Console

from ...infra.db.models import init_db

console = Console()


@click.command()
@click.argument("story_key", required=False)
@click.option(
    "--global", "global_diag", is_flag=True, help="Generate global diagnostics bundle"
)
@click.option("--output", "-o", default=None, help="Output zip path or directory")
@click.option(
    "--include-diff", is_flag=True, help="Include full git diff (default: off)"
)
@click.option("--event-limit", default=200, type=int, help="Max event_log entries")
@click.option("--no-zip", is_flag=True, help="Output directory instead of zip")
def diagnostics(story_key, global_diag, output, include_diff, event_limit, no_zip):
    """Generate diagnostic bundle for a story or the system.

    \b
    Examples:
      story diagnostics STORY-001
      story diagnostics STORY-001 --no-zip
      story diagnostics --global
    """
    init_db()

    if global_diag:
        from ...orchestrator.observability.diagnostics import (
            create_global_diagnostics_bundle,
        )

        result = create_global_diagnostics_bundle(
            output_path=output,
            no_zip=no_zip,
        )
    elif story_key:
        from ...orchestrator.observability.diagnostics import (
            create_story_diagnostics_bundle,
        )

        result = create_story_diagnostics_bundle(
            story_key=story_key,
            output_path=output,
            include_diff=include_diff,
            event_limit=event_limit,
            no_zip=no_zip,
        )
    else:
        click.echo("Usage: story diagnostics STORY_KEY or story diagnostics --global")
        raise SystemExit(1)

    if isinstance(result, dict) and result.get("error"):
        console.print(f"[red]Error: {result['error']}[/]")
        raise SystemExit(1)

    dest = result.get("path", "unknown")
    console.print(f"Diagnostic bundle created:\n[bold cyan]{dest}[/]")
