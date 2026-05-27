"""Stable Debug Packet — single source of truth for story diagnostics.

TUI panel and CLI diagnostics command both consume build_debug_packet().
No write side-effects. No LLM calls.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from ..db import models as db
from .paths import (
    stage_done_file,
    done_snapshot_file,
    malformed_done_file,
    context_dir,
    done_dir,
)


# ---------------------------------------------------------------------------
# Stuck Reason
# ---------------------------------------------------------------------------


def _check_llm_configured() -> bool:
    """Check if LLM is configured (env vars or config file)."""
    import os

    if os.environ.get("STORY_LLM_API_KEY"):
        return True
    try:
        from ..cli.setup import get_config

        cfg = get_config()
        if cfg.get("api_key") or cfg.get("STORY_LLM_API_KEY"):
            return True
    except Exception:
        pass
    return False


def _has_loop_exhausted(story_key: str) -> bool:
    """Check if evaluator loop reached max_rounds or no-progress."""
    events = db.get_story_events(story_key)
    for e in reversed(events):
        et = e.get("event_type", "")
        if et in ("evaluator_loop_completed", "evaluator_loop_round"):
            payload = e.get("payload")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    continue
            if isinstance(payload, dict):
                if payload.get("decision") == "fail":
                    return True
                if payload.get("no_progress"):
                    return True
    return False


def _explain_stuck_reason(
    story: dict,
    done_exists: bool,
    done_valid: bool | None,
    cli_exit,
    session_alive: bool,
    stage_elapsed_seconds: int = 0,
) -> dict:
    """Determine why a story might be stuck. Pure deterministic rules."""
    status = story.get("status", "")

    # 1. missing_config
    if not _check_llm_configured():
        return {
            "code": "missing_config",
            "severity": "error",
            "message": "LLM 配置缺失，请运行 story setup",
        }

    # 2. story_blocked
    if status == "blocked":
        return {
            "code": "story_blocked",
            "severity": "error",
            "message": "Story 已阻塞，需要人工恢复或失败处理",
        }

    # 3. waiting_subtasks
    if status == "waiting_subtasks":
        return {
            "code": "waiting_subtasks",
            "severity": "info",
            "message": "父 Story 正在等待子任务完成",
        }

    # 4. gate_blocked
    if status == "paused":
        ctx = {}
        try:
            ctx = json.loads(story.get("context_json") or "{}")
        except Exception:
            pass
        if ctx.get("last_gate_decision_id"):
            return {
                "code": "gate_blocked",
                "severity": "warning",
                "message": "Gate 阻塞，需要处理审查结果",
            }

    # 5. done_malformed
    if done_exists and done_valid is False:
        return {
            "code": "done_malformed",
            "severity": "error",
            "message": "done JSON 损坏，请查看 malformed 文件",
        }

    # 6. stage_timeout
    if session_alive and not done_exists and stage_elapsed_seconds > 900:
        return {
            "code": "stage_timeout",
            "severity": "warning",
            "message": "当前阶段运行时间过长，可能陷入等待或长耗时命令",
        }

    # 7. cli_exited_without_done
    from .entry import CliExitState

    if cli_exit == CliExitState.EXITED_WITHOUT_DONE:
        return {
            "code": "cli_exited_without_done",
            "severity": "warning",
            "message": "CLI 已退出，但当前阶段未写 done 文件。",
        }

    # 8. done_waiting
    if session_alive and not done_exists:
        return {
            "code": "done_waiting",
            "severity": "info",
            "message": "Agent 正在执行或等待 done 文件",
        }

    # 9. loop_exhausted
    if _has_loop_exhausted(story.get("story_key", "")):
        return {
            "code": "loop_exhausted",
            "severity": "warning",
            "message": "对抗循环已达到上限，可能需要人工介入",
        }

    # 10. none
    return {
        "code": "none",
        "severity": "info",
        "message": "当前未发现阻塞信号",
    }


# ---------------------------------------------------------------------------
# build_debug_packet
# ---------------------------------------------------------------------------


def build_debug_packet(story_key: str) -> dict:
    """Build a stable debug packet for a story. Pure read-only.

    Returns a dict matching the Debug Packet Schema (snake_case).
    If story doesn't exist, returns {"error": "Story not found"}.
    """
    s = db.get_story(story_key)
    if not s:
        return {"error": "Story not found"}

    now = datetime.now(timezone.utc).isoformat()
    workspace = s.get("workspace", "") or str(Path.cwd())
    stage = s.get("current_stage", "")
    status = s.get("status", "")

    # --- done state ---
    done_path = stage_done_file(workspace, story_key, stage)
    done_exists = done_path.exists()
    done_valid = None
    if done_exists:
        from .nodes import robust_json_parse

        try:
            data = robust_json_parse(done_path)
            if isinstance(data, dict) and data:
                done_valid = True
            else:
                done_valid = False
        except Exception:
            done_valid = False

    malformed_p = malformed_done_file(workspace, story_key, stage)
    snapshot_p = done_snapshot_file(workspace, story_key, stage)

    # --- session state ---
    from .entry import resolve_cli_exit_state, CliExitState

    cli_exit = resolve_cli_exit_state(s)
    cli_exit_str = cli_exit.value if cli_exit != CliExitState.NONE else ""

    session_alive = False
    session_name = ""
    try:
        from ..terminal import ttyd

        session_name = ttyd.session_name(story_key)
        session_alive = ttyd.session_alive(session_name)
    except Exception:
        pass

    # --- terminal output availability ---
    terminal_available = False
    terminal_path = ""
    terminal_missing_reason = ""
    if session_alive:
        terminal_available = True
        terminal_path = f"terminal/{session_name}/recent_output.txt"
    else:
        terminal_missing_reason = "session not alive"

    # --- stage timing ---
    stage_logs = db.get_stage_logs(story_key, limit=30)
    stage_started_at = ""
    stage_elapsed_seconds = 0
    for log in reversed(stage_logs):  # logs are newest-first, reverse = oldest-first
        if log.get("stage") == stage:
            raw_ts = log.get("created_at", "")
            if raw_ts:
                stage_started_at = str(raw_ts)
                try:
                    ts = str(raw_ts).replace(" ", "T")
                    started_dt = datetime.fromisoformat(ts)
                    if started_dt.tzinfo is None:
                        started_dt = started_dt.replace(tzinfo=timezone.utc)
                    delta = datetime.now(timezone.utc) - started_dt
                    stage_elapsed_seconds = int(delta.total_seconds())
                except Exception:
                    pass
            break

    # --- stuck reason ---
    stuck = _explain_stuck_reason(
        s, done_exists, done_valid, cli_exit, session_alive, stage_elapsed_seconds
    )

    # --- recent data ---
    all_events = db.get_story_events(story_key)
    recent_events = []
    for e in all_events[-50:]:
        payload = e.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                payload = {}
        recent_events.append(
            {
                "id": e.get("id"),
                "event_type": e.get("event_type"),
                "stage": e.get("stage"),
                "payload": payload if isinstance(payload, dict) else {},
                "created_at": e.get("created_at"),
            }
        )

    gate_results = db.get_gate_results(story_key, limit=20)

    # --- file hints ---
    story_home = Path.home() / ".story-lifecycle"
    ctx_dir = context_dir(workspace, story_key)
    dn_dir = done_dir(workspace)

    return {
        "schema_version": 1,
        "generated_at": now,
        "story": {
            "story_key": story_key,
            "title": s.get("title", ""),
            "status": status,
            "current_stage": stage,
            "workspace": workspace,
            "profile": s.get("profile", "minimal"),
            "execution_count": s.get("execution_count", 0),
            "last_error": s.get("last_error", ""),
        },
        "done_state": {
            "stage": stage,
            "path": str(done_path),
            "exists": done_exists,
            "valid": done_valid,
            "malformed_path": str(malformed_p)
            if not done_valid and done_exists
            else "",
            "snapshot_path": str(snapshot_p),
        },
        "session_state": {
            "backend": "zellij",
            "session_name": session_name,
            "session_alive": session_alive,
            "cli_exit_state": cli_exit_str or "none",
            "stage_started_at": stage_started_at,
            "stage_elapsed_seconds": stage_elapsed_seconds,
        },
        "terminal_output": {
            "available": terminal_available,
            "path": terminal_path,
            "line_count": 0,
            "truncated": False,
            "missing_reason": terminal_missing_reason,
        },
        "stuck_reason": stuck,
        "recent_events": recent_events,
        "recent_stage_logs": stage_logs,
        "gate_results": gate_results,
        "file_hints": {
            "story_context_dir": str(ctx_dir.relative_to(workspace))
            if workspace
            else str(ctx_dir),
            "done_dir": str(dn_dir.relative_to(workspace))
            if workspace
            else str(dn_dir),
            "graph_error_log": str(story_home / "graph_error.log"),
            "planner_error_log": str(story_home / "planner_error.log"),
        },
    }


# ---------------------------------------------------------------------------
# Redaction helpers
# ---------------------------------------------------------------------------

_REDACT_PATTERNS: list[tuple[str, str]] = [
    (r"sk-[A-Za-z0-9_\-]{20,}", "[REDACTED_API_KEY]"),
    (r"sk-ant-[A-Za-z0-9_\-]{20,}", "[REDACTED_API_KEY]"),
    (r"AKID[A-Za-z0-9_\-]{20,}", "[REDACTED_API_KEY]"),
    (r"(api_key\s*[:=]\s*)[\"']?([^\s,'\"}]+)[\"']?", r"\1[REDACTED]"),
    (r"(apikey\s*[:=]\s*)[\"']?([^\s,'\"}]+)[\"']?", r"\1[REDACTED]"),
    (r"(token\s*[:=]\s*)[\"']?([^\s,'\"}]+)[\"']?", r"\1[REDACTED]"),
    (r"(password\s*[:=]\s*)[\"']?([^\s,'\"}]+)[\"']?", r"\1[REDACTED]"),
    (r"(secret\s*[:=]\s*)[\"']?([^\s,'\"}]+)[\"']?", r"\1[REDACTED]"),
    (r"(authorization\s*[:=]\s*)[\"']?([^\s,'\"}]+)[\"']?", r"\1[REDACTED]"),
    (r"(cookie\s*[:=]\s*)[\"']?([^\s,'\"}]+)[\"']?", r"\1[REDACTED]"),
    (r"(STORY_LLM_API_KEY\s*=\s*)([^\s\n]+)", r"\1[REDACTED]"),
    (r"(export\s+STORY_LLM_API_KEY\s*=\s*)([^\s\n]+)", r"\1[REDACTED]"),
    (r"(Bearer\s+)([A-Za-z0-9_\-\.]{20,})", r"\1[REDACTED]"),
]


def redact_text(text: str) -> str:
    """Apply all redaction patterns to a text string."""
    for pattern, replacement in _REDACT_PATTERNS:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


def redact_mapping(data: dict, keys: set | None = None) -> dict:
    """Recursively redact sensitive keys in a dict."""
    if keys is None:
        keys = {
            "api_key",
            "token",
            "password",
            "secret",
            "authorization",
            "cookie",
            "apikey",
            "key",
            "api_secret",
            "access_token",
            "refresh_token",
        }
    result = {}
    for k, v in data.items():
        if k.lower() in keys or any(
            s in k.lower() for s in ("secret", "token", "password", "api_key")
        ):
            result[k] = "[REDACTED]"
        elif isinstance(v, dict):
            result[k] = redact_mapping(v, keys)
        elif isinstance(v, str):
            result[k] = redact_text(v)
        else:
            result[k] = v
    return result
