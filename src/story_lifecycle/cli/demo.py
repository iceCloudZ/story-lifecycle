"""`story demo` — run a simulated lifecycle with zero dependencies."""

import json
import shutil
import time
from pathlib import Path
from unittest.mock import patch

from rich.console import Console
from rich.table import Table

from ..db import models as db
from ..orchestrator import graph as graph_mod
from ..orchestrator.demo_tool import DemoTool

console = Console()

_DEMO_KEY = "demo-hello"
_DEMO_TITLE = "Demo: Hello Story Lifecycle"


def run_demo():
    """Run a simulated story lifecycle — no LLM, no AI CLI, no tmux."""
    import tempfile

    tmp = tempfile.mkdtemp(prefix="story-demo-")
    workspace = Path(tmp)
    db_path = workspace / "story.db"
    checkpoint_path = workspace / "checkpoint.db"

    try:
        _run_demo_inner(workspace, db_path, checkpoint_path)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run_demo_inner(workspace: Path, db_path: Path, checkpoint_path: Path):
    db_patcher = patch.object(db, "get_db_path", return_value=db_path)
    ck_patcher = patch.object(graph_mod, "checkpoint_db", checkpoint_path)
    db_patcher.start()
    ck_patcher.start()

    db.init_db()

    db.upsert_story(
        _DEMO_KEY,
        title=_DEMO_TITLE,
        workspace=str(workspace),
        profile="minimal",
        current_stage="design",
        status="active",
    )

    console.print()
    console.rule("[bold cyan]Story Lifecycle Demo[/]")
    console.print()
    console.print(f"  Story: [cyan]{_DEMO_KEY}[/]")
    console.print("  Profile: [dim]minimal (design → implement → test)[/]")
    console.print("  Mode: [dim]simulated (no real AI)[/]")
    console.print()

    demo_tool = DemoTool()
    start = time.monotonic()

    with (
        patch("story_lifecycle.orchestrator.nodes.planner") as mock_planner,
        patch("story_lifecycle.orchestrator.tools.get_tool") as mock_get_tool,
        patch("story_lifecycle.orchestrator.nodes.ttyd") as mock_ttyd,
        patch("story_lifecycle.orchestrator.nodes.notify"),
        patch("story_lifecycle.orchestrator.graph.emit_plan_done"),
        patch("story_lifecycle.orchestrator.graph.emit_terminal_opened"),
        patch(
            "story_lifecycle.orchestrator.nodes.interrupt", side_effect=lambda x: None
        ),
    ):
        mock_planner.is_available.return_value = False
        mock_planner.compress_context.return_value = None

        mock_get_tool.return_value = demo_tool
        mock_ttyd.session_name.return_value = f"story-{_DEMO_KEY}"
        mock_ttyd.session_alive.return_value = True
        mock_ttyd._MPLEX = None

        from story_lifecycle.orchestrator import nodes as nodes_mod

        with patch.object(nodes_mod, "STORY_HOME", workspace):
            graph_mod._run_story_impl(_DEMO_KEY)

    elapsed = time.monotonic() - start

    story = db.get_story(_DEMO_KEY)
    events = db.get_story_events(_DEMO_KEY)

    # Stop patchers so SQLite connections can close
    db_patcher.stop()
    ck_patcher.stop()

    if story:
        status = story["status"]
        stage = story["current_stage"]
        status_style = "green" if status == "completed" else "red"
        console.print(f"  Status: [{status_style}]{status}[/]")
        console.print(f"  Final stage: [dim]{stage}[/]")
        console.print(f"  Time: [dim]{elapsed:.1f}s[/]")
        console.print()

        stages_done = {}
        for e in events:
            if e["event_type"] == "execute":
                stages_done[e["stage"]] = True

        for s in ("design", "implement", "review"):
            mark = "[green]✓[/]" if s in stages_done else "[dim]○[/]"
            console.print(f"  {mark} {s}")

        console.print()

        if events:
            table = Table(title="Event Log", show_lines=False, padding=0)
            table.add_column("Stage", style="cyan", width=12)
            table.add_column("Type", style="white", width=12)
            table.add_column("Detail", style="dim")
            for e in events:
                e_stage = e.get("stage", "")
                etype = e.get("event_type", "")
                payload = e.get("payload", "")
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except (json.JSONDecodeError, TypeError):
                        pass
                detail = ""
                if isinstance(payload, dict):
                    attempt = payload.get("attempt", "")
                    tool = payload.get("tool", "")
                    detail = (
                        f"attempt {attempt} ({tool})" if tool else str(payload)[:60]
                    )
                table.add_row(e_stage, etype, detail)
            console.print(table)
            console.print()

    console.rule("[bold green]Demo Complete[/]")
    console.print()
    console.print("  [dim]Next steps:[/]")
    console.print("  [dim]  story          — launch interactive board[/]")
    console.print("  [dim]  story serve    — start API server[/]")
    console.print("  [dim]  story doctor   — check environment[/]")
    console.print()
