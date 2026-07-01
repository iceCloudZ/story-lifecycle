"""P3 Policy Engine — autonomy levels, decision envelopes, and Guarded Apply.

Upgrades SuggestedAction to DecisionEnvelope with policy evaluation.
Tracks rejection history to enforce safety boundaries.

Guarded Apply (v1.0): L0-L5 execution autonomy levels.
- L0: Full manual — all apply actions need explicit human approval
- L1: Shadow only — proposals recorded but never executed
- L2: Confirm — all apply actions convert to needs_confirm
- L3: Supervised — low-risk auto, high-risk confirm
- L4: Autonomous — budget-controlled, model/retry/stage insert allowed
- L5: Full auto — SWE-bench / explicit authorization only

Design doc: idea-orchestrator-agent.md §Guarded Apply
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from ...infra.db import models as db


class AutonomyLevel(str, Enum):
    SHADOW = "shadow"
    CONFIRM = "confirm"
    APPLY = "apply"
    FORBIDDEN = "forbidden"


class GuardedAutonomy(str, Enum):
    """L0-L5 execution autonomy levels for Guarded Apply.

    Each level defines what the engine can do without human approval.
    Higher levels allow more autonomous actions but require stronger
    authorization (explicit profile config or SWE-bench mode).
    """

    L0_FULL_MANUAL = "L0"  # All actions need human approval
    L1_SHADOW_ONLY = "L1"  # Proposals only, never execute
    L2_CONFIRM = "L2"  # All apply → needs_confirm
    L3_SUPERVISED = "L3"  # Low-risk auto, high-risk confirm
    L4_AUTONOMOUS = "L4"  # Budget-controlled autonomy
    L5_FULL_AUTO = "L5"  # SWE-bench / explicit auth only


class ActionCategory(str, Enum):
    """Categories of actions that the engine can perform."""

    READ_ONLY = "read_only"  # Query / inspect state
    LOCAL_CONFIG = "local_config"  # Modify local config files
    WORKFLOW_STATE = "workflow_state"  # Change story/stage state
    ROUTING = "routing"  # Route to retry/skip/fail
    MODEL_SWITCH = "model_switch"  # Switch LLM model/provider
    STAGE_INSERT = "stage_insert"  # Insert a stage into the graph
    STAGE_SKIP = "stage_skip"  # Skip a planned stage
    BUDGET_ADJUST = "budget_adjust"  # Modify budget allocation
    CODE_MODIFY = "code_modify"  # Modify source code
    DESTRUCTIVE = "destructive"  # Irreversible operations


# ── Guarded Apply rules matrix ──
#
# Maps (GuardedAutonomy level, ActionCategory) → AutonomyLevel
# This defines what happens at each autonomy level for each action category.

GUARDED_RULES: dict[tuple[str, str], AutonomyLevel] = {
    # L0: Full manual — everything needs approval
    ("L0", ActionCategory.READ_ONLY.value): AutonomyLevel.APPLY,
    ("L0", ActionCategory.LOCAL_CONFIG.value): AutonomyLevel.CONFIRM,
    ("L0", ActionCategory.WORKFLOW_STATE.value): AutonomyLevel.CONFIRM,
    ("L0", ActionCategory.ROUTING.value): AutonomyLevel.CONFIRM,
    ("L0", ActionCategory.MODEL_SWITCH.value): AutonomyLevel.CONFIRM,
    ("L0", ActionCategory.STAGE_INSERT.value): AutonomyLevel.SHADOW,
    ("L0", ActionCategory.STAGE_SKIP.value): AutonomyLevel.SHADOW,
    ("L0", ActionCategory.BUDGET_ADJUST.value): AutonomyLevel.CONFIRM,
    ("L0", ActionCategory.CODE_MODIFY.value): AutonomyLevel.CONFIRM,
    ("L0", ActionCategory.DESTRUCTIVE.value): AutonomyLevel.FORBIDDEN,
    # L1: Shadow only — proposals recorded but never executed
    ("L1", ActionCategory.READ_ONLY.value): AutonomyLevel.APPLY,
    ("L1", ActionCategory.LOCAL_CONFIG.value): AutonomyLevel.SHADOW,
    ("L1", ActionCategory.WORKFLOW_STATE.value): AutonomyLevel.SHADOW,
    ("L1", ActionCategory.ROUTING.value): AutonomyLevel.SHADOW,
    ("L1", ActionCategory.MODEL_SWITCH.value): AutonomyLevel.SHADOW,
    ("L1", ActionCategory.STAGE_INSERT.value): AutonomyLevel.SHADOW,
    ("L1", ActionCategory.STAGE_SKIP.value): AutonomyLevel.SHADOW,
    ("L1", ActionCategory.BUDGET_ADJUST.value): AutonomyLevel.SHADOW,
    ("L1", ActionCategory.CODE_MODIFY.value): AutonomyLevel.SHADOW,
    ("L1", ActionCategory.DESTRUCTIVE.value): AutonomyLevel.FORBIDDEN,
    # L2: Confirm — all apply actions convert to needs_confirm
    ("L2", ActionCategory.READ_ONLY.value): AutonomyLevel.APPLY,
    ("L2", ActionCategory.LOCAL_CONFIG.value): AutonomyLevel.CONFIRM,
    ("L2", ActionCategory.WORKFLOW_STATE.value): AutonomyLevel.CONFIRM,
    ("L2", ActionCategory.ROUTING.value): AutonomyLevel.CONFIRM,
    ("L2", ActionCategory.MODEL_SWITCH.value): AutonomyLevel.CONFIRM,
    ("L2", ActionCategory.STAGE_INSERT.value): AutonomyLevel.SHADOW,
    ("L2", ActionCategory.STAGE_SKIP.value): AutonomyLevel.SHADOW,
    ("L2", ActionCategory.BUDGET_ADJUST.value): AutonomyLevel.CONFIRM,
    ("L2", ActionCategory.CODE_MODIFY.value): AutonomyLevel.CONFIRM,
    ("L2", ActionCategory.DESTRUCTIVE.value): AutonomyLevel.FORBIDDEN,
    # L3: Supervised — low-risk auto, high-risk confirm
    ("L3", ActionCategory.READ_ONLY.value): AutonomyLevel.APPLY,
    ("L3", ActionCategory.LOCAL_CONFIG.value): AutonomyLevel.APPLY,
    ("L3", ActionCategory.WORKFLOW_STATE.value): AutonomyLevel.APPLY,
    ("L3", ActionCategory.ROUTING.value): AutonomyLevel.APPLY,
    ("L3", ActionCategory.MODEL_SWITCH.value): AutonomyLevel.CONFIRM,
    ("L3", ActionCategory.STAGE_INSERT.value): AutonomyLevel.CONFIRM,
    ("L3", ActionCategory.STAGE_SKIP.value): AutonomyLevel.CONFIRM,
    ("L3", ActionCategory.BUDGET_ADJUST.value): AutonomyLevel.CONFIRM,
    ("L3", ActionCategory.CODE_MODIFY.value): AutonomyLevel.APPLY,
    ("L3", ActionCategory.DESTRUCTIVE.value): AutonomyLevel.FORBIDDEN,
    # L4: Autonomous — budget-controlled
    ("L4", ActionCategory.READ_ONLY.value): AutonomyLevel.APPLY,
    ("L4", ActionCategory.LOCAL_CONFIG.value): AutonomyLevel.APPLY,
    ("L4", ActionCategory.WORKFLOW_STATE.value): AutonomyLevel.APPLY,
    ("L4", ActionCategory.ROUTING.value): AutonomyLevel.APPLY,
    ("L4", ActionCategory.MODEL_SWITCH.value): AutonomyLevel.APPLY,
    ("L4", ActionCategory.STAGE_INSERT.value): AutonomyLevel.APPLY,
    ("L4", ActionCategory.STAGE_SKIP.value): AutonomyLevel.CONFIRM,
    ("L4", ActionCategory.BUDGET_ADJUST.value): AutonomyLevel.APPLY,
    ("L4", ActionCategory.CODE_MODIFY.value): AutonomyLevel.APPLY,
    ("L4", ActionCategory.DESTRUCTIVE.value): AutonomyLevel.CONFIRM,
    # L5: Full auto
    ("L5", ActionCategory.READ_ONLY.value): AutonomyLevel.APPLY,
    ("L5", ActionCategory.LOCAL_CONFIG.value): AutonomyLevel.APPLY,
    ("L5", ActionCategory.WORKFLOW_STATE.value): AutonomyLevel.APPLY,
    ("L5", ActionCategory.ROUTING.value): AutonomyLevel.APPLY,
    ("L5", ActionCategory.MODEL_SWITCH.value): AutonomyLevel.APPLY,
    ("L5", ActionCategory.STAGE_INSERT.value): AutonomyLevel.APPLY,
    ("L5", ActionCategory.STAGE_SKIP.value): AutonomyLevel.APPLY,
    ("L5", ActionCategory.BUDGET_ADJUST.value): AutonomyLevel.APPLY,
    ("L5", ActionCategory.CODE_MODIFY.value): AutonomyLevel.APPLY,
    ("L5", ActionCategory.DESTRUCTIVE.value): AutonomyLevel.CONFIRM,
}

DEFAULT_GUARDED_LEVEL = GuardedAutonomy.L2_CONFIRM


DEFAULT_POLICY: dict[str, AutonomyLevel] = {
    "read_only": AutonomyLevel.APPLY,
    "local_config": AutonomyLevel.CONFIRM,
    "workflow_state": AutonomyLevel.CONFIRM,
    "destructive": AutonomyLevel.FORBIDDEN,
}

MAX_REJECTIONS_BEFORE_FORBIDDEN = 3


@dataclass
class PolicyDecision:
    level: AutonomyLevel
    reason: str
    matched_rule: str = ""
    rejection_count: int = 0


@dataclass
class DecisionEnvelope:
    decision_id: str
    action: str
    label: str
    risk: str
    reason: str
    policy: PolicyDecision
    requires_confirm: bool


# ── Guarded Apply data structures ──


@dataclass
class AutonomyDecision:
    """Result of a Guarded Apply evaluation.

    Records the full decision chain for audit: what was requested,
    at what autonomy level, what was decided, and why.
    """

    decision_id: str
    story_key: str
    action: str
    action_category: ActionCategory
    autonomy_level: GuardedAutonomy
    decision: AutonomyLevel  # The resulting decision
    reason: str
    matched_rule: str = ""
    overriden_by: str = ""  # If human overrode the decision
    budget_check_passed: bool = True
    created_at: str = ""


@dataclass
class AutonomyTrace:
    """Audit trail entry for autonomy decisions."""

    trace_id: str
    story_key: str
    decisions: list[AutonomyDecision] = field(default_factory=list)
    effective_level: GuardedAutonomy = GuardedAutonomy.L2_CONFIRM
    created_at: str = ""


AUTONOMY_TRACE_DIR = None  # Set dynamically to avoid import issues


def _get_trace_dir():
    from pathlib import Path

    d = Path.home() / ".story-lifecycle" / "autonomy-traces"
    d.mkdir(parents=True, exist_ok=True)
    return d


def evaluate_policy(action: str, risk: str, story_key: str) -> PolicyDecision:
    """Evaluate the autonomy policy for a copilot-suggested action."""

    rejections = _count_rejections(story_key, action)

    if risk == "destructive":
        return PolicyDecision(
            level=AutonomyLevel.FORBIDDEN,
            reason="destructive 操作禁止由 Copilot 发起",
            matched_rule="destructive_forbidden",
        )

    if rejections >= MAX_REJECTIONS_BEFORE_FORBIDDEN:
        return PolicyDecision(
            level=AutonomyLevel.FORBIDDEN,
            reason=f"该操作已被连续拒绝 {rejections} 次，暂时禁止",
            matched_rule="rejection_threshold",
            rejection_count=rejections,
        )

    base_level = DEFAULT_POLICY.get(risk, AutonomyLevel.CONFIRM)

    reason_map = {
        AutonomyLevel.APPLY: f"{risk} 操作，低风险自动执行",
        AutonomyLevel.CONFIRM: f"{risk} 操作，需用户确认",
    }
    return PolicyDecision(
        level=base_level,
        reason=reason_map.get(base_level, f"默认策略: {base_level}"),
        matched_rule=f"default_{risk}",
        rejection_count=rejections,
    )


# ── Guarded Apply evaluation ──


def evaluate_guarded(
    action: str,
    action_category: ActionCategory,
    story_key: str,
    autonomy_level: GuardedAutonomy | None = None,
    budget_remaining: dict[str, float] | None = None,
) -> AutonomyDecision:
    """Evaluate an action under the Guarded Apply framework.

    Uses the L0-L5 rules matrix to determine whether an action
    can be automatically applied, needs confirmation, is shadow-only,
    or is forbidden.

    Args:
        action: The action being evaluated.
        action_category: Category of the action.
        story_key: Story context.
        autonomy_level: The effective autonomy level (default from profile).
        budget_remaining: Current budget state for budget checks.

    Returns:
        An AutonomyDecision with the full evaluation result.
    """
    if autonomy_level is None:
        autonomy_level = DEFAULT_GUARDED_LEVEL

    # Look up the rule
    key = (autonomy_level.value, action_category.value)
    decision = GUARDED_RULES.get(key, AutonomyLevel.CONFIRM)

    # Additional checks
    reason = ""
    matched_rule = key
    budget_check_passed = True

    # Budget check for L4+
    if autonomy_level.value in ("L4", "L5") and budget_remaining is not None:
        remaining_minutes = budget_remaining.get("minutes", 0)
        remaining_calls = budget_remaining.get("llm_calls", 0)
        if remaining_minutes <= 0 or remaining_calls <= 0:
            # Budget exhausted — downgrade to CONFIRM
            if decision == AutonomyLevel.APPLY:
                decision = AutonomyLevel.CONFIRM
                reason = "预算耗尽，降级为需确认"
                budget_check_passed = False

    # Rejection escalation
    if decision != AutonomyLevel.FORBIDDEN:
        rejections = _count_rejections(story_key, action)
        if rejections >= MAX_REJECTIONS_BEFORE_FORBIDDEN:
            decision = AutonomyLevel.FORBIDDEN
            reason = f"连续拒绝 {rejections} 次，临时禁止"
            matched_rule = "rejection_escalation"

    if not reason:
        reason_map = {
            AutonomyLevel.APPLY: f"L{autonomy_level.value[1:]} {action_category.value} → 自动执行",
            AutonomyLevel.CONFIRM: f"L{autonomy_level.value[1:]} {action_category.value} → 需确认",
            AutonomyLevel.SHADOW: f"L{autonomy_level.value[1:]} {action_category.value} → 仅记录",
            AutonomyLevel.FORBIDDEN: f"L{autonomy_level.value[1:]} {action_category.value} → 禁止",
        }
        reason = reason_map.get(decision, f"L{autonomy_level.value[1:]} 默认策略")

    result = AutonomyDecision(
        decision_id=uuid.uuid4().hex[:12],
        story_key=story_key,
        action=action,
        action_category=action_category,
        autonomy_level=autonomy_level,
        decision=decision,
        reason=reason,
        matched_rule=matched_rule,
        budget_check_passed=budget_check_passed,
        created_at=datetime.now().isoformat(),
    )

    # Write audit trace
    _write_autonomy_trace(result)

    return result


def _write_autonomy_trace(decision: AutonomyDecision) -> None:
    """Write an autonomy decision to the audit trace."""
    try:
        trace_dir = _get_trace_dir()
        trace_file = trace_dir / f"{decision.decision_id}.json"
        data = {
            "decision_id": decision.decision_id,
            "story_key": decision.story_key,
            "action": decision.action,
            "action_category": decision.action_category.value,
            "autonomy_level": decision.autonomy_level.value,
            "decision": decision.decision.value,
            "reason": decision.reason,
            "matched_rule": decision.matched_rule,
            "budget_check_passed": decision.budget_check_passed,
            "created_at": decision.created_at,
        }
        trace_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass  # Audit trace failure should not break the main flow

    # Also log to event_log
    try:
        db.log_event(
            decision.story_key,
            "",
            "autonomy_decision",
            {
                "decision_id": decision.decision_id,
                "action": decision.action,
                "category": decision.action_category.value,
                "level": decision.autonomy_level.value,
                "result": decision.decision.value,
                "reason": decision.reason,
            },
        )
    except Exception:
        pass


def get_effective_autonomy(story_key: str) -> GuardedAutonomy:
    """Get the effective autonomy level for a story.

    Reads from the story's strategy envelope (if available),
    falls back to the profile default, then to L2.

    Args:
        story_key: Story to query.

    Returns:
        The effective GuardedAutonomy level.
    """
    # Try loading from strategy
    try:
        from .meta_planner import load_strategy

        strategy = load_strategy(story_key)
        if strategy and strategy.signals.get("autonomy_level"):
            level_str = strategy.signals["autonomy_level"]
            return GuardedAutonomy(level_str)
    except Exception:
        pass

    # Try loading from profile
    try:
        story = db.get_story(story_key)
        if story:
            ctx = json.loads(story.get("context_json") or "{}")
            level_str = ctx.get("autonomy_level", "")
            if level_str:
                return GuardedAutonomy(level_str)
    except Exception:
        pass

    return DEFAULT_GUARDED_LEVEL


def list_autonomy_traces(story_key: str = "", limit: int = 50) -> list[dict]:
    """List autonomy decision traces with optional filters."""
    try:
        trace_dir = _get_trace_dir()
        results: list[dict] = []
        for f in sorted(
            trace_dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True
        ):
            if len(results) >= limit:
                break
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if story_key and data.get("story_key") != story_key:
                continue
            results.append(data)
        return results
    except Exception:
        return []


def wrap_actions(raw_actions: list[dict], story_key: str) -> list[DecisionEnvelope]:
    """Wrap normalized actions in DecisionEnvelope with policy evaluation."""
    envelopes = []
    for a in raw_actions:
        policy = evaluate_policy(a["action"], a["risk"], story_key)
        requires_confirm = (
            policy.level in (AutonomyLevel.CONFIRM,)
            and policy.level != AutonomyLevel.FORBIDDEN
        )
        envelopes.append(
            DecisionEnvelope(
                decision_id=uuid.uuid4().hex[:12],
                action=a["action"],
                label=a["label"],
                risk=a["risk"],
                reason=a.get("reason", ""),
                policy=policy,
                requires_confirm=requires_confirm,
            )
        )
    return envelopes


def _count_rejections(story_key: str, action: str) -> int:
    """Count recent consecutive rejections for a specific action."""
    try:
        events = db.get_story_events(story_key)
        count = 0
        for e in reversed(events):
            et = e.get("event_type", "")
            if et == "copilot_action_rejected":
                payload = e.get("payload", {})
                if isinstance(payload, str):
                    payload = json.loads(payload)
                if isinstance(payload, dict) and payload.get("action") == action:
                    count += 1
                else:
                    break
            elif et in ("copilot_action_confirmed", "copilot_action_applied"):
                break  # Successful action resets the counter
        return count
    except Exception:
        return 0
