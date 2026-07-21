"""Tests for offline prompt-quality export (GET /api/analysis/prompts).

The export pairs each (story, stage) prompt with its outcome so an external
AI can correlate prompt patterns with stage failures / retries. Tests cover:
filtering by status/stage, prompt file read, done-file pairing, event
attribution, and the "no prompt file → skipped" case.
"""

import json
from pathlib import Path

from story_lifecycle.infra.db import models as db
from story_lifecycle.infra.paths import stage_done_file_rel
from story_lifecycle.orchestrator.observability import prompt_export


def _seed_story_with_prompt(
    story_key: str,
    ws: Path,
    *,
    stage: str = "design",
    status: str = "completed",
    task_actions: list[str] | None = None,
    write_done: bool = True,
    write_prompt: bool = True,
    done_summary: str = "做了 X",
    files_changed: list[str] | None = None,
) -> None:
    """Helper: create a story + write a prompt_<stage>.md + optional done file."""
    ws.mkdir(parents=True, exist_ok=True)
    ctx = {
        "_agent_actions": [
            {
                "action": "launch",
                "stage": stage,
                "adapter": "claude",
                "focus": "调研 X",
                "task_actions": task_actions or ["write_design_doc"],
                "done_file": stage_done_file_rel(story_key, stage),
            }
        ],
        "workspace_path": str(ws),
    }
    db.create_story(story_key, "T", str(ws))
    db.update_story(
        story_key,
        context_json=json.dumps(ctx),
        status=status,
        current_stage=stage,
    )
    if write_prompt:
        prompt_dir = ws / ".story" / "context" / story_key
        prompt_dir.mkdir(parents=True, exist_ok=True)
        (prompt_dir / f"prompt_{stage}.md").write_text(
            f"## 任务: {stage}\n\n这是一个测试 prompt。\n", encoding="utf-8"
        )
    if write_done:
        done_path = ws / stage_done_file_rel(story_key, stage)
        done_path.parent.mkdir(parents=True, exist_ok=True)
        done_path.write_text(
            json.dumps(
                {
                    "stage": stage,
                    "status": "done",
                    "summary": done_summary,
                    "files_changed": files_changed or [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        db.log_event(story_key, stage, "completed", {"summary": done_summary})


class TestExportPromptAnalysis:
    def test_returns_stage_with_prompt_and_done(self, isolated_story_home, tmp_path):
        ws = tmp_path / "ws1"
        _seed_story_with_prompt("EXP1", ws, status="completed")

        result = prompt_export.export_prompt_analysis(status="completed")

        assert result["count"] == 1
        item = result["items"][0]
        assert item["story_key"] == "EXP1"
        assert item["status"] == "completed"
        assert len(item["stages"]) == 1
        stage_item = item["stages"][0]
        assert stage_item["stage"] == "design"
        assert "测试 prompt" in stage_item["prompt"]
        assert stage_item["done_status"] == "done"
        assert stage_item["done_summary"] == "做了 X"
        # events include the completed we logged
        assert any(e["event_type"] == "completed" for e in stage_item["events"])

    def test_status_filter_excludes_other_statuses(
        self, isolated_story_home, tmp_path
    ):
        _seed_story_with_prompt("EXP2A", tmp_path / "a", status="completed")
        _seed_story_with_prompt("EXP2B", tmp_path / "b", status="failed")

        result = prompt_export.export_prompt_analysis(status="completed")

        keys = [it["story_key"] for it in result["items"]]
        assert "EXP2A" in keys
        assert "EXP2B" not in keys

    def test_stage_filter_returns_only_that_stage(
        self, isolated_story_home, tmp_path
    ):
        # Seed one story with two stages
        ws = tmp_path / "ws3"
        ctx = {
            "_agent_actions": [
                {
                    "action": "launch",
                    "stage": "design",
                    "adapter": "claude",
                    "task_actions": ["write_design_doc"],
                    "done_file": stage_done_file_rel("EXP3", "design"),
                },
                {
                    "action": "launch",
                    "stage": "build",
                    "adapter": "kimi",
                    "task_actions": ["write_code"],
                    "done_file": stage_done_file_rel("EXP3", "build"),
                },
            ],
            "workspace_path": str(ws),
        }
        ws.mkdir(parents=True, exist_ok=True)
        db.create_story("EXP3", "T", str(ws))
        db.update_story("EXP3", context_json=json.dumps(ctx), status="completed")
        prompt_dir = ws / ".story" / "context" / "EXP3"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        (prompt_dir / "prompt_design.md").write_text("design prompt", encoding="utf-8")
        (prompt_dir / "prompt_build.md").write_text("build prompt", encoding="utf-8")

        result = prompt_export.export_prompt_analysis(status="completed", stage="build")

        assert result["count"] == 1
        stages = result["items"][0]["stages"]
        assert len(stages) == 1
        assert stages[0]["stage"] == "build"
        assert stages[0]["adapter"] == "kimi"

    def test_story_without_prompt_file_skipped(self, isolated_story_home, tmp_path):
        # Story exists but no prompt_<stage>.md was ever written (e.g. emergency
        # stop before spawn). Should be skipped, not error.
        _seed_story_with_prompt(
            "EXP4", tmp_path / "ws4", status="completed", write_prompt=False
        )

        result = prompt_export.export_prompt_analysis(status="completed")

        assert result["count"] == 0

    def test_done_file_missing_returns_none_status(self, isolated_story_home, tmp_path):
        _seed_story_with_prompt(
            "EXP5",
            tmp_path / "ws5",
            status="failed",
            write_done=False,
        )

        result = prompt_export.export_prompt_analysis(status="failed")

        assert result["count"] == 1
        stage_item = result["items"][0]["stages"][0]
        assert stage_item["done_status"] is None
        assert stage_item["done_summary"] is None

    def test_filters_echoed_back(self, isolated_story_home, tmp_path):
        result = prompt_export.export_prompt_analysis(
            status="failed", stage="build", profile="single-pass", limit=10
        )
        f = result["filters"]
        assert f["status"] == "failed"
        assert f["stage"] == "build"
        assert f["profile"] == "single-pass"
        assert f["limit"] == 10

    def test_limit_clamped_by_endpoint(self):
        """The endpoint (not the helper) clamps limit; helper trusts caller."""
        # Helper itself does NOT clamp — it just applies whatever limit it gets.
        # Endpoint does the clamping. Test helper here, endpoint in API test.
        # Just confirm helper doesn't blow up on a large limit when no rows.
        result = prompt_export.export_prompt_analysis(limit=999)
        assert result["count"] == 0

    def test_orphan_prompt_files_returned_even_without_plan(
        self, isolated_story_home, tmp_path
    ):
        """Story got emergency-stopped before plan was written, but a prompt
        file exists. We still surface it (useful for failure analysis)."""
        ws = tmp_path / "ws_orphan"
        ws.mkdir(parents=True, exist_ok=True)
        db.create_story("EXP_ORPHAN", "T", str(ws))
        db.update_story("EXP_ORPHAN", status="failed")
        prompt_dir = ws / ".story" / "context" / "EXP_ORPHAN"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        (prompt_dir / "prompt_design.md").write_text(
            "orphan prompt", encoding="utf-8"
        )

        result = prompt_export.export_prompt_analysis(status="failed")

        assert result["count"] == 1
        item = result["items"][0]
        # No _agent_actions → action is None → these fields default
        stage = item["stages"][0]
        assert stage["stage"] == "design"
        assert stage["task_actions"] == []
        assert stage["focus"] == ""
        assert "orphan prompt" in stage["prompt"]
