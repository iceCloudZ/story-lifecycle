"""Stage Library — all valid atomic stage definitions.

The Stage Library defines the universe of legal stages that can
appear in a story's execution graph. Each stage is an atomic unit
with defined inputs, outputs, and properties.

Design doc: idea-orchestrator-agent.md §Stage Library
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StageCategory(str, Enum):
    """Category of a stage."""

    PLANNING = "planning"
    EXECUTION = "execution"
    REVIEW = "review"
    VALIDATION = "validation"
    DEPLOYMENT = "deployment"
    HUMAN = "human"


class StageRisk(str, Enum):
    """Risk level of modifying a stage in the graph."""

    LOW = "low"  # Read-only or reversible
    MEDIUM = "medium"  # Changes state but recoverable
    HIGH = "high"  # Irreversible or production-impacting
    CRITICAL = "critical"  # Directly affects production systems


@dataclass
class StageDefinition:
    """Definition of a single atomic stage.

    Attributes:
        name: Unique stage name (e.g. "plan", "implement", "architecture_review").
        category: Stage category.
        description: Human-readable description.
        required_inputs: List of input artifact names required.
        expected_outputs: List of output artifact names produced.
        risk: Risk level of this stage.
        max_retries: Default maximum retries.
        timeout_minutes: Default timeout.
        requires_human: Whether this stage requires human interaction.
        allowed_modifiers: Who can modify this stage in the graph.
        properties: Additional stage-specific properties.
    """

    name: str
    category: StageCategory
    description: str
    required_inputs: list[str] = field(default_factory=list)
    expected_outputs: list[str] = field(default_factory=list)
    risk: StageRisk = StageRisk.MEDIUM
    max_retries: int = 3
    timeout_minutes: int = 30
    requires_human: bool = False
    allowed_modifiers: list[str] = field(default_factory=lambda: ["router", "human"])
    properties: dict[str, Any] = field(default_factory=dict)


# ── built-in stage definitions ──

BUILTIN_STAGES: dict[str, StageDefinition] = {
    "plan": StageDefinition(
        name="plan",
        category=StageCategory.PLANNING,
        description="Generate execution plan for the stage",
        required_inputs=["prd", "story_context"],
        expected_outputs=["plan_summary", "extra_instructions", "adapter_choice"],
        risk=StageRisk.LOW,
        max_retries=3,
        timeout_minutes=15,
    ),
    "plan_review": StageDefinition(
        name="plan_review",
        category=StageCategory.REVIEW,
        description="Adversarial review of execution plan quality",
        required_inputs=["plan_summary", "extra_instructions"],
        expected_outputs=["review_verdict", "blockers", "suggestions"],
        risk=StageRisk.LOW,
        max_retries=2,
        timeout_minutes=10,
    ),
    "implement": StageDefinition(
        name="implement",
        category=StageCategory.EXECUTION,
        description="Execute the implementation using AI CLI",
        required_inputs=["plan_summary", "extra_instructions"],
        expected_outputs=["implementation_artifacts"],
        risk=StageRisk.MEDIUM,
        max_retries=3,
        timeout_minutes=30,
    ),
    "review": StageDefinition(
        name="review",
        category=StageCategory.REVIEW,
        description="Quality review of stage output",
        required_inputs=["implementation_artifacts"],
        expected_outputs=["review_summary", "quality_verdict", "findings"],
        risk=StageRisk.LOW,
        max_retries=3,
        timeout_minutes=15,
    ),
    "test": StageDefinition(
        name="test",
        category=StageCategory.VALIDATION,
        description="Run tests and validate output",
        required_inputs=["implementation_artifacts"],
        expected_outputs=["test_results", "verification_evidence"],
        risk=StageRisk.LOW,
        max_retries=2,
        timeout_minutes=20,
    ),
    "architecture_review": StageDefinition(
        name="architecture_review",
        category=StageCategory.REVIEW,
        description="Cross-cutting architecture review before production",
        required_inputs=["implementation_artifacts", "plan_summary"],
        expected_outputs=["arch_verdict", "arch_findings"],
        risk=StageRisk.HIGH,
        max_retries=1,
        timeout_minutes=20,
        requires_human=True,
    ),
    "final_review": StageDefinition(
        name="final_review",
        category=StageCategory.REVIEW,
        description="Final quality gate before completion",
        required_inputs=["test_results", "review_summary"],
        expected_outputs=["final_verdict"],
        risk=StageRisk.MEDIUM,
        max_retries=1,
        timeout_minutes=10,
    ),
    "deploy": StageDefinition(
        name="deploy",
        category=StageCategory.DEPLOYMENT,
        description="Deploy to target environment",
        required_inputs=["final_verdict", "implementation_artifacts"],
        expected_outputs=["deployment_result"],
        risk=StageRisk.CRITICAL,
        max_retries=1,
        timeout_minutes=30,
        requires_human=True,
    ),
    "human_review": StageDefinition(
        name="human_review",
        category=StageCategory.HUMAN,
        description="Pause for human review and decision",
        required_inputs=["current_state"],
        expected_outputs=["human_decision"],
        risk=StageRisk.LOW,
        max_retries=0,
        timeout_minutes=1440,  # 24 hours
        requires_human=True,
    ),
}


def get_stage_definition(name: str) -> StageDefinition | None:
    """Look up a stage definition by name.

    Args:
        name: Stage name to look up.

    Returns:
        StageDefinition if found, None otherwise.
    """
    return BUILTIN_STAGES.get(name)


def list_stages(category: StageCategory | None = None) -> list[StageDefinition]:
    """List all stage definitions, optionally filtered by category.

    Args:
        category: If set, only return stages of this category.

    Returns:
        List of matching StageDefinition objects.
    """
    stages = list(BUILTIN_STAGES.values())
    if category:
        stages = [s for s in stages if s.category == category]
    return stages


def is_valid_stage(name: str) -> bool:
    """Check if a stage name is a valid built-in stage.

    Args:
        name: Stage name to validate.

    Returns:
        True if the stage is defined in the library.
    """
    return name in BUILTIN_STAGES


def validate_stage_inputs(name: str, available_inputs: list[str]) -> list[str]:
    """Validate that all required inputs for a stage are available.

    Args:
        name: Stage name to validate.
        available_inputs: List of available input artifact names.

    Returns:
        List of missing required inputs (empty if all satisfied).
    """
    stage = get_stage_definition(name)
    if stage is None:
        return [f"unknown stage: {name}"]
    return [inp for inp in stage.required_inputs if inp not in available_inputs]
