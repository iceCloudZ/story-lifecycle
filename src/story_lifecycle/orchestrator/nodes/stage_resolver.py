from typing import Optional

from ...db import models as db
from .state import StoryState
from .profile_loader import get_stage_config


def _is_cancelled(state: StoryState) -> bool:
    """Check if this run has been superseded by a newer epoch."""
    if state.get("_cancelled"):
        return True
    epoch = state.get("_epoch", 0)
    if not epoch:
        return False
    from ..graph import is_epoch_current

    if not is_epoch_current(state["story_key"], epoch):
        state["_cancelled"] = True
        return True
    return False


def _block_for_planner(state: StoryState, reason: str) -> StoryState:
    state["last_error"] = reason
    state["_pre_routed_action"] = "wait_confirm"
    state["plan_summary"] = reason
    try:
        db.log_event(
            state["story_key"],
            state["current_stage"],
            "planner_blocked",
            {"reason": reason},
        )
    except Exception:
        pass
    return state


def resolve_next_stage(state: StoryState) -> Optional[str]:
    """Determine next stage from profile config + complexity."""
    cfg = get_stage_config(state.get("profile", "minimal"), state["current_stage"])
    next_map = cfg.get("next_default", {})

    if isinstance(next_map, list):
        return next_map[0] if next_map else None
    if isinstance(next_map, dict):
        complexity = state.get("complexity", "M")
        candidates = next_map.get(complexity, next_map.get("default", []))
        return candidates[0] if candidates else None
    return None
