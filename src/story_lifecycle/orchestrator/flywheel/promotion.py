"""Shared Promotion Pipeline — proposed → sandbox_validated → active.

Both domain assets and engine strategies share the same promotion
pipeline. The pipeline ensures that only validated knowledge reaches
active state.

Priority arbitration: safety > domain production > engine execution

Design doc: idea-dual-flywheel-domain-and-engine.md §Promotion Pipeline
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from ...db import models as db

# ── data structures ──


class PromotionStage(str, Enum):
    """Stages in the promotion pipeline."""

    PROPOSED = "proposed"
    SANDBOX_VALIDATED = "sandbox_validated"
    ACTIVE = "active"
    REJECTED = "rejected"
    DEPRECATED = "deprecated"


class ItemSource(str, Enum):
    """Origin of the promotion item."""

    DOMAIN = "domain"
    ENGINE = "engine"


@dataclass
class PromotionItem:
    """An item in the shared promotion pipeline.

    Both domain assets and engine strategies go through this
    pipeline before becoming active.
    """

    item_id: str
    source: ItemSource
    source_id: str  # asset_id or strategy_id
    name: str
    description: str
    stage: PromotionStage = PromotionStage.PROPOSED
    priority: int = 0  # Higher = more important
    safety_tags: list[str] = field(
        default_factory=list
    )  # e.g. ["destructive", "production"]
    validation_results: list[dict[str, Any]] = field(default_factory=list)
    promoted_by: str = ""
    promoted_at: str = ""
    rejected_by: str = ""
    rejected_at: str = ""
    rejection_reason: str = ""
    created_at: str = ""


@dataclass
class PromotionQueue:
    """The shared promotion queue with priority arbitration."""

    items: list[PromotionItem] = field(default_factory=list)

    def pending_count(self) -> int:
        return sum(1 for i in self.items if i.stage == PromotionStage.PROPOSED)

    def sandbox_count(self) -> int:
        return sum(1 for i in self.items if i.stage == PromotionStage.SANDBOX_VALIDATED)

    def active_count(self) -> int:
        return sum(1 for i in self.items if i.stage == PromotionStage.ACTIVE)


# ── persistence ──

PROMOTION_DIR = Path.home() / ".story-lifecycle" / "flywheel" / "promotion"


def _now_iso() -> str:
    return datetime.now().isoformat()


def propose_item(
    source: ItemSource,
    source_id: str,
    name: str,
    description: str,
    priority: int = 0,
    safety_tags: list[str] | None = None,
) -> PromotionItem:
    """Create a new proposed item in the promotion pipeline.

    Args:
        source: Origin (domain or engine).
        source_id: The source-specific ID.
        name: Human-readable name.
        description: What this item does.
        priority: Priority level (higher = more important).
        safety_tags: Tags that affect priority arbitration.

    Returns:
        The proposed PromotionItem.
    """
    item = PromotionItem(
        item_id=uuid.uuid4().hex[:12],
        source=source,
        source_id=source_id,
        name=name,
        description=description,
        stage=PromotionStage.PROPOSED,
        priority=priority,
        safety_tags=safety_tags or [],
        created_at=_now_iso(),
    )

    _save_promotion_item(item)

    # Log event
    db.log_event(
        "",
        "",
        "promotion_proposed",
        {
            "item_id": item.item_id,
            "source": source.value,
            "name": name,
            "priority": priority,
            "safety_tags": item.safety_tags,
        },
    )

    return item


def promote_item(
    item_id: str,
    promoted_by: str = "",
    validation_result: dict[str, Any] | None = None,
) -> PromotionItem | None:
    """Promote an item to the next stage in the pipeline.

    Pipeline: proposed → sandbox_validated → active

    Priority arbitration:
    - Items with "destructive" safety tag cannot be auto-promoted
    - Domain production items have higher priority than engine execution
    - Safety always takes precedence

    Args:
        item_id: The item to promote.
        promoted_by: Who/what triggered the promotion.
        validation_result: Optional validation evidence.

    Returns:
        The updated PromotionItem, or None if not found.
    """
    item = _load_promotion_item(item_id)
    if item is None:
        return None

    # Safety check: destructive items need explicit approval
    if "destructive" in item.safety_tags and item.stage == PromotionStage.PROPOSED:
        # Cannot auto-promote destructive items
        return None

    # Determine next stage
    if item.stage == PromotionStage.PROPOSED:
        item.stage = PromotionStage.SANDBOX_VALIDATED
    elif item.stage == PromotionStage.SANDBOX_VALIDATED:
        item.stage = PromotionStage.ACTIVE
    else:
        return item  # Already active or rejected

    item.promoted_by = promoted_by
    item.promoted_at = _now_iso()

    if validation_result:
        item.validation_results.append(validation_result)

    _save_promotion_item(item)

    # Log event
    db.log_event(
        "",
        "",
        "promotion_advanced",
        {
            "item_id": item_id,
            "new_stage": item.stage.value,
            "promoted_by": promoted_by,
        },
    )

    return item


def reject_item(
    item_id: str,
    rejected_by: str = "",
    reason: str = "",
) -> PromotionItem | None:
    """Reject a promotion item.

    Args:
        item_id: The item to reject.
        rejected_by: Who/what rejected it.
        reason: Rejection reason.

    Returns:
        The updated PromotionItem, or None if not found.
    """
    item = _load_promotion_item(item_id)
    if item is None:
        return None

    item.stage = PromotionStage.REJECTED
    item.rejected_by = rejected_by
    item.rejected_at = _now_iso()
    item.rejection_reason = reason

    _save_promotion_item(item)

    # Log event
    db.log_event(
        "",
        "",
        "promotion_rejected",
        {
            "item_id": item_id,
            "rejected_by": rejected_by,
            "reason": reason,
        },
    )

    return item


def deprecate_item(item_id: str, reason: str = "") -> PromotionItem | None:
    """Deprecate an active item.

    Args:
        item_id: The item to deprecate.
        reason: Deprecation reason.

    Returns:
        The updated PromotionItem, or None if not found.
    """
    item = _load_promotion_item(item_id)
    if item is None:
        return None

    item.stage = PromotionStage.DEPRECATED
    item.rejection_reason = reason

    _save_promotion_item(item)
    return item


# ── persistence helpers ──


def _save_promotion_item(item: PromotionItem) -> None:
    """Persist a promotion item to disk."""
    PROMOTION_DIR.mkdir(parents=True, exist_ok=True)
    f = PROMOTION_DIR / f"{item.item_id}.json"
    data = {
        "item_id": item.item_id,
        "source": item.source.value,
        "source_id": item.source_id,
        "name": item.name,
        "description": item.description,
        "stage": item.stage.value,
        "priority": item.priority,
        "safety_tags": item.safety_tags,
        "validation_results": item.validation_results,
        "promoted_by": item.promoted_by,
        "promoted_at": item.promoted_at,
        "rejected_by": item.rejected_by,
        "rejected_at": item.rejected_at,
        "rejection_reason": item.rejection_reason,
        "created_at": item.created_at,
    }
    f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_promotion_item(item_id: str) -> PromotionItem | None:
    """Load a promotion item by ID."""
    f = PROMOTION_DIR / f"{item_id}.json"
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return PromotionItem(
        item_id=data["item_id"],
        source=ItemSource(data["source"]),
        source_id=data["source_id"],
        name=data["name"],
        description=data["description"],
        stage=PromotionStage(data["stage"]),
        priority=data.get("priority", 0),
        safety_tags=data.get("safety_tags", []),
        validation_results=data.get("validation_results", []),
        promoted_by=data.get("promoted_by", ""),
        promoted_at=data.get("promoted_at", ""),
        rejected_by=data.get("rejected_by", ""),
        rejected_at=data.get("rejected_at", ""),
        rejection_reason=data.get("rejection_reason", ""),
        created_at=data.get("created_at", ""),
    )


# ── queue operations ──


def load_promotion_queue(
    stage: PromotionStage | None = None,
    source: ItemSource | None = None,
    limit: int = 100,
) -> PromotionQueue:
    """Load the promotion queue with optional filters.

    Args:
        stage: Filter by stage (None = all).
        source: Filter by source (None = all).
        limit: Max items to load.

    Returns:
        A PromotionQueue with matching items.
    """
    PROMOTION_DIR.mkdir(parents=True, exist_ok=True)
    items: list[PromotionItem] = []

    for f in sorted(
        PROMOTION_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True
    ):
        if len(items) >= limit:
            break
        item = _load_promotion_item_from_file(f)
        if item is None:
            continue
        if stage and item.stage != stage:
            continue
        if source and item.source != source:
            continue
        items.append(item)

    # Sort by priority (higher first), then by creation time
    items.sort(key=lambda i: (-i.priority, i.created_at))

    return PromotionQueue(items=items)


def _load_promotion_item_from_file(f: Path) -> PromotionItem | None:
    """Load a promotion item from a file path."""
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return PromotionItem(
        item_id=data["item_id"],
        source=ItemSource(data["source"]),
        source_id=data["source_id"],
        name=data["name"],
        description=data["description"],
        stage=PromotionStage(data["stage"]),
        priority=data.get("priority", 0),
        safety_tags=data.get("safety_tags", []),
        validation_results=data.get("validation_results", []),
        promoted_by=data.get("promoted_by", ""),
        promoted_at=data.get("promoted_at", ""),
        rejected_by=data.get("rejected_by", ""),
        rejected_at=data.get("rejected_at", ""),
        rejection_reason=data.get("rejection_reason", ""),
        created_at=data.get("created_at", ""),
    )


# ── priority arbitration ──


def arbitrate_priority(items: list[PromotionItem]) -> list[PromotionItem]:
    """Arbitrate promotion priority.

    Priority order (higher wins):
    1. Safety-critical items (have safety tags)
    2. Domain production items
    3. Engine execution items

    Within each group, higher explicit priority wins.

    Args:
        items: Items to arbitrate.

    Returns:
        Sorted items with arbitration applied.
    """

    def priority_key(item: PromotionItem) -> tuple[int, int, int]:
        # Group: safety=3, domain=2, engine=1
        group = 1
        if item.safety_tags:
            group = 3
        elif item.source == ItemSource.DOMAIN:
            group = 2

        return (-group, -item.priority, 0)

    return sorted(items, key=priority_key)
