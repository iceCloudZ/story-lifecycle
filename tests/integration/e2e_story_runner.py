"""Project-level E2E test harness for story-lifecycle.

Runs a full story lifecycle through the real orchestrator graph with the AI CLI
and LLM layers mocked out. This lets CI verify that the whole pipeline
(intake → planning → execution → done) works end-to-end without human intervention.
"""

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

from story_lifecycle.db import models as db
from story_lifecycle.orchestrator import graph as graph_mod


class FakeAdapter:
    """Minimal adapter that satisfies the launch interface."""

    def interactive_launch_cmd(self, model: str = "") -> str:
        return "fake-cli"


def _ensure_agent_pty(story_key: str, launch_cmd: str, workspace: str, prompt: str):
    """Simulate CLI execution by writing the expected done file.

    The payload is taken from the demo payloads table keyed by current stage.
    """
    story = db.get_story(story_key)
    stage = story["current_stage"]
    payload = _DEFAULT_PAYLOADS.get(stage, {"status": "done"})

    done_dir = Path(workspace) / ".story" / "done" / story_key
    done_dir.mkdir(parents=True, exist_ok=True)
    done_file = done_dir / f"{stage}.json"
    done_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    db.log_event(story_key, stage, "execute", {"tool": "e2e_fake_cli", "stage": stage})


_DEFAULT_PAYLOADS = {
    "design": {
        "spec_path": "docs/spec.md",
        "complexity": "S",
        "summary": "E2E design completed",
    },
    "implement": {
        "files_changed": ["src/main.py"],
        "implementation_summary": "E2E implementation completed",
        "summary": "E2E implementation completed",
    },
    "review": {
        "quality": "pass",
        "summary": "E2E review passed",
    },
}


def make_actions(story_key: str, stages: list[str] | None = None) -> list[dict]:
    """Build a deterministic action list for a story."""
    stages = stages or ["design", "implement", "review"]
    return [
        {
            "action": "launch",
            "adapter": "claude",
            "stage": stage,
            "focus": f"E2E {stage} step",
            "done_file": f".story/done/{story_key}/{stage}.json",
        }
        for stage in stages
    ]


def run_story_lifecycle(
    workspace: Path,
    story_key: str = "E2E-FULL-001",
    title: str = "E2E full lifecycle",
    profile: str = "minimal",
    actions: list[dict] | None = None,
    payloads: dict[str, dict] | None = None,
) -> dict:
    """Create and run a story end-to-end in an isolated workspace.

    Returns the final story row from the DB.
    """
    db_path = workspace / "story.db"
    actions = actions or make_actions(story_key)
    payloads = payloads or _DEFAULT_PAYLOADS

    # Redirect the story DB to the temp workspace.
    db_patcher = patch.object(db, "get_db_path", return_value=str(db_path))
    db_patcher.start()
    try:
        db.init_db()
        db.upsert_story(
            story_key,
            title=title,
            workspace=str(workspace),
            profile=profile,
            current_stage=actions[0]["stage"],
            status="active",
        )
        ctx = {
            "_agent_actions": actions,
            "_plan_confirmed": True,
        }
        db.update_story(
            story_key,
            context_json=json.dumps(ctx, ensure_ascii=False),
            status="active",
        )

        def _fake_ensure_agent_pty(story_key, launch_cmd, ws, prompt):
            story = db.get_story(story_key)
            stage = story["current_stage"]
            payload = payloads.get(stage, {"status": "done"})
            done_dir = Path(ws) / ".story" / "done" / story_key
            done_dir.mkdir(parents=True, exist_ok=True)
            done_file = done_dir / f"{stage}.json"
            done_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            db.log_event(story_key, stage, "execute", {"tool": "e2e_fake_cli", "stage": stage})

        fake_adapter = FakeAdapter()
        with (
            patch("story_lifecycle.terminal.pty.ensure_agent_pty", _fake_ensure_agent_pty),
            patch("story_lifecycle.adapters.get_adapter", return_value=fake_adapter),
            patch("story_lifecycle.context_providers.get_transcript_context", return_value=None),
        ):
            # Keep workspace lock files inside the temp workspace.
            with patch.object(graph_mod, "STORY_HOME", workspace):
                graph_mod.run_story(story_key)

        return db.get_story(story_key)
    finally:
        db_patcher.stop()


def assert_story_completed(story: dict, expected_final_stage: str = "review"):
    assert story is not None
    assert story["status"] == "completed", f"story failed: {story.get('last_error')}"
    assert story["current_stage"] == expected_final_stage
