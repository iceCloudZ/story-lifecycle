"""Export (prompt, stage_meta, outcome, llm_calls, events) tuples for offline
prompt-quality analysis by an external AI (e.g. a separate LLM acting as judge).

Design rationale:
  - Prompt quality is fuzzy (focus clarity, constraint consistency, playbook
    fit). A deterministic pre-spawn judge both wastes tokens and misjudges
    structured conditions as if they were prose. Instead we expose the data
    here so an external AI can analyze "which prompt patterns correlate with
    stage failures / retries / long durations" offline, then feed findings
    back into template changes.
  - Per-stage granularity: one row per (story, stage) pair — the unit at
    which a prompt is assembled and a result is produced.
  - Filters narrow the corpus (status / stage / profile / since / limit).

No PII scrubbing — workspaces and titles are developer-visible. The endpoint
is read-only and intended for local/single-machine serve (matches the rest
of the API surface).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ...infra.db import models as db
from ...infra.story_paths import safe_story_path

log = logging.getLogger("story-lifecycle.prompt_export")


def _list_candidate_stories(
    *,
    status: str,
    profile: str,
    since: str,
    limit: int,
) -> list[dict]:
    """Fetch candidate stories for export.

    status filter accepts: "" / "all" (no filter) or a specific status value.
    `since` is ISO datetime string; "" means no lower bound.
    """
    # Status set resolution. Empty / "all" → every status except archived
    # (archived is typically uninteresting for prompt analysis).
    if status in ("", "all"):
        status_set: tuple[str, ...] = (
            "planning",
            "active",
            "paused",
            "completed",
            "failed",
            "aborted",
        )
    else:
        status_set = (status,)

    sql = "SELECT * FROM story WHERE status IN ({}) ".format(
        ",".join("?" * len(status_set))
    )
    params: list[Any] = list(status_set)
    if profile and profile != "all":
        sql += "AND profile = ? "
        params.append(profile)
    if since:
        sql += "AND updated_at >= ? "
        params.append(since)
    sql += "ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)

    with db._db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def _read_done_file(workspace: str, story_key: str, stage: str) -> dict | None:
    """Return parsed done.json content for a stage, or None if missing."""
    if not workspace:
        return None
    try:
        from ...infra.paths import stage_done_file

        done_path = stage_done_file(workspace, story_key, stage)
        if not done_path.exists():
            return None
        text = done_path.read_text(encoding="utf-8", errors="replace")
        data = json.loads(text)
        return data if isinstance(data, dict) else {"_raw": text}
    except Exception:
        return None


def _events_for_stage(events: list[dict], stage: str) -> list[dict]:
    """Filter story events to those tied to this stage (+ story-wide events)."""
    out = []
    for e in events:
        es = e.get("stage") or ""
        # empty stage = story-wide event (e.g. emergency_stop); always include
        if es == "" or es == stage:
            out.append(
                {
                    "event_type": e.get("event_type"),
                    "stage": es,
                    "created_at": e.get("created_at"),
                    "payload": e.get("payload"),
                }
            )
    return out


def _llm_calls_for_stage(calls: list[dict], stage: str) -> list[dict]:
    """Filter llm_calls (from get_story_llm_calls) to those tagged with stage.

    A call's stage comes from llm_trace.stage. Calls without a stage tag
    (story-level orchestrator decisions) are excluded — they're not stage-
    specific prompt inputs.
    """
    out = []
    for c in calls:
        if (c.get("stage") or "") != stage:
            continue
        out.append(
            {
                "id": c.get("id"),
                "operation": c.get("operation"),
                "model": c.get("model"),
                "prompt_text": c.get("prompt_text"),
                "response_text": c.get("response_text"),
                "reasoning_text": c.get("reasoning_text"),
                "tool_calls_json": c.get("tool_calls_json"),
                "prompt_tokens": c.get("prompt_tokens"),
                "completion_tokens": c.get("completion_tokens"),
                "total_tokens": c.get("total_tokens"),
                "duration_ms": c.get("duration_ms"),
                "success": c.get("success"),
                "error": c.get("error"),
                "created_at": c.get("created_at"),
            }
        )
    return out


def _build_stage_item(
    *,
    story: dict,
    stage: str,
    action: dict | None,
    events: list[dict],
    llm_calls: list[dict],
) -> dict | None:
    """Build one (story, stage) row for export. Returns None if no prompt file
    exists for this stage (stage never ran / was skipped)."""
    workspace = story.get("workspace", "")
    story_key = story["story_key"]
    prompt_dir = safe_story_path(workspace, ".story", "context", story_key)
    prompt_path = prompt_dir / f"prompt_{stage}.md"
    if not prompt_path.exists():
        return None

    try:
        prompt_text = prompt_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    done_data = _read_done_file(workspace, story_key, stage)

    # Outcome synthesis — combine story status + done file + per-stage events.
    # `completed_at` is the timestamp of the stage's "completed" event.
    stage_events = _events_for_stage(events, stage)
    completed_at = None
    for ev in stage_events:
        if ev.get("event_type") == "completed":
            completed_at = ev.get("created_at")
            break

    files_changed = []
    if isinstance(done_data, dict):
        fc = done_data.get("files_changed") or []
        if isinstance(fc, list):
            files_changed = [str(x) for x in fc]

    return {
        "stage": stage,
        "task_actions": (action or {}).get("task_actions") or [],
        "adapter": (action or {}).get("adapter"),
        "focus": (action or {}).get("focus") or "",
        "grill": bool((action or {}).get("grill")),
        "prompt_path": str(prompt_path),
        "prompt": prompt_text,
        "prompt_chars": len(prompt_text),
        "done_path": str(
            prompt_dir.parent.parent / "done" / story_key / f"{stage}.json"
        ),
        "done_status": (done_data or {}).get("status"),
        "done_summary": (done_data or {}).get("summary"),
        "done_files_changed": files_changed,
        "done_spec_path": (done_data or {}).get("spec_path"),
        "done_test_report_path": (done_data or {}).get("test_report_path"),
        "completed_at": completed_at,
        "events": stage_events,
        "llm_calls": _llm_calls_for_stage(llm_calls, stage),
    }


def export_prompt_analysis(
    *,
    status: str = "completed",
    stage: str = "",
    profile: str = "",
    since: str = "",
    limit: int = 50,
) -> dict:
    """Top-level export: return filtered (prompt, outcome, ...) items.

    See module docstring for the design rationale.
    """
    # Default `since` to 30 days ago when not specified — keeps payload bounded
    # for large corpora without forcing the caller to always pass it.
    if not since:
        from datetime import datetime, timedelta

        since = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d 00:00:00")

    stories = _list_candidate_stories(
        status=status, profile=profile, since=since, limit=limit
    )

    items: list[dict] = []
    for story in stories:
        ctx: dict = {}
        try:
            ctx = json.loads(story.get("context_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            ctx = {}
        actions = ctx.get("_agent_actions") or []
        launch_stages = [
            (a.get("stage"), a) for a in actions if a.get("action") == "launch"
        ]
        # If story had no plan (e.g. emergency-stopped pre-plan), still try to
        # export whatever prompt files exist — useful for failure analysis.
        if not launch_stages:
            workspace = story.get("workspace", "")
            prompt_dir = safe_story_path(
                workspace, ".story", "context", story["story_key"]
            )
            if prompt_dir.exists():
                for p in sorted(prompt_dir.glob("prompt_*.md")):
                    launch_stages.append((p.stem.replace("prompt_", ""), None))

        # stage filter — applied AFTER fetch so we can still return the story
        # row count correctly when caller asks for all stages.
        target_stages = [(stage, None)] if stage and stage != "all" else launch_stages

        # Fetch per-story accessory data once.
        events = db.get_story_events(story["story_key"])
        try:
            llm_calls = db.get_story_llm_calls(story["story_key"])
        except Exception:  # noqa: BLE001 — llm_calls is best-effort
            llm_calls = []

        # Map stage → action (so target_stages lookup works for filtered case).
        action_map = {s: a for s, a in launch_stages}
        stage_items: list[dict] = []
        for stg, _ in target_stages:
            action = action_map.get(stg)
            item = _build_stage_item(
                story=story,
                stage=stg,
                action=action,
                events=events,
                llm_calls=llm_calls,
            )
            if item is not None:
                stage_items.append(item)

        if not stage_items:
            continue

        items.append(
            {
                "story_key": story["story_key"],
                "title": story.get("title"),
                "profile": story.get("profile"),
                "source_type": story.get("source_type"),
                "task_type": ctx.get("task_type"),
                "workspace": story.get("workspace"),
                "workspace_path": ctx.get("workspace_path"),
                "status": story.get("status"),
                "current_stage": story.get("current_stage"),
                "execution_count": story.get("execution_count"),
                "last_error": story.get("last_error"),
                "created_at": story.get("created_at"),
                "updated_at": story.get("updated_at"),
                "stages": stage_items,
            }
        )

    return {
        "count": len(items),
        "filters": {
            "status": status or "all",
            "stage": stage or "all",
            "profile": profile or "all",
            "since": since,
            "limit": limit,
        },
        "items": items,
    }
