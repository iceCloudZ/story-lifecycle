"""Doctor paths — scan and migrate legacy .story-done, .story-context, .story-runs."""

from __future__ import annotations

import shutil
from pathlib import Path

from rich.console import Console

from ...infra.story_paths import assert_within_workspace

console = Console()

LEGACY_DIRS = {
    ".story-done": "done",
    ".story-context": "context",
    ".story-runs": "runs",
}


def _dir_size(p: Path) -> int:
    total = 0
    if p.is_dir():
        for f in p.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    return total


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def run_doctor_paths(root: str | Path | None = None):
    """Scan for legacy directories and offer to migrate into .story/."""
    root = Path(root) if root else Path.cwd()
    story = root / ".story"

    found = []
    for old_name, new_sub in LEGACY_DIRS.items():
        old_dir = root / old_name
        if old_dir.exists():
            size = _dir_size(old_dir)
            found.append((old_name, new_sub, old_dir, size))

    if not found:
        console.print("[green]No legacy directories found. Workspace is clean.[/]")
        return

    console.print("[bold]Legacy directories detected:[/]\n")
    for old_name, new_sub, old_dir, size in found:
        console.print(
            f"  [yellow]{old_name}[/] ({_fmt_size(size)}) → .story/{new_sub}/"
        )

    console.print()
    answer = console.input("[bold]Move into .story/? [y/N][/] ").strip().lower()
    if answer not in ("y", "yes"):
        console.print("[dim]Aborted. No changes made.[/]")
        return

    story.mkdir(exist_ok=True)
    moved = []
    for old_name, new_sub, old_dir, size in found:
        target = story / new_sub
        try:
            if target.exists():
                # Merge: move contents
                for item in old_dir.iterdir():
                    dest = target / item.name
                    if dest.exists():
                        # Blast shield: never rmtree outside the workspace.
                        assert_within_workspace(dest, story.parent)
                        shutil.rmtree(str(dest), ignore_errors=True)
                    shutil.move(str(item), str(dest))
            else:
                shutil.move(str(old_dir), str(target))
            moved.append(old_name)
            console.print(f"  [green]✓[/] {old_name} → .story/{new_sub}/")
        except Exception as e:
            console.print(f"  [red]✗[/] {old_name}: {e}")

    if not moved:
        return

    console.print()
    delete_answer = (
        console.input("[bold]Delete old directories? [y/N][/] ").strip().lower()
    )
    if delete_answer in ("y", "yes"):
        for old_name, _, old_dir, _ in found:
            if old_dir.exists():
                try:
                    assert_within_workspace(old_dir, story.parent)
                    shutil.rmtree(str(old_dir), ignore_errors=True)
                    console.print(f"  [green]✓[/] Removed {old_name}/")
                except Exception as e:
                    console.print(f"  [red]✗[/] Could not remove {old_name}: {e}")
    else:
        console.print("[dim]Old directories kept for manual inspection.[/]")
