"""TUI entry decision logic — .done helpers, SessionBackend, state resolver, action decider."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal, Protocol


# ---------------------------------------------------------------------------
# Layer 1: .done helpers
# ---------------------------------------------------------------------------


def stage_done_file(story: dict) -> Path:
    ws = story.get("workspace", "")
    key = story.get("story_key", "")
    stage = story.get("current_stage", "")
    return Path(ws) / ".story-done" / key / f"{stage}.json"


def has_stage_done(story: dict) -> bool:
    return stage_done_file(story).exists()


class DoneStatus(Enum):
    OK = "ok"
    CORRUPTED = "corrupted"
    MISSING = "missing"


@dataclass
class DoneValidationResult:
    status: DoneStatus
    data: dict | None = None
    error: str | None = None


def validate_stage_done(story: dict) -> DoneValidationResult:
    done = stage_done_file(story)
    if not done.exists():
        return DoneValidationResult(status=DoneStatus.MISSING)

    from .nodes import robust_json_parse

    try:
        data = robust_json_parse(done)
        return DoneValidationResult(status=DoneStatus.OK, data=data)
    except Exception as exc:
        return DoneValidationResult(status=DoneStatus.CORRUPTED, error=str(exc))


# ---------------------------------------------------------------------------
# Layer 2: SessionBackend
# ---------------------------------------------------------------------------


class SessionBackend(Protocol):
    def is_healthy(self, session_id: str) -> bool: ...
    def attach_foreground(self, session_id: str) -> list[str]: ...
    def launch_independent_terminal(
        self, story_key: str, workspace: str, launch_cmd: str, prompt_file: str
    ) -> None: ...


class TtydSessionBackend:
    """Default implementation wrapping the ttyd module."""

    def is_healthy(self, session_id: str) -> bool:
        from ..terminal import ttyd

        return ttyd.session_alive(session_id)

    def attach_foreground(self, session_id: str) -> list[str]:
        from ..terminal import ttyd

        return ttyd.attach_args(session_id)

    def launch_independent_terminal(
        self, story_key: str, workspace: str, launch_cmd: str, prompt_file: str
    ) -> None:
        from ..terminal import ttyd

        ttyd.launch_cli(story_key, workspace, launch_cmd, prompt_file)


# ---------------------------------------------------------------------------
# Layer 3: State resolver + action decider
# ---------------------------------------------------------------------------


_FINISHED_STATUSES = frozenset({"completed", "failed", "aborted"})


class StageEntryState(Enum):
    DONE = "done"
    DONE_CORRUPTED = "done_corrupted"
    RUNNING_HEALTHY = "running_healthy"
    RUNNING_DEAD = "running_dead"
    IDLE = "idle"
    STORY_FINISHED = "story_finished"


class StageEntryAction(Enum):
    ATTACH = "attach"
    START_OR_RESUME = "start_or_resume"
    PROMPT_PRESS_R = "prompt_press_r"
    PROMPT_DONE_PRESS_R = "prompt_done_press_r"
    PROMPT_FIX_DONE = "prompt_fix_done"
    NOOP = "noop"


def resolve_stage_state(
    story: dict,
    backend: SessionBackend,
    is_running: bool,
) -> StageEntryState:
    status = story.get("status", "")

    if status in _FINISHED_STATUSES:
        return StageEntryState.STORY_FINISHED

    validation = validate_stage_done(story)
    if validation.status == DoneStatus.OK:
        return StageEntryState.DONE
    if validation.status == DoneStatus.CORRUPTED:
        return StageEntryState.DONE_CORRUPTED

    if is_running:
        session_id = _session_id_for_story(story)
        if backend.is_healthy(session_id):
            return StageEntryState.RUNNING_HEALTHY
        return StageEntryState.RUNNING_DEAD

    return StageEntryState.IDLE


def _session_id_for_story(story: dict) -> str:
    from ..terminal import ttyd

    return ttyd.session_name(story.get("story_key", ""))


_ACTION_TABLE: dict[tuple[StageEntryState, str], StageEntryAction] = {
    (StageEntryState.DONE, "e"): StageEntryAction.PROMPT_DONE_PRESS_R,
    (StageEntryState.DONE, "r"): StageEntryAction.START_OR_RESUME,
    (StageEntryState.DONE_CORRUPTED, "e"): StageEntryAction.PROMPT_FIX_DONE,
    (StageEntryState.DONE_CORRUPTED, "r"): StageEntryAction.PROMPT_FIX_DONE,
    (StageEntryState.RUNNING_HEALTHY, "e"): StageEntryAction.ATTACH,
    (StageEntryState.RUNNING_HEALTHY, "r"): StageEntryAction.NOOP,
    (StageEntryState.RUNNING_DEAD, "e"): StageEntryAction.PROMPT_PRESS_R,
    (StageEntryState.RUNNING_DEAD, "r"): StageEntryAction.START_OR_RESUME,
    (StageEntryState.IDLE, "e"): StageEntryAction.PROMPT_PRESS_R,
    (StageEntryState.IDLE, "r"): StageEntryAction.START_OR_RESUME,
    (StageEntryState.STORY_FINISHED, "e"): StageEntryAction.NOOP,
    (StageEntryState.STORY_FINISHED, "r"): StageEntryAction.NOOP,
}


def decide_action(
    state: StageEntryState,
    user_action: Literal["e", "r"],
) -> StageEntryAction:
    key = (state, user_action)
    if key not in _ACTION_TABLE:
        raise ValueError(
            f"No action for state={state.value!r} user_action={user_action!r}"
        )
    return _ACTION_TABLE[key]
