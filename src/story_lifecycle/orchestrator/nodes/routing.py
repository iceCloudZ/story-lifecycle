from .state import StoryState


def route_after_plan(state: StoryState) -> str:
    """Conditional edge after plan_stage: skip, blocked, execute, or end."""
    if state.get("_cancelled"):
        return "__end__"
    if state.get("status") == "skipping":
        return "router"  # router will handle skip internally
    if state.get("status") == "waiting_subtasks":
        return "__end__"
    if state.get("last_error") and not state.get("plan_summary"):
        return "router"  # plan failed, let router decide
    return "execute_and_wait"


def route_after_execute(state: StoryState) -> str:
    """Conditional edge after execute_and_wait: review or router (if error)."""
    if state.get("_cancelled"):
        return "__end__"
    if state.get("last_error"):
        return "router"
    if state.get("_waiting_for_agent"):
        return "__end__"
    return "review_stage"


def route_from_router(state: StoryState) -> str:
    """Read _next_action set by router_node."""
    if state.get("_cancelled"):
        return "__end__"
    action = state.get("_next_action", "__end__")
    return action


def route_after_advance(state: StoryState) -> str:
    """Conditional edge after advance: next plan_stage or end."""
    if state.get("_cancelled"):
        return "__end__"
    if state.get("last_error"):
        return "router"
    if state.get("status") == "completed":
        return "__end__"
    return "plan_stage"
