"""`story seed-quality` — Quality Flywheel seed pipeline.

Analyze story artifacts via LLM, propose findings/patterns,
review and apply to database.
"""

import sys
from pathlib import Path

import click
import yaml
from rich.console import Console

from ..db.models import init_db
from ..orchestrator import planner

console = Console()


@click.group(name="seed-quality")
def seed_quality_group():
    """Quality Flywheel seed pipeline — propose findings/patterns from story artifacts."""
    init_db()


@seed_quality_group.command()
@click.argument("manifest", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--dry-run/--write",
    default=True,
    help="Generate proposal without writing files (default: --dry-run)",
)
@click.option(
    "--workspace",
    "-w",
    default=None,
    help="Workspace directory (default: current directory)",
)
def analyze(manifest, dry_run, workspace):
    """Analyze story artifacts via LLM and generate proposed findings/patterns.

    MANIFEST is a YAML file describing the story and its artifact paths.

    By default runs in dry-run mode (prints proposal, does not write).
    Use --write to generate the proposal file.
    """
    from ..orchestrator.seed_pipeline import (
        load_manifest,
        load_artifacts,
        summarize_context,
        run_llm_analysis,
        validate_proposal,
        write_proposal,
    )

    # 1. Load and validate manifest
    raw = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    if raw is None:
        console.print(f"[red]Error: Empty or invalid YAML in {manifest}[/]")
        sys.exit(1)

    try:
        manifest_data = load_manifest(raw)
    except ValueError as e:
        console.print("[red]Error: Invalid manifest:[/]")
        console.print(str(e))
        sys.exit(1)

    story_key = manifest_data["story_key"]
    console.print(f"\n[bold cyan]Story:[/] {story_key}")
    console.print(f"  Title: {manifest_data['title']}")
    console.print(f"  Type: {manifest_data['type']}")
    console.print(f"  Artifacts: {len(manifest_data['artifacts'])}")

    # 3. Load artifacts
    try:
        artifacts = load_artifacts(manifest_data)
    except FileNotFoundError as e:
        console.print("[red]Error:[/]")
        console.print(str(e))
        sys.exit(1)

    if not artifacts:
        console.print(
            "[yellow]Warning: No artifacts loaded (all files empty or skipped).[/]"
        )

    console.print(f"  Loaded: {len(artifacts)} artifact(s)")

    # 4. Summarize context
    context = summarize_context(artifacts, manifest_data)
    console.print(f"  Context: {len(context)} chars")

    # 5. LLM analysis
    console.print("\n[dim]Calling LLM for semantic analysis...[/]")
    api_key, base_url, model = planner._api_config()
    try:
        llm_output = run_llm_analysis(
            manifest_data,
            context,
            api_key=api_key,
            base_url=base_url,
            model=model,
        )
    except (RuntimeError, ValueError) as e:
        console.print(f"[red]Error: {e}[/]")
        sys.exit(1)

    # 6. Validate
    validated, warnings = validate_proposal(llm_output, manifest_data)
    validated["_warnings"] = warnings

    if warnings:
        console.print(f"\n[yellow]Validation ({len(warnings)} warnings):[/]")
        for w in warnings:
            console.print(f"  [yellow]- {w}[/]")

    n_findings = len(validated.get("proposed_findings", []))
    n_patterns = len(validated.get("proposed_patterns", []))
    console.print(
        f"\n[green]Proposal generated:[/] {n_findings} finding(s), {n_patterns} pattern(s)"
    )

    # 7. Write / display
    ws = workspace or str(Path.cwd())
    try:
        proposal_path = write_proposal(validated, manifest_data, ws, dry_run)
    except Exception as e:
        console.print(f"[red]Error writing proposal: {e}[/]")
        sys.exit(1)

    if dry_run:
        n_questions = len(validated.get("review_questions", []))
        console.print(
            f"\n[dim]Dry run complete. {n_questions} review question(s) shown above.\n"
            "Use --write to save the proposal file.[/]"
        )
    else:
        reviewed_dir = Path(ws) / ".story/quality-seed/reviewed"
        reviewed_dir.mkdir(parents=True, exist_ok=True)
        console.print(f"\n[green]Proposal written to: {proposal_path}[/]")
        console.print("\n[dim]Next steps:")
        console.print("  1. Edit the proposal file and fill in review_status")
        console.print(
            "  2. Copy to reviewed: "
            f"[bold]cp {proposal_path} {reviewed_dir / f'{story_key}.json'}[/]"
        )
        console.print(
            f"  3. Apply: [bold]story seed-quality apply {reviewed_dir / f'{story_key}.json'}[/]"
        )
        console.print("[/dim]")


@seed_quality_group.command()
@click.argument("reviewed_file", type=click.Path(exists=True, path_type=Path))
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
def apply(reviewed_file, yes):
    """Apply a reviewed proposal — write approved items to the database.

    REVIEWED_FILE should be a JSON file from the review queue with
    review_status completed.
    """
    from ..orchestrator.seed_pipeline import load_reviewed_proposal, apply_reviewed

    # 1. Load and validate the reviewed file
    try:
        proposal = load_reviewed_proposal(str(reviewed_file))
    except ValueError as e:
        console.print(f"[red]Error: {e}[/]")
        sys.exit(1)

    # 2. Show summary
    story_key = proposal["manifest"]["story_key"]
    review_status = proposal["review_status"]
    n_findings = len(review_status.get("findings_approved", []))
    n_patterns = len(review_status.get("patterns_approved", []))
    n_rej_f = len(review_status.get("findings_rejected", []))
    n_rej_p = len(review_status.get("patterns_rejected", []))

    console.print(f"\n[bold cyan]Story:[/] {story_key}")
    console.print(f"  Findings to write: [green]{n_findings}[/]")
    console.print(f"  Patterns to write (as proposed): [green]{n_patterns}[/]")
    console.print(f"  Reviewed at: {review_status.get('reviewed_at', 'N/A')}")
    if n_rej_f or n_rej_p:
        console.print(
            f"  [dim]Rejected: {n_rej_f} findings, {n_rej_p} patterns (skipped)[/]"
        )
    notes = review_status.get("reviewer_notes", "")
    if notes:
        console.print(f"  Notes: [dim]{notes}[/]")

    # 3. Confirm
    if not yes:
        if n_findings == 0 and n_patterns == 0:
            console.print("\n[yellow]Nothing approved to write.[/]")
            return
        answer = console.input("\n[bold]Write to database? [y/N][/] ").strip().lower()
        if answer not in ("y", "yes"):
            console.print("[dim]Cancelled.[/]")
            return

    # 4. Apply
    result = apply_reviewed(proposal)

    console.print(
        f"\n[green]Applied:[/] {result['findings_written']} findings, "
        f"{result['patterns_written']} patterns"
    )
    if result["errors"]:
        console.print("[yellow]Errors:[/]")
        for err in result["errors"]:
            console.print(f"  [yellow]- {err}[/]")


@seed_quality_group.command()
@click.argument("story_key")
@click.option("--stage", default="design", help="Lifecycle stage (default: design)")
@click.option(
    "--tags", "-t", multiple=True, help="Relevance tags for pattern filtering"
)
def preview_packet(story_key, stage, tags):
    """Preview the Quality Packet that would be injected for a story."""
    from ..orchestrator.quality import build_quality_packet, build_quality_checklist
    from ..db import models as db

    story = db.get_story(story_key)
    if not story:
        console.print(
            f"[yellow]Story '{story_key}' not found in DB. Showing global active patterns.[/]"
        )

    # Derive relevance tags
    if tags:
        relevant_tags = list(tags)
    elif story:
        import json

        relevant_tags = [stage]
        if story.get("source_type"):
            relevant_tags.append(story["source_type"])
        if story.get("sub_type"):
            relevant_tags.append(story["sub_type"])
        try:
            ctx = json.loads(story.get("context_json") or "{}")
            modules = ctx.get("affected_modules", [])
            if isinstance(modules, list):
                relevant_tags.extend(modules)
            elif isinstance(modules, str):
                relevant_tags.append(modules)
            if ctx.get("category"):
                relevant_tags.append(ctx["category"])
        except Exception:
            pass
    else:
        relevant_tags = [stage]

    packet = build_quality_packet(story_key, stage, relevant_tags=relevant_tags)
    checklist = build_quality_checklist(story_key, stage)

    console.print()
    console.rule(f"[bold cyan]Quality Packet Preview: {story_key}[/]")
    console.print(f"  Stage: [dim]{stage}[/]")
    console.print(f"  Relevance tags: [dim]{', '.join(relevant_tags)}[/]")
    console.print()
    console.print(packet)
    if checklist:
        console.print()
        console.print(checklist)
    console.print()

    # Summary line
    patterns = db.get_active_learned_patterns(limit=50)
    relevant = db.find_relevant_patterns(list(relevant_tags), limit=5)
    console.print(
        f"[dim]Active patterns total: {len(patterns)}, "
        f"relevant to these tags: {len(relevant)}[/]"
    )
