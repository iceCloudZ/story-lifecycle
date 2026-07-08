"""Repair-packet builder for the verify-gate retry path.

The adversarial-loop scaffolding (LoopResult, AdversarialConfig, _make_loop_id,
detect_no_progress, _get_stage_config_from_state) was LangGraph-era code; ISS-008
removed these dead parts. In FC mode the LLM drives its own retries (planner
re-inserts a launch action) and gate.run_verify_gate consumes build_repair_packet
directly.
"""

from __future__ import annotations

import logging

from ...infra.story_paths import safe_story_path

log = logging.getLogger("story-lifecycle.evaluator_loop")

# Token budget approximations (1 token ~ 4 chars for English/mixed Chinese)
CHARS_PER_TOKEN = 4
TARGET_BUDGET_TOKENS = 4000
HARD_BUDGET_TOKENS = 20000
EMERGENCY_COMPACT_TOKENS = 6000


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
    repair_dir = safe_story_path(workspace, ".story", "context", story_key)
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
