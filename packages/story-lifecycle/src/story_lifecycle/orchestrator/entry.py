"""TUI entry decision logic — .done helpers, SessionBackend, action decider."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol


# ---------------------------------------------------------------------------
# Layer 1: .done helpers
# ---------------------------------------------------------------------------


def stage_done_file(story: dict) -> Path:
    from ..infra.paths import stage_done_file as _stage_done_file

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

    from ..infra.json_helpers import robust_json_parse

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

    from ..infra.story_paths import safe_segment

    return Path(gettempdir()) / f"story-exit-{safe_segment(story_key)}"


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
        from ..infra.terminal import ttyd

        return ttyd.session_alive(session_id)

    def resolve_session_state(self, session_id: str) -> str:
        from ..infra.terminal import ttyd

        return ttyd.resolve_session_state(session_id)

    def attach_foreground(self, session_id: str) -> list[str]:
        from ..infra.terminal import ttyd

        return ttyd.attach_args(session_id)

    def launch_independent_terminal(
        self, story_key: str, workspace: str, launch_cmd: str, prompt_file: str
    ) -> None:
        from ..infra.terminal import ttyd

        ttyd.launch_cli(story_key, workspace, launch_cmd, prompt_file)


# ---------------------------------------------------------------------------
# Layer 3: Action decider — story status driven, graph/session deferred
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
    SHOW_STARTING = "show_starting"
    RETRY_REVIEW = "retry_review"
    NOOP = "noop"


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
    from ..infra.terminal import ttyd

    return ttyd.session_name(story.get("story_key", ""))


def decide_enter_action(
    story: dict,
    backend: SessionBackend,
    is_running: bool,
    workspace_state: WorkspaceState | None = None,
) -> StageEntryAction:
    """Decide what to do when user wants to enter the terminal.

    Decision tree: story status → workspace → .done state → graph/session.
    """
    status = story.get("status", "")

    # 1. finished
    if status in _FINISHED_STATUSES:
        return StageEntryAction.SHOW_STATUS

    # 2. workspace blocked
    if workspace_state == WorkspaceState.LOCKED_BY_OTHER:
        return StageEntryAction.SHOW_WORKSPACE_BUSY

    # 3. .done corrupted
    validation = validate_stage_done(story)
    if validation.status == DoneStatus.CORRUPTED:
        return StageEntryAction.PROMPT_FIX_DONE

    # 4. gate wait
    if _is_in_gate_wait(story):
        return StageEntryAction.SHOW_GATE_STATUS

    # 5. session state
    session_id = _session_id_for_story(story)
    session = backend.resolve_session_state(session_id)

    if session == "live":
        return StageEntryAction.ATTACH
    if session == "unknown":
        return StageEntryAction.SHOW_SESSION_UNKNOWN

    # 6. running but no session → starting
    if is_running and session == "missing":
        return StageEntryAction.SHOW_STARTING

    # 7. idle or dead session → prompt to start
    return StageEntryAction.PROMPT_PRESS_R


def decide_resume_action(
    story: dict,
    backend: SessionBackend,
    is_running: bool,
    workspace_state: WorkspaceState | None = None,
) -> StageEntryAction:
    """Decide what to do when user wants to resume/start the story.

    Decision tree: story status → workspace → .done state → gate → graph/session.
    """
    status = story.get("status", "")

    # 1. finished
    if status in _FINISHED_STATUSES:
        return StageEntryAction.SHOW_STATUS

    # 2. workspace blocked
    if workspace_state == WorkspaceState.LOCKED_BY_OTHER:
        return StageEntryAction.SHOW_WORKSPACE_BUSY

    # 3. .done corrupted
    validation = validate_stage_done(story)
    if validation.status == DoneStatus.CORRUPTED:
        return StageEntryAction.PROMPT_FIX_DONE

    # 4. gate wait → retry review
    if _is_in_gate_wait(story):
        return StageEntryAction.RETRY_REVIEW

    # 5. .done ok → consume and advance
    if validation.status == DoneStatus.OK:
        return StageEntryAction.CONSUME_DONE_RESUME

    # 6. CLI exited without .done
    if resolve_cli_exit_state(story) == CliExitState.EXITED_WITHOUT_DONE:
        return StageEntryAction.START_OR_RESUME

    # 7. graph running → check session
    if is_running:
        session_id = _session_id_for_story(story)
        session = backend.resolve_session_state(session_id)
        if session == "exited":
            return StageEntryAction.CLEANUP_DEAD_AND_RESTART
        if session == "unknown":
            return StageEntryAction.SHOW_SESSION_UNKNOWN
        # live or missing while running = already running / starting
        return StageEntryAction.SHOW_RUNNING

    # 8. idle → check session
    session_id = _session_id_for_story(story)
    session = backend.resolve_session_state(session_id)
    if session == "exited":
        return StageEntryAction.CLEANUP_DEAD_AND_START
    if session == "live":
        return StageEntryAction.START_OR_RESUME
    # missing → normal start
    return StageEntryAction.START_OR_RESUME


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
        StageEntryAction.SHOW_SESSION_UNKNOWN: "无法确定 session 状态，请检查 Zellij 是否正常。",
        StageEntryAction.SHOW_CLI_EXIT_ERROR: f"CLI 进程异常退出（stage: {stage}），按 r 重新启动。",
        StageEntryAction.SHOW_STARTING: "Session 正在启动中，请稍候再按 e 进入终端。",
        StageEntryAction.SHOW_GATE_STATUS: (
            f"Story {key} 被 review gate 阻塞（{story.get('last_error', '')}）。"
            f"按 r 重试 review，R 重试 stage，A 接受风险推进。"
        ),
        StageEntryAction.RETRY_REVIEW: f"重试 review for {key}...",
        StageEntryAction.PROMPT_KEY_EXISTS: f"Story key {key} 已存在，请使用现有 story 或换 key。",
        StageEntryAction.NOOP: "当前状态无需操作。",
    }.get(action)
