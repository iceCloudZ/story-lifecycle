"""Budget Ledger — track and enforce resource limits per story.

Each story has a budget that limits time, LLM calls, retries, and human
interrupts. The ledger is updated by each DecisionEnvelope and checked
by the Policy Engine. Over-budget triggers hard kill.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path


@dataclass
class BudgetLedger:
    """Resource budget tracker for a single story."""

    max_minutes: int = 30
    used_minutes: float = 0.0
    max_llm_calls: int = 50
    used_llm_calls: int = 0
    max_retries: int = 3
    used_retries: int = 0
    max_human_interrupts: int = 2
    used_human_interrupts: int = 0


def _budget_path(workspace: str, story_key: str) -> Path:
    """Return budget ledger file path."""
    return Path(workspace) / ".story" / "context" / story_key / "budget.json"


def load_budget(workspace: str, story_key: str) -> BudgetLedger:
    """Load budget from disk, or return default if not found."""
    path = _budget_path(workspace, story_key)
    if not path.exists():
        return BudgetLedger()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return BudgetLedger(
            max_minutes=data.get("max_minutes", 30),
            used_minutes=data.get("used_minutes", 0.0),
            max_llm_calls=data.get("max_llm_calls", 50),
            used_llm_calls=data.get("used_llm_calls", 0),
            max_retries=data.get("max_retries", 3),
            used_retries=data.get("used_retries", 0),
            max_human_interrupts=data.get("max_human_interrupts", 2),
            used_human_interrupts=data.get("used_human_interrupts", 0),
        )
    except (json.JSONDecodeError, OSError):
        return BudgetLedger()


def save_budget(workspace: str, story_key: str, budget: BudgetLedger) -> None:
    """Persist budget ledger to disk."""
    path = _budget_path(workspace, story_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(budget), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def is_over_budget(budget: BudgetLedger) -> bool:
    """Check if any budget limit has been exceeded."""
    return (
        budget.used_minutes >= budget.max_minutes
        or budget.used_llm_calls >= budget.max_llm_calls
        or budget.used_retries >= budget.max_retries
        or budget.used_human_interrupts >= budget.max_human_interrupts
    )


def budget_delta(
    budget: BudgetLedger,
    minutes: float = 0.0,
    llm_calls: int = 0,
    retries: int = 0,
    human_interrupts: int = 0,
) -> BudgetLedger:
    """Apply a delta to the budget and return updated ledger."""
    budget.used_minutes += minutes
    budget.used_llm_calls += llm_calls
    budget.used_retries += retries
    budget.used_human_interrupts += human_interrupts
    return budget


def budget_remaining(budget: BudgetLedger) -> dict[str, float]:
    """Return remaining budget for each resource type."""
    return {
        "minutes_remaining": budget.max_minutes - budget.used_minutes,
        "llm_calls_remaining": budget.max_llm_calls - budget.used_llm_calls,
        "retries_remaining": budget.max_retries - budget.used_retries,
        "human_interrupts_remaining": budget.max_human_interrupts
        - budget.used_human_interrupts,
    }


def budget_burn_rate(budget: BudgetLedger) -> dict[str, float]:
    """Return percentage used for each resource type."""
    return {
        "minutes_pct": (budget.used_minutes / budget.max_minutes * 100)
        if budget.max_minutes
        else 0,
        "llm_calls_pct": (budget.used_llm_calls / budget.max_llm_calls * 100)
        if budget.max_llm_calls
        else 0,
        "retries_pct": (budget.used_retries / budget.max_retries * 100)
        if budget.max_retries
        else 0,
        "human_interrupts_pct": (
            budget.used_human_interrupts / budget.max_human_interrupts * 100
        )
        if budget.max_human_interrupts
        else 0,
    }
