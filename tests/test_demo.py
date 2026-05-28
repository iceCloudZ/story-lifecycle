"""Tests for the `story demo` CLI command."""

import json
from pathlib import Path
from unittest.mock import patch

from story_lifecycle.db import models as db
from story_lifecycle.orchestrator import graph as graph_mod


def _run_demo_with_db(tmp_path: Path):
    """Run demo with isolated DB, return (story, events)."""
    db_path = tmp_path / "story.db"
    checkpoint_path = tmp_path / "checkpoint.db"

    with (
        patch.object(db, "get_db_path", return_value=db_path),
        patch.object(graph_mod, "checkpoint_db", checkpoint_path),
        patch("story_lifecycle.orchestrator.nodes.graph_nodes.planner") as mock_planner,
        patch("story_lifecycle.orchestrator.tools.get_tool") as mock_get_tool,
        patch("story_lifecycle.orchestrator.nodes.ttyd") as mock_ttyd,
        patch("story_lifecycle.orchestrator.nodes.notify"),
        patch("story_lifecycle.orchestrator.graph.emit_plan_done"),
        patch("story_lifecycle.orchestrator.graph.emit_terminal_opened"),
        patch(
            "story_lifecycle.orchestrator.nodes.graph_nodes.interrupt",
            side_effect=lambda x: None,
        ),
        patch(
            "story_lifecycle.orchestrator.nodes.graph_nodes.load_profile",
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
        patch("story_lifecycle.orchestrator.nodes.state.STORY_HOME", tmp_path),
    ):
        from story_lifecycle.orchestrator.demo_tool import DemoTool
        from story_lifecycle.orchestrator import nodes as nodes_mod

        mock_planner.compress_context.return_value = None
        mock_planner.plan_stage.return_value = {
            "adapter": "claude",
            "provider": "deepseek",
            "model": "sonnet",
            "skip": False,
            "summary": "Test plan",
            "extra_instructions": "Do the thing",
            "reasoning": "test",
            "trajectory_score": 0.9,
        }
        mock_planner.review_stage.return_value = {
            "quality": "pass",
            "summary": "Looks good",
            "issues": [],
            "suggestions": [],
            "trajectory_score": 0.9,
            "context_updates": {},
            "reasoning": "test",
        }
        mock_get_tool.return_value = DemoTool()
        mock_ttyd.session_name.return_value = "story-demo-hello"
        mock_ttyd.session_alive.return_value = True
        mock_ttyd._MPLEX = None

        db.init_db()
        db.upsert_story(
            "demo-hello",
            title="Demo: Hello Story Lifecycle",
            workspace=str(tmp_path),
            profile="demo",
            current_stage="design",
            status="active",
        )

        with patch.object(nodes_mod, "STORY_HOME", tmp_path):
            graph_mod._run_story_impl("demo-hello")

    with patch.object(db, "get_db_path", return_value=db_path):
        story = db.get_story("demo-hello")
        events = db.get_story_events("demo-hello")

    return story, events


class TestDemo:
    def test_demo_completes_all_stages(self, tmp_path):
        story, events = _run_demo_with_db(tmp_path)

        assert story is not None
        assert story["status"] == "completed"
        assert story["current_stage"] == "review"

    def test_demo_creates_execute_events(self, tmp_path):
        story, events = _run_demo_with_db(tmp_path)

        execute_events = [e for e in events if e["event_type"] == "execute"]
        assert len(execute_events) == 3

        stages = [e["stage"] for e in execute_events]
        assert stages == ["design", "implement", "review"]

    def test_demo_context_has_expected_fields(self, tmp_path):
        story, events = _run_demo_with_db(tmp_path)

        ctx = json.loads(story.get("context_json", "{}"))
        assert ctx.get("spec_path") == "docs/spec.md"
        assert ctx.get("complexity") == "S"

    def test_demo_no_llm_required(self, tmp_path):
        """Verify demo works without any LLM config."""
        import os

        for key in ("STORY_LLM_API_KEY", "STORY_LLM_BASE_URL", "STORY_LLM_MODEL"):
            os.environ.pop(key, None)

        story, events = _run_demo_with_db(tmp_path)
        assert story["status"] == "completed"
