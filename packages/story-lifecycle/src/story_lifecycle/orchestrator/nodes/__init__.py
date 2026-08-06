"""Orchestrator node helpers — shared utilities for story execution.

Previously held LangGraph node implementations (plan_stage_node, execute_and_wait_node,
review_stage_node, router_node, advance_node) and LangGraph-specific routing/state.
These have been replaced by the Agent-driven execution loop in planner.py.

ISS-005 removed the last LangGraph leftovers: stage_resolver, subtask_delegate,
knowledge, errors (NodeError), and state (StoryState TypedDict). This module
now only re-exports the still-live shared utilities.

ISS-006 moved `json_helpers` (robust_json_parse) up to the top-level infra
module `story_lifecycle.json_helpers`; callers import it directly from there.
"""

# ---- Module-level attributes (tests access nodes.planner, nodes.ttyd, etc.) ----
from ..engine import planner as planner  # noqa: F401
from ..engine import router as llm_router  # noqa: F401
from ...infra.terminal import ttyd as ttyd
from ..engine.notify import send as notify  # noqa: F401

# ---- Config loaders (used by 5+ external files) ----
from ..engine.profile_loader import (
    load_profile as load_profile,
    get_stage_config as get_stage_config,
)

# ---- Prompt rendering (used by cli/main.py dry-run) ----
from ..engine.prompt_renderer import _render_prompt as _render_prompt
