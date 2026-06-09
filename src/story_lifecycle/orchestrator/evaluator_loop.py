"""Evaluator-Optimizer adversarial loop logic.

Plan Loop: in-node while loop inside plan_stage_node.
Code Loop: cross-node iterative retry via review_stage_node -> router retry.
"""

from __future__ import annotations

import os
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger("story-lifecycle.evaluator_loop")

# Token budget approximations (1 token ~ 4 chars for English/mixed Chinese)
CHARS_PER_TOKEN = 4
TARGET_BUDGET_TOKENS = 4000
HARD_BUDGET_TOKENS = 20000
EMERGENCY_COMPACT_TOKENS = 6000


@dataclass
class LoopResult:
    decision: str  # "pass" | "fail" | "max_rounds" | "no_progress" | "wait_confirm"
    rounds: int
    final_plan: dict | None = None
    final_review: dict | None = None
    reason: str = ""
    remaining_findings: list[str] = field(default_factory=list)


@dataclass
class _SubLoopConfig:
    enabled: bool = False
    stages: list[str] = field(default_factory=list)
    max_rounds: int = 3
    reviewer_model: str = ""
    pass_condition: str = ""
    mode: str = "short_lived"


@dataclass
class AdversarialConfig:
    enabled: bool = False
    plan_loop: _SubLoopConfig = field(default_factory=_SubLoopConfig)
    code_loop: _SubLoopConfig = field(default_factory=_SubLoopConfig)

    @classmethod
    def from_profile(cls, profile: dict) -> AdversarialConfig:
        adv = profile.get("adversarial", {})
        if not adv or not adv.get("enabled"):
            return cls()

        plan_raw = adv.get("plan_loop", {})
        code_raw = adv.get("code_loop", {})

        plan_cfg = _SubLoopConfig(
            enabled=plan_raw.get("enabled", False),
            stages=plan_raw.get("stages", []),
            max_rounds=plan_raw.get("max_rounds", 3),
            reviewer_model=plan_raw.get("reviewer_model", ""),
            pass_condition=plan_raw.get("pass_condition", "no_open_blocker_or_major"),
        )

        code_cfg = _SubLoopConfig(
            enabled=code_raw.get("enabled", False),
            stages=[],
            max_rounds=code_raw.get("max_rounds", 3),
            reviewer_model=code_raw.get("reviewer_model", ""),
            pass_condition=code_raw.get("pass_condition", "no_open_blocker"),
            mode=code_raw.get("mode", "short_lived"),
        )

        return cls(enabled=True, plan_loop=plan_cfg, code_loop=code_cfg)

    def plan_loop_enabled(self, stage: str) -> bool:
        if not self.enabled or not self.plan_loop.enabled:
            return False
        stages = self.plan_loop.stages
        return stage in stages if stages else True

    def code_loop_enabled(self, stage: str) -> bool:
        if not self.enabled or not self.code_loop.enabled:
            return False
        return True

    def resolve_reviewer_model(self, loop_type: str) -> str:
        if loop_type == "plan":
            model = self.plan_loop.reviewer_model
        else:
            model = self.code_loop.reviewer_model
        if model:
            return model
        return os.environ.get("STORY_LLM_MODEL", "")


def _make_loop_id(loop_type: str, stage: str) -> str:
    ts = datetime.now().strftime("%Y%m%d")
    short = uuid.uuid4().hex[:6]
    return f"{loop_type}:{ts}-{short}"


# -- Repair Packet --


def build_repair_packet(
    *,
    story_key: str,
    stage: str,
    workspace: str,
    plan_summary: str,
    stage_output_summary: str,
    findings: list[dict],
    verification: dict,
    round_num: int,
    accepted_risks: list[str] | None = None,
    must_preserve_decisions: list[str] | None = None,
    write_file: bool = False,
) -> str | None:
    """Build repair packet content. Optionally write to disk.

    Returns the packet string, or the file path if write_file=True.
    """
    sections = []

    sections.append(f"# Repair Packet: {stage} Round {round_num}\n")
    sections.append(f"## Story\n- Key: {story_key}\n")

    sections.append(f"## 当前阶段 Plan\n{plan_summary}\n")

    sections.append(f"## 阶段产出摘要\n{stage_output_summary}\n")

    # Findings grouped by severity
    high_findings = [f for f in findings if f.get("severity") == "high"]
    medium_findings = [f for f in findings if f.get("severity") == "medium"]
    low_findings = [f for f in findings if f.get("severity") not in ("high", "medium")]

    if high_findings:
        sections.append("## 阻塞/高级别 Findings")
        for f in high_findings:
            loc = f.get("location", "")
            sections.append(
                f"- [{f.get('severity', '').upper()}] {f.get('category', '')}: "
                f"{f.get('description', '')}" + (f" @ {loc}" if loc else "")
            )
            if f.get("recommendation"):
                sections.append(f"  Required change: {f['recommendation']}")
        sections.append("")

    if medium_findings:
        sections.append("## 中级别 Findings")
        for f in medium_findings:
            loc = f.get("location", "")
            sections.append(
                f"- [{f.get('severity', '').upper()}] {f.get('category', '')}: "
                f"{f.get('description', '')}" + (f" @ {loc}" if loc else "")
            )
            if f.get("recommendation"):
                sections.append(f"  Required change: {f['recommendation']}")
        sections.append("")

    if low_findings:
        sections.append("## 低级别 Findings")
        for f in low_findings:
            sections.append(f"- {f.get('description', '')}")
        sections.append("")

    # Verification status
    ver_status = verification.get("status", "not_run")
    ver_cmds = verification.get("commands", [])
    sections.append(f"## 验证状态\nStatus: {ver_status}")
    if ver_cmds:
        sections.append(f"Commands: {', '.join(str(c) for c in ver_cmds)}")
    if ver_status == "unavailable":
        sections.append("原因: 验证基础设施不可用或不可靠")
    sections.append("")

    if accepted_risks:
        sections.append("## 已接受的风险")
        for r in accepted_risks:
            sections.append(f"- {r}")
        sections.append("")

    if must_preserve_decisions:
        sections.append("## 必须保留的决策")
        for d in must_preserve_decisions:
            sections.append(f"- {d}")
        sections.append("")

    sections.append(
        "## 指令\n"
        "- 请仅修复上述 findings 中列出的具体问题\n"
        "- 不要进行无关的重构或风格调整\n"
        "- 保持与现有代码风格一致"
    )

    packet = "\n".join(sections)

    # Trim to hard budget
    hard_chars = HARD_BUDGET_TOKENS * CHARS_PER_TOKEN
    if len(packet) > hard_chars:
        packet = _trim_packet(
            packet, findings, plan_summary, verification, round_num, story_key
        )

    if not write_file:
        return packet

    # Write to disk
    repair_dir = Path(workspace) / ".story" / "context" / story_key
    repair_dir.mkdir(parents=True, exist_ok=True)
    repair_file = repair_dir / f"repair_{stage}_round{round_num}.md"
    repair_file.write_text(packet, encoding="utf-8")
    return str(repair_file)


def _trim_packet(
    packet: str,
    findings: list[dict],
    plan_summary: str,
    verification: dict,
    round_num: int,
    story_key: str,
) -> str:
    """Trim repair packet to emergency compact budget when over hard limit."""
    emergency_chars = EMERGENCY_COMPACT_TOKENS * CHARS_PER_TOKEN

    # Priority 1: keep blocking/high findings and verification
    kept = [f"# Repair Packet (compacted) — Round {round_num}"]
    kept.append(f"Story: {story_key}")
    kept.append(f"Plan: {plan_summary[:200]}")

    high = [f for f in findings if f.get("severity") == "high"]
    if high:
        kept.append("## Blocking Findings")
        for f in high:
            kept.append(f"- {f.get('description', '')} @ {f.get('location', '')}")
            if f.get("recommendation"):
                kept.append(f"  Fix: {f['recommendation']}")

    kept.append(f"Verification: {verification.get('status', 'not_run')}")

    result = "\n".join(kept)
    if len(result) > emergency_chars:
        result = result[:emergency_chars]
    return result


# -- No-Progress Detection --


def _category_of(finding: dict) -> str:
    """Extract category from a finding or issue dict (handles both schemas)."""
    return finding.get("category", "") or finding.get("type", "")


def detect_no_progress(
    previous_round_findings: list[dict],
    current_round_findings: list[dict],
) -> bool:
    """Detect if Major/Blocker findings are semantically repeated.

    Only considers high-severity findings. Returns True if current round's
    high findings have matching category+location in previous round, indicating
    the implementer did not fix them.
    """
    prev_high = {
        (_category_of(f), f.get("location", ""))
        for f in previous_round_findings
        if f.get("severity") == "high"
    }
    if not prev_high:
        return False

    curr_high = [
        (_category_of(f), f.get("location", ""), f.get("description", ""))
        for f in current_round_findings
        if f.get("severity") == "high"
    ]
    if not curr_high:
        return False

    repeated = 0
    for cat, loc, _desc in curr_high:
        if (cat, loc) in prev_high:
            repeated += 1

    # All current high findings are repeats -> no progress
    return repeated == len(curr_high) and len(curr_high) > 0


# -- In-Node Plan Loop --


def _get_stage_config_from_state(state: dict) -> dict:
    """Resolve stage config from state's resolved profile."""
    rp = state.get("_resolved_profile")
    stage = state.get("current_stage", "")
    if rp:
        return rp.get("stages", {}).get(stage, {})
    from .nodes import get_stage_config

    return get_stage_config(state.get("profile", "minimal"), stage)


def run_plan_loop(
    state: dict,
    adv_config: AdversarialConfig,
    adapters: list[str],
) -> LoopResult:
    """In-node while loop that drives planner <-> reviewer convergence.

    Called from plan_stage_node when adversarial.plan_loop is enabled.
    Returns a LoopResult with the final decision, plan, and review.
    """
    from . import planner
    from .loop_events import log_loop_started, log_loop_round, log_loop_completed

    story_key = state.get("story_key", "")
    stage = state.get("current_stage", "")
    cfg = _get_stage_config_from_state(state)
    max_rounds = adv_config.plan_loop.max_rounds
    reviewer_model = adv_config.resolve_reviewer_model("plan")

    loop_id = _make_loop_id("plan", stage)

    # Resolve optimizer model from env for logging
    optimizer_model = os.environ.get("STORY_LLM_MODEL", "")

    log_loop_started(
        story_key=story_key,
        stage=stage,
        loop_id=loop_id,
        loop_type="plan",
        mode="in_node",
        max_rounds=max_rounds,
        optimizer_model=optimizer_model,
        reviewer_model=reviewer_model,
        attempt_id=f"{stage}:plan_loop",
    )

    result = LoopResult(decision="fail", rounds=0, reason="unknown")

    # Work on a copy so we don't pollute caller state on non-pass exits
    loop_state = dict(state)

    try:
        prev_blockers: list[dict] = []

        for round_num in range(1, max_rounds + 1):
            result.rounds = round_num

            # --- Plan ---
            try:
                plan = planner.plan_stage(loop_state, cfg, adapters)
            except Exception as exc:
                log.warning("plan_stage failed in round %d: %s", round_num, exc)
                result.decision = "fail"
                result.reason = f"planner_error:round_{round_num}:{type(exc).__name__}"
                return result

            # Planner decided to skip
            if plan.get("skip"):
                result.decision = "pass"
                result.final_plan = plan
                result.reason = "planner_skip"
                return result

            result.final_plan = plan

            # --- Review ---
            try:
                review = planner.review_plan(
                    loop_state, plan, cfg, reviewer_model=reviewer_model
                )
            except Exception as exc:
                log.warning(
                    "review_plan failed in round %d, accepting plan: %s",
                    round_num,
                    exc,
                )
                # Graceful degradation: accept the plan when reviewer fails
                result.decision = "pass"
                result.final_review = None
                result.reason = f"reviewer_error:round_{round_num}:accepted"
                return result

            result.final_review = review

            quality = review.get("quality", "revise")
            blockers = review.get("blockers", [])
            high_blockers = [b for b in blockers if b.get("severity") == "high"]

            # No-progress detection (only after round 1)
            no_progress = False
            if round_num > 1 and high_blockers:
                no_progress = detect_no_progress(prev_blockers, high_blockers)

            # Determine decision
            if quality == "pass" or not high_blockers:
                decision = "pass"
            elif no_progress:
                decision = "no_progress"
            else:
                decision = "revise"

            # Log round event
            log_loop_round(
                story_key=story_key,
                stage=stage,
                loop_id=loop_id,
                round_id=round_num,
                loop_type="plan",
                mode="in_node",
                decision=decision,
                score=float(len(high_blockers) == 0),
                findings={
                    "open_before": [
                        f"{b.get('category', '')}:{b.get('description', '')}"
                        for b in prev_blockers
                    ],
                    "new": [
                        f"{b.get('category', '')}:{b.get('description', '')}"
                        for b in high_blockers
                    ],
                    "resolved": [],
                    "repeated": [],
                },
                verification={"status": "not_run", "commands": []},
                prompt_tokens={
                    "total": 0,
                    "context": 0,
                    "feedback": 0,
                    "repeated_context": 0,
                    "estimated": True,
                },
                no_progress=no_progress,
            )

            if decision == "pass":
                result.decision = "pass"
                result.reason = "all_blockers_resolved"
                result.remaining_findings = [
                    b.get("description", "")
                    for b in blockers
                    if b.get("severity") != "high"
                ]
                return result

            if decision == "no_progress":
                result.decision = "no_progress"
                result.reason = "no_progress_on_high_blockers"
                result.remaining_findings = [
                    b.get("description", "") for b in high_blockers
                ]
                return result

            # Revise: feed blocker context back into state for next round
            blocker_summary_parts = []
            for b in high_blockers:
                blocker_summary_parts.append(
                    f"[{b.get('category', '')}] {b.get('description', '')}"
                )
            for s in review.get("suggestions", []):
                blocker_summary_parts.append(f"Suggestion: {s}")

            loop_state["review_summary"] = (
                f"Plan review round {round_num} — revise:\n"
                + "\n".join(blocker_summary_parts)
            )
            prev_blockers = list(high_blockers)

        # Exhausted all rounds
        result.decision = "max_rounds"
        result.reason = f"max_rounds_reached:{max_rounds}"
        result.remaining_findings = [b.get("description", "") for b in prev_blockers]
        return result

    finally:
        # Always log completion on exit
        log_loop_completed(
            story_key=story_key,
            stage=stage,
            loop_id=loop_id,
            loop_type="plan",
            decision=result.decision,
            rounds=result.rounds,
            reason=result.reason,
            remaining_findings=result.remaining_findings,
        )


# -- Cross-Node Code Review Loop --


def run_code_review_loop(
    state: dict,
    adv_config: AdversarialConfig,
    stage_output: dict,
) -> LoopResult:
    """Single-round code review with finding recording and repair packet.

    NOT a while loop — runs exactly ONE round of fresh reviewer per call.
    The "loop" happens through the existing graph router retry mechanism.
    """
    import json as _json
    from ..db import models as db
    from . import planner
    from .loop_events import log_loop_started, log_loop_round, log_loop_completed
    from .quality import record_finding, update_finding_status

    story_key = state.get("story_key", "")
    stage = state.get("current_stage", "")
    workspace = state.get("workspace", "")
    cfg = _get_stage_config_from_state(state)
    reviewer_model = adv_config.resolve_reviewer_model("code")
    execution_count = state.get("execution_count", 0)
    optimizer_model = os.environ.get("STORY_LLM_MODEL", "")

    loop_id = _make_loop_id("code", stage)

    log_loop_started(
        story_key=story_key,
        stage=stage,
        loop_id=loop_id,
        loop_type="code",
        mode="short_lived",
        max_rounds=adv_config.code_loop.max_rounds,
        optimizer_model=optimizer_model,
        reviewer_model=reviewer_model,
        attempt_id=f"{stage}:code_review",
    )

    # --- Review ---
    try:
        review = planner.review_stage(
            state, cfg, stage_output, reviewer_model=reviewer_model
        )
    except Exception as exc:
        log.warning("review_stage failed: %s", exc)
        # Read previous findings for classification even on error (scoped to stage)
        prev_high_err: list[dict] = []
        try:
            all_high_err = db.get_open_findings(story_key, min_severity="high")
            prev_high_err = [f for f in all_high_err if f.get("stage") == stage]
        except Exception:
            pass
        log_loop_round(
            story_key=story_key,
            stage=stage,
            loop_id=loop_id,
            round_id=1,
            loop_type="code",
            mode="short_lived",
            decision="fail",
            score=0.0,
            findings={
                "open_before": [
                    f"{f.get('category', '')}:{f.get('description', '')}"
                    for f in prev_high_err
                ],
                "new": [],
                "resolved": [],
                "repeated": [],
            },
            verification={"status": "not_run", "commands": []},
            prompt_tokens={
                "total": 0,
                "context": 0,
                "feedback": 0,
                "repeated_context": 0,
                "estimated": True,
            },
        )
        log_loop_completed(
            story_key=story_key,
            stage=stage,
            loop_id=loop_id,
            loop_type="code",
            decision="fail",
            rounds=1,
            reason=f"reviewer_error:{type(exc).__name__}",
            remaining_findings=[],
        )
        return LoopResult(
            decision="fail", rounds=1, reason=f"reviewer_error:{type(exc).__name__}"
        )

    quality = review.get("quality", "revise")
    issues = review.get("issues", [])
    score = review.get("trajectory_score", 0.0)

    # --- Read previous open findings for round-level classification ---
    # Note: "resolved" means "reviewer didn't re-raise this round", not DB finding
    # lifecycle status. This is round-level tracking for no-progress detection only.
    # Scoped to current stage to avoid cross-stage false no_progress.
    prev_high_findings: list[dict] = []
    try:
        all_high = db.get_open_findings(story_key, min_severity="high")
        prev_high_findings = [f for f in all_high if f.get("stage") == stage]
    except Exception:
        pass

    # --- Record findings to DB ---
    finding_descriptions: list[str] = []
    current_high_issues: list[dict] = []
    for issue in issues:
        try:
            record_finding(
                story_key,
                stage,
                {
                    "source": "code_review",
                    "severity": issue.get("severity", "medium"),
                    "category": issue.get("type", issue.get("category", "unknown")),
                    "description": issue.get("description", ""),
                    "location": issue.get("location", ""),
                    "recommendation": issue.get("recommendation", ""),
                },
            )
            finding_descriptions.append(
                f"{issue.get('severity', 'medium')}:{issue.get('description', '')}"
            )
            if issue.get("severity") == "high":
                current_high_issues.append(issue)
        except Exception:
            log.warning(
                "Failed to record finding for issue: %s", issue.get("description", "")
            )

    # --- Classify findings: new / resolved / repeated ---
    prev_high_set = {
        (_category_of(f), f.get("location", "")) for f in prev_high_findings
    }
    curr_high_set = {
        (_category_of(issue), issue.get("location", ""))
        for issue in current_high_issues
    }
    repeated_keys = prev_high_set & curr_high_set
    new_keys = curr_high_set - prev_high_set
    resolved_keys = prev_high_set - curr_high_set

    findings_classified = {
        "open_before": [
            f"{f.get('category', '')}:{f.get('description', '')}"
            for f in prev_high_findings
        ],
        "new": [f"{cat}:{loc}" for cat, loc in new_keys],
        "resolved": [f"{cat}:{loc}" for cat, loc in resolved_keys],
        "repeated": [f"{cat}:{loc}" for cat, loc in repeated_keys],
    }

    # --- Sync resolved findings to DB ---
    if resolved_keys:
        for f in prev_high_findings:
            key = (_category_of(f), f.get("location", ""))
            if key in resolved_keys:
                try:
                    update_finding_status(
                        story_key,
                        f["id"],
                        "verified",
                        reason="resolved in adversarial code loop",
                    )
                except Exception:
                    log.warning(
                        "Failed to verify resolved finding %s for %s",
                        f.get("id", "?"),
                        story_key,
                    )

    # --- No-progress detection ---
    no_progress = False
    if execution_count > 0 and current_high_issues:
        no_progress = detect_no_progress(prev_high_findings, current_high_issues)

    # --- Build repair packet if revise ---
    repair_path: str | None = None
    if quality == "revise":
        round_num = execution_count + 1
        try:
            repair_path = build_repair_packet(
                story_key=story_key,
                stage=stage,
                workspace=workspace,
                plan_summary=state.get("review_summary", ""),
                stage_output_summary=_json.dumps(stage_output, ensure_ascii=False)[
                    :500
                ],
                findings=[
                    {
                        "severity": i.get("severity", "medium"),
                        "category": i.get("type", i.get("category", "unknown")),
                        "description": i.get("description", ""),
                        "location": i.get("location", ""),
                        "recommendation": i.get("recommendation", ""),
                    }
                    for i in issues
                ],
                verification={"status": "not_run", "commands": []},
                round_num=round_num,
                write_file=True,
            )
        except Exception as exc:
            log.warning("repair packet write failed: %s", exc)
            log_loop_round(
                story_key=story_key,
                stage=stage,
                loop_id=loop_id,
                round_id=execution_count + 1,
                loop_type="code",
                mode="short_lived",
                decision="fail",
                score=float(score),
                findings=findings_classified,
                verification={"status": "not_run", "commands": []},
                prompt_tokens={
                    "total": max(1, len(_json.dumps(stage_output)) // CHARS_PER_TOKEN),
                    "context": max(
                        1, len(_json.dumps(stage_output)) // CHARS_PER_TOKEN
                    ),
                    "feedback": 0,
                    "repeated_context": 0,
                    "estimated": True,
                },
            )
            log_loop_completed(
                story_key=story_key,
                stage=stage,
                loop_id=loop_id,
                loop_type="code",
                decision="fail",
                rounds=1,
                reason=f"repair_packet_error:{type(exc).__name__}",
                remaining_findings=finding_descriptions,
            )
            return LoopResult(
                decision="fail",
                rounds=1,
                final_review=review,
                reason=f"repair_packet_error:{type(exc).__name__}",
                remaining_findings=finding_descriptions,
            )

    # --- Estimate prompt tokens (design schema: total/context/feedback/repeated_context/estimated) ---
    ctx_chars = len(_json.dumps(stage_output))
    prompt_tokens = {
        "total": max(1, ctx_chars // CHARS_PER_TOKEN),
        "context": max(1, ctx_chars // CHARS_PER_TOKEN),
        "feedback": 0,
        "repeated_context": 0,
        "estimated": True,
    }

    # --- Log round event ---
    log_loop_round(
        story_key=story_key,
        stage=stage,
        loop_id=loop_id,
        round_id=execution_count + 1,
        loop_type="code",
        mode="short_lived",
        decision=quality,
        score=float(score),
        findings=findings_classified,
        verification={"status": "not_run", "commands": []},
        prompt_tokens=prompt_tokens,
        no_progress=no_progress,
    )

    # --- Decision ---
    if no_progress:
        log_loop_completed(
            story_key=story_key,
            stage=stage,
            loop_id=loop_id,
            loop_type="code",
            decision="wait_confirm",
            rounds=1,
            reason="no_progress_on_high_findings",
            remaining_findings=finding_descriptions,
        )
        return LoopResult(
            decision="wait_confirm",
            rounds=1,
            final_review=review,
            reason="no_progress_on_high_findings",
            remaining_findings=finding_descriptions,
        )

    if quality == "pass":
        log_loop_completed(
            story_key=story_key,
            stage=stage,
            loop_id=loop_id,
            loop_type="code",
            decision="pass",
            rounds=1,
            reason="code_review_passed",
            remaining_findings=[],
        )
        return LoopResult(
            decision="pass", rounds=1, final_review=review, reason="code_review_passed"
        )

    # revise or fail
    if repair_path:
        review["repair_packet_path"] = repair_path
    log_loop_completed(
        story_key=story_key,
        stage=stage,
        loop_id=loop_id,
        loop_type="code",
        decision=quality,
        rounds=1,
        reason=f"code_review_{quality}",
        remaining_findings=finding_descriptions,
    )
    return LoopResult(
        decision=quality, rounds=1, final_review=review, reason=f"code_review_{quality}"
    )
