"""Orchestrator node helpers — shared utilities for story execution.

Previously held LangGraph node implementations (plan_stage_node, execute_and_wait_node,
review_stage_node, router_node, advance_node) and LangGraph-specific routing/state.
These have been replaced by the Agent-driven execution loop in planner.py.

This module now only re-exports shared utilities used across the codebase.
"""

from pathlib import Path

# ---- Module-level attributes (tests access nodes.planner, nodes.ttyd, etc.) ----
from .. import planner as planner  # noqa: F401
from .. import router as llm_router  # noqa: F401
from ...terminal import ttyd as ttyd
from ..notify import send as notify  # noqa: F401
from ..evaluator_loop import AdversarialConfig as AdversarialConfig

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

# ---- Subtask delegation ----
from .subtask_delegate import (
    _delegate_subtasks as _delegate_subtasks,
)

# ---- Error handling ----
from .errors import NodeError as NodeError

# ---- Knowledge management ----
from .knowledge import (
    _check_pattern_recurrence as _check_pattern_recurrence,
    _update_knowledge as _update_knowledge,
)

# ---- Prompt rendering ----
from .prompt_renderer import (
    _strip_planner_contract_duplicates as _strip_planner_contract_duplicates,
    _build_stage_contract as _build_stage_contract,
    _build_plan_executor_prompt as _build_plan_executor_prompt,
    _render_prompt as _render_prompt,
    _derive_relevance_tags as _derive_relevance_tags,
    _build_prd_task_section as _build_prd_task_section,
)

# ---- Constants (previously from state.py, now defined here) ----
STORY_HOME = Path.home() / ".story-lifecycle"
TIMEOUT_SECONDS = 30 * 60  # 30 minutes per stage
POLL_INTERVAL = 15  # seconds between poll checks
MAX_REVIEW_RETRIES = 3
