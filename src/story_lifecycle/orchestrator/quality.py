# src/story_lifecycle/orchestrator/quality.py
from __future__ import annotations

import json
from datetime import datetime

from ..db import models as db


def record_finding(story_key: str, stage: str, finding: dict) -> str:
    """Create a finding + write code_review_finding event."""
    fid = db.create_finding(
        story_key=story_key,
        stage=stage,
        source=finding.get("source", "code_review"),
        severity=finding["severity"],
        category=finding["category"],
        description=finding["description"],
        location=finding.get("location"),
        recommendation=finding.get("recommendation"),
        root_cause=finding.get("root_cause"),
    )
    db.log_event(story_key, stage, "code_review_finding", {"finding_id": fid, **finding})
    return fid


def update_finding_status(
    story_key: str,
    finding_id: str,
    status: str,
    reason: str = "",
    evidence: dict | None = None,
) -> None:
    """Update finding status + write audit event."""
    old = db.get_finding(finding_id)
    old_status = old["status"] if old else "unknown"

    kwargs = {"status": status}
    if evidence and evidence.get("verification_event_id"):
        kwargs["verification_event_id"] = evidence["verification_event_id"]
    db.update_finding(finding_id, **kwargs)

    db.log_event(story_key, old.get("stage", ""), "finding_status_changed", {
        "finding_id": finding_id,
        "from": old_status,
        "to": status,
        "reason": reason,
        "evidence": evidence,
    })


def record_verification(
    story_key: str,
    stage: str,
    commands: list[dict],
    covered_findings: list[str] | None = None,
    commit: str | None = None,
) -> None:
    """Write verification_result event."""
    db.log_event(story_key, stage, "verification_result", {
        "commands": commands,
        "covered_findings": covered_findings or [],
        "commit": commit,
        "timestamp": datetime.now().isoformat(),
    })


def record_story_intake(story_key: str, source: str, source_id: str, metadata: dict | None = None) -> None:
    """Record story intake event."""
    db.log_event(story_key, "", "story_intake", {
        "source": source,
        "source_id": source_id,
        "timestamp": datetime.now().isoformat(),
        **(metadata or {}),
    })


def build_quality_packet(story_key: str, stage: str, max_items: int = 5) -> str:
    """Build compact Quality Packet for prompt injection."""
    lines = [f"Quality Packet for {story_key}", ""]

    # Open findings
    findings = db.get_open_findings(story_key)
    if findings:
        lines.append("Open Findings:")
        for f in findings[:max_items]:
            lines.append(f"- [{f['severity'].upper()}] {f['category']}: {f['description']}")
            if f.get("recommendation"):
                lines.append(f"  Fix: {f['recommendation']}")
        lines.append("")
    else:
        lines.append("Open Findings: none")
        lines.append("")

    # Verification baseline
    events = db.get_recent_quality_events(story_key, ["verification_result"], limit=3)
    if events:
        lines.append("Verification Baseline:")
        for e in events[:max_items]:
            payload = json.loads(e.get("payload", "{}")) if isinstance(e.get("payload"), str) else e.get("payload", {})
            for cmd in payload.get("commands", []):
                lines.append(f"- {cmd.get('cmd', '?')}: {cmd.get('status', '?')}")
        lines.append("")

    return "\n".join(lines)


def build_quality_checklist(story_key: str, stage: str) -> str:
    """Build compact Quality Checklist for executor task file."""
    findings = db.get_open_findings(story_key)
    if not findings:
        return ""

    lines = ["## Quality Checklist", ""]
    for f in findings[:5]:
        lines.append(f"- [ ] Fix: {f['description']}")
        if f.get("recommendation"):
            lines.append(f"      Approach: {f['recommendation']}")
    lines.append("- [ ] Run: pytest && ruff check src tests")
    lines.append("")
    return "\n".join(lines)
