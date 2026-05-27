from .state import StoryState


def route_after_plan(state: StoryState) -> str:
    """Conditional edge after plan_stage: skip, blocked, execute, or end."""
    if state.get("_cancelled"):
        return "__end__"
    if state.get("status") == "skipping":
        return "skip_stage"
    if state.get("status") == "waiting_subtasks":
        return "__end__"
    # Plan loop blocked — route to router which reads _pre_routed_action
    if state.get("_pre_routed_action") or state.get("last_error"):
        return "router"
    return "execute_stage"


def route_after_poll(state: StoryState) -> str:
    """Conditional edge after poll_completion: review or router (if error)."""
    if state.get("_cancelled"):
        return "__end__"
    if state.get("last_error"):
        return "router"
    return "review_stage"


def route_from_router(state: StoryState) -> str:
    action = state.get("_next_action", "fail")
    if state.get("_cancelled"):
        return "__end__"
    return action


def route_after_advance(state: StoryState) -> str:
    """Conditional edge after advance: router (if error), end, or next plan_stage."""
    if state.get("_cancelled"):
        return "__end__"
    if state.get("last_error"):
        return "router"
    if state.get("status") == "completed":
        return "__end__"
    return "plan_stage"
