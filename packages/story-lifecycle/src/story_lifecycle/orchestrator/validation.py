"""Stage output validator — swebench finalize artifact gate.

Currently scoped to the swebench benchmark profile's finalize stage
(`require_model_patch`). No production caller in the FC orchestration path
(stage completion is handled by gate.run_verify_gate); exercised by
test_swebench.py. Relocate to evaluation/ or benchmarks/ during stage-1
layer partition (ISS-010).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .nodes import get_stage_config

log = logging.getLogger("story-lifecycle.validation")


@dataclass
class ValidationResult:
    ok: bool
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)


def validate_stage_outputs(
    state: dict,
    profile_config: dict | None = None,
) -> ValidationResult:
    """Validate that a stage produced the required outputs.

    Returns a structured result — the caller decides what to do
    with it.  This function never mutates state or writes DB.
    """
    stage = state.get("current_stage", "")
    profile = state.get("profile", "minimal")
    ctx = state.get("context", {})
    cfg = get_stage_config(profile, stage)

    # 1. expected_outputs (skip for synthetic headless output)
    stage_synthetic = ctx.get(f"_synthetic_{stage}")
    if stage_synthetic:
        missing = []
    else:
        missing = [k for k in cfg.get("expected_outputs", []) if k not in ctx]
    if missing:
        return ValidationResult(
            ok=False,
            reason=f"Missing expected outputs: {missing}",
            details={"missing": missing, "validator": "expected_outputs"},
        )

    # 2. Profile-specific artifact gates
    gates = _resolve_artifact_gates(profile, stage, profile_config)
    if gates:
        gate_result = _check_artifact_gates(state, gates)
        if not gate_result.ok:
            return gate_result

    return ValidationResult(ok=True)


def _resolve_artifact_gates(
    profile: str, stage: str, profile_config: dict | None
) -> dict | None:
    """Return artifact gate rules for this stage.

    P0.5: hardcoded for swebench/finalize.
    P2: reads from profile_config["artifact_gates"][stage] if available.
    """
    if profile_config and isinstance(profile_config, dict):
        gates = profile_config.get("artifact_gates", {}).get(stage)
        if gates:
            return gates

    # Hardcoded defaults (P0.5)
    if profile == "swebench" and stage == "finalize":
        return {"require_model_patch": True, "allow_git_diff_fallback": True}

    return None


def _check_artifact_gates(state: dict, gates: dict) -> ValidationResult:
    """Check artifact-level gates (e.g. model_patch presence)."""
    if gates.get("require_model_patch"):
        from ..benchmarks.artifacts import extract_model_patch

        result = extract_model_patch(
            workspace=state["workspace"],
            story_key=state["story_key"],
            context=state.get("context", {}),
        )
        if not result.patch:
            return ValidationResult(
                ok=False,
                reason="finalize has no model_patch and no git diff",
                details={
                    "validator": "artifact_gate",
                    "source": result.source,
                    "reason": result.reason,
                },
            )
    return ValidationResult(ok=True)
