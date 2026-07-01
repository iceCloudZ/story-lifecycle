"""E2E runner — patches deps, runs graph, returns result for assertion."""

from pathlib import Path
import json
from unittest.mock import patch

from story_lifecycle.infra.db import models as db
from story_lifecycle.orchestrator.engine import graph as graph_mod

from .scenario import Scenario


def _profile_without_adversarial() -> dict:
    return {
        "cli": "claude",
        "stages": {
            "design": {
                "description": "Design",
                "review": True,
                "expected_outputs": ["spec_path", "complexity"],
                "next_default": ["implement"],
            },
            "implement": {
                "description": "Implement",
                "review": True,
                "expected_outputs": ["files_changed", "summary"],
                "next_default": ["review"],
            },
            "review": {
                "description": "Review",
                "review": False,
                "expected_outputs": [],
                "next_default": [],
            },
        },
        "adversarial": {"enabled": False},
    }


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

    # Build review mock that returns scenario-defined review results
    def _mock_review_stage(state, cfg, stage_output):
        stage = state["current_stage"]
        exec_count = state.get("execution_count", 1)
        return scenario.review_payload(stage, execution_index=exec_count)

    with (
        patch("story_lifecycle.orchestrator.nodes.graph_nodes.planner") as mock_planner,
        patch("story_lifecycle.orchestrator.nodes.ttyd") as mock_ttyd,
        patch("story_lifecycle.orchestrator.nodes.notify"),
        patch(
            "story_lifecycle.orchestrator.engine.profile_loader.load_profile",
            return_value=_profile_without_adversarial(),
        ),
        patch(
            "story_lifecycle.orchestrator.engine.profile_loader._load_raw",
            return_value=_profile_without_adversarial(),
        ),
    ):
        mock_planner.compress_context.return_value = None

        # Always provide mocked planner returns
        mock_planner.review_stage.return_value = {
            "quality": "pass",
            "summary": "E2E review pass",
            "issues": [],
            "suggestions": [],
            "trajectory_score": 0.9,
            "context_updates": {},
            "reasoning": "test",
        }

        # If scenario has reviews, use side_effect for dynamic behavior
        if scenario.reviews:
            mock_planner.review_stage.side_effect = _mock_review_stage

        # Fake ttyd — session always alive
        mock_ttyd.session_name.return_value = f"story-{key}"
        mock_ttyd.session_alive.return_value = True
        mock_ttyd._MPLEX = None  # Skip session crash detection

        # Run the graph synchronously
        graph_mod._run_story_impl(key)

    result = E2EResult(key, str(workspace))
    result.refresh()
    return result


def assert_scenario_expect(result: E2EResult, expect: dict) -> None:
    """Assert common scenario expectations against DB state and events."""
    assert result.story is not None

    if "status" in expect:
        assert result.story["status"] == expect["status"]

    if "last_error_contains" in expect:
        assert expect["last_error_contains"] in (result.story.get("last_error") or "")

    if "context" in expect:
        ctx = json.loads(result.story.get("context_json") or "{}")
        for key, value in expect["context"].items():
            assert ctx.get(key) == value

    if "event_counts" in expect:
        event_types = [event["event_type"] for event in result.events]
        for event_type, count in expect["event_counts"].items():
            assert event_types.count(event_type) == count
