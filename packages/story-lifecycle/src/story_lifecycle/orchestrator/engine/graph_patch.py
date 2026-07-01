"""Graph Patch Registry — runtime modifications to the Stage Graph.

A Graph Patch represents a single runtime deviation from the default
profile-defined path. All patches must pass policy validation before
execution.

Patch types:
- insert_stage: Add a stage into the execution path
- repeat_stage: Re-execute a stage (loop)
- skip_stage: Skip a planned stage
- split_sub_story: Decompose into sub-stories
- switch_model: Change the LLM model mid-execution
- pause_for_human: Insert a human review point

Design doc: idea-orchestrator-agent.md §Graph Patch Registry
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from ...infra.db import models as db
from .stage_graph import StageGraph, build_default_graph

# ── data structures ──


class PatchType(str, Enum):
    """Types of graph patches."""

    INSERT_STAGE = "insert_stage"
    REPEAT_STAGE = "repeat_stage"
    SKIP_STAGE = "skip_stage"
    SPLIT_SUB_STORY = "split_sub_story"
    SWITCH_MODEL = "switch_model"
    PAUSE_FOR_HUMAN = "pause_for_human"


class PatchStatus(str, Enum):
    """Status of a graph patch."""

    PROPOSED = "proposed"
    VALIDATED = "validated"
    APPLIED = "applied"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"


@dataclass
class GraphPatch:
    """A runtime modification to the Stage Graph.

    Attributes:
        patch_id: Unique identifier.
        story_key: Story this patch applies to.
        patch_type: Type of modification.
        target_stage: The stage being modified.
        insert_after: If insert_stage, the stage to insert after.
        new_stage: If insert_stage, the new stage name.
        model_name: If switch_model, the new model name.
        reason: Why this patch is needed.
        risk_assessment: Risk level of this patch.
        status: Current patch status.
        validated_by: Who/what validated the patch.
        applied_at: When the patch was applied.
        rollback_reason: Why the patch was rolled back (if applicable).
        metadata: Additional patch-specific data.
        created_at: When the patch was created.
    """

    patch_id: str
    story_key: str
    patch_type: PatchType
    target_stage: str
    insert_after: str = ""
    new_stage: str = ""
    model_name: str = ""
    reason: str = ""
    risk_assessment: str = ""
    status: PatchStatus = PatchStatus.PROPOSED
    validated_by: str = ""
    applied_at: str = ""
    rollback_reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""


# ── patch risk assessment ──


def assess_patch_risk(patch: GraphPatch, graph: StageGraph | None = None) -> str:
    """Assess the risk level of a graph patch.

    Args:
        patch: The patch to assess.
        graph: The stage graph to validate against.

    Returns:
        Risk level: "low", "medium", "high", or "critical".
    """
    if graph is None:
        graph = build_default_graph()

    # Critical: modifying production stages
    if patch.patch_type == PatchType.INSERT_STAGE and patch.new_stage in (
        "deploy",
        "architecture_review",
    ):
        return "critical"

    # High: repeating stages (loops) and sub-story splits
    if patch.patch_type in (PatchType.REPEAT_STAGE, PatchType.SPLIT_SUB_STORY):
        return "high"

    # Medium: skipping stages, inserting non-critical stages
    if patch.patch_type in (PatchType.SKIP_STAGE, PatchType.INSERT_STAGE):
        return "medium"

    # Low: model switch, human pause
    if patch.patch_type in (PatchType.SWITCH_MODEL, PatchType.PAUSE_FOR_HUMAN):
        return "low"

    return "medium"


# ── patch validation ──


def validate_patch(patch: GraphPatch, graph: StageGraph | None = None) -> list[str]:
    """Validate a graph patch against the stage graph and policy rules.

    Args:
        patch: The patch to validate.
        graph: The stage graph to validate against.

    Returns:
        List of validation errors (empty if valid).
    """
    if graph is None:
        graph = build_default_graph()

    errors: list[str] = []

    # Validate patch type
    if not isinstance(patch.patch_type, PatchType):
        errors.append(f"invalid patch type: {patch.patch_type}")

    # Type-specific validation
    if patch.patch_type == PatchType.INSERT_STAGE:
        if not patch.new_stage:
            errors.append("insert_stage requires new_stage")
        if not patch.insert_after:
            errors.append("insert_stage requires insert_after")
        # Check if the insertion point exists
        if patch.insert_after and not graph.get_successors(patch.insert_after):
            errors.append(
                f"insert_after stage '{patch.insert_after}' has no successors in graph"
            )

    elif patch.patch_type == PatchType.SKIP_STAGE:
        if not patch.target_stage:
            errors.append("skip_stage requires target_stage")

    elif patch.patch_type == PatchType.REPEAT_STAGE:
        if not patch.target_stage:
            errors.append("repeat_stage requires target_stage")

    elif patch.patch_type == PatchType.SWITCH_MODEL:
        if not patch.model_name:
            errors.append("switch_model requires model_name")

    elif patch.patch_type == PatchType.SPLIT_SUB_STORY:
        if not patch.metadata.get("subtasks"):
            errors.append("split_sub_story requires subtasks in metadata")

    # Assess risk
    risk = assess_patch_risk(patch, graph)
    patch.risk_assessment = risk

    # High/critical patches need human approval
    if risk in ("high", "critical") and patch.status == PatchStatus.PROPOSED:
        errors.append(f"patch risk is {risk}, requires human approval")

    return errors


# ── patch lifecycle ──

PATCH_DIR = Path.home() / ".story-lifecycle" / "graph-patches"


def _now_iso() -> str:
    return datetime.now().isoformat()


def create_patch(
    story_key: str,
    patch_type: PatchType,
    target_stage: str,
    reason: str = "",
    insert_after: str = "",
    new_stage: str = "",
    model_name: str = "",
    metadata: dict[str, Any] | None = None,
) -> GraphPatch:
    """Create a new graph patch proposal.

    Args:
        story_key: Story to patch.
        patch_type: Type of modification.
        target_stage: Stage being modified.
        reason: Why the patch is needed.
        insert_after: For insert_stage, stage to insert after.
        new_stage: For insert_stage, new stage name.
        model_name: For switch_model, new model name.
        metadata: Additional data.

    Returns:
        The proposed GraphPatch (status: PROPOSED).
    """
    patch = GraphPatch(
        patch_id=uuid.uuid4().hex[:12],
        story_key=story_key,
        patch_type=patch_type,
        target_stage=target_stage,
        insert_after=insert_after,
        new_stage=new_stage,
        model_name=model_name,
        reason=reason,
        metadata=metadata or {},
        created_at=_now_iso(),
    )

    # Auto-assess risk
    patch.risk_assessment = assess_patch_risk(patch)

    # Persist
    _save_patch(patch)

    # Log event
    db.log_event(
        story_key,
        target_stage,
        "graph_patch_proposed",
        {
            "patch_id": patch.patch_id,
            "patch_type": patch_type.value,
            "target_stage": target_stage,
            "risk": patch.risk_assessment,
            "reason": reason,
        },
    )

    return patch


def approve_patch(patch_id: str, validated_by: str = "") -> GraphPatch | None:
    """Validate and approve a graph patch.

    Args:
        patch_id: The patch to approve.
        validated_by: Who/what approved it.

    Returns:
        The updated GraphPatch, or None if validation fails.
    """
    patch = _load_patch(patch_id)
    if patch is None:
        return None

    # Validate
    errors = validate_patch(patch)
    # Allow high/critical patches if explicitly approved by human
    if errors and validated_by != "human":
        non_risk_errors = [e for e in errors if "requires human approval" not in e]
        if non_risk_errors:
            return None  # Validation failed

    patch.status = PatchStatus.VALIDATED
    patch.validated_by = validated_by

    _save_patch(patch)

    db.log_event(
        patch.story_key,
        patch.target_stage,
        "graph_patch_validated",
        {
            "patch_id": patch_id,
            "validated_by": validated_by,
        },
    )

    return patch


def apply_patch(patch_id: str) -> GraphPatch | None:
    """Apply a validated graph patch.

    Args:
        patch_id: The patch to apply.

    Returns:
        The applied GraphPatch, or None if not validated.
    """
    patch = _load_patch(patch_id)
    if patch is None:
        return None

    if patch.status != PatchStatus.VALIDATED:
        return None  # Must be validated first

    patch.status = PatchStatus.APPLIED
    patch.applied_at = _now_iso()

    _save_patch(patch)

    db.log_event(
        patch.story_key,
        patch.target_stage,
        "graph_patch_applied",
        {
            "patch_id": patch_id,
            "patch_type": patch.patch_type.value,
        },
    )

    return patch


def reject_patch(patch_id: str, reason: str = "") -> GraphPatch | None:
    """Reject a graph patch.

    Args:
        patch_id: The patch to reject.
        reason: Rejection reason.

    Returns:
        The rejected GraphPatch, or None if not found.
    """
    patch = _load_patch(patch_id)
    if patch is None:
        return None

    patch.status = PatchStatus.REJECTED
    patch.rollback_reason = reason

    _save_patch(patch)

    db.log_event(
        patch.story_key,
        patch.target_stage,
        "graph_patch_rejected",
        {
            "patch_id": patch_id,
            "reason": reason,
        },
    )

    return patch


def rollback_patch(patch_id: str, reason: str = "") -> GraphPatch | None:
    """Roll back an applied graph patch.

    Args:
        patch_id: The patch to roll back.
        reason: Rollback reason.

    Returns:
        The rolled-back GraphPatch, or None if not found.
    """
    patch = _load_patch(patch_id)
    if patch is None:
        return None

    if patch.status != PatchStatus.APPLIED:
        return None  # Can only rollback applied patches

    patch.status = PatchStatus.ROLLED_BACK
    patch.rollback_reason = reason

    _save_patch(patch)

    db.log_event(
        patch.story_key,
        patch.target_stage,
        "graph_patch_rolled_back",
        {
            "patch_id": patch_id,
            "reason": reason,
        },
    )

    return patch


# ── persistence ──


def _save_patch(patch: GraphPatch) -> None:
    """Persist a graph patch to disk."""
    PATCH_DIR.mkdir(parents=True, exist_ok=True)
    f = PATCH_DIR / f"{patch.patch_id}.json"
    data = {
        "patch_id": patch.patch_id,
        "story_key": patch.story_key,
        "patch_type": patch.patch_type.value,
        "target_stage": patch.target_stage,
        "insert_after": patch.insert_after,
        "new_stage": patch.new_stage,
        "model_name": patch.model_name,
        "reason": patch.reason,
        "risk_assessment": patch.risk_assessment,
        "status": patch.status.value,
        "validated_by": patch.validated_by,
        "applied_at": patch.applied_at,
        "rollback_reason": patch.rollback_reason,
        "metadata": patch.metadata,
        "created_at": patch.created_at,
    }
    f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_patch(patch_id: str) -> GraphPatch | None:
    """Load a graph patch by ID."""
    f = PATCH_DIR / f"{patch_id}.json"
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return GraphPatch(
        patch_id=data["patch_id"],
        story_key=data["story_key"],
        patch_type=PatchType(data["patch_type"]),
        target_stage=data["target_stage"],
        insert_after=data.get("insert_after", ""),
        new_stage=data.get("new_stage", ""),
        model_name=data.get("model_name", ""),
        reason=data.get("reason", ""),
        risk_assessment=data.get("risk_assessment", ""),
        status=PatchStatus(data.get("status", "proposed")),
        validated_by=data.get("validated_by", ""),
        applied_at=data.get("applied_at", ""),
        rollback_reason=data.get("rollback_reason", ""),
        metadata=data.get("metadata", {}),
        created_at=data.get("created_at", ""),
    )


# ── query helpers ──


def list_patches(
    story_key: str = "",
    status: PatchStatus | None = None,
    limit: int = 50,
) -> list[dict]:
    """List graph patches with optional filters."""
    PATCH_DIR.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    for f in sorted(
        PATCH_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True
    ):
        if len(results) >= limit:
            break
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if story_key and data.get("story_key") != story_key:
            continue
        if status and data.get("status") != status.value:
            continue
        results.append(data)
    return results


def get_active_patches(story_key: str) -> list[GraphPatch]:
    """Get all currently applied patches for a story.

    Args:
        story_key: Story to query.

    Returns:
        List of applied GraphPatch objects.
    """
    patches_data = list_patches(story_key=story_key, status=PatchStatus.APPLIED)
    result: list[GraphPatch] = []
    for data in patches_data:
        result.append(
            GraphPatch(
                patch_id=data["patch_id"],
                story_key=data["story_key"],
                patch_type=PatchType(data["patch_type"]),
                target_stage=data["target_stage"],
                insert_after=data.get("insert_after", ""),
                new_stage=data.get("new_stage", ""),
                model_name=data.get("model_name", ""),
                reason=data.get("reason", ""),
                risk_assessment=data.get("risk_assessment", ""),
                status=PatchStatus(data.get("status", "applied")),
                metadata=data.get("metadata", {}),
                created_at=data.get("created_at", ""),
            )
        )
    return result
