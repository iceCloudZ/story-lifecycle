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

    # Smart Orchestrator fields
    plan_summary: Optional[str]
    review_summary: Optional[str]
    trajectory_score: Optional[float]
    plan: Optional[dict]
    _next_action: Optional[str]
    _pending_sub_keys: Optional[list]
    _router_decision: Optional[dict]
    _pre_routed_action: Optional[str]

    # Run epoch — for stale-thread cancellation
    _epoch: int
    _cancelled: bool
