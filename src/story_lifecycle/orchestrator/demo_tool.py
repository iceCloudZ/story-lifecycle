"""DemoTool — simulates stage execution for `story demo`.

Writes .story/done files with predefined payloads, no real AI needed.
"""

import json
from pathlib import Path

from ..db import models as db

# Predefined payloads for the 3-stage minimal profile
_DEMO_PAYLOADS = {
    "design": {
        "spec_path": "docs/spec.md",
        "complexity": "S",
        "summary": "Demo design completed",
    },
    "implement": {
        "files_changed": ["src/demo.py"],
        "implementation_summary": "Demo implementation completed",
        "summary": "Demo implementation completed",
    },
    "review": {"quality": "pass", "summary": "Demo review completed"},
}


class DemoTool:
    """Simulates stage execution for demo mode.

    Writes .story/done/{key}/{stage}.json with predefined payloads.
    """

    def __init__(self, payloads: dict | None = None):
        self.payloads = payloads or _DEMO_PAYLOADS

    def execute(self, state: dict, args: dict) -> dict:
        key = state["story_key"]
        stage = state["current_stage"]
        workspace = state["workspace"]
        next_count = state.get("execution_count", 0) + 1

        done_dir = Path(workspace) / ".story" / "done" / key
        done_dir.mkdir(parents=True, exist_ok=True)
        done_file = done_dir / f"{stage}.json"

        payload = self.payloads.get(stage, {"status": "done"})
        done_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        db.log_event(
            key, stage, "execute", {"attempt": next_count, "tool": "demo_tool"}
        )

        return {
            **state,
            "execution_count": next_count,
            "stage_start_time": 0.0,
            "last_error": None,
        }

    def describe(self) -> str:
        return "DemoTool (simulated execution)"
