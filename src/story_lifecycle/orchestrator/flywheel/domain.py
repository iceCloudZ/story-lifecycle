"""Domain Flywheel — domain asset lifecycle and outcome tracking.

The Domain Flywheel manages the lifecycle of domain-specific knowledge:
- DomainAsset: reusable patterns, rules, heuristics discovered from domain
- DomainOutcome: records of how domain assets performed in production
- TraceMaturity: maturity level of domain traces (raw → structured → validated)

Design doc: idea-dual-flywheel-domain-and-engine.md §Domain Flywheel
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

from ...db import models as db

# ── data structures ──


class TraceMaturity(str, Enum):
    """Maturity level of domain trace data."""

    RAW = "raw"  # Unstructured observation
    STRUCTURED = "structured"  # Extracted into schema
    VALIDATED = "validated"  # Verified against outcomes


@dataclass
class DomainAsset:
    """A reusable domain knowledge asset.

    DomainAssets are discovered from real project outcomes and
    promoted through the shared promotion pipeline.
    """

    asset_id: str
    name: str
    asset_type: str  # "pattern", "rule", "heuristic", "constraint"
    domain: str  # e.g. "web-frontend", "data-pipeline", "api-design"
    description: str
    content: str  # The actual knowledge content (rule, pattern, etc.)
    applies_to: list[str] = field(default_factory=list)
    maturity: TraceMaturity = TraceMaturity.RAW
    confidence: float = 0.0  # 0-1
    source_stories: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


@dataclass
class DomainOutcome:
    """Record of a domain asset's performance in a story outcome.

    Links domain assets to actual story outcomes, enabling
    confidence recalculation and maturity promotion.
    """

    outcome_id: str
    asset_id: str
    story_key: str
    stage: str
    was_helpful: bool  # Did the asset contribute positively?
    impact_score: float = 0.0  # -1 to 1 (negative = harmful)
    note: str = ""
    created_at: str = ""


# ── persistence ──

DOMAIN_DIR = Path.home() / ".story-lifecycle" / "flywheel" / "domain"


def _now_iso() -> str:
    return datetime.now().isoformat()


def save_domain_asset(asset: DomainAsset) -> str:
    """Persist a domain asset to disk."""
    DOMAIN_DIR.mkdir(parents=True, exist_ok=True)
    asset_file = DOMAIN_DIR / f"{asset.asset_id}.json"
    data = {
        "asset_id": asset.asset_id,
        "name": asset.name,
        "asset_type": asset.asset_type,
        "domain": asset.domain,
        "description": asset.description,
        "content": asset.content,
        "applies_to": asset.applies_to,
        "maturity": asset.maturity.value,
        "confidence": asset.confidence,
        "source_stories": asset.source_stories,
        "created_at": asset.created_at,
        "updated_at": asset.updated_at,
    }
    asset_file.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return asset.asset_id


def load_domain_asset(asset_id: str) -> DomainAsset | None:
    """Load a domain asset by ID."""
    asset_file = DOMAIN_DIR / f"{asset_id}.json"
    if not asset_file.exists():
        return None
    try:
        data = json.loads(asset_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return DomainAsset(
        asset_id=data["asset_id"],
        name=data["name"],
        asset_type=data["asset_type"],
        domain=data["domain"],
        description=data["description"],
        content=data["content"],
        applies_to=data.get("applies_to", []),
        maturity=TraceMaturity(data.get("maturity", "raw")),
        confidence=data.get("confidence", 0.0),
        source_stories=data.get("source_stories", []),
        created_at=data.get("created_at", ""),
        updated_at=data.get("updated_at", ""),
    )


# ── outcome tracking ──

OUTCOME_DIR = Path.home() / ".story-lifecycle" / "flywheel" / "outcomes"


def record_domain_outcome(
    asset_id: str,
    story_key: str,
    stage: str,
    was_helpful: bool,
    impact_score: float = 0.0,
    note: str = "",
) -> DomainOutcome:
    """Record a domain asset's performance in a story outcome.

    Also recalculates the asset's confidence based on accumulated outcomes.

    Args:
        asset_id: The domain asset that was applied.
        story_key: Story where it was used.
        stage: Stage where it was applied.
        was_helpful: Whether the asset helped positively.
        impact_score: -1 to 1 impact score.
        note: Optional free-text note.

    Returns:
        The recorded DomainOutcome.
    """
    outcome = DomainOutcome(
        outcome_id=uuid.uuid4().hex[:12],
        asset_id=asset_id,
        story_key=story_key,
        stage=stage,
        was_helpful=was_helpful,
        impact_score=impact_score,
        note=note,
        created_at=_now_iso(),
    )

    # Persist outcome
    OUTCOME_DIR.mkdir(parents=True, exist_ok=True)
    outcome_file = OUTCOME_DIR / f"{outcome.outcome_id}.json"
    data = {
        "outcome_id": outcome.outcome_id,
        "asset_id": outcome.asset_id,
        "story_key": outcome.story_key,
        "stage": outcome.stage,
        "was_helpful": outcome.was_helpful,
        "impact_score": outcome.impact_score,
        "note": outcome.note,
        "created_at": outcome.created_at,
    }
    outcome_file.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Log event
    db.log_event(
        story_key,
        stage,
        "domain_outcome",
        data,
    )

    # Recalculate asset confidence
    _recalculate_confidence(asset_id)

    return outcome


def _recalculate_confidence(asset_id: str) -> None:
    """Recalculate domain asset confidence from all outcomes."""
    asset = load_domain_asset(asset_id)
    if asset is None:
        return

    outcomes = _load_asset_outcomes(asset_id)
    if not outcomes:
        return

    # Weighted average of impact scores
    total_weight = 0.0
    weighted_sum = 0.0
    for o in outcomes:
        # More recent outcomes have higher weight
        weight = 1.0
        weighted_sum += o.impact_score * weight
        total_weight += weight

    if total_weight > 0:
        asset.confidence = max(0.0, min(1.0, (weighted_sum / total_weight + 1) / 2))

    # Promote maturity if enough positive outcomes
    helpful_count = sum(1 for o in outcomes if o.was_helpful)
    if helpful_count >= 5 and asset.maturity == TraceMaturity.STRUCTURED:
        asset.maturity = TraceMaturity.VALIDATED
    elif helpful_count >= 2 and asset.maturity == TraceMaturity.RAW:
        asset.maturity = TraceMaturity.STRUCTURED

    asset.updated_at = _now_iso()
    save_domain_asset(asset)


def _load_asset_outcomes(asset_id: str) -> list[DomainOutcome]:
    """Load all outcomes for a domain asset."""
    OUTCOME_DIR.mkdir(parents=True, exist_ok=True)
    results: list[DomainOutcome] = []
    for f in OUTCOME_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("asset_id") == asset_id:
            results.append(
                DomainOutcome(
                    outcome_id=data["outcome_id"],
                    asset_id=data["asset_id"],
                    story_key=data["story_key"],
                    stage=data["stage"],
                    was_helpful=data.get("was_helpful", False),
                    impact_score=data.get("impact_score", 0.0),
                    note=data.get("note", ""),
                    created_at=data.get("created_at", ""),
                )
            )
    return results


# ── query helpers ──


def list_domain_assets(
    domain: str = "",
    asset_type: str = "",
    min_confidence: float = 0.0,
    limit: int = 50,
) -> list[dict]:
    """List domain assets with optional filters."""
    DOMAIN_DIR.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    for f in sorted(
        DOMAIN_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True
    ):
        if len(results) >= limit:
            break
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if domain and data.get("domain") != domain:
            continue
        if asset_type and data.get("asset_type") != asset_type:
            continue
        if data.get("confidence", 0) < min_confidence:
            continue
        results.append(data)
    return results
