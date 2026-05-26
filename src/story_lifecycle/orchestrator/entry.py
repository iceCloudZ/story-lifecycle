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
    from .paths import stage_done_file as _stage_done_file

    ws = story.get("workspace", "")
    key = story.get("story_key", "")
    stage = story.get("current_stage", "")
    return _stage_done_file(ws, key, stage)


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
    except Exception as exc:
        return DoneValidationResult(status=DoneStatus.CORRUPTED, error=str(exc))

    if not isinstance(data, dict) or not data:
        return DoneValidationResult(
            status=DoneStatus.CORRUPTED,
            error=f".done file parsed but contains no data: {done}",
        )

    return DoneValidationResult(status=DoneStatus.OK, data=data)


def cli_exit_marker_path(story_key: str) -> Path:
    """Path to the CLI exit marker file for a story."""
    from tempfile import gettempdir

    return Path(gettempdir()) / f"story-exit-{story_key}"


def resolve_cli_exit_state(story: dict) -> CliExitState:
    """Check if the CLI process exited without writing .done."""
    marker = cli_exit_marker_path(story.get("story_key", ""))
    if not marker.exists():
        return CliExitState.NONE
    done = validate_stage_done(story)
    if done.status == DoneStatus.OK:
        return CliExitState.NONE
    return CliExitState.EXITED_WITHOUT_DONE


# ---------------------------------------------------------------------------
# Layer 2: SessionBackend
# ---------------------------------------------------------------------------


class SessionBackend(Protocol):
    def is_healthy(self, session_id: str) -> bool: ...
    def resolve_session_state(self, session_id: str) -> str: ...
    def attach_foreground(self, session_id: str) -> list[str]: ...
    def launch_independent_terminal(
        self, story_key: str, workspace: str, launch_cmd: str, prompt_file: str
    ) -> None: ...


class TtydSessionBackend:
    """Default implementation wrapping the ttyd module."""

    def is_healthy(self, session_id: str) -> bool:
        from ..terminal import ttyd

        return ttyd.session_alive(session_id)

    def resolve_session_state(self, session_id: str) -> str:
        from ..terminal import ttyd

        return ttyd.resolve_session_state(session_id)

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


class CliExitState(Enum):
    EXITED_WITHOUT_DONE = "exited_without_done"
    NONE = "none"
    UNKNOWN = "unknown"


class WorkspaceState(Enum):
    LOCKED_BY_SELF = "locked_by_self"
    LOCKED_BY_OTHER = "locked_by_other"
    FREE = "free"
    UNKNOWN = "unknown"


class StageEntryState(Enum):
    STORY_FINISHED = "story_finished"
    DONE_CORRUPTED = "done_corrupted"
    DONE_OK = "done_ok"
    CLI_EXITED_WITHOUT_DONE = "cli_exited_without_done"
    BLOCKED_BY_WORKSPACE = "blocked_by_workspace"
    RUNNING_WITH_LIVE_SESSION = "running_with_live_session"
    RUNNING_WITH_DEAD_SESSION = "running_with_dead_session"
    RUNNING_WITH_UNKNOWN_SESSION = "running_with_unknown_session"
    IDLE_WITH_LIVE_SESSION = "idle_with_live_session"
    IDLE_WITH_DEAD_SESSION = "idle_with_dead_session"
    IDLE = "idle"
    GATE_WAIT_CONFIRM = "gate_wait_confirm"
    UNKNOWN = "unknown"


class StageEntryAction(Enum):
    ATTACH = "attach"
    START_OR_RESUME = "start_or_resume"
    CONSUME_DONE_RESUME = "consume_done_resume"
    CLEANUP_DEAD_AND_START = "cleanup_dead_and_start"
    CLEANUP_DEAD_AND_RESTART = "cleanup_dead_and_restart"
    PROMPT_KEY_EXISTS = "prompt_key_exists"
    CONFIRM_AND_DESTROY = "confirm_and_destroy"
    PROMPT_PRESS_R = "prompt_press_r"
    PROMPT_FIX_DONE = "prompt_fix_done"
    SHOW_STATUS = "show_status"
    SHOW_RUNNING = "show_running"
    SHOW_WORKSPACE_BUSY = "show_workspace_busy"
    SHOW_SESSION_UNKNOWN = "show_session_unknown"
    SHOW_CLI_EXIT_ERROR = "show_cli_exit_error"
    SHOW_GATE_STATUS = "show_gate_status"
    RETRY_REVIEW = "retry_review"
    NOOP = "noop"


def resolve_stage_state(
    story: dict,
    backend: SessionBackend,
    is_running: bool,
    cli_exit_state: CliExitState | None = None,
    workspace_state: WorkspaceState | None = None,
) -> StageEntryState:
    status = story.get("status", "")

    # Priority 1: terminal story states
    if status in _FINISHED_STATUSES:
        return StageEntryState.STORY_FINISHED

    # Priority 2: .done corrupted
    validation = validate_stage_done(story)
    if validation.status == DoneStatus.CORRUPTED:
        return StageEntryState.DONE_CORRUPTED

    # Priority 2.5: Gate wait state (paused + has gate decision)
    if _is_in_gate_wait(story):
        return StageEntryState.GATE_WAIT_CONFIRM

    # Priority 3: .done ok
    if validation.status == DoneStatus.OK:
        return StageEntryState.DONE_OK

    # Priority 4: CLI exited without .done
    if cli_exit_state is None:
        cli_exit_state = resolve_cli_exit_state(story)
    if cli_exit_state == CliExitState.EXITED_WITHOUT_DONE:
        return StageEntryState.CLI_EXITED_WITHOUT_DONE

    # Priority 5: workspace blocked
    if workspace_state is None:
        workspace_state = WorkspaceState.FREE
    if workspace_state == WorkspaceState.LOCKED_BY_OTHER:
        return StageEntryState.BLOCKED_BY_WORKSPACE

    # Priority 6-7: graph running/not running + session state
    session_id = _session_id_for_story(story)
    session = backend.resolve_session_state(session_id)

    if is_running:
        if session == "live":
            return StageEntryState.RUNNING_WITH_LIVE_SESSION
        if session == "exited":
            return StageEntryState.RUNNING_WITH_DEAD_SESSION
        if session == "missing":
            return StageEntryState.RUNNING_WITH_DEAD_SESSION
        return StageEntryState.RUNNING_WITH_UNKNOWN_SESSION

    # graph not running
    if session == "live":
        return StageEntryState.IDLE_WITH_LIVE_SESSION
    if session == "exited":
        return StageEntryState.IDLE_WITH_DEAD_SESSION
    if session == "missing":
        return StageEntryState.IDLE

    return StageEntryState.UNKNOWN


def _is_in_gate_wait(story: dict) -> bool:
    """Check if a story is in gate-wait state based on context_json markers."""
    if story.get("status") != "paused":
        return False
    try:
        import json

        ctx = json.loads(story.get("context_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        return False
    return bool(ctx.get("last_gate_decision_id"))


def _session_id_for_story(story: dict) -> str:
    from ..terminal import ttyd

    return ttyd.session_name(story.get("story_key", ""))


_ACTION_TABLE: dict[tuple[StageEntryState, str], StageEntryAction] = {
    # STORY_FINISHED
    (StageEntryState.STORY_FINISHED, "e"): StageEntryAction.SHOW_STATUS,
    (StageEntryState.STORY_FINISHED, "r"): StageEntryAction.SHOW_STATUS,
    # DONE_CORRUPTED
    (StageEntryState.DONE_CORRUPTED, "e"): StageEntryAction.PROMPT_FIX_DONE,
    (StageEntryState.DONE_CORRUPTED, "r"): StageEntryAction.PROMPT_FIX_DONE,
    # DONE_OK
    (StageEntryState.DONE_OK, "e"): StageEntryAction.PROMPT_PRESS_R,
    (StageEntryState.DONE_OK, "r"): StageEntryAction.CONSUME_DONE_RESUME,
    # CLI_EXITED_WITHOUT_DONE
    (
        StageEntryState.CLI_EXITED_WITHOUT_DONE,
        "e",
    ): StageEntryAction.SHOW_CLI_EXIT_ERROR,
    (StageEntryState.CLI_EXITED_WITHOUT_DONE, "r"): StageEntryAction.START_OR_RESUME,
    # BLOCKED_BY_WORKSPACE
    (StageEntryState.BLOCKED_BY_WORKSPACE, "e"): StageEntryAction.SHOW_WORKSPACE_BUSY,
    (StageEntryState.BLOCKED_BY_WORKSPACE, "r"): StageEntryAction.SHOW_WORKSPACE_BUSY,
    # RUNNING_WITH_LIVE_SESSION
    (StageEntryState.RUNNING_WITH_LIVE_SESSION, "e"): StageEntryAction.ATTACH,
    (StageEntryState.RUNNING_WITH_LIVE_SESSION, "r"): StageEntryAction.SHOW_RUNNING,
    # RUNNING_WITH_DEAD_SESSION
    (StageEntryState.RUNNING_WITH_DEAD_SESSION, "e"): StageEntryAction.PROMPT_PRESS_R,
    (
        StageEntryState.RUNNING_WITH_DEAD_SESSION,
        "r",
    ): StageEntryAction.CLEANUP_DEAD_AND_RESTART,
    # RUNNING_WITH_UNKNOWN_SESSION
    (
        StageEntryState.RUNNING_WITH_UNKNOWN_SESSION,
        "e",
    ): StageEntryAction.SHOW_SESSION_UNKNOWN,
    (
        StageEntryState.RUNNING_WITH_UNKNOWN_SESSION,
        "r",
    ): StageEntryAction.SHOW_SESSION_UNKNOWN,
    # IDLE_WITH_LIVE_SESSION
    (StageEntryState.IDLE_WITH_LIVE_SESSION, "e"): StageEntryAction.ATTACH,
    (StageEntryState.IDLE_WITH_LIVE_SESSION, "r"): StageEntryAction.START_OR_RESUME,
    # IDLE_WITH_DEAD_SESSION
    (StageEntryState.IDLE_WITH_DEAD_SESSION, "e"): StageEntryAction.PROMPT_PRESS_R,
    (
        StageEntryState.IDLE_WITH_DEAD_SESSION,
        "r",
    ): StageEntryAction.CLEANUP_DEAD_AND_START,
    # IDLE
    (StageEntryState.IDLE, "e"): StageEntryAction.PROMPT_PRESS_R,
    (StageEntryState.IDLE, "r"): StageEntryAction.START_OR_RESUME,
    # GATE_WAIT_CONFIRM
    (StageEntryState.GATE_WAIT_CONFIRM, "e"): StageEntryAction.SHOW_GATE_STATUS,
    (StageEntryState.GATE_WAIT_CONFIRM, "r"): StageEntryAction.RETRY_REVIEW,
    # UNKNOWN
    (StageEntryState.UNKNOWN, "e"): StageEntryAction.SHOW_SESSION_UNKNOWN,
    (StageEntryState.UNKNOWN, "r"): StageEntryAction.SHOW_SESSION_UNKNOWN,
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


def entry_action_notice(action: StageEntryAction, story: dict) -> str | None:
    """Return a short user-visible notice for non-terminal entry actions."""
    stage = story.get("current_stage", "")
    key = story.get("story_key", "")

    return {
        StageEntryAction.PROMPT_PRESS_R: "没有运行中的 session，按 r 启动或恢复执行。",
        StageEntryAction.PROMPT_FIX_DONE: ".done 文件损坏，请修复或删除后重试。",
        StageEntryAction.SHOW_STATUS: f"Story {key} 已结束（{story.get('status', '')}），不可操作。",
        StageEntryAction.SHOW_RUNNING: f"Story {key} 正在运行中，AI session 健康，无需重复启动。",
        StageEntryAction.SHOW_WORKSPACE_BUSY: "Workspace 被其他 story 占用，请等待完成后再试。",
        StageEntryAction.SHOW_SESSION_UNKNOWN: "无法确定 session 状态，请检查 Zellij/tmux 是否正常。",
        StageEntryAction.SHOW_CLI_EXIT_ERROR: f"CLI 进程异常退出（stage: {stage}），按 r 重新启动。",
        StageEntryAction.SHOW_GATE_STATUS: (
            f"Story {key} 被 review gate 阻塞（{story.get('last_error', '')}）。"
            f"按 r 重试 review，R 重试 stage，A 接受风险推进。"
        ),
        StageEntryAction.RETRY_REVIEW: f"重试 review for {key}...",
        StageEntryAction.PROMPT_KEY_EXISTS: f"Story key {key} 已存在，请使用现有 story 或换 key。",
        StageEntryAction.NOOP: "当前状态无需操作。",
    }.get(action)
