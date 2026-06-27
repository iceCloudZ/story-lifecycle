"""Story execution state — TypedDict shared across helpers.

Previously part of the LangGraph 5-node architecture. Now kept as a
plain TypedDict for backward compatibility with stage_resolver, prompt_renderer, etc.
"""

from pathlib import Path
from typing import TypedDict, Optional

TIMEOUT_SECONDS = 30 * 60  # 30 minutes per stage
POLL_INTERVAL = 15  # seconds between poll checks
STORY_HOME = Path.home() / ".story-lifecycle"
MAX_REVIEW_RETRIES = 3


class StoryState(TypedDict, total=False):
    story_key: str
    title: str
    workspace: str
    profile: str
    current_stage: str
    status: str
    complexity: str
    context: dict
    execution_count: int
    last_error: Optional[str]
    stage_start_time: float
    _execution_mode: Optional[str]
    _waiting_for_agent: bool

    # Smart Orchestrator fields
    plan_summary: Optional[str]
    review_summary: Optional[str]
    trajectory_score: Optional[float]
    plan: Optional[dict]

    # Routing — single output field for conditional edges
    _next_action: Optional[str]

    # Cancellation
    _epoch: int
    _cancelled: bool
    _pending_sub_keys: Optional[list]

    # Resolved profile (parsed once at start)
    _resolved_profile: Optional[dict]
