"""P3 Policy Engine — autonomy levels and decision envelopes.

Upgrades SuggestedAction to DecisionEnvelope with policy evaluation.
Tracks rejection history to enforce safety boundaries.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from enum import Enum


class AutonomyLevel(str, Enum):
    SHADOW = "shadow"
    CONFIRM = "confirm"
    APPLY = "apply"
    FORBIDDEN = "forbidden"


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
        from ..db.models import get_story_events

        events = get_story_events(story_key)
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
