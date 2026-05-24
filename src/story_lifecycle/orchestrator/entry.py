"""TUI entry decision logic — .done helpers, SessionBackend, state resolver, action decider."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
# Future layers will import Literal, Protocol here


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
