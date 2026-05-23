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
    db.log_event(
        story_key, stage, "code_review_finding", {"finding_id": fid, **finding}
    )
    return fid


def update_finding_status(
    story_key: str,
    finding_id: str,
    status: str,
    reason: str = "",
    evidence: dict | None = None,
) -> None:
    """Update finding status + write audit event.

    Raises ValueError if finding_id does not exist.
    """
    old = db.get_finding(finding_id)
    if old is None:
        raise ValueError(f"Finding not found: {finding_id}")
    old_status = old["status"]

    kwargs = {"status": status}
    if evidence and evidence.get("verification_event_id"):
        kwargs["verification_event_id"] = evidence["verification_event_id"]
    db.update_finding(finding_id, **kwargs)

    db.log_event(
        story_key,
        old.get("stage", ""),
        "finding_status_changed",
        {
            "finding_id": finding_id,
            "from": old_status,
            "to": status,
            "reason": reason,
            "evidence": evidence,
        },
    )


def record_verification(
    story_key: str,
    stage: str,
    commands: list[dict],
    covered_findings: list[str] | None = None,
    commit: str | None = None,
) -> None:
    """Write verification_result event."""
    db.log_event(
        story_key,
        stage,
        "verification_result",
        {
            "commands": commands,
            "covered_findings": covered_findings or [],
            "commit": commit,
            "timestamp": datetime.now().isoformat(),
        },
    )


def record_story_intake(
    story_key: str, source: str, source_id: str, metadata: dict | None = None
) -> None:
    """Record story intake event."""
    db.log_event(
        story_key,
        "",
        "story_intake",
        {
            "source": source,
            "source_id": source_id,
            "timestamp": datetime.now().isoformat(),
            **(metadata or {}),
        },
    )


def build_quality_packet(
    story_key: str,
    stage: str,
    max_items: int = 5,
    relevant_tags: list[str] | None = None,
) -> str:
    """Build compact Quality Packet for prompt injection."""
    lines = [f"Quality Packet for {story_key}", ""]

    # Open findings
    findings = db.get_open_findings(story_key)
    if findings:
        lines.append("Open Findings:")
        for f in findings[:max_items]:
            lines.append(
                f"- [{f['severity'].upper()}] {f['category']}: {f['description']}"
            )
            if f.get("recommendation"):
                lines.append(f"  Fix: {f['recommendation']}")
        lines.append("")
    else:
        lines.append("Open Findings: none")
        lines.append("")

    # Learned patterns (relevance-filtered if tags provided)
    if relevant_tags:
        patterns = db.find_relevant_patterns(relevant_tags, limit=max_items)
    else:
        patterns = db.get_active_learned_patterns(limit=max_items)
    if patterns:
        lines.append("Relevant Learned Patterns:")
        for p in patterns:
            lines.append(f"- {p['pattern']}:")
            lines.append(f"  {p['rule']}")
        lines.append("")

    # Verification baseline
    events = db.get_recent_quality_events(story_key, ["verification_result"], limit=3)
    if events:
        lines.append("Verification Baseline:")
        for e in events[:max_items]:
            payload = (
                json.loads(e.get("payload", "{}"))
                if isinstance(e.get("payload"), str)
                else e.get("payload", {})
            )
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


def propose_learned_pattern(
    story_key: str,
    pattern: str,
    applies_to: list[str],
    rule: str,
    source_findings: list[str] | None = None,
    confidence: str = "medium",
) -> str:
    """Propose a learned pattern from verified findings. Status: proposed."""
    pid = db.create_learned_pattern(
        pattern=pattern,
        applies_to=applies_to,
        rule=rule,
        source_findings=source_findings,
        confidence=confidence,
    )
    db.log_event(
        story_key,
        "",
        "learned_pattern",
        {
            "pattern_id": pid,
            "pattern": pattern,
            "status": "proposed",
            "applies_to": applies_to,
        },
    )
    return pid


def approve_pattern(pattern_id: str) -> None:
    """Approve a proposed pattern. proposed -> approved."""
    db.update_learned_pattern(pattern_id, status="approved")


def activate_pattern(pattern_id: str) -> None:
    """Activate an approved pattern. approved -> active."""
    db.update_learned_pattern(pattern_id, status="active")


def deprecate_pattern(pattern_id: str) -> None:
    """Deprecate an active pattern. active -> deprecated."""
    db.update_learned_pattern(pattern_id, status="deprecated")


def reject_pattern(pattern_id: str) -> None:
    """Reject a proposed pattern. proposed -> rejected."""
    db.update_learned_pattern(pattern_id, status="rejected")


def check_dor(story_key: str, stage: str, record: bool = True) -> dict:
    """Definition of Ready check. Returns {ready, missing, warnings}.

    Set record=False for read-only queries (e.g. dashboard polling).
    """
    story = db.get_story(story_key)
    if not story:
        return {"ready": False, "missing": ["story not found"], "warnings": []}

    missing = []
    warnings = []

    if not story.get("title"):
        missing.append("title")
    if not story.get("source_type"):
        warnings.append("no external source linked")

    ctx = json.loads(story.get("context_json") or "{}")
    if not ctx.get("prd_path"):
        warnings.append("no PRD file")
    if not ctx.get("acceptance_criteria"):
        warnings.append("no acceptance criteria")
    if not ctx.get("affected_modules"):
        warnings.append("affected modules not declared")

    ready = len(missing) == 0
    if record:
        db.log_event(
            story_key,
            stage,
            "readiness_check",
            {
                "ready": ready,
                "missing": missing,
                "warnings": warnings,
            },
        )
    return {"ready": ready, "missing": missing, "warnings": warnings}


def check_dod(story_key: str, stage: str) -> dict:
    """Definition of Done check. Returns {passed, blocking, warnings}."""
    blocking = []
    warnings = []

    # No open high findings
    open_high = db.get_open_findings(story_key, min_severity="high")
    if open_high:
        blocking.append(f"{len(open_high)} open high finding(s)")

    # Verification result exists
    verifications = db.get_recent_quality_events(
        story_key, ["verification_result"], limit=1
    )
    if not verifications:
        warnings.append("no verification result recorded")

    story = db.get_story(story_key)
    if story and story.get("sub_type") == "bug-fix":
        ctx = json.loads(story.get("context_json") or "{}")
        if not ctx.get("regression_tests_added"):
            warnings.append("bugfix story missing regression tests")

    passed = len(blocking) == 0
    return {"passed": passed, "blocking": blocking, "warnings": warnings}
