"""Dual Flywheel Governance — domain and engine unified governance.

This sub-package implements the dual flywheel pattern where:
- Domain Flywheel: manages domain assets (patterns, rules, heuristics)
- Engine Flywheel: manages engine execution traces and strategies
- Promotion: shared pipeline for promoting proposals to active state

Design doc: idea-dual-flywheel-domain-and-engine.md
"""

from .domain import DomainAsset, DomainOutcome, TraceMaturity, record_domain_outcome
from .engine import EngineTrace, EvalEvidence, StrategyRecord, record_engine_trace
from .promotion import (
    PromotionItem,
    PromotionQueue,
    PromotionStage,
    promote_item,
    reject_item,
)

__all__ = [
    "DomainAsset",
    "DomainOutcome",
    "TraceMaturity",
    "record_domain_outcome",
    "EngineTrace",
    "EvalEvidence",
    "StrategyRecord",
    "record_engine_trace",
    "PromotionItem",
    "PromotionQueue",
    "PromotionStage",
    "promote_item",
    "reject_item",
]
