"""P5 Meta-Planner — Story-level strategy and decomposition.

The Meta-Planner runs at Story START to establish:
1. StrategyEnvelope — mode, budget, thresholds, fallback_plan
2. Scope classification — S/M/L/Epic with decomposition decision
3. Task Packets — per-task context sharding for sub-stories

Design doc: idea-orchestrator-agent.md §Meta-Planner
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from ..db import models as db

# ── data structures ──


class StoryScope(str, Enum):
    """Story complexity classification."""

    SIMPLE = "S"  # Single file, <50 lines change
    MEDIUM = "M"  # 2-4 files, clear scope
    LARGE = "L"  # 5+ files or cross-module
    EPIC = "E"  # Multi-system, needs decomposition


class ExecutionMode(str, Enum):
    """How the story should be executed."""

    SIMPLE_PATH = "simple"  # Fast path: plan → implement → test
    STANDARD = "standard"  # Normal: plan → review → implement → review → test
    STRICT = "strict"  # Extra gates: plan → plan_review → implement → review → test → final_review
    DECOMPOSED = "decomposed"  # Split into sub-stories


@dataclass
class RouterThresholds:
    """Thresholds that trigger Strategic Router activation."""

    max_retries: int = 3
    min_trajectory_score: float = 0.3
    max_review_rounds: int = 3
    provider_failure_rate: float = 0.5
    budget_burn_percent: float = 0.5


@dataclass
class StrategyEnvelope:
    """Story-level execution strategy, generated at Story START."""

    strategy_id: str
    story_key: str
    scope: StoryScope
    mode: ExecutionMode
    budget_minutes: int = 30
    budget_llm_calls: int = 50
    budget_retries: int = 3
    router_thresholds: RouterThresholds = field(default_factory=RouterThresholds)
    fallback_plan: str = ""  # What to do if strategy fails
    decomposition: list[dict[str, Any]] = field(
        default_factory=list
    )  # sub-task definitions
    created_at: str = ""

    # Scope signals (what informed the classification)
    signals: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskPacket:
    """Per-task context shard for a sub-story.

    Each sub-story gets a focused context packet instead of
    the full parent story context.
    """

    task_id: str
    parent_story_key: str
    key_suffix: str
    title: str
    summary: str
    scope_files: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    context_shard: dict[str, Any] = field(default_factory=dict)
    quality_checklist: list[str] = field(default_factory=list)


# ── scope classification ──


def classify_scope(
    title: str = "",
    description: str = "",
    acceptance_criteria: list[str] | None = None,
    affected_modules: list[str] | None = None,
    prd_lines: int = 0,
) -> tuple[StoryScope, dict[str, Any]]:
    """Classify story scope from available signals.

    Uses simple heuristic rules. Future: LLM-assisted classification.

    Args:
        title: Story title.
        description: Story description.
        acceptance_criteria: List of acceptance criteria.
        affected_modules: List of affected module names.
        prd_lines: Lines in the PRD document.

    Returns:
        Tuple of (StoryScope, signals_dict).
    """
    signals: dict[str, Any] = {
        "title_length": len(title),
        "description_length": len(description),
        "criteria_count": len(acceptance_criteria or []),
        "module_count": len(affected_modules or []),
        "prd_lines": prd_lines,
    }

    criteria_count = len(acceptance_criteria or [])
    module_count = len(affected_modules or [])

    # Scoring heuristic
    score = 0
    if criteria_count >= 8:
        score += 3
    elif criteria_count >= 4:
        score += 2
    elif criteria_count >= 2:
        score += 1

    if module_count >= 5:
        score += 3
    elif module_count >= 3:
        score += 2
    elif module_count >= 1:
        score += 1

    if prd_lines >= 100:
        score += 2
    elif prd_lines >= 30:
        score += 1

    # Keywords suggesting complexity
    complex_keywords = [
        "重构",
        "迁移",
        "架构",
        "refactor",
        "migrate",
        "architect",
        "epic",
        "子系统",
    ]
    title_lower = title.lower()
    desc_lower = description.lower()
    for kw in complex_keywords:
        if kw in title_lower or kw in desc_lower:
            score += 2
            break

    signals["complexity_score"] = score

    if score >= 7:
        scope = StoryScope.EPIC
    elif score >= 4:
        scope = StoryScope.LARGE
    elif score >= 2:
        scope = StoryScope.MEDIUM
    else:
        scope = StoryScope.SIMPLE

    return scope, signals


# ── execution mode selection ──


def select_mode(
    scope: StoryScope,
    profile_mode: str = "",
    strict_profile: bool = False,
) -> ExecutionMode:
    """Select execution mode based on scope and profile settings.

    Args:
        scope: Story scope classification.
        profile_mode: Profile-specified mode ("simple", "standard", "strict").
        strict_profile: Whether the profile enforces strict mode.

    Returns:
        Selected ExecutionMode.
    """
    # Profile override takes precedence
    if profile_mode == "simple" and scope in (StoryScope.SIMPLE, StoryScope.MEDIUM):
        return ExecutionMode.SIMPLE_PATH
    if strict_profile or profile_mode == "strict":
        return ExecutionMode.STRICT

    # Default mode selection based on scope
    mode_map = {
        StoryScope.SIMPLE: ExecutionMode.SIMPLE_PATH,
        StoryScope.MEDIUM: ExecutionMode.STANDARD,
        StoryScope.LARGE: ExecutionMode.STANDARD,
        StoryScope.EPIC: ExecutionMode.DECOMPOSED,
    }
    return mode_map.get(scope, ExecutionMode.STANDARD)


# ── strategy generation ──

STRATEGY_DIR = Path.home() / ".story-lifecycle" / "strategies"


def generate_strategy(
    story_key: str,
    title: str = "",
    description: str = "",
    acceptance_criteria: list[str] | None = None,
    affected_modules: list[str] | None = None,
    prd_lines: int = 0,
    profile_mode: str = "",
    strict_profile: bool = False,
) -> StrategyEnvelope:
    """Generate a StrategyEnvelope for a story.

    This is called at Story START to establish the execution strategy.

    Args:
        story_key: Story identifier.
        title: Story title.
        description: Story description.
        acceptance_criteria: List of acceptance criteria.
        affected_modules: List of affected modules.
        prd_lines: Lines in PRD document.
        profile_mode: Profile-specified execution mode.
        strict_profile: Whether profile enforces strict mode.

    Returns:
        A StrategyEnvelope with scope, mode, budget, and thresholds.
    """
    scope, signals = classify_scope(
        title=title,
        description=description,
        acceptance_criteria=acceptance_criteria,
        affected_modules=affected_modules,
        prd_lines=prd_lines,
    )

    mode = select_mode(scope, profile_mode, strict_profile)

    # Budget allocation based on scope
    budget_map = {
        StoryScope.SIMPLE: (15, 20, 2),
        StoryScope.MEDIUM: (30, 50, 3),
        StoryScope.LARGE: (60, 80, 4),
        StoryScope.EPIC: (120, 150, 5),
    }
    budget_minutes, budget_llm_calls, budget_retries = budget_map.get(
        scope, (30, 50, 3)
    )

    # Thresholds based on scope
    thresholds = RouterThresholds(
        max_retries=budget_retries,
        min_trajectory_score=0.3
        if scope in (StoryScope.SIMPLE, StoryScope.MEDIUM)
        else 0.4,
        max_review_rounds=3 if scope != StoryScope.EPIC else 5,
    )

    # Fallback plan
    fallback_map = {
        StoryScope.SIMPLE: "skip and mark for human review",
        StoryScope.MEDIUM: "retry with different provider, then fail to human",
        StoryScope.LARGE: "decompose into sub-stories, then fail to human",
        StoryScope.EPIC: "decompose and redistribute budget, then pause for planning",
    }
    fallback_plan = fallback_map.get(scope, "fail to human")

    envelope = StrategyEnvelope(
        strategy_id=uuid.uuid4().hex[:12],
        story_key=story_key,
        scope=scope,
        mode=mode,
        budget_minutes=budget_minutes,
        budget_llm_calls=budget_llm_calls,
        budget_retries=budget_retries,
        router_thresholds=thresholds,
        fallback_plan=fallback_plan,
        signals=signals,
        created_at=datetime.now().isoformat(),
    )

    # Persist
    _save_strategy(envelope)

    # Log event
    db.log_event(
        story_key,
        "",
        "strategy_envelope",
        {
            "strategy_id": envelope.strategy_id,
            "scope": scope.value,
            "mode": mode.value,
            "budget_minutes": budget_minutes,
            "budget_llm_calls": budget_llm_calls,
            "fallback_plan": fallback_plan,
            "signals": signals,
        },
    )

    return envelope


def _save_strategy(envelope: StrategyEnvelope) -> None:
    """Persist strategy envelope to disk."""
    STRATEGY_DIR.mkdir(parents=True, exist_ok=True)
    strategy_file = STRATEGY_DIR / f"{envelope.story_key}.json"

    data = {
        "strategy_id": envelope.strategy_id,
        "story_key": envelope.story_key,
        "scope": envelope.scope.value,
        "mode": envelope.mode.value,
        "budget_minutes": envelope.budget_minutes,
        "budget_llm_calls": envelope.budget_llm_calls,
        "budget_retries": envelope.budget_retries,
        "router_thresholds": {
            "max_retries": envelope.router_thresholds.max_retries,
            "min_trajectory_score": envelope.router_thresholds.min_trajectory_score,
            "max_review_rounds": envelope.router_thresholds.max_review_rounds,
            "provider_failure_rate": envelope.router_thresholds.provider_failure_rate,
            "budget_burn_percent": envelope.router_thresholds.budget_burn_percent,
        },
        "fallback_plan": envelope.fallback_plan,
        "decomposition": envelope.decomposition,
        "created_at": envelope.created_at,
        "signals": envelope.signals,
    }
    strategy_file.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_strategy(story_key: str) -> StrategyEnvelope | None:
    """Load a strategy envelope by story key."""
    strategy_file = STRATEGY_DIR / f"{story_key}.json"
    if not strategy_file.exists():
        return None

    try:
        data = json.loads(strategy_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    thresholds_data = data.get("router_thresholds", {})
    return StrategyEnvelope(
        strategy_id=data["strategy_id"],
        story_key=data["story_key"],
        scope=StoryScope(data["scope"]),
        mode=ExecutionMode(data["mode"]),
        budget_minutes=data.get("budget_minutes", 30),
        budget_llm_calls=data.get("budget_llm_calls", 50),
        budget_retries=data.get("budget_retries", 3),
        router_thresholds=RouterThresholds(**thresholds_data),
        fallback_plan=data.get("fallback_plan", ""),
        decomposition=data.get("decomposition", []),
        created_at=data.get("created_at", ""),
        signals=data.get("signals", {}),
    )


# ── decomposition gate ──


def should_decompose(envelope: StrategyEnvelope) -> bool:
    """Decide whether a story should be decomposed into sub-stories.

    A story should be decomposed if:
    - Scope is EPIC (always decompose)
    - Scope is LARGE and mode is DECOMPOSED
    - Explicit decomposition was requested

    Args:
        envelope: The story's strategy envelope.

    Returns:
        True if decomposition is recommended.
    """
    return (
        envelope.scope == StoryScope.EPIC or envelope.mode == ExecutionMode.DECOMPOSED
    )


def generate_task_packets(
    envelope: StrategyEnvelope,
    subtasks: list[dict[str, Any]],
) -> list[TaskPacket]:
    """Generate Task Packets for sub-story decomposition.

    Each subtask gets a focused context shard derived from the
    parent strategy, with quality checklist from the parent's
    acceptance criteria.

    Args:
        envelope: The parent story's strategy envelope.
        subtasks: List of subtask dicts with key_suffix, title, summary, depends_on.

    Returns:
        List of TaskPacket, one per subtask.
    """
    packets: list[TaskPacket] = []
    total_subtasks = len(subtasks)
    if total_subtasks == 0:
        return packets

    # Distribute budget across subtasks
    per_task_minutes = envelope.budget_minutes // max(total_subtasks, 1)
    per_task_llm_calls = envelope.budget_llm_calls // max(total_subtasks, 1)

    for i, subtask in enumerate(subtasks):
        key_suffix = subtask.get("key_suffix", f"sub-{i}")
        depends_on = subtask.get("depends_on", [])

        packet = TaskPacket(
            task_id=uuid.uuid4().hex[:8],
            parent_story_key=envelope.story_key,
            key_suffix=key_suffix,
            title=subtask.get("title", ""),
            summary=subtask.get("summary", ""),
            scope_files=subtask.get("scope_files", []),
            depends_on=depends_on if isinstance(depends_on, list) else [],
            context_shard={
                "parent_scope": envelope.scope.value,
                "parent_mode": envelope.mode.value,
                "budget_minutes": per_task_minutes,
                "budget_llm_calls": per_task_llm_calls,
                "task_index": i,
                "total_tasks": total_subtasks,
            },
            quality_checklist=subtask.get("quality_checklist", []),
        )
        packets.append(packet)

    # Update the envelope with decomposition info
    envelope.decomposition = [
        {
            "task_id": p.task_id,
            "key_suffix": p.key_suffix,
            "title": p.title,
            "depends_on": p.depends_on,
        }
        for p in packets
    ]
    _save_strategy(envelope)

    # Log decomposition
    db.log_event(
        envelope.story_key,
        "",
        "decomposition",
        {
            "strategy_id": envelope.strategy_id,
            "total_subtasks": total_subtasks,
            "subtasks": envelope.decomposition,
        },
    )

    return packets


# ── query helpers ──


def list_strategies(limit: int = 50) -> list[dict]:
    """List all strategy envelopes, most recent first."""
    STRATEGY_DIR.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    files = sorted(
        STRATEGY_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True
    )
    for f in files:
        if len(results) >= limit:
            break
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            results.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    return results
