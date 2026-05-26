"""Review Gate decision model and helpers.

GateDecision is the primary structured object for every gate outcome:
advance / retry_stage / retry_review / wait_confirm / fail / accept_risk_advance.

Every blocking (non-advance) decision must write:
  - story.last_error
  - event_log: gate_decision
  - markdown gate report
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---- context_json key helpers ----

_REVIEW_ROUND_KEY_PREFIX = "review_round_count_"


def _review_round_key(stage: str) -> str:
    return f"{_REVIEW_ROUND_KEY_PREFIX}{stage}"


def get_review_round_count(context: dict, stage: str) -> int:
    """Read review_round_count from context dict. Returns 0 if never set."""
    try:
        return int(context.get(_review_round_key(stage), 0))
    except (TypeError, ValueError):
        return 0


def increment_review_round_count(context: dict, stage: str) -> int:
    """Increment review_round_count in the context dict, return new count."""
    key = _review_round_key(stage)
    current = get_review_round_count(context, stage)
    context[key] = current + 1
    return current + 1


# ---- GateDecision dataclass ----


@dataclass
class GateDecision:
    story_key: str
    stage: str
    gate_name: str = "adversarial_review"
    decision_id: str = ""
    decision: str = "wait_confirm"  # advance|retry_stage|retry_review|wait_confirm|fail|accept_risk_advance
    reason_code: str = "review_unavailable"
    human_message: str = ""
    executor_attempt_count: int = 0
    review_round_count: int = 0
    retry_limit: int = 3
    reviewer: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)
    allowed_actions: list = field(
        default_factory=lambda: [
            "retry_review",
            "retry_stage",
            "accept_risk_advance",
            "fail_story",
        ]
    )
    created_at: str = ""

    def __post_init__(self):
        import uuid

        if not self.decision_id:
            self.decision_id = f"{self.stage}-gate-{uuid.uuid4().hex[:8]}"
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()
        if not self.human_message:
            self.human_message = (
                f"Gate blocked at {self.stage}. Manual decision required."
            )
        if not self.reviewer:
            self.reviewer = {"kind": "unknown", "adapter": "", "model": ""}
        if not self.evidence:
            self.evidence = {
                "done_consumed": False,
                "review_run_id": None,
                "open_findings": [],
                "report_path": "",
            }

    def to_dict(self) -> dict[str, Any]:
        return {
            "story_key": self.story_key,
            "stage": self.stage,
            "gate_name": self.gate_name,
            "decision_id": self.decision_id,
            "decision": self.decision,
            "reason_code": self.reason_code,
            "human_message": self.human_message,
            "executor_attempt_count": self.executor_attempt_count,
            "review_round_count": self.review_round_count,
            "retry_limit": self.retry_limit,
            "reviewer": self.reviewer,
            "evidence": self.evidence,
            "allowed_actions": self.allowed_actions,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> GateDecision:
        return cls(
            story_key=d.get("story_key", ""),
            stage=d.get("stage", ""),
            gate_name=d.get("gate_name", "adversarial_review"),
            decision_id=d.get("decision_id", ""),
            decision=d.get("decision", "wait_confirm"),
            reason_code=d.get("reason_code", "review_unavailable"),
            human_message=d.get("human_message", ""),
            executor_attempt_count=d.get("executor_attempt_count", 0),
            review_round_count=d.get("review_round_count", 0),
            retry_limit=d.get("retry_limit", 3),
            reviewer=d.get("reviewer", {}),
            evidence=d.get("evidence", {}),
            allowed_actions=d.get("allowed_actions", []),
            created_at=d.get("created_at", ""),
        )


# ---- Gate report writer ----


def write_gate_report(gd: GateDecision, workspace: str) -> Path:
    """Write a markdown gate report. Returns the absolute Path to the report."""
    report_dir = Path(workspace) / ".story" / "context" / gd.story_key / "gates"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{gd.stage}-review-gate.md"

    findings_lines = ""
    for f in gd.evidence.get("open_findings", []):
        sev = f.get("severity", "?")
        desc = f.get("description", "")
        loc = f.get("location", "")
        findings_lines += (
            f"- [{sev.upper()}] {desc}" + (f" @ {loc}" if loc else "") + "\n"
        )
    if not findings_lines:
        findings_lines = (
            "No concrete reviewer findings were produced in this gate decision.\n"
        )

    reviewer = gd.reviewer
    reviewer_line = reviewer.get("kind", "?")
    if reviewer.get("model"):
        reviewer_line += f" / {reviewer['model']}"
    if reviewer.get("session"):
        reviewer_line += f" / {reviewer['session']}"

    actions_list = "\n".join(f"- {a}" for a in gd.allowed_actions)

    content = (
        f"# Review Gate: {gd.stage}\n\n"
        f"## Decision\n{gd.decision}\n\n"
        f"## Reason\n{gd.human_message}\n\n"
        f"## Actors\n"
        f"- Executor: {reviewer.get('adapter', 'unknown')} CLI"
        f", model {reviewer.get('model', 'unknown')}\n"
        f"- Reviewer: {reviewer_line}\n"
        f"- Gate: {gd.gate_name}\n\n"
        f"## Counts\n"
        f"- Executor attempts: {gd.executor_attempt_count}\n"
        f"- Review rounds: {gd.review_round_count}\n"
        f"- Retry limit: {gd.retry_limit}\n\n"
        f"## Evidence\n"
        f"- Done consumed: {'yes' if gd.evidence.get('done_consumed') else 'no'}\n"
        f"- Review run ID: {gd.evidence.get('review_run_id') or 'none'}\n"
        f"- Report path: {gd.evidence.get('report_path') or 'none'}\n\n"
        f"## Findings\n{findings_lines}\n"
        f"## Available Actions\n{actions_list}\n"
    )

    report_path.write_text(content, encoding="utf-8")
    return report_path


# ---- Factory helper ----


def gate_decision_from_state(
    state: dict,
    decision: str = "wait_confirm",
    reason_code: str = "review_unavailable",
    human_message: str = "",
    reviewer: dict | None = None,
) -> GateDecision:
    """Build a GateDecision from graph state fields."""
    stage = state.get("current_stage", "")
    key = state.get("story_key", "")
    exec_count = state.get("execution_count", 0)
    ctx = state.get("context", {})

    review_rounds = get_review_round_count(ctx, stage)

    return GateDecision(
        story_key=key,
        stage=stage,
        gate_name="adversarial_review",
        decision=decision,
        reason_code=reason_code,
        human_message=human_message,
        executor_attempt_count=exec_count,
        review_round_count=review_rounds,
        retry_limit=3,
        reviewer=reviewer or {},
        evidence={
            "done_consumed": bool(ctx),
        },
    )
