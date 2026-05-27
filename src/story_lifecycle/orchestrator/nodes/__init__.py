"""LangGraph node implementations — plan, execute, poll, review, advance, skip, retry, fail.

This package re-exports everything from sub-modules for backward compatibility.
All existing ``from .nodes import X`` imports continue to work.
"""

# ---- Module-level attributes (tests access nodes.planner, nodes.interrupt, etc.) ----
from .. import planner as planner
from .. import router as router
from langgraph.types import interrupt as interrupt
from langgraph.errors import GraphInterrupt as GraphInterrupt

# ---- State & constants ----
from .state import (
    StoryState as StoryState,
    TIMEOUT_SECONDS as TIMEOUT_SECONDS,
    POLL_INTERVAL as POLL_INTERVAL,
    STORY_HOME as STORY_HOME,
    MAX_REVIEW_RETRIES as MAX_REVIEW_RETRIES,
)

# ---- Config loaders (used by 5+ external files) ----
from .profile_loader import (
    load_profile as load_profile,
    get_stage_config as get_stage_config,
)

# ---- JSON parsing (used by debug_packet.py, entry.py) ----
from .json_helpers import (
    robust_json_parse as robust_json_parse,
    _extract_json_object as _extract_json_object,
)

# ---- Stage resolution helpers ----
from .stage_resolver import (
    _is_cancelled as _is_cancelled,
    _block_for_planner as _block_for_planner,
    resolve_next_stage as resolve_next_stage,
)

# ---- Routing functions (used by graph.py) ----
from .routing import (
    route_after_plan as route_after_plan,
    route_after_poll as route_after_poll,
    route_from_router as route_from_router,
    route_after_advance as route_after_advance,
)

# ---- Subtask delegation ----
from .subtask_delegate import _delegate_subtasks as _delegate_subtasks

# ---- Knowledge management ----
from .knowledge import (
    _check_pattern_recurrence as _check_pattern_recurrence,
    _update_knowledge as _update_knowledge,
)

# ---- Prompt rendering (used by tui.py, main.py) ----
from .prompt_renderer import (
    _strip_planner_contract_duplicates as _strip_planner_contract_duplicates,
    _build_stage_contract as _build_stage_contract,
    _build_plan_executor_prompt as _build_plan_executor_prompt,
    _render_prompt as _render_prompt,
    _derive_relevance_tags as _derive_relevance_tags,
    _build_prd_task_section as _build_prd_task_section,
)

# ---- Graph node functions (used by graph.py) ----
from .graph_nodes import (
    plan_stage_node as plan_stage_node,
    execute_stage_node as execute_stage_node,
    poll_completion_node as poll_completion_node,
    review_stage_node as review_stage_node,
    router_node as router_node,
    advance_node as advance_node,
    retry_node as retry_node,
    skip_node as skip_node,
    fail_node as fail_node,
    wait_confirm_node as wait_confirm_node,
)
