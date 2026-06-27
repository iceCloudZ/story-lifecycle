"""Publisher — Step 3: batch create GitHub Issues via gh CLI."""

from __future__ import annotations

import logging
from pathlib import Path

from ..sources.github_cli import GithubCli, GithubCliError
from .decomposer import load_issues
from .state import update_step

log = logging.getLogger(__name__)


def publish_issues(
    repo: str,
    *,
    cwd: str | None = None,
    dry_run: bool = False,
    accept_label: str = "lifecycle:accepted",
) -> list[dict]:
    """Batch create GitHub Issues from issues.json.

    Args:
        repo: GitHub repo in "owner/repo" format.
        cwd: Working directory.
        dry_run: If True, only preview what would be created.
        accept_label: Label to add for auto-accepted issues.

    Returns:
        List of created Issue dicts with {"number": int, "title": str, "url": str}.
    """
    issues = load_issues(cwd=cwd)
    if not issues:
        raise FileNotFoundError(
            "No issues.json found. Run 'story plan decompose' first."
        )

    cli = GithubCli(repo)
    # Ensure lifecycle:accepted label exists before creating issues
    cli.ensure_label(accept_label, color="0e8a16")
    results = []

    for i, issue in enumerate(issues):
        title = issue.get("title", f"Issue {i + 1}")
        body = issue.get("body", "")
        labels = issue.get("labels", [])
        labels_str = ", ".join(labels) if labels else "none"

        if dry_run:
            log.info("[DRY RUN] Would create: %s (labels: %s)", title, labels_str)
            results.append({"number": 0, "title": title, "url": "", "dry_run": True})
            continue

        try:
            # Create issue with lifecycle:accepted label only
            number = cli.create_issue(title, body, label=[accept_label])

            # Try to add custom labels, skip if they don't exist in the repo
            for lb in labels:
                try:
                    cli.add_label(number, lb)
                except GithubCliError:
                    log.debug("Skipped label '%s' for #%d (not in repo)", lb, number)

            url = f"https://github.com/{repo}/issues/{number}"
            log.info("Created #%d: %s", number, title)
            results.append({"number": number, "title": title, "url": url})
        except GithubCliError as e:
            log.warning("Failed to create issue '%s': %s", title, e)
            results.append({"number": 0, "title": title, "url": "", "error": str(e)})

    if not dry_run:
        _update_roadmap_with_numbers(results, cwd=cwd)
        update_step(
            "step_3",
            {"published_count": len([r for r in results if r.get("number")])},
            cwd=cwd,
        )

    return results


def _update_roadmap_with_numbers(
    results: list[dict], *, cwd: str | None = None
) -> None:
    """Update roadmap.md to link Issue numbers."""
    root = Path(cwd) if cwd else Path.cwd()
    roadmap_path = root / ".story" / "planning" / "roadmap.md"
    if not roadmap_path.is_file():
        return

    content = roadmap_path.read_text(encoding="utf-8")
    for r in results:
        if r.get("number") and r.get("title"):
            # Append issue link after matching title in roadmap
            old = r["title"]
            new = f"{old} [#{r['number']}]({r['url']})"
            content = content.replace(old, new, 1)

    roadmap_path.write_text(content, encoding="utf-8")
