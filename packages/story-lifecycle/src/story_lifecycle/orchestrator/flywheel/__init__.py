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
    ItemSource,
    PromotionItem,
    PromotionQueue,
    PromotionStage,
    propose_item,
    promote_item,
    reject_item,
    load_promotion_queue,
    arbitrate_priority,
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
    "ItemSource",
    "PromotionItem",
    "PromotionQueue",
    "PromotionStage",
    "propose_item",
    "promote_item",
    "reject_item",
    "load_promotion_queue",
    "arbitrate_priority",
]
