"""`story review-feedback` and `story approvals` — Review Feedback Intake CLI."""

import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from ..db.models import init_db

console = Console()


@click.group(name="review-feedback")
def review_feedback_group():
    """Import and manage review feedback findings."""
    init_db()


@review_feedback_group.command(name="import")
@click.argument("story_key")
@click.argument("review_file", type=click.Path(exists=True, path_type=Path))
def import_cmd(story_key, review_file):
    """Import review feedback from a file and extract candidate findings.

    \b
    Examples:
      story review-feedback import STORY-123 review.md
      story review-feedback import STORY-123 review.json
    """
    from ..db import models as db
    from ..orchestrator.review_feedback import import_review

    story = db.get_story(story_key)
    if not story:
        console.print(f"[red]Story '{story_key}' not found.[/]")
        sys.exit(1)

    content = review_file.read_text(encoding="utf-8")
    if not content.strip():
        console.print("[red]Review file is empty.[/]")
        sys.exit(1)

    console.print(f"\n[bold cyan]Story:[/] {story_key}")
    console.print(f"  File: {review_file.name}")
    console.print(f"  Size: {len(content)} chars")

    console.print("\n[dim]Extracting candidate findings...[/]")
    result = import_review(story_key, content)

    mode_label = (
        "[green]LLM[/]" if result["mode"] == "llm" else "[yellow]rule fallback[/]"
    )
    console.print(f"  Mode: {mode_label}")
    console.print(f"  Imported: [green]{result['imported']}[/] finding(s)")

    if result["skipped"]:
        console.print(f"  Skipped: [yellow]{result['skipped']}[/]")

    if result["warnings"]:
        console.print("\n[yellow]Warnings:[/]")
        for w in result["warnings"]:
            console.print(f"  [yellow]- {w}[/]")

    if result["imported"] == 0:
        console.print("\n[dim]No candidate findings extracted.[/]")
    else:
        console.print(
            f"\n[dim]Run [bold]story review-feedback list {story_key}[/] to view.[/]"
        )


@review_feedback_group.command("list")
@click.argument("story_key")
def list_findings(story_key):
    """List all findings for a story."""
    from ..db import models as db

    findings = db.get_findings_by_story(story_key)
    if not findings:
        console.print(f"[dim]No findings for story '{story_key}'.[/]")
        return

    table = Table(title=f"Findings: {story_key}")
    table.add_column("ID", style="dim", max_width=20)
    table.add_column("Status", style="cyan")
    table.add_column("Severity", style="bold")
    table.add_column("Category", style="white")
    table.add_column("Description", max_width=50)
    table.add_column("Source", style="dim")

    sev_colors = {"high": "red", "medium": "yellow", "low": "green"}
    status_colors = {
        "open": "cyan",
        "accepted": "green",
        "fixed": "green",
        "verified": "bold green",
        "rejected": "red",
        "deferred": "yellow",
        "learned": "blue",
    }

    for f in findings:
        sev = f["severity"]
        status = f["status"]
        table.add_row(
            f["id"],
            f"[{status_colors.get(status, 'white')}]{status}[/]",
            f"[{sev_colors.get(sev, 'white')}]{sev.upper()}[/]",
            f["category"],
            f["description"][:80],
            f["source"],
        )

    console.print(table)


@review_feedback_group.command()
@click.argument("finding_id")
@click.option("--accept", "action", flag_value="accept", help="Accept finding")
@click.option("--reject", "action", flag_value="reject", help="Reject finding")
@click.option("--defer", "action", flag_value="defer", help="Defer finding")
@click.option(
    "--downgrade", "action", flag_value="downgrade", help="Downgrade severity"
)
@click.option("--verify", "action", flag_value="verify", help="Mark as verified")
@click.option("--reason", "-r", default="", help="Reason for the decision")
def decide(finding_id, action, reason):
    """Make a decision on a candidate finding.

    \b
    Examples:
      story review-feedback decide finding-xxx --accept
      story review-feedback decide finding-yyy --reject --reason "overclaimed"
      story review-feedback decide finding-zzz --defer
      story review-feedback decide finding-www --downgrade
      story review-feedback decide finding-xxx --verify --reason "test passed"
    """
    from ..db import models as db
    from ..orchestrator.quality import update_finding_status

    if not action:
        console.print(
            "[red]Specify one of: --accept, --reject, --defer, --downgrade, --verify[/]"
        )
        sys.exit(1)

    finding = db.get_finding(finding_id)
    if not finding:
        console.print(f"[red]Finding '{finding_id}' not found.[/]")
        sys.exit(1)

    story_key = finding["story_key"]

    if action == "accept":
        update_finding_status(story_key, finding_id, "accepted", reason=reason)
        console.print(f"[green]Accepted[/] {finding_id}")
    elif action == "reject":
        update_finding_status(story_key, finding_id, "rejected", reason=reason)
        console.print(f"[red]Rejected[/] {finding_id}")
    elif action == "defer":
        update_finding_status(story_key, finding_id, "deferred", reason=reason)
        console.print(f"[yellow]Deferred[/] {finding_id}")
    elif action == "downgrade":
        sev_order = {"high": "medium", "medium": "low", "low": "low"}
        new_sev = sev_order.get(finding["severity"], "low")
        db.update_finding(finding_id, severity=new_sev)
        db.log_event(
            story_key,
            finding.get("stage", ""),
            "finding_downgraded",
            {
                "finding_id": finding_id,
                "from": finding["severity"],
                "to": new_sev,
                "reason": reason,
            },
        )
        console.print(
            f"[yellow]Downgraded[/] {finding_id}: {finding['severity']} -> {new_sev}"
        )
    elif action == "verify":
        update_finding_status(story_key, finding_id, "verified", reason=reason)
        console.print(f"[green]Verified[/] {finding_id}")

    if reason:
        console.print(f"  Reason: [dim]{reason}[/]")


# ── Approvals group ──


@click.group(name="approvals")
def approvals_group():
    """View and manage the approval queue for pending findings."""
    init_db()


@approvals_group.command("list")
def approvals_list():
    """List all pending findings (open + accepted) across stories."""
    from ..db import models as db

    pending = db.get_all_pending_findings()
    if not pending:
        console.print("[dim]No pending findings.[/]")
        return

    table = Table(title="Approval Queue")
    table.add_column("ID", style="dim", max_width=20)
    table.add_column("Story", style="cyan")
    table.add_column("Status", style="cyan")
    table.add_column("Severity", style="bold")
    table.add_column("Category")
    table.add_column("Description", max_width=50)
    table.add_column("Source", style="dim")

    sev_colors = {"high": "red", "medium": "yellow", "low": "green"}
    status_colors = {"open": "cyan", "accepted": "green"}

    for f in pending:
        sev = f["severity"]
        status = f["status"]
        table.add_row(
            f["id"],
            f["story_key"],
            f"[{status_colors.get(status, 'white')}]{status}[/]",
            f"[{sev_colors.get(sev, 'white')}]{sev.upper()}[/]",
            f["category"],
            f["description"][:80],
            f["source"],
        )

    console.print(table)
    console.print(f"\n[dim]{len(pending)} pending finding(s)[/]")


@approvals_group.command(name="decide")
@click.argument("finding_id")
@click.option("--accept", "action", flag_value="accept", help="Accept finding")
@click.option("--reject", "action", flag_value="reject", help="Reject finding")
@click.option("--reason", "-r", default="", help="Reason")
def decide_approval(finding_id, action, reason):
    """Make a decision on a pending finding.

    \b
    Examples:
      story approvals decide finding-xxx --accept
      story approvals decide finding-yyy --reject --reason "not actionable"
    """
    from ..db import models as db
    from ..orchestrator.quality import update_finding_status

    if not action:
        console.print("[red]Specify --accept or --reject[/]")
        sys.exit(1)

    finding = db.get_finding(finding_id)
    if not finding:
        console.print(f"[red]Finding '{finding_id}' not found.[/]")
        sys.exit(1)

    story_key = finding["story_key"]

    if action == "accept":
        update_finding_status(story_key, finding_id, "accepted", reason=reason)
        console.print(f"[green]Accepted[/] {finding_id}")
    elif action == "reject":
        update_finding_status(story_key, finding_id, "rejected", reason=reason)
        console.print(f"[red]Rejected[/] {finding_id}")

    if reason:
        console.print(f"  Reason: [dim]{reason}[/]")
