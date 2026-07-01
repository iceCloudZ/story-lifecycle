"""FakeStageTool — writes .story/done without real AI, for headless E2E."""

from story_lifecycle.orchestrator.engine.demo_tool import DemoTool


class FakeStageTool(DemoTool):
    """Replaces real tool execution. Uses Scenario config for payloads."""

    def __init__(self, scenario):
        self.scenario = scenario
        super().__init__()

    def execute(self, state: dict, args: dict) -> dict:
        key = state["story_key"]
        stage = state["current_stage"]
        workspace = state["workspace"]
        next_count = state.get("execution_count", 0) + 1

        from pathlib import Path

        done_dir = Path(workspace) / ".story" / "done" / key
        done_dir.mkdir(parents=True, exist_ok=True)
        done_file = done_dir / f"{stage}.json"

        import json
        from story_lifecycle.db import models as db

        # Check if scenario has raw_done (for testing invalid JSON)
        raw = self.scenario.stage_raw_done(stage)
        if raw is not None:
            done_file.write_text(str(raw), encoding="utf-8")
        else:
            payload = self.scenario.stage_payload(stage, execution_index=next_count)
            done_file.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )

        db.log_event(
            key,
            stage,
            "execute",
            {"attempt": next_count, "tool": "fake_stage_tool"},
        )

        return {
            **state,
            "execution_count": next_count,
            "stage_start_time": 0.0,
            "last_error": None,
        }

    def describe(self) -> str:
        return "FakeStageTool for headless E2E testing"
