"""Scenario — loads a YAML test scenario and provides stage payloads."""

from pathlib import Path
from typing import Any

import yaml


class Scenario:
    """Represents a single E2E test scenario loaded from YAML."""

    def __init__(self, path: str | Path):
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        self.story_key: str = raw["story_key"]
        self.title: str = raw.get("title", "")
        self.profile: str = raw.get("profile", "minimal")
        self.stages: dict[str, dict] = raw.get("stages", {})
        self.reviews: dict[str, dict] = raw.get("reviews", {})
        self.expect: dict = raw.get("expect", {})

    def stage_payload(self, stage: str, execution_index: int = 1) -> dict[str, Any]:
        """Return the done-file payload for a given stage.

        execution_index is 1-based. If the stage uses `executions` array,
        pick the element at execution_index-1 (clamped to last element).
        Otherwise use the single `done` dict or `raw_done` string.
        """
        stage_cfg = self.stages.get(stage, {})

        # Multiple executions defined
        if "executions" in stage_cfg:
            execs = stage_cfg["executions"]
            idx = min(execution_index - 1, len(execs) - 1)
            return execs[idx].get("done", {})

        # Raw done (for testing invalid JSON)
        if "raw_done" in stage_cfg:
            return stage_cfg["raw_done"]

        # Single done payload
        return stage_cfg.get("done", {})

    def stage_raw_done(self, stage: str) -> str | None:
        """Return raw_done string if the stage defines one, else None."""
        stage_cfg = self.stages.get(stage, {})
        return stage_cfg.get("raw_done")

    def review_payload(self, stage: str, execution_index: int = 1) -> dict:
        """Return the review result for a given stage at a given execution.

        If no reviews are defined for the stage, returns {"quality": "pass"}.
        """
        stage_reviews = self.reviews.get(stage, {})
        if "executions" in stage_reviews:
            execs = stage_reviews["executions"]
            idx = min(execution_index - 1, len(execs) - 1)
            return execs[idx]
        return stage_reviews if stage_reviews else {"quality": "pass"}
