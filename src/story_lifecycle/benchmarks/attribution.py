"""SWE-bench Attribution — analyze a single instance for failure root cause.

Produces a structured attribution report with:
- Failure node identification
- Root cause classification
- Counterfactual improvement candidates
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


@dataclass
class AttributionReport:
    """Structured failure analysis report."""

    instance_id: str = ""
    repo: str = ""
    failure_stage: str = ""
    failure_node: str = ""
    root_cause_category: str = ""
    root_cause_detail: str = ""
    counterfactual_candidates: list[str] = field(default_factory=list)
    gradient_signals: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def analyze_instance(workspace: str, instance_id: str) -> AttributionReport:
    """Analyze a completed SWE-bench instance for failure attribution.

    Reads the story's stage logs and done files to identify the failure
    point and classify the root cause.
    """
    from ..db import models as db

    report = AttributionReport(instance_id=instance_id)

    # Find the story
    story = db.get_story(instance_id)
    if not story:
        report.root_cause_category = "story_not_found"
        return report

    report.repo = story.get("title", "")

    # Read stage logs to find failure point
    logs = db.list_stage_logs(instance_id)
    for log in reversed(logs):
        if log.get("action") in ("fail", "error", "stage_error"):
            report.failure_stage = log.get("stage", "")
            report.failure_node = log.get("action", "")
            report.root_cause_detail = log.get("detail", "")
            break

    # Read event log for more context
    events = db.list_events(instance_id)
    stage_errors = [e for e in events if e.get("event_type") == "stage_error"]
    if stage_errors:
        report.root_cause_category = classify_error(stage_errors[0].get("detail", ""))

    # Generate counterfactual candidates from trajectory
    if story.get("context_json"):
        try:
            ctx = json.loads(story["context_json"])
            trajectory = ctx.get("trajectory_score", 0)
            if trajectory < 0.5:
                report.counterfactual_candidates = [
                    "增加 review 严格度",
                    "使用更详细的 PRD 输入",
                    "降低 scope 复杂度",
                ]
        except json.JSONDecodeError:
            pass

    return report


def classify_error(detail: str) -> str:
    """Classify a stage error into a root cause category."""
    if "timeout" in detail.lower():
        return "timeout"
    if "malformed" in detail.lower() or "json" in detail.lower():
        return "output_malformed"
    if "gate" in detail.lower() or "review" in detail.lower():
        return "gate_blocked"
    if "adapter" in detail.lower() or "cli" in detail.lower():
        return "adapter_failure"
    if "workspace" in detail.lower():
        return "workspace_issue"
    return "unknown"


def write_attribution_report(report: AttributionReport, output_path: str) -> None:
    """Write attribution report to JSON file."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(report), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
