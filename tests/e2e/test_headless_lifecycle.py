"""Headless E2E lifecycle tests.

Runs the full LangGraph lifecycle with FakeStageTool.
Each test loads a YAML scenario and asserts final DB state.
"""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from story_lifecycle.db import models as db
from story_lifecycle.orchestrator import graph as graph_mod

from .runner import assert_scenario_expect, run_scenario
from .scenario import Scenario


SCENARIOS = Path(__file__).parent / "scenarios"


def _load(name: str) -> Scenario:
    return Scenario(SCENARIOS / f"{name}.yaml")


def test_execution_payload_overrun_fails_fast():
    scenario = _load("review_retry_then_pass")

    with pytest.raises(IndexError, match="No payload configured"):
        scenario.stage_payload("design", execution_index=3)


class TestHappyPath:
    """design -> implement -> test -> completed"""

    def test_full_lifecycle(self, isolated_story_home, e2e_workspace):
        scenario = _load("happy_path")
        result = run_scenario(scenario, e2e_workspace)

        assert result.story is not None
        assert_scenario_expect(result, scenario.expect)

        # Verify context has expected fields from design stage
        ctx = json.loads(result.story.get("context_json", "{}"))
        assert ctx.get("spec_path") == "docs/spec.md"
        assert ctx.get("complexity") == "S"

        # Verify events cover the full lifecycle
        event_types = [e["event_type"] for e in result.events]
        assert "execute" in event_types


class TestMarkdownDoneJson:
    """Done file is wrapped in markdown fences — robust_json_parse handles it."""

    def test_parses_markdown_json(self, isolated_story_home, e2e_workspace):
        scenario = _load("markdown_done_json")
        result = run_scenario(scenario, e2e_workspace)

        assert result.story is not None
        assert_scenario_expect(result, scenario.expect)

        ctx = json.loads(result.story.get("context_json", "{}"))
        assert ctx.get("spec_path") == "docs/spec.md"
        # Verify markdown-fence wrapped JSON was parsed (spec_path only appears via robust_json_parse)
        assert ctx.get("complexity") == "S"


class TestMissingExpectedOutput:
    """Design stage omits required `spec_path` -> story should end blocked."""

    def test_blocked_on_missing_field(self, isolated_story_home, e2e_workspace):
        scenario = _load("missing_expected_output")
        result = run_scenario(scenario, e2e_workspace)

        assert result.story is not None
        assert_scenario_expect(result, scenario.expect)


class TestReviewRetryThenPass:
    """First review returns revise, second returns pass."""

    def test_retry_then_complete(self, isolated_story_home, e2e_workspace):
        scenario = _load("review_retry_then_pass")
        result = run_scenario(scenario, e2e_workspace)

        assert result.story is not None
        assert_scenario_expect(result, scenario.expect)

        # Should have multiple execute events (retry)
        execute_events = [e for e in result.events if e["event_type"] == "execute"]
        assert len(execute_events) >= 2  # at least 2 executions for design stage

        # Should have review events
        review_events = [e for e in result.events if e["event_type"] == "review"]
        assert len(review_events) >= 1


class TestSubStoryWaitResume:
    """Parent story delegates to sub-stories, parent enters waiting_subtasks."""

    def test_parent_waits_for_children(self, isolated_story_home, e2e_workspace):
        scenario = _load("sub_story_wait_resume")
        key = scenario.story_key

        # Create parent story
        db.upsert_story(
            key,
            title=scenario.title,
            workspace=str(e2e_workspace),
            profile=scenario.profile,
            current_stage="design",
            status="active",
        )

        fake_tool = MagicMock()
        fake_tool.execute.side_effect = lambda state, args: {
            **state,
            "execution_count": state.get("execution_count", 0) + 1,
            "stage_start_time": 0.0,
            "last_error": None,
        }

        with (
            patch("story_lifecycle.orchestrator.nodes.planner") as mock_planner,
            patch("story_lifecycle.orchestrator.tools.get_tool") as mock_get_tool,
            patch("story_lifecycle.orchestrator.nodes.ttyd") as mock_ttyd,
            patch("story_lifecycle.orchestrator.nodes.notify"),
            patch(
                "story_lifecycle.orchestrator.nodes.load_profile",
                return_value={
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
                },
            ),
            patch("story_lifecycle.orchestrator.graph.emit_plan_done"),
            patch("story_lifecycle.orchestrator.graph.emit_terminal_opened"),
            patch("story_lifecycle.orchestrator.graph._executor.submit") as mock_submit,
        ):
            # Planner returns a split decision
            mock_planner.is_available.return_value = True
            mock_planner.compress_context.return_value = None
            mock_planner.plan_stage.return_value = {
                "split": True,
                "subtasks": [
                    {
                        "key_suffix": "auth",
                        "title": "Auth module",
                        "summary": "Implement auth",
                        "depends_on": [],
                    },
                    {
                        "key_suffix": "api",
                        "title": "API layer",
                        "summary": "Implement API",
                        "depends_on": ["auth"],
                    },
                ],
                "summary": "Splitting into sub-stories",
            }

            mock_get_tool.return_value = fake_tool
            mock_ttyd.session_name.return_value = f"story-{key}"
            mock_ttyd.session_alive.return_value = True
            mock_ttyd._MPLEX = None

            graph_mod._run_story_impl(key)

        # Verify parent is waiting_subtasks
        parent = db.get_story(key)
        assert parent is not None
        assert parent["status"] == "waiting_subtasks"
        mock_submit.assert_called_once()
        assert not (isolated_story_home / "planner_error.log").exists()

        # Verify sub-stories exist in DB
        sub_auth = db.get_story(f"{key}-auth")
        sub_api = db.get_story(f"{key}-api")
        assert sub_auth is not None
        assert sub_api["parent_key"] == key
        assert sub_api["status"] == "blocked"  # depends on auth

        # Verify delegation events
        delegate_events = [
            e for e in db.get_story_events(key) if e["event_type"] == "delegate"
        ]
        assert len(delegate_events) == 2
