"""Planning state — checkpoint/resume for long planning flows."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

STATE_FILE = "state.json"
PLANNING_DIR = ".story" / "planning"


def _state_path(cwd: str | Path | None = None) -> Path:
    root = Path(cwd) if cwd else Path.cwd()
    return root / PLANNING_DIR / STATE_FILE


def load_state(cwd: str | Path | None = None) -> dict | None:
    """Load planning state. Returns None if no state file exists."""
    path = _state_path(cwd)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to load planning state: %s", e)
        return None


def save_state(state: dict, cwd: str | Path | None = None) -> None:
    """Save planning state. Creates .story/planning/ if needed."""
    path = _state_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    state["last_updated"] = datetime.now().isoformat()
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def update_step(
    step: str, context: dict | None = None, cwd: str | Path | None = None
) -> dict:
    """Mark a step as completed and advance current_step."""
    state = load_state(cwd) or {
        "current_step": "",
        "completed_steps": [],
        "context": {},
    }
    if step not in state["completed_steps"]:
        state["completed_steps"].append(step)
    state["current_step"] = step
    if context:
        state["context"].update(context)
    save_state(state, cwd)
    return state


def clear_state(cwd: str | Path | None = None) -> None:
    """Remove the planning state file."""
    path = _state_path(cwd)
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        log.warning("Failed to clear planning state: %s", e)


def get_resume_info(cwd: str | Path | None = None) -> dict | None:
    """Get resume info if there's an incomplete planning flow.

    Returns None if no state, or dict with current_step and completed_steps.
    """
    state = load_state(cwd)
    if not state:
        return None
    return {
        "current_step": state.get("current_step", ""),
        "completed_steps": state.get("completed_steps", []),
        "context": state.get("context", {}),
    }
