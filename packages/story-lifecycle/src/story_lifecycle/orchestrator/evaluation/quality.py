# src/story_lifecycle/orchestrator/quality.py
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from ...infra.db import models as db


_FAILURE_CHECKLIST_PATH = (
    Path(__file__).parents[5] / "story-miner" / "docs" / "failure-checklist.md"
)


def _load_failure_checklist_items(task_type: str | None = None, limit: int = 5) -> list[str]:
    """Load preventive checklist items from failure-checklist.md.

    Falls back to the generic items if no task_type-specific file exists.
    """
    items: list[str] = []
    if _FAILURE_CHECKLIST_PATH.exists():
        text = _FAILURE_CHECKLIST_PATH.read_text(encoding="utf-8")
        # Parse the markdown table under "## 预防检查项"
        in_table = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("## 预防检查项"):
                in_table = True
                continue
            if in_table and stripped.startswith("|"):
                # Skip header and separator rows
                if re.match(r"^\|\s*-+\s*\|", stripped):
                    continue
                cols = [c.strip() for c in stripped.strip("|").split("|")]
                if len(cols) >= 2 and cols[0] not in ("优先级", ""):
                    item = cols[1]
                    if item and item != "检查项":
                        items.append(item)
            elif in_table and stripped.startswith("##"):
                break
    # TODO: load task_type-specific failure checklists from knowledge/playbooks
    # when Brief F produces them.
    return items[:limit]


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
        candidates = db.find_relevant_patterns(relevant_tags, limit=20)
    else:
        candidates = db.get_active_learned_patterns(limit=20)

    # LLM rerank if candidates exist
    patterns = candidates
    pattern_mode = "tag_overlap"
    if candidates:
        try:
            from .semantic import rerank_relevant_patterns

            story = db.get_story(story_key) or {}
            ctx = json.loads(story.get("context_json") or "{}")
            story_context = {
                "title": story.get("title", story_key),
                "stage": stage,
                "type": story.get("sub_type", ""),
                "summary": ctx.get("prd_summary", "")[:500],
            }
            rerank = rerank_relevant_patterns(
                story_context, candidates, limit=max_items
            )
            if rerank["ok"] and rerank["mode"] == "llm":
                selected_ids = [
                    s["pattern_id"] for s in rerank["data"].get("selected", [])
                ]
                reranked = [p for p in candidates if p["id"] in selected_ids]
                # Fallback to tag overlap if LLM returns empty selection
                if reranked:
                    patterns = reranked
                    pattern_mode = "llm_rerank"
            # else: keep tag overlap order
        except Exception:
            pass  # keep candidates as-is

    if patterns:
        lines.append(f"Relevant Learned Patterns (mode: {pattern_mode}):")
        for p in patterns[:max_items]:
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

    story = db.get_story(story_key) or {}
    ctx = json.loads(story.get("context_json") or "{}")
    task_type = ctx.get("task_type")

    lines = ["## Quality Checklist", ""]

    # Inject mined failure-mode preventive checks
    failure_items = _load_failure_checklist_items(task_type=task_type, limit=5)
    if failure_items:
        lines.append("### 失败模式预防检查项")
        for item in failure_items:
            lines.append(f"- [ ] {item}")
        lines.append("")

    if findings:
        lines.append("### 当前 Open Findings")
        for f in findings[:5]:
            lines.append(f"- [ ] Fix: {f['description']}")
            if f.get("recommendation"):
                lines.append(f"      Approach: {f['recommendation']}")
        lines.append("")

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
