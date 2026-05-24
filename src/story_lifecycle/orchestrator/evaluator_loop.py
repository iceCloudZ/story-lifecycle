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
    fallback: str = "repair_packet"


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
            fallback=code_raw.get("fallback", "repair_packet"),
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
    repair_dir = Path(workspace) / ".story-context" / story_key
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
        (f.get("category", ""), f.get("location", ""))
        for f in previous_round_findings
        if f.get("severity") == "high"
    }
    if not prev_high:
        return False

    curr_high = [
        (f.get("category", ""), f.get("location", ""), f.get("description", ""))
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
