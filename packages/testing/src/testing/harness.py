"""Real-AI story harness — programmatically run a story through the live orchestrator.

Unlike ``tests/integration/e2e_story_runner`` (which mocks the AI CLI), this
harness drives the **real** story-lifecycle API: it seeds a story + action list,
then lets ``planner.continue_orchestrator_agent`` launch a genuine
Claude/Codex CLI via the adapter's ``interactive_launch_cmd``. No ``unittest.mock``
patching of the adapter or PTY layer.

The two story-lifecycle entry points it depends on:

* ``orchestrator.service.create_and_start_story`` — creates the DB row, writes
  the PRD into ``<workspace>/.story/context/<key>/``, sets ``current_stage``.
* ``orchestrator.planner.continue_orchestrator_agent`` — walks the
  ``_agent_actions`` list, and for each ``launch`` action builds the stage
  prompt and calls ``ensure_agent_pty(story_key, launch_cmd, workspace, prompt)``
  which spawns the real AI CLI.

The harness is **blocking and synchronous**: it runs ``continue_orchestrator_agent``
in-process (it is itself synchronous; the async ``run_story``/``start_story_async``
in ``graph.py`` just submit this to a thread pool) and polls ``db.get_story``
between stages so the caller can assert per-stage artifacts.

A full real run needs the ``claude`` (or ``codex``) CLI on PATH plus API
credentials. When those are absent, ``run_real_story`` raises
``HarnessError`` with a clear message — the test layer above marks the test
``real_e2e`` so it never runs in the default suite.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("testing.harness")

try:
    # Best-effort reuse of the canonical sanitize helper when the
    # story-lifecycle package is importable (editable install in the monorepo).
    from story_lifecycle.infra.story_paths import safe_segment
except ImportError:  # pragma: no cover - testing package standalone fallback
    import re

    def safe_segment(value: str) -> str:  # type: ignore[misc]
        cleaned = re.sub(r"[^\w.-]+", "-", value or "", flags=re.UNICODE).strip("-_").rstrip(".")
        if "/" in cleaned or "\\" in cleaned or cleaned in {"..", "."}:
            raise ValueError(f"refusing unsafe path segment: {value!r}")
        return cleaned or "story"


class HarnessError(RuntimeError):
    """Raised when the real-AI harness cannot run (missing CLI/key, bad contract)."""


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------


@dataclass
class StageResult:
    """Outcome of a single stage after the AI was launched for it."""

    stage: str
    story_key: str
    done_file: Path
    story_snapshot: dict
    error: str | None = None


@dataclass
class StoryRunResult:
    """Aggregate outcome of a real story run across all configured stages."""

    story_key: str
    workspace: str
    stages: list[StageResult] = field(default_factory=list)
    final_story: dict | None = None

    def stage(self, name: str) -> StageResult | None:
        for s in self.stages:
            if s.stage == name:
                return s
        return None


# ---------------------------------------------------------------------------
# Action-list construction
# ---------------------------------------------------------------------------


def build_agent_actions(
    story_key: str,
    stages: list[str],
    *,
    adapter: str = "claude",
    focus_per_stage: dict[str, str] | None = None,
) -> list[dict]:
    """Build the ``_agent_actions`` list the orchestrator walks.

    Mirrors the shape used by ``planner.run_orchestrator_agent`` /
    ``continue_orchestrator_agent``: one ``launch`` action per stage, each
    pointing at its own ``.story/done/<key>/<stage>.json`` done file.
    """
    focus_per_stage = focus_per_stage or {}
    actions: list[dict] = []
    for i, stage in enumerate(stages):
        actions.append(
            {
                "action": "launch",
                "adapter": adapter,
                "stage": stage,
                "focus": focus_per_stage.get(stage, f"Real E2E: {stage} step"),
                # done_file path is relative to the workspace, matching the
                # convention continue_orchestrator_agent expects.
                "done_file": f".story/done/{safe_segment(story_key)}/{stage}.json",
            }
        )
    return actions


# ---------------------------------------------------------------------------
# Pre-flight: confirm a real AI CLI is available
# ---------------------------------------------------------------------------


def _ai_cli_available(adapter: str) -> tuple[bool, str]:
    """Best-effort check that the requested AI CLI binary is on PATH.

    Returns (available, detail). This is advisory — a missing CLI is surfaced
    as a clear HarnessError rather than a cryptic PTY spawn failure deep in
    the orchestrator.
    """
    import shutil

    from story_lifecycle.adapters import get_adapter

    try:
        ad = get_adapter(adapter)
    except Exception as exc:  # unknown adapter name
        return False, f"adapter '{adapter}' not registered: {exc}"

    try:
        cmd = ad.interactive_launch_cmd(model="")
    except Exception as exc:
        return False, f"interactive_launch_cmd failed: {exc}"

    # interactive_launch_cmd returns list[str] (e.g. ["claude"]) for claude.
    binary = cmd[0] if isinstance(cmd, (list, tuple)) else str(cmd)
    # Resolve through story-lifecycle's own executable resolver when present,
    # else fall back to PATH lookup.
    try:
        from story_lifecycle.terminal.platform_ops import resolve_executable

        resolved = resolve_executable(binary)
        if resolved and shutil.which(resolved):
            return True, resolved
    except Exception:
        pass

    found = shutil.which(binary)
    if found:
        return True, found
    return False, f"AI CLI '{binary}' not found on PATH"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_real_story(
    workspace: str | Path,
    story_key: str,
    prd_path: str | Path,
    *,
    stages: list[str] | None = None,
    profile: str = "minimal",
    title: str = "Real E2E story",
    adapter: str = "claude",
    headless: bool = True,
    check_ai_cli: bool = True,
    per_stage_callback: Callable[[StageResult], None] | None = None,
) -> StoryRunResult:
    """Create and run a story against the REAL story-lifecycle orchestrator + AI CLI.

    Parameters
    ----------
    workspace:
        Directory the AI operates in (must be a git repo for
        ``reset_workspace`` red-green reset; see ``workspace.py``).
    story_key:
        Unique story key (also used to locate ``.story/done/<key>/*.json`` and
        to link the miner session via ``sessions.story_id``).
    prd_path:
        Path to the PRD markdown; copied into the story evidence dir.
    stages:
        Ordered stage names to run (e.g. ``["design", "implement", "verify"]``).
        Defaults to ``["design", "implement", "verify"]``.
    profile:
        story-lifecycle profile name. ``"minimal"`` is design→build→verify; use
        a custom profile if you need different stage names.
    adapter:
        Real AI adapter name (``"claude"`` or ``"codex"``).
    check_ai_cli:
        When True (default), fail fast with HarnessError if the AI CLI binary
        is not on PATH. Set False to let the test exercise the full launch path
        even if the binary is missing (PTY spawn will then fail naturally).
    per_stage_callback:
        Invoked after each stage's done-file is consumed, with the StageResult.
        The test layer uses this to run per-stage assertions.

    Returns
    -------
    StoryRunResult with per-stage snapshots and the final story row.

    Raises
    ------
    HarnessError if the AI CLI is unavailable (and check_ai_cli) or the
    story-lifecycle API contract is violated.
    """
    # Local imports: keeps the module importable even if a dependency is not
    # yet on sys.path, and surfaces real-AI requirement lazily.
    from story_lifecycle.db import models as db
    from story_lifecycle.orchestrator.service import create_and_start_story
    from story_lifecycle.orchestrator import planner

    workspace = str(Path(workspace).resolve())
    stages = list(stages or ["design", "implement", "verify"])
    if not stages:
        raise HarnessError("stages must be non-empty")

    # --- Pre-flight: real AI CLI present? -----------------------------------
    if check_ai_cli:
        ok, detail = _ai_cli_available(adapter)
        if not ok:
            raise HarnessError(
                f"Real AI CLI not available for adapter '{adapter}': {detail}. "
                f"Install the {adapter} CLI and set credentials, or run this test "
                f"explicitly (it is marked real_e2e and skipped by default)."
            )

    # --- 1. Create the story (DB row + PRD evidence) ------------------------
    try:
        create_and_start_story(
            story_key=story_key,
            title=title,
            profile=profile,
            workspace=workspace,
            prd_path=str(prd_path),
        )
    except Exception as exc:
        raise HarnessError(f"create_and_start_story failed: {exc}") from exc

    # --- 2. Seed the agent action list + confirm plan -----------------------
    actions = build_agent_actions(story_key, stages, adapter=adapter)
    story = db.get_story(story_key)
    if not story:
        raise HarnessError(f"story {story_key} not found after create")
    ctx: dict[str, Any] = {}
    try:
        ctx = json.loads(story.get("context_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        ctx = {}
    ctx["_agent_actions"] = actions
    ctx["_plan_confirmed"] = True
    db.update_story(
        story_key,
        context_json=json.dumps(ctx, ensure_ascii=False),
        status="active",
        current_stage=stages[0],
    )

    # --- 3. Run the orchestrator in-process (real AI launches) -------------
    # continue_orchestrator_agent is synchronous: it walks _agent_actions and
    # blocks until the final stage's done file is consumed. We invoke it
    # directly rather than via graph.start_story_async (which only wraps it in
    # a thread-pool submit) so the test can capture exceptions inline.
    try:
        planner.continue_orchestrator_agent(story_key, headless=headless)
    except Exception as exc:
        log.exception("continue_orchestrator_agent raised for %s", story_key)
        # Snapshot whatever stages completed before the failure.
        result = StoryRunResult(story_key=story_key, workspace=workspace)
        result.final_story = db.get_story(story_key)
        for stage in stages:
            done = Path(workspace) / ".story" / "done" / safe_segment(story_key) / f"{stage}.json"
            result.stages.append(
                StageResult(
                    stage=stage,
                    story_key=story_key,
                    done_file=done,
                    story_snapshot=result.final_story or {},
                    error=str(exc),
                )
            )
        return result

    # --- 4. Collect per-stage snapshots -------------------------------------
    final = db.get_story(story_key)
    result = StoryRunResult(story_key=story_key, workspace=workspace, final_story=final)
    for stage in stages:
        done = Path(workspace) / ".story" / "done" / safe_segment(story_key) / f"{stage}.json"
        snap = final or {}
        # If the story advanced past this stage, snapshot reflects completion;
        # the per-stage assertion reads the done file + workspace artifacts.
        sr = StageResult(
            stage=stage,
            story_key=story_key,
            done_file=done,
            story_snapshot=snap,
        )
        result.stages.append(sr)
        if per_stage_callback:
            try:
                per_stage_callback(sr)
            except Exception:
                # Let the assertion error propagate but still keep collecting.
                log.exception("per_stage_callback raised for stage %s", stage)
                raise

    return result


# ---------------------------------------------------------------------------
# Convenience: wait helper for the (rare) async path
# ---------------------------------------------------------------------------


def wait_for_story_terminal(
    story_key: str, *, timeout: float = 1800.0, poll: float = 2.0
) -> dict:
    """Poll db.get_story until status is terminal (completed/failed/blocked).

    Only needed if the harness ever switches to ``graph.start_story_async``.
    The default synchronous path does not require this.
    """
    from story_lifecycle.db import models as db

    terminal = {"completed", "failed", "blocked", "aborted"}
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = db.get_story(story_key) or {}
        if last.get("status") in terminal:
            return last
        time.sleep(poll)
    return last
