"""Working Memory — persistent context that accumulates across stages.

Persists to `.story/context/{story_key}/working_memory.json`.
Read at plan_stage start, structured update at review_stage end.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


@dataclass
class WorkingMemory:
    """Accumulated context that survives across stage transitions."""

    confirmed_facts: list[str] = field(default_factory=list)
    open_risks: list[str] = field(default_factory=list)
    discarded_paths: list[str] = field(default_factory=list)
    latest_findings: list[dict[str, Any]] = field(default_factory=list)
    budget_status: dict[str, Any] = field(default_factory=dict)


def _wm_path(workspace: str, story_key: str) -> Path:
    """Return working memory file path."""
    return Path(workspace) / ".story" / "context" / story_key / "working_memory.json"


def load_working_memory(workspace: str, story_key: str) -> WorkingMemory:
    """Load working memory from disk, or return empty if not found."""
    path = _wm_path(workspace, story_key)
    if not path.exists():
        return WorkingMemory()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return WorkingMemory(
            confirmed_facts=data.get("confirmed_facts", []),
            open_risks=data.get("open_risks", []),
            discarded_paths=data.get("discarded_paths", []),
            latest_findings=data.get("latest_findings", []),
            budget_status=data.get("budget_status", {}),
        )
    except (json.JSONDecodeError, OSError):
        return WorkingMemory()


def save_working_memory(workspace: str, story_key: str, wm: WorkingMemory) -> None:
    """Persist working memory to disk."""
    path = _wm_path(workspace, story_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(wm), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def update_working_memory_from_review(
    wm: WorkingMemory,
    review_summary: str | None = None,
    findings: list[dict[str, Any]] | None = None,
    risks: list[str] | None = None,
    budget_status: dict[str, Any] | None = None,
) -> WorkingMemory:
    """Update working memory with structured review outputs."""
    if findings:
        wm.latest_findings = findings
    if risks:
        wm.open_risks = risks
    if budget_status:
        wm.budget_status = budget_status
    if review_summary:
        # Extract confirmed facts from review (simple heuristic)
        wm.confirmed_facts.append(review_summary)
        # Keep only last 10 facts
        wm.confirmed_facts = wm.confirmed_facts[-10:]
    return wm


def format_working_memory_for_prompt(wm: WorkingMemory) -> str:
    """Format working memory as a prompt section for injection."""
    if not wm.confirmed_facts and not wm.open_risks and not wm.discarded_paths:
        return ""

    lines = ["## Working Memory（跨阶段持久上下文）"]

    if wm.confirmed_facts:
        lines.append("\n### 已确认事实")
        for fact in wm.confirmed_facts:
            lines.append(f"- {fact}")

    if wm.open_risks:
        lines.append("\n### 未关闭风险")
        for risk in wm.open_risks:
            lines.append(f"- ⚠️ {risk}")

    if wm.discarded_paths:
        lines.append("\n### 已放弃方案")
        for path in wm.discarded_paths:
            lines.append(f"- ✗ {path}")

    return "\n".join(lines)
