"""Project-level integration test: run a complete story automatically.

This test drives the real story-lifecycle orchestrator from intake to done
using the E2E harness (mocked AI CLI). It proves the whole pipeline works
without human intervention.
"""

from .e2e_story_runner import run_story_lifecycle, assert_story_completed


def test_full_story_lifecycle_completes_automatically(tmp_path):
    """A story with a seeded action list should reach 'completed' on its own."""
    story = run_story_lifecycle(tmp_path)
    assert_story_completed(story)


def test_full_story_lifecycle_with_custom_stages(tmp_path):
    """Support custom stage lists (e.g. design only)."""
    story = run_story_lifecycle(
        tmp_path,
        story_key="E2E-DESIGN-ONLY",
        actions=[
            {
                "action": "launch",
                "adapter": "claude",
                "stage": "design",
                "focus": "Just design",
                "done_file": ".story/done/E2E-DESIGN-ONLY/design.json",
            }
        ],
        payloads={"design": {"spec_path": "docs/spec.md", "complexity": "L"}},
    )
    assert story["status"] == "completed"
    assert story["current_stage"] == "design"
