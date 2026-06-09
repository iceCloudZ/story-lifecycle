"""Pydantic models for LLM structured output — replaces hand-written JSON schema validation."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


# ── Planner ──


class SubtaskDef(BaseModel):
    key_suffix: str
    title: str
    summary: str = ""
    depends_on: list[str] = Field(default_factory=list)


class PlanResult(BaseModel):
    adapter: str = "claude"
    provider: Optional[str] = None
    model: Optional[str] = None
    skip: bool = False
    split: bool = False
    subtasks: Optional[list[SubtaskDef]] = None
    summary: str = ""
    extra_instructions: str = ""
    reasoning: str = ""
    trajectory_score: float = 0.5


# ── Reviewer ──


class Issue(BaseModel):
    type: str = "unknown"
    severity: Literal["high", "medium", "low"] = "medium"
    location: str = ""
    description: str = ""
    recommendation: str = ""


class ReviewResult(BaseModel):
    quality: Literal["pass", "revise", "fail"] = "pass"
    summary: str = ""
    feedback: str = ""
    issues: list[Issue] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    trajectory_score: float = 0.5
    context_updates: dict = Field(default_factory=dict)
    reasoning: str = ""


# ── Plan Reviewer (adversarial loop) ──


class Blocker(BaseModel):
    severity: Literal["high", "medium", "low"] = "medium"
    category: str = ""
    description: str = ""


class PlanReviewResult(BaseModel):
    quality: Literal["pass", "revise"] = "pass"
    blockers: list[Blocker] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    reasoning: str = ""


# ── Router ──


class RouteDecision(BaseModel):
    action: Literal["retry", "skip", "fail"] = "fail"
    reasoning: str = ""
    provider_override: Optional[str] = None


# ── Semantic extraction ──


class BugContextResult(BaseModel):
    description: str = ""
    steps_to_reproduce: str = ""
    expected_behavior: str = ""
    actual_behavior: str = ""
    environment: str = ""
    logs: str = ""
    missing_fields: list[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "low"


class PatternMatch(BaseModel):
    pattern_id: str = ""
    matched: bool = False
    confidence: Literal["high", "medium", "low"] = "low"
    reasoning: str = ""
    evidence: list[str] = Field(default_factory=list)


class PatternRecurrenceResult(BaseModel):
    matches: list[PatternMatch] = Field(default_factory=list)


class SelectedPattern(BaseModel):
    pattern_id: str = ""
    relevance: Literal["high", "medium"] = "medium"
    reasoning: str = ""


class RejectedPattern(BaseModel):
    pattern_id: str = ""
    reasoning: str = ""


class RerankResult(BaseModel):
    selected: list[SelectedPattern] = Field(default_factory=list)
    rejected: list[RejectedPattern] = Field(default_factory=list)


class ReviewSummaryResult(BaseModel):
    quality: Literal["pass", "revise", "fail", "unknown"] = "unknown"
    key_issues: list[dict] = Field(default_factory=list)
    useful_for_learning: bool = False
    summary: str = ""
    confidence: Literal["high", "medium", "low"] = "low"


class RecoveryRecommendation(BaseModel):
    failure_type: Literal[
        "done_file_parse_error",
        "missing_expected_outputs",
        "dor_blocked",
        "dod_blocked",
        "tool_crash",
        "review_retry_exhausted",
        "unknown",
    ] = "unknown"
    likely_cause: str = ""
    recommended_action: Literal[
        "retry", "retry_with_prompt", "fix_input", "ask_human", "defer", "fail"
    ] = "ask_human"
    safe_to_retry: bool = False
    confidence: Literal["high", "medium", "low"] = "low"
    evidence: list[str] = Field(default_factory=list)
    human_message: str = ""
