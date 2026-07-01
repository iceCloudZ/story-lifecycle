"""`story demo` — run a simulated lifecycle with zero dependencies."""

import json
import shutil
import time
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

from ..db import models as db
from ..orchestrator.engine import graph as graph_mod

console = Console()

_DEMO_KEY = "demo-hello"
_DEMO_TITLE = "Demo: Hello Story Lifecycle"


def run_demo():
    """Run a simulated story lifecycle — no LLM, no AI CLI."""
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
    from ..orchestrator.nodes import load_profile

    stages = list(load_profile("minimal").get("stages", {}).keys())
    console.print(f"  Profile: [dim]minimal ({' → '.join(stages)})[/]")
    console.print("  Mode: [dim]simulated (no real AI)[/]")
    console.print()

    start = time.monotonic()

    from ..orchestrator.engine import router as llm_router

    def _demo_route(state, cfg):
        return {"action": "advance", "reasoning": "Demo mode"}

    _mock_targets = [
        "story_lifecycle.orchestrator.nodes.graph_nodes.planner",
        "story_lifecycle.orchestrator.nodes.planner",
        "story_lifecycle.orchestrator.engine.planner",
    ]

    with (
        patch("story_lifecycle.orchestrator.nodes.ttyd") as mock_ttyd,
        patch("story_lifecycle.orchestrator.nodes.notify"),
        patch.object(llm_router, "route", _demo_route),
        patch(
            "story_lifecycle.orchestrator.nodes.graph_nodes.llm_router.route",
            _demo_route,
        ),
    ):
        # Mock planner at both import sites
        mock_planners = []
        for target in _mock_targets:
            mp = patch(target)
            m = mp.start()
            m.compress_context.return_value = None
            mock_planners.append(mp)

        mock_ttyd.session_name.return_value = f"story-{_DEMO_KEY}"
        mock_ttyd.session_alive.return_value = True
        mock_ttyd._MPLEX = None

        from story_lifecycle.orchestrator import nodes as nodes_mod

        with patch.object(nodes_mod, "STORY_HOME", workspace):
            graph_mod._run_story_impl(_DEMO_KEY)

        for mp in mock_planners:
            mp.stop()

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

        if events:
            _show_types = {"plan", "execute", "review"}
            current_stage = None
            for e in events:
                etype = e.get("event_type", "")
                if etype not in _show_types:
                    continue
                e_stage = e.get("stage", "")
                if e_stage != current_stage:
                    current_stage = e_stage
                    console.print(f"  [bold cyan]\\[{e_stage}][/]")
                payload = e.get("payload", "")
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except (json.JSONDecodeError, TypeError):
                        pass
                detail = ""
                if isinstance(payload, dict):
                    if etype == "plan":
                        detail = payload.get("summary", "")[:60]
                    elif etype == "review":
                        q = payload.get("quality", "")
                        detail = f"{q} — {payload.get('summary', '')[:40]}"
                    elif etype == "execute":
                        detail = f"attempt {payload.get('attempt', '?')} ({payload.get('tool', '')})"
                console.print(f"    [green]✓[/] {etype:8s} {detail}")
            console.print()

    console.rule("[bold green]Demo Complete[/]")
    console.print()
    console.print("  [dim]Next steps:[/]")
    console.print("  [dim]  story          — launch interactive board[/]")
    console.print("  [dim]  story serve    — start API server[/]")
    console.print("  [dim]  story doctor   — check environment[/]")
    console.print()
