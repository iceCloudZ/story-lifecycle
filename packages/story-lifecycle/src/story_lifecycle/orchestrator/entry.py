"""TUI entry — CLI exit state detection (used by debug_packet).

历史背景:本模块曾包含 stage done 判定 / SessionBackend / decide_enter_action /
decide_resume_action 等 TUI 入口决策逻辑。这些符号零生产引用(只在
test_entry_decisions 内部自测),随死代码清理删除。仅保留 debug_packet 用的
``resolve_cli_exit_state`` + ``CliExitState`` + 其依赖(``validate_stage_done`` /
``DoneStatus`` / ``cli_exit_marker_path``)。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


def stage_done_file(story: dict) -> Path:
    """Story 级 done 文件路径(供 validate_stage_done 用)。"""
    from ..infra.paths import stage_done_file as _stage_done_file

    ws = story.get("workspace", "")
    key = story.get("story_key", "")
    stage = story.get("current_stage", "")
    return _stage_done_file(ws, key, stage)


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


class CliExitState(Enum):
    EXITED_WITHOUT_DONE = "exited_without_done"
    NONE = "none"
    UNKNOWN = "unknown"
