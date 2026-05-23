"""E2E runner — patches deps, runs graph, returns result for assertion."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

from story_lifecycle.db import models as db
from story_lifecycle.orchestrator import graph as graph_mod
from story_lifecycle.orchestrator.nodes import StoryState

from .scenario import Scenario
from .fake_tool import FakeStageTool


class E2EResult:
    """Holds the result of a headless E2E run."""

    def __init__(self, story_key: str, workspace: str):
        self.story_key = story_key
        self.workspace = workspace
        self.story: dict | None = None
        self.events: list[dict] = []
        self.final_state: dict | None = None

    def refresh(self):
        self.story = db.get_story(self.story_key)
        self.events = db.get_story_events(self.story_key)


def run_scenario(scenario: Scenario, workspace: Path) -> E2EResult:
    """Run a full headless E2E lifecycle for a scenario.

    - Creates the story in DB
    - Patches planner, tools, ttyd, notify
    - Calls _run_story_impl() directly (synchronous)
    - Returns E2EResult for assertion
    """
    key = scenario.story_key

    # Create story in DB
    db.upsert_story(
        key,
        title=scenario.title,
        workspace=str(workspace),
        profile=scenario.profile,
        current_stage="design",
        status="active",
    )

    # Write PRD placeholder
    prd_dir = workspace / "prd"
    prd_dir.mkdir(exist_ok=True)
    prd_file = prd_dir / f"{key}.md"
    prd_file.write_text(f"# {scenario.title}\n\nTest PRD content.\n", encoding="utf-8")

    fake_tool = FakeStageTool(scenario)

    # Build review mock that returns scenario-defined review results
    def _mock_review_stage(state, cfg, stage_output):
        stage = state["current_stage"]
        exec_count = state.get("execution_count", 1)
        return scenario.review_payload(stage, execution_index=exec_count)

    with (
        patch("story_lifecycle.orchestrator.nodes.planner") as mock_planner,
        patch("story_lifecycle.orchestrator.tools.get_tool") as mock_get_tool,
        patch("story_lifecycle.orchestrator.nodes.ttyd") as mock_ttyd,
        patch("story_lifecycle.orchestrator.nodes.notify"),
        patch("story_lifecycle.orchestrator.graph.emit_plan_done"),
        patch("story_lifecycle.orchestrator.graph.emit_terminal_opened"),
        patch("story_lifecycle.orchestrator.nodes.interrupt", side_effect=lambda x: None),
    ):
        # Disable real LLM planner
        mock_planner.is_available.return_value = False
        mock_planner.compress_context.return_value = None

        # If scenario has reviews, enable planner with mock review
        if scenario.reviews:
            mock_planner.is_available.return_value = True
            mock_planner.review_stage.side_effect = _mock_review_stage
            mock_planner.plan_stage.return_value = {
                "adapter": "claude",
                "provider": "deepseek",
                "model": "sonnet",
                "skip": False,
                "summary": "Fallback plan",
                "extra_instructions": "",
                "reasoning": "test",
                "trajectory_score": 0.8,
            }

        # Fake tool dispatch
        mock_get_tool.return_value = fake_tool

        # Fake ttyd — session always alive
        mock_ttyd.session_name.return_value = f"story-{key}"
        mock_ttyd.session_alive.return_value = True
        mock_ttyd._MPLEX = None  # Skip session crash detection

        # Run the graph synchronously
        graph_mod._run_story_impl(key)

    result = E2EResult(key, str(workspace))
    result.refresh()
    return result
