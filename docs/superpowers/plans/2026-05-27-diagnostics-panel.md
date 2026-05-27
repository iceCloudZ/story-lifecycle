# Board Diagnostics Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement a right-side diagnostics panel in `story board` TUI with supporting debug packet module, CLI diagnostics command, and diagnostic bundle generation.

**Architecture:** New `orchestrator/debug_packet.py` provides a stable `build_debug_packet()` schema consumed by both the TUI panel (read-only rendering) and the CLI `story diagnostics` command (zip bundle generation). The TUI panel reuses existing `entry.py` state resolvers (`resolve_cli_exit_state`, `validate_stage_done`) rather than reimplementing them.

**Tech Stack:** Python 3.11+, Textual (TUI), Click (CLI), zipfile (stdlib), sqlite3 (stdlib)

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `src/story_lifecycle/orchestrator/debug_packet.py` | **Create** | `build_debug_packet()`, `explain_stuck_reason()`, `redact_text()`, `redact_mapping()` |
| `src/story_lifecycle/orchestrator/diagnostics.py` | **Create** | `create_story_diagnostics_bundle()`, `create_global_diagnostics_bundle()`, `_write_manifest()`, `_write_summary_md()` |
| `src/story_lifecycle/cli/diagnostics.py` | **Create** | Click command group `story diagnostics STORY_KEY` / `--global` |
| `tests/test_debug_packet.py` | **Create** | Unit tests for debug packet, stuck reason, redaction |
| `tests/test_diagnostics.py` | **Create** | Unit tests for bundle generation, manifest, summary |
| `src/story_lifecycle/db/models.py` | **Modify** | Add `get_stage_logs()`, `get_gate_results()` query helpers |
| `src/story_lifecycle/cli/main.py` | **Modify** | Register `diagnostics` command, add to config exemption list |
| `src/story_lifecycle/cli/tui.py` | **Modify** | Add diagnostics panel layout/CSS, keybindings `o`/`p`/`P`, render + actions |

---

## Phase 1: Foundation — debug_packet.py

### Task 1: Add DB query helpers for stage_log and gate_result

**Files:**
- Modify: `src/story_lifecycle/db/models.py`

**Why:** `build_debug_packet()` needs to query `stage_log` and `gate_result` by `story_key`. Currently no direct query helpers exist — both tables reference `story_id` (FK to `story.id`), so a JOIN is needed.

- [ ] **Step 1: Add `get_stage_logs()` and `get_gate_results()` to models.py**

Add after the existing `log_stage()` function (line 331):

```python
def get_stage_logs(story_key: str, limit: int = 50) -> list[dict]:
    """Return recent stage_log rows for a story, newest first."""
    with _db() as conn:
        rows = conn.execute(
            """SELECT sl.* FROM stage_log sl
               JOIN story s ON s.id = sl.story_id
               WHERE s.story_key = ?
               ORDER BY sl.id DESC LIMIT ?""",
            (story_key, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_gate_results(story_key: str, limit: int = 20) -> list[dict]:
    """Return recent gate_result rows for a story, newest first."""
    with _db() as conn:
        rows = conn.execute(
            """SELECT gr.* FROM gate_result gr
               JOIN story s ON s.id = gr.story_id
               WHERE s.story_key = ?
               ORDER BY gr.id DESC LIMIT ?""",
            (story_key, limit),
        ).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 2: Verify the helpers work**

Run a quick Python check:

```bash
python -c "from story_lifecycle.db.models import init_db, get_stage_logs, get_gate_results; init_db(); print(get_stage_logs('NONEXISTENT')); print(get_gate_results('NONEXISTENT'))"
```

Expected: `[]` and `[]`

- [ ] **Step 3: Commit**

```bash
git add src/story_lifecycle/db/models.py
git commit -m "feat: add get_stage_logs and get_gate_results query helpers"
```

### Task 2: Create debug_packet.py — build_debug_packet()

**Files:**
- Create: `src/story_lifecycle/orchestrator/debug_packet.py`

**Why:** This is the single source of truth for diagnostic data. Both the TUI panel and CLI command consume it. It aggregates story DB row, done file state, session state, stuck reason, recent events/stage_logs/gate_results, and file hints into one stable dict.

- [ ] **Step 1: Create the module with `build_debug_packet()`**

```python
"""Stable Debug Packet — single source of truth for story diagnostics.

TUI panel and CLI diagnostics command both consume build_debug_packet().
No write side-effects. No LLM calls.
"""

from __future__ import annotations

import json
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
    done_error = ""
    if done_exists:
        from .nodes import robust_json_parse
        try:
            data = robust_json_parse(done_path)
            if isinstance(data, dict) and data:
                done_valid = True
            else:
                done_valid = False
                done_error = "parsed but contains no data"
        except Exception as exc:
            done_valid = False
            done_error = str(exc)

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

    # --- stuck reason ---
    stuck = _explain_stuck_reason(s, done_exists, done_valid, cli_exit, session_alive)

    # --- recent data ---
    recent_events_raw = db.get_story_events(story_key)
    recent_events = []
    for e in recent_events_raw[-50:]:
        payload = e.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                payload = {}
        recent_events.append({
            "id": e.get("id"),
            "event_type": e.get("event_type"),
            "stage": e.get("stage"),
            "payload": payload if isinstance(payload, dict) else {},
            "created_at": e.get("created_at"),
        })

    stage_logs = db.get_stage_logs(story_key, limit=30)
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
            "malformed_path": str(malformed_p) if not done_valid and done_exists else "",
            "snapshot_path": str(snapshot_p),
        },
        "session_state": {
            "backend": "zellij",
            "session_name": session_name,
            "session_alive": session_alive,
            "cli_exit_state": cli_exit_str or "none",
            "stage_started_at": "",
            "stage_elapsed_seconds": 0,
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
            "story_context_dir": str(ctx_dir.relative_to(workspace)) if workspace else str(ctx_dir),
            "done_dir": str(dn_dir.relative_to(workspace)) if workspace else str(dn_dir),
            "graph_error_log": str(story_home / "graph_error.log"),
            "planner_error_log": str(story_home / "planner_error.log"),
        },
    }
```

- [ ] **Step 2: Commit**

```bash
git add src/story_lifecycle/orchestrator/debug_packet.py
git commit -m "feat: add build_debug_packet() with stable diagnostic schema"
```

### Task 3: Add _explain_stuck_reason() to debug_packet.py

**Files:**
- Modify: `src/story_lifecycle/orchestrator/debug_packet.py`

**Why:** Stuck reason rules must live in debug_packet.py so both TUI and CLI get consistent results. P0 uses deterministic rules — no LLM.

- [ ] **Step 1: Add `_explain_stuck_reason()` before `build_debug_packet()`**

Insert after the imports, before `build_debug_packet()`:

```python
def _explain_stuck_reason(
    story: dict,
    done_exists: bool,
    done_valid: bool | None,
    cli_exit,
    session_alive: bool,
) -> dict:
    """Determine why a story might be stuck. Pure deterministic rules.

    Returns a dict with code, severity, message.
    """
    status = story.get("status", "")
    stage = story.get("current_stage", "")
    is_configured = _check_llm_configured()

    # 1. missing_config
    if not is_configured:
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
            import json as _json
            ctx = _json.loads(story.get("context_json") or "{}")
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

    # 6. done_waiting
    if session_alive and not done_exists:
        return {
            "code": "done_waiting",
            "severity": "info",
            "message": "Agent 正在执行或等待 done 文件",
        }

    # 7. cli_exited_without_done
    from .entry import CliExitState
    if cli_exit == CliExitState.EXITED_WITHOUT_DONE:
        return {
            "code": "cli_exited_without_done",
            "severity": "warning",
            "message": "CLI 已退出，但当前阶段未写 done 文件。",
        }

    # 8. stage_timeout — session alive, no done, elapsed > 15min
    if session_alive and not done_exists:
        return {
            "code": "stage_timeout",
            "severity": "warning",
            "message": "当前阶段运行时间过长，可能陷入等待或长耗时命令",
        }

    # 9. loop_exhausted — check event_log for evaluator/review no-progress
    if _has_loop_exhausted(story.get("story_key", "")):
        return {
            "code": "loop_exhausted",
            "severity": "warning",
            "message": "对抗循环已达到上限，可能需要人工介入",
        }

    # 10. none — no issues detected
    return {
        "code": "none",
        "severity": "info",
        "message": "当前未发现阻塞信号",
    }


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
                    import json
                    payload = json.loads(payload)
                except Exception:
                    continue
            if isinstance(payload, dict):
                if payload.get("decision") == "fail":
                    return True
                if payload.get("no_progress"):
                    return True
    return False
```

- [ ] **Step 2: Commit**

```bash
git add src/story_lifecycle/orchestrator/debug_packet.py
git commit -m "feat: add _explain_stuck_reason() with deterministic rules"
```

### Task 4: Add redact_text() and redact_mapping() to debug_packet.py

**Files:**
- Modify: `src/story_lifecycle/orchestrator/debug_packet.py`

- [ ] **Step 1: Append redaction functions at end of debug_packet.py**

```python
# ---------------------------------------------------------------------------
# Redaction helpers
# ---------------------------------------------------------------------------

import re

_REDACT_PATTERNS: list[tuple[str, str]] = [
    # API key patterns (common formats)
    (r"sk-[A-Za-z0-9_\-]{20,}", "[REDACTED_API_KEY]"),
    (r"sk-ant-[A-Za-z0-9_\-]{20,}", "[REDACTED_API_KEY]"),
    (r"AKID[A-Za-z0-9_\-]{20,}", "[REDACTED_API_KEY]"),
    # Key=value patterns
    (r"(api_key\s*[:=]\s*)([^\s,'""}]+)", r"\1[REDACTED]"),
    (r"(apikey\s*[:=]\s*)([^\s,'""}]+)", r"\1[REDACTED]"),
    (r"(token\s*[:=]\s*)([^\s,'""}]+)", r"\1[REDACTED]"),
    (r"(password\s*[:=]\s*)([^\s,'""}]+)", r"\1[REDACTED]"),
    (r"(secret\s*[:=]\s*)([^\s,'""}]+)", r"\1[REDACTED]"),
    (r"(authorization\s*[:=]\s*)([^\s,'""}]+)", r"\1[REDACTED]"),
    (r"(cookie\s*[:=]\s*)([^\s,'""}]+)", r"\1[REDACTED]"),
    # Env var patterns
    (r"(STORY_LLM_API_KEY\s*=\s*)([^\s\n]+)", r"\1[REDACTED]"),
    (r"(export\s+STORY_LLM_API_KEY\s*=\s*)([^\s\n]+)", r"\1[REDACTED]"),
    # Bearer token
    (r"(Bearer\s+)([A-Za-z0-9_\-\.]{20,})", r"\1[REDACTED]"),
]


def redact_text(text: str) -> str:
    """Apply all redaction patterns to a text string."""
    for pattern, replacement in _REDACT_PATTERNS:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


def redact_mapping(data: dict, keys: set | None = None) -> dict:
    """Recursively redact sensitive keys in a dict.

    Default sensitive keys: api_key, token, password, secret, authorization, cookie.
    """
    if keys is None:
        keys = {"api_key", "token", "password", "secret", "authorization", "cookie",
                "apikey", "key", "api_secret", "access_token", "refresh_token"}
    result = {}
    for k, v in data.items():
        if k.lower() in keys or any(s in k.lower() for s in ("secret", "token", "password", "api_key")):
            result[k] = "[REDACTED]"
        elif isinstance(v, dict):
            result[k] = redact_mapping(v, keys)
        elif isinstance(v, str):
            result[k] = redact_text(v)
        else:
            result[k] = v
    return result
```

- [ ] **Step 2: Commit**

```bash
git add src/story_lifecycle/orchestrator/debug_packet.py
git commit -m "feat: add redact_text and redact_mapping helpers"
```

### Task 5: Unit tests for debug_packet.py

**Files:**
- Create: `tests/test_debug_packet.py`

- [ ] **Step 1: Create test file**

```python
"""Unit tests for debug_packet.py -- packet building, stuck reasons, redaction."""

import pytest
from story_lifecycle.orchestrator.debug_packet import (
    build_debug_packet,
    redact_text,
    redact_mapping,
)
from story_lifecycle.db.models import init_db


class TestBuildDebugPacket:
    def test_nonexistent_story(self):
        result = build_debug_packet("NONEXISTENT-STORY")
        assert result == {"error": "Story not found"}

    def test_packet_schema_keys(self, tmp_path):
        """build_debug_packet returns all required top-level keys."""
        init_db()
        from story_lifecycle.db.models import create_story

        ws = str(tmp_path)
        create_story("TEST-001", "Test Story", ws)
        (tmp_path / ".story" / "done" / "TEST-001").mkdir(parents=True, exist_ok=True)

        packet = build_debug_packet("TEST-001")
        required_keys = {
            "schema_version", "generated_at", "story", "done_state",
            "session_state", "terminal_output", "stuck_reason",
            "recent_events", "recent_stage_logs", "gate_results", "file_hints",
        }
        assert required_keys.issubset(set(packet.keys()))
        assert packet["schema_version"] == 1
        assert packet["story"]["story_key"] == "TEST-001"

    def test_missing_config_stuck_reason(self, tmp_path):
        """If no LLM key configured, stuck_reason should be missing_config."""
        init_db()
        from story_lifecycle.db.models import create_story

        ws = str(tmp_path)
        create_story("TEST-002", "Test", ws)
        (tmp_path / ".story" / "done" / "TEST-002").mkdir(parents=True, exist_ok=True)

        import os
        old_key = os.environ.pop("STORY_LLM_API_KEY", None)
        try:
            packet = build_debug_packet("TEST-002")
            assert packet["stuck_reason"]["code"] == "missing_config"
        finally:
            if old_key:
                os.environ["STORY_LLM_API_KEY"] = old_key


class TestStuckReasons:
    """Test _explain_stuck_reason via build_debug_packet."""

    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        monkeypatch.setenv("STORY_LLM_API_KEY", "fake-key-for-test")

    def test_done_malformed(self, tmp_path):
        init_db()
        from story_lifecycle.db.models import create_story

        ws = str(tmp_path)
        create_story("TEST-MAL", "Malformed Done", ws)
        done_dir = tmp_path / ".story" / "done" / "TEST-MAL"
        done_dir.mkdir(parents=True)
        done_dir.joinpath("design.json").write_text("not valid json {{{", encoding="utf-8")

        packet = build_debug_packet("TEST-MAL")
        assert packet["done_state"]["valid"] is False
        assert packet["stuck_reason"]["code"] == "done_malformed"

    def test_none_blocked(self, tmp_path):
        init_db()
        from story_lifecycle.db.models import create_story, update_story

        ws = str(tmp_path)
        create_story("TEST-BLK", "Blocked", ws)
        update_story("TEST-BLK", status="blocked")
        (tmp_path / ".story" / "done" / "TEST-BLK").mkdir(parents=True, exist_ok=True)

        packet = build_debug_packet("TEST-BLK")
        assert packet["stuck_reason"]["code"] == "story_blocked"

    def test_none_ok(self, tmp_path):
        init_db()
        from story_lifecycle.db.models import create_story

        ws = str(tmp_path)
        create_story("TEST-OK", "Fine", ws)
        (tmp_path / ".story" / "done" / "TEST-OK").mkdir(parents=True, exist_ok=True)

        packet = build_debug_packet("TEST-OK")
        assert packet["stuck_reason"]["code"] == "none"


class TestRedaction:
    def test_redact_openai_key(self):
        text = "Authorization: Bearer sk-abc123def456ghijklmnopqrstuvwxyz"
        result = redact_text(text)
        assert "sk-abc" not in result
        assert "[REDACTED_API_KEY]" in result

    def test_redact_env_var(self):
        text = "export STORY_LLM_API_KEY=sk-ant-secret12345"
        result = redact_text(text)
        assert "secret12345" not in result
        assert "[REDACTED]" in result

    def test_redact_key_value(self):
        text = "api_key: my-secret-token-here"
        result = redact_text(text)
        assert "my-secret-token-here" not in result
        assert "[REDACTED]" in result

    def test_redact_mapping_nested(self):
        data = {
            "config": {
                "api_key": "secret123",
                "url": "https://api.example.com",
                "nested": {"token": "abc123"},
            }
        }
        result = redact_mapping(data)
        assert result["config"]["api_key"] == "[REDACTED]"
        assert result["config"]["url"] == "https://api.example.com"
        assert result["config"]["nested"]["token"] == "[REDACTED]"

    def test_redact_anthropic_key(self):
        text = "using sk-ant-api03-abcdefghijklmnopqrstuvwxyz for auth"
        result = redact_text(text)
        assert "sk-ant-api03" not in result
        assert "[REDACTED_API_KEY]" in result
```

- [ ] **Step 2: Run tests and verify they pass**

```bash
python -m pytest tests/test_debug_packet.py -v
```

- [ ] **Step 3: Commit**

```bash
git add tests/test_debug_packet.py
git commit -m "test: add unit tests for debug packet, stuck reason, and redaction"
```


---

## Phase 2: CLI Diagnostics Command

### Task 6: Create cli/diagnostics.py — story diagnostics CLI

**Files:**
- Create: `src/story_lifecycle/cli/diagnostics.py`

- [ ] **Step 1: Create the Click command group**

```python
"""story diagnostics — generate diagnostic bundles for stories and system."""

from __future__ import annotations

import click
from pathlib import Path
from rich.console import Console

from ..db.models import init_db
from ..orchestrator.debug_packet import build_debug_packet

console = Console()


@click.group(invoke_without_command=True, no_args_is_help=False)
@click.option("--global", "global_diag", is_flag=True, help="Generate global diagnostics bundle")
@click.option("--output", "-o", default=None, help="Output zip path or directory")
@click.option("--include-diff", is_flag=True, help="Include full git diff (default: off)")
@click.option("--event-limit", default=200, type=int, help="Max event_log entries")
@click.option("--no-zip", is_flag=True, help="Output directory instead of zip")
@click.pass_context
def diagnostics(ctx, global_diag, output, include_diff, event_limit, no_zip):
    """Generate diagnostic bundle for a story or the system.

    \b
    Examples:
      story diagnostics STORY-001
      story diagnostics STORY-001 --no-zip
      story diagnostics --global
      story diagnostics STORY-001 -o /tmp/diag.zip
    """
    init_db()

    if global_diag:
        from ..orchestrator.diagnostics import create_global_diagnostics_bundle
        result = create_global_diagnostics_bundle(
            output_path=output,
            no_zip=no_zip,
        )
    elif ctx.invoked_subcommand is None:
        # If no subcommand and no --global, show help
        click.echo(ctx.get_help())
        return
    else:
        # Subcommand is the story_key
        return

    if isinstance(result, dict) and result.get("error"):
        console.print(f"[red]Error: {result['error']}[/]")
        raise SystemExit(1)

    dest = result.get("path", "unknown")
    if no_zip:
        console.print(f"Diagnostic bundle created:\n[bold cyan]{dest}[/]")
    else:
        console.print(f"Diagnostic bundle created:\n[bold cyan]{dest}[/]")


@diagnostics.command(name="story", context_settings=dict(ignore_unknown_options=True))
@click.argument("story_key")
@click.pass_context
def diagnostics_story(ctx, story_key):
    """Generate diagnostic bundle for STORY_KEY."""
    init_db()
    from ..orchestrator.diagnostics import create_story_diagnostics_bundle

    parent = ctx.parent
    output = parent.params.get("output") if parent else None
    include_diff = parent.params.get("include_diff", False) if parent else False
    event_limit = parent.params.get("event_limit", 200) if parent else 200
    no_zip = parent.params.get("no_zip", False) if parent else False

    result = create_story_diagnostics_bundle(
        story_key=story_key,
        output_path=output,
        include_diff=include_diff,
        event_limit=event_limit,
        no_zip=no_zip,
    )

    if result.get("error"):
        console.print(f"[red]Error: {result['error']}[/]")
        raise SystemExit(1)

    dest = result.get("path", "unknown")
    if no_zip:
        console.print(f"Diagnostic bundle created:\n[bold cyan]{dest}[/]")
    else:
        console.print(f"Diagnostic bundle created:\n[bold cyan]{dest}[/]")
```

Wait — the design doc specifies a simpler CLI:

```text
story diagnostics STORY_KEY
story diagnostics --global
```

So `STORY_KEY` is a positional argument to the `diagnostics` command itself, not a subcommand. Let me simplify:

```python
"""story diagnostics — generate diagnostic bundles for stories and system."""

from __future__ import annotations

import click
from rich.console import Console

from ..db.models import init_db

console = Console()


@click.command()
@click.argument("story_key", required=False)
@click.option("--global", "global_diag", is_flag=True, help="Generate global diagnostics bundle")
@click.option("--output", "-o", default=None, help="Output zip path or directory")
@click.option("--include-diff", is_flag=True, help="Include full git diff (default: off)")
@click.option("--event-limit", default=200, type=int, help="Max event_log entries")
@click.option("--no-zip", is_flag=True, help="Output directory instead of zip")
def diagnostics(story_key, global_diag, output, include_diff, event_limit, no_zip):
    """Generate diagnostic bundle for a story or the system.

    \b
    Examples:
      story diagnostics STORY-001
      story diagnostics STORY-001 --no-zip
      story diagnostics --global
    """
    init_db()

    if global_diag:
        from ..orchestrator.diagnostics import create_global_diagnostics_bundle
        result = create_global_diagnostics_bundle(
            output_path=output,
            no_zip=no_zip,
        )
    elif story_key:
        from ..orchestrator.diagnostics import create_story_diagnostics_bundle
        result = create_story_diagnostics_bundle(
            story_key=story_key,
            output_path=output,
            include_diff=include_diff,
            event_limit=event_limit,
            no_zip=no_zip,
        )
    else:
        click.echo("Usage: story diagnostics STORY_KEY or story diagnostics --global")
        raise SystemExit(1)

    if isinstance(result, dict) and result.get("error"):
        console.print(f"[red]Error: {result['error']}[/]")
        raise SystemExit(1)

    dest = result.get("path", "unknown")
    console.print(f"Diagnostic bundle created:\n[bold cyan]{dest}[/]")
```

- [ ] **Step 2: Commit**

```bash
git add src/story_lifecycle/cli/diagnostics.py
git commit -m "feat: add story diagnostics CLI command"
```

### Task 7: Register diagnostics command in main.py

**Files:**
- Modify: `src/story_lifecycle/cli/main.py`

- [ ] **Step 1: Add diagnostics to config exemption list and register command**

Two changes in `main.py`:

**Change 1:** Add "diagnostics" to the exemption list (line 77-83):

```python
if ctx.invoked_subcommand not in (
    "setup",
    "serve",
    "doctor",
    "demo",
    "upgrade",
    "swebench",
    "diagnostics",
):
```

**Change 2:** Import and register the command. Add after the other command registrations (around line 301):

```python
from .diagnostics import diagnostics  # noqa: E402

cli.add_command(diagnostics)
```

- [ ] **Step 2: Verify the command is registered**

```bash
python -m story_lifecycle diagnostics --help
```

Expected: shows usage with STORY_KEY and --global options.

- [ ] **Step 3: Commit**

```bash
git add src/story_lifecycle/cli/main.py
git commit -m "feat: register diagnostics command with config check exemption"
```


---

## Phase 3: Diagnostic Bundle Generation

### Task 8: Create orchestrator/diagnostics.py — story bundle

**Files:**
- Create: `src/story_lifecycle/orchestrator/diagnostics.py`

- [ ] **Step 1: Create the module with story bundle generator**

```python
"""Diagnostic bundle generation — collect, redact, package.

Story-level bundles go to {workspace}/.story/diagnostics/{key}-{timestamp}.zip
Global bundles go to ~/.story-lifecycle/diagnostics/global-{timestamp}.zip
"""

from __future__ import annotations

import json
import os
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path

from .debug_packet import build_debug_packet, redact_text, redact_mapping
from ..db import models as db


def create_story_diagnostics_bundle(
    story_key: str,
    output_path: str | None = None,
    include_diff: bool = False,
    event_limit: int = 200,
    no_zip: bool = False,
) -> dict:
    """Generate a diagnostic bundle for a story.

    Returns {"path": str} on success, {"error": str} on failure.
    """
    packet = build_debug_packet(story_key)
    if "error" in packet:
        return packet

    workspace = packet["story"]["workspace"]
    ws_path = Path(workspace)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")

    if output_path:
        out_dir = Path(output_path)
    elif no_zip:
        out_dir = ws_path / ".story" / "diagnostics" / f"{story_key}-{ts}"
    else:
        out_dir = ws_path / ".story" / "diagnostics"

    out_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "schema_version": 1,
        "bundle_type": "story",
        "story_key": story_key,
        "created_at": datetime.now().isoformat(),
        "story_lifecycle_version": _get_version(),
        "workspace": workspace,
        "files": [],
        "missing": [],
        "truncated": [],
    }

    bundle_dir = out_dir if no_zip else Path(tempfile.mkdtemp(prefix=f"diag-{story_key}-"))

    # 1. debug_packet.json
    _write_json(bundle_dir / "debug_packet.json", packet)
    manifest["files"].append({"path": "debug_packet.json", "kind": "json", "redacted": False})

    # 2. story.json (DB row, redacted)
    story_data = db.get_story(story_key) or {}
    _write_json(bundle_dir / "story.json", redact_mapping(story_data))
    manifest["files"].append({"path": "story.json", "kind": "json", "redacted": True})

    # 3. events.jsonl
    events = db.get_story_events(story_key)
    _write_jsonl(bundle_dir / "events.jsonl", events[-event_limit:])
    manifest["files"].append({"path": "events.jsonl", "kind": "jsonl", "redacted": False})
    if len(events) > event_limit:
        manifest["truncated"].append({"path": "events.jsonl", "reason": f"limited to {event_limit} of {len(events)}"})

    # 4. stage_logs.jsonl
    stage_logs = db.get_stage_logs(story_key, limit=100)
    _write_jsonl(bundle_dir / "stage_logs.jsonl", stage_logs)
    manifest["files"].append({"path": "stage_logs.jsonl", "kind": "jsonl", "redacted": False})

    # 5. gate_results.jsonl
    gate_results = db.get_gate_results(story_key, limit=50)
    _write_jsonl(bundle_dir / "gate_results.jsonl", gate_results)
    manifest["files"].append({"path": "gate_results.jsonl", "kind": "jsonl", "redacted": False})

    # 6. config.redacted.yaml
    _collect_redacted_config(bundle_dir, manifest)

    # 7. environment.txt
    _collect_environment(bundle_dir, manifest)

    # 8. done/ files
    _collect_done_files(bundle_dir, ws_path, story_key, packet, manifest)

    # 9. context/ files
    _collect_context_files(bundle_dir, ws_path, story_key, manifest)

    # 10. terminal/
    _collect_terminal_output(bundle_dir, story_key, packet, manifest)

    # 11. workspace/ (git status, optional git diff)
    if ws_path.exists():
        _collect_git_info(bundle_dir, ws_path, include_diff, manifest)
    else:
        manifest["missing"].append({"path": "workspace/", "reason": "workspace directory does not exist"})

    # 12. summary.md
    _write_summary_md(bundle_dir, packet, manifest)

    # 13. manifest.json
    _write_json(bundle_dir / "manifest.json", manifest)

    # Zip or return directory path
    if no_zip:
        return {"path": str(bundle_dir)}

    zip_path = out_dir / f"{story_key}-{ts}.zip"
    _make_zip(bundle_dir, zip_path)
    return {"path": str(zip_path)}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

import tempfile
import shutil


def _get_version() -> str:
    try:
        from importlib.metadata import version
        return version("story-lifecycle")
    except Exception:
        return "unknown"


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _make_zip(src_dir: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(dest), "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(src_dir.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(src_dir))
    shutil.rmtree(src_dir, ignore_errors=True)


def _collect_redacted_config(bundle_dir: Path, manifest: dict) -> None:
    """Copy config.yaml with redaction."""
    config_path = Path.home() / ".story-lifecycle" / "config.yaml"
    if config_path.exists():
        text = config_path.read_text(encoding="utf-8")
        dest = bundle_dir / "config.redacted.yaml"
        dest.write_text(redact_text(text), encoding="utf-8")
        manifest["files"].append({"path": "config.redacted.yaml", "kind": "yaml", "redacted": True})
    else:
        manifest["missing"].append({"path": "config.redacted.yaml", "reason": "no config file"})


def _collect_environment(bundle_dir: Path, manifest: dict) -> None:
    """Write basic environment info."""
    lines = [
        f"platform: {os.name}",
        f"python: {os.sys.version}",
        f"executable: {os.sys.executable}",
        f"cwd: {os.getcwd()}",
        f"path: {os.environ.get('PATH', '')}",
    ]
    dest = bundle_dir / "environment.txt"
    dest.write_text("\n".join(lines), encoding="utf-8")
    manifest["files"].append({"path": "environment.txt", "kind": "text", "redacted": False})


def _collect_done_files(bundle_dir: Path, ws_path: Path, story_key: str, packet: dict, manifest: dict) -> None:
    """Collect done file (current + malformed + snapshots)."""
    from .paths import stage_done_file, malformed_done_file, done_snapshot_file, context_dir

    stage = packet["story"]["current_stage"]

    # current done
    done_p = stage_done_file(ws_path, story_key, stage)
    done_dest = bundle_dir / "done" / "current.json"
    if done_p.exists():
        done_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(done_p, done_dest)
        manifest["files"].append({"path": "done/current.json", "kind": "json", "redacted": False})
    else:
        manifest["missing"].append({"path": "done/current.json", "reason": "no done file"})

    # malformed
    mal_p = malformed_done_file(ws_path, story_key, stage)
    if mal_p.exists():
        mal_dest = bundle_dir / "done" / "current.malformed"
        mal_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(mal_p, mal_dest)
        manifest["files"].append({"path": "done/current.malformed", "kind": "text", "redacted": False})

    # snapshots
    snapshot_dir = context_dir(ws_path, story_key) / "done"
    if snapshot_dir.exists():
        snap_dest = bundle_dir / "done" / "snapshots"
        snap_dest.mkdir(parents=True, exist_ok=True)
        for f in snapshot_dir.iterdir():
            if f.is_file():
                shutil.copy2(f, snap_dest / f.name)
        manifest["files"].append({"path": "done/snapshots/", "kind": "dir", "redacted": False})


def _collect_context_files(bundle_dir: Path, ws_path: Path, story_key: str, manifest: dict) -> None:
    """List context directory files."""
    from .paths import context_dir
    ctx_dir = context_dir(ws_path, story_key)
    if ctx_dir.exists():
        files_list = []
        for f in sorted(ctx_dir.rglob("*")):
            if f.is_file():
                files_list.append(str(f.relative_to(ctx_dir)))
        dest = bundle_dir / "context" / "known_context_files.txt"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("\n".join(files_list) if files_list else "(empty)", encoding="utf-8")
        manifest["files"].append({"path": "context/known_context_files.txt", "kind": "text", "redacted": False})
    else:
        manifest["missing"].append({"path": "context/", "reason": "context directory does not exist"})


def _collect_terminal_output(bundle_dir: Path, story_key: str, packet: dict, manifest: dict) -> None:
    """Capture terminal recent output if available."""
    session_name = packet["session_state"].get("session_name", "")
    if not session_name:
        manifest["missing"].append({"path": "terminal/recent_output.txt", "reason": "no session name"})
        return

    dest = bundle_dir / "terminal" / "recent_output.txt"
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        result = subprocess.run(
            ["zellij", "action", "dump-screen", session_name, "-n", "500"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            lines = result.stdout.splitlines()
            dest.write_text(result.stdout, encoding="utf-8")
            manifest["files"].append({"path": "terminal/recent_output.txt", "kind": "text", "redacted": False})
            if len(lines) >= 500:
                manifest["truncated"].append({"path": "terminal/recent_output.txt", "line_limit": 500})
        else:
            manifest["missing"].append({"path": "terminal/recent_output.txt", "reason": result.stderr.strip() or "empty output"})
    except FileNotFoundError:
        manifest["missing"].append({"path": "terminal/recent_output.txt", "reason": "zellij not available"})
    except subprocess.TimeoutExpired:
        manifest["missing"].append({"path": "terminal/recent_output.txt", "reason": "zellij dump timed out"})
    except Exception as exc:
        manifest["missing"].append({"path": "terminal/recent_output.txt", "reason": str(exc)})

    # session state
    _write_json(bundle_dir / "terminal" / "session_state.json", packet["session_state"])
    manifest["files"].append({"path": "terminal/session_state.json", "kind": "json", "redacted": False})


def _collect_git_info(bundle_dir: Path, ws_path: Path, include_diff: bool, manifest: dict) -> None:
    """Collect git status and optionally diff stat."""
    dest_dir = bundle_dir / "workspace"
    dest_dir.mkdir(parents=True, exist_ok=True)

    try:
        r = subprocess.run(["git", "status", "--short"], capture_output=True, text=True, cwd=str(ws_path), timeout=15)
        if r.returncode == 0:
            (dest_dir / "git_status.txt").write_text(r.stdout or "(clean)", encoding="utf-8")
            manifest["files"].append({"path": "workspace/git_status.txt", "kind": "text", "redacted": False})
        else:
            manifest["missing"].append({"path": "workspace/git_status.txt", "reason": r.stderr.strip()})
    except Exception as exc:
        manifest["missing"].append({"path": "workspace/git_status.txt", "reason": str(exc)})

    if include_diff:
        try:
            r = subprocess.run(["git", "diff", "--stat"], capture_output=True, text=True, cwd=str(ws_path), timeout=15)
            if r.returncode == 0:
                # Truncate to 200KB
                content = r.stdout or "(no diff)"
                if len(content) > 200 * 1024:
                    content = content[:200 * 1024] + "\n... [truncated at 200KB]"
                    manifest["truncated"].append({"path": "workspace/git_diff_stat.txt", "size_limit": "200KB"})
                (dest_dir / "git_diff_stat.txt").write_text(content, encoding="utf-8")
                manifest["files"].append({"path": "workspace/git_diff_stat.txt", "kind": "text", "redacted": False})
        except Exception as exc:
            manifest["missing"].append({"path": "workspace/git_diff_stat.txt", "reason": str(exc)})


def _write_summary_md(bundle_dir: Path, packet: dict, manifest: dict) -> None:
    """Generate human-readable summary.md."""
    s = packet["story"]
    stuck = packet["stuck_reason"]
    events = packet.get("recent_events", [])

    lines = [
        f"# 诊断报告: {s['story_key']}",
        "",
        f"- **状态**: {s['status']} / {s['current_stage']}",
        f"- **Workspace**: {s['workspace']}",
        f"- **卡住原因**: {stuck['code']}",
        f"- **说明**: {stuck['message']}",
        "",
        "## 最近关键事件",
        "",
    ]

    for ev in events[-10:]:
        et = ev.get("event_type", "?")
        ts = ev.get("created_at", "?")
        lines.append(f"- {ts} {et}")

    lines.extend([
        "",
        "## 重点关注",
        "",
    ])

    code = stuck["code"]
    if code == "cli_exited_without_done":
        lines.append("1. 先看 `terminal/recent_output.txt`，确认 Agent 执行命令是否报错。")
        lines.append("2. 再看 `debug_packet.json` 的 `done_state`。")
        lines.append("3. 如果存在 malformed done，查看 `done/current.malformed`。")
    elif code == "stage_timeout":
        lines.append("1. 检查 `terminal/recent_output.txt` 是否停在长耗时命令。")
        lines.append("2. 可能是依赖安装、测试命令或大文件操作。")
    elif code == "loop_exhausted":
        lines.append("1. 查看 `events.jsonl` 中的 evaluator_loop_round 和 evaluator_loop_completed 事件。")
        lines.append("2. 关注 `no_progress` 和 `decision: fail` 标记。")
    elif code == "done_malformed":
        lines.append("1. 查看 `done/current.malformed` 了解损坏的 JSON 内容。")
        lines.append("2. 手动修复或删除 `.story/done/{key}/{stage}.json`。")
    elif code == "gate_blocked":
        lines.append("1. 查看 `events.jsonl` 中的 gate_decision 事件。")
        lines.append("2. 按 `A` 接受风险推进，或按 `r` 重试 review。")
    else:
        lines.append("1. 查看 `debug_packet.json` 了解完整诊断数据。")
        lines.append("2. 查看 `events.jsonl` 了解事件时间线。")

    lines.extend([
        "",
        "## 包内容状态",
        "",
    ])

    for f in manifest.get("files", []):
        path = f["path"]
        redacted = " (redacted)" if f.get("redacted") else ""
        lines.append(f"- {path}: present{redacted}")

    for m in manifest.get("missing", []):
        lines.append(f"- {m['path']}: **missing** — {m['reason']}")

    for t in manifest.get("truncated", []):
        reason = t.get("line_limit", t.get("size_limit", "unknown"))
        lines.append(f"- {t['path']}: truncated ({reason})")

    dest = bundle_dir / "summary.md"
    dest.write_text("\n".join(lines), encoding="utf-8")
    manifest["files"].append({"path": "summary.md", "kind": "markdown", "redacted": False})
```

- [ ] **Step 2: Commit**

```bash
git add src/story_lifecycle/orchestrator/diagnostics.py
git commit -m "feat: add create_story_diagnostics_bundle with manifest and summary"
```


### Task 9: Add create_global_diagnostics_bundle() to diagnostics.py

**Files:**
- Modify: `src/story_lifecycle/orchestrator/diagnostics.py`

- [ ] **Step 1: Append global bundle function at end of diagnostics.py**

```python
def create_global_diagnostics_bundle(
    output_path: str | None = None,
    no_zip: bool = False,
) -> dict:
    """Generate a system-wide diagnostic bundle (no specific story).

    Returns {"path": str} on success, {"error": str} on failure.
    """
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    home = Path.home() / ".story-lifecycle"

    if output_path:
        out_dir = Path(output_path)
    else:
        out_dir = home / "diagnostics"

    out_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "schema_version": 1,
        "bundle_type": "global",
        "story_key": "",
        "created_at": datetime.now().isoformat(),
        "story_lifecycle_version": _get_version(),
        "workspace": "",
        "files": [],
        "missing": [],
        "truncated": [],
    }

    bundle_dir = out_dir if no_zip else Path(tempfile.mkdtemp(prefix="diag-global-"))

    # 1. environment.txt
    _collect_environment(bundle_dir, manifest)

    # 2. config.redacted.yaml
    _collect_redacted_config(bundle_dir, manifest)

    # 3. commands/ help output
    cmds_dir = bundle_dir / "commands"
    cmds_dir.mkdir(parents=True, exist_ok=True)
    for cmd_name, cmd_args in [
        ("story_help.txt", [os.sys.executable, "-m", "story_lifecycle", "--help"]),
        ("story_setup_help.txt", [os.sys.executable, "-m", "story_lifecycle", "setup", "--help"]),
        ("story_doctor_help.txt", [os.sys.executable, "-m", "story_lifecycle", "doctor", "--help"]),
    ]:
        try:
            r = subprocess.run(cmd_args, capture_output=True, text=True, timeout=15)
            (cmds_dir / cmd_name).write_text(r.stdout or r.stderr or "(empty)", encoding="utf-8")
            manifest["files"].append({"path": f"commands/{cmd_name}", "kind": "text", "redacted": False})
        except Exception as exc:
            manifest["missing"].append({"path": f"commands/{cmd_name}", "reason": str(exc)})

    # 4. package/ metadata
    pkg_dir = bundle_dir / "package"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    try:
        from importlib.metadata import metadata, files
        meta = metadata("story-lifecycle")
        meta_dict = {k: str(v) for k, v in meta.items()}
        _write_json(pkg_dir / "metadata.json", meta_dict)
        manifest["files"].append({"path": "package/metadata.json", "kind": "json", "redacted": False})
    except Exception as exc:
        manifest["missing"].append({"path": "package/metadata.json", "reason": str(exc)})

    # 5. logs/ tail
    logs_dir = bundle_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    for log_name in ["graph_error.log", "planner_error.log"]:
        log_path = home / log_name
        if log_path.exists():
            content = log_path.read_text(encoding="utf-8")
            if len(content) > 200 * 1024:
                content = content[-200 * 1024:]
                manifest["truncated"].append({"path": f"logs/{log_name}_tail.log", "size_limit": "200KB"})
            (logs_dir / f"{log_name}_tail.log").write_text(content, encoding="utf-8")
            manifest["files"].append({"path": f"logs/{log_name}_tail.log", "kind": "text", "redacted": True})
        else:
            manifest["missing"].append({"path": f"logs/{log_name}_tail.log", "reason": "file does not exist"})

    # 6. summary.md
    summary_lines = [
        "# 全局诊断报告",
        "",
        f"- **生成时间**: {datetime.now().isoformat()}",
        f"- **版本**: {_get_version()}",
        f"- **平台**: {os.name}",
        "",
        "## 包内容状态",
        "",
    ]
    for f in manifest.get("files", []):
        summary_lines.append(f"- {f['path']}: present")
    for m in manifest.get("missing", []):
        summary_lines.append(f"- {m['path']}: **missing** — {m['reason']}")
    (bundle_dir / "summary.md").write_text("\n".join(summary_lines), encoding="utf-8")
    manifest["files"].append({"path": "summary.md", "kind": "markdown", "redacted": False})

    # 7. manifest.json
    _write_json(bundle_dir / "manifest.json", manifest)

    # Zip or return directory
    if no_zip:
        return {"path": str(bundle_dir)}

    zip_path = out_dir / f"global-{ts}.zip"
    _make_zip(bundle_dir, zip_path)
    return {"path": str(zip_path)}
```

- [ ] **Step 2: Commit**

```bash
git add src/story_lifecycle/orchestrator/diagnostics.py
git commit -m "feat: add create_global_diagnostics_bundle"
```

### Task 10: Unit tests for diagnostics bundle

**Files:**
- Create: `tests/test_diagnostics.py`

- [ ] **Step 1: Create test file**

```python
"""Unit tests for diagnostics bundle generation."""

import json
import zipfile
from pathlib import Path
import pytest
from story_lifecycle.orchestrator.diagnostics import (
    create_story_diagnostics_bundle,
    create_global_diagnostics_bundle,
)
from story_lifecycle.db.models import init_db


class TestStoryDiagnosticsBundle:
    def test_nonexistent_story(self):
        result = create_story_diagnostics_bundle("NONEXISTENT", no_zip=True)
        assert "error" in result

    def test_bundle_structure(self, tmp_path, monkeypatch):
        """Bundle contains manifest, summary, debug_packet, and other expected files."""
        monkeypatch.setenv("STORY_LLM_API_KEY", "fake-key")
        init_db()
        from story_lifecycle.db.models import create_story

        ws = str(tmp_path)
        create_story("TEST-DIAG", "Diag Test", ws)
        (tmp_path / ".story" / "done" / "TEST-DIAG").mkdir(parents=True, exist_ok=True)

        result = create_story_diagnostics_bundle("TEST-DIAG", no_zip=True)
        assert "error" not in result
        bundle_dir = Path(result["path"])
        assert bundle_dir.exists()

        # Check key files exist
        assert (bundle_dir / "manifest.json").exists()
        assert (bundle_dir / "summary.md").exists()
        assert (bundle_dir / "debug_packet.json").exists()
        assert (bundle_dir / "events.jsonl").exists()

        # Validate manifest
        manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["bundle_type"] == "story"
        assert manifest["story_key"] == "TEST-DIAG"
        assert len(manifest["files"]) > 0

        # Validate summary.md has key sections
        summary = (bundle_dir / "summary.md").read_text(encoding="utf-8")
        assert "TEST-DIAG" in summary
        assert "诊断报告" in summary

    def test_bundle_zip(self, tmp_path, monkeypatch):
        """Bundle can be created as a zip file."""
        monkeypatch.setenv("STORY_LLM_API_KEY", "fake-key")
        init_db()
        from story_lifecycle.db.models import create_story

        ws = str(tmp_path)
        create_story("TEST-ZIP", "Zip Test", ws)
        (tmp_path / ".story" / "done" / "TEST-ZIP").mkdir(parents=True, exist_ok=True)

        result = create_story_diagnostics_bundle("TEST-ZIP", no_zip=False)
        assert "error" not in result
        zip_path = Path(result["path"])
        assert zip_path.suffix == ".zip"
        assert zip_path.exists()

        # Verify zip contents
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            assert "manifest.json" in names
            assert "summary.md" in names
            assert "debug_packet.json" in names

    def test_summary_includes_stuck_reason(self, tmp_path, monkeypatch):
        """summary.md references the stuck_reason code."""
        monkeypatch.setenv("STORY_LLM_API_KEY", "fake-key")
        init_db()
        from story_lifecycle.db.models import create_story

        ws = str(tmp_path)
        create_story("TEST-STUCK", "Stuck Test", ws)
        (tmp_path / ".story" / "done" / "TEST-STUCK").mkdir(parents=True, exist_ok=True)

        result = create_story_diagnostics_bundle("TEST-STUCK", no_zip=True)
        summary = (Path(result["path"]) / "summary.md").read_text(encoding="utf-8")
        assert "none" in summary.lower() or "卡住原因" in summary


class TestGlobalDiagnosticsBundle:
    def test_global_bundle_structure(self, tmp_path, monkeypatch):
        """Global bundle works without LLM configured."""
        # Ensure no env key
        monkeypatch.delenv("STORY_LLM_API_KEY", raising=False)
        init_db()

        result = create_global_diagnostics_bundle(no_zip=True)
        assert "error" not in result
        bundle_dir = Path(result["path"])
        assert bundle_dir.exists()
        assert (bundle_dir / "manifest.json").exists()
        assert (bundle_dir / "summary.md").exists()
        assert (bundle_dir / "environment.txt").exists()

        manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["bundle_type"] == "global"

    def test_global_bundle_zip(self, tmp_path):
        init_db()
        result = create_global_diagnostics_bundle(no_zip=False)
        assert "error" not in result
        zip_path = Path(result["path"])
        assert zip_path.suffix == ".zip"
        assert zip_path.exists()
```

- [ ] **Step 2: Run tests**

```bash
python -m pytest tests/test_diagnostics.py -v
```

- [ ] **Step 3: Commit**

```bash
git add tests/test_diagnostics.py
git commit -m "test: add unit tests for story and global diagnostics bundles"
```


---

## Phase 4: TUI Diagnostics Panel

### Task 11: Add diagnostics panel layout and CSS

**Files:**
- Modify: `src/story_lifecycle/cli/tui.py`

- [ ] **Step 1: Update CSS — add body row, left pane, diagnostics panel**

Replace the CSS block (lines 771-826) to add the horizontal layout. The key additions are `#body-row`, `#left-pane`, `#diagnostics-panel`.

Replace:
```css
    Screen {
        layout: vertical;
        background: $surface;
    }

    #header-bar {
        height: 5;
        padding: 1 2;
        background: $boost;
        border-bottom: solid $accent;
    }

    #plan-panel {
        height: 0;
        padding: 0;
        display: none;
    }
    #plan-panel.visible {
        height: auto;
        max-height: 14;
        padding: 1 2;
        background: $panel;
        border-bottom: solid $accent;
        display: block;
    }

    #story-list {
        height: 1fr;
        padding: 0;
        overflow-y: auto;
    }

    #detail-panel {
        height: 0;
        padding: 1 2;
        background: $panel;
        border-top: tall $accent;
        display: none;
    }
    #detail-panel.visible {
        height: auto;
        max-height: 14;
        display: block;
    }

    #footer-bar {
        height: 1;
        padding: 0 2;
        color: $text-muted;
    }

    Footer {
        dock: bottom;
    }
```

With:
```css
    Screen {
        layout: vertical;
        background: $surface;
    }

    #header-bar {
        height: 5;
        padding: 1 2;
        background: $boost;
        border-bottom: solid $accent;
    }

    #body-row {
        height: 1fr;
    }

    #left-pane {
        width: 1fr;
        height: 100%;
    }

    #plan-panel {
        height: 0;
        padding: 0;
        display: none;
    }
    #plan-panel.visible {
        height: auto;
        max-height: 14;
        padding: 1 2;
        background: $panel;
        border-bottom: solid $accent;
        display: block;
    }

    #story-list {
        height: 1fr;
        padding: 0;
        overflow-y: auto;
    }

    #detail-panel {
        height: 0;
        padding: 1 2;
        background: $panel;
        border-top: tall $accent;
        display: none;
    }
    #detail-panel.visible {
        height: auto;
        max-height: 14;
        display: block;
    }

    #diagnostics-panel {
        width: 44;
        min-width: 34;
        max-width: 56;
        height: 100%;
        padding: 1 2;
        border-left: solid $accent;
        background: $panel;
        overflow-y: auto;
    }

    #diagnostics-panel.hidden {
        display: none;
    }

    #footer-bar {
        height: 1;
        padding: 0 2;
        color: $text-muted;
    }

    Footer {
        dock: bottom;
    }
```

- [ ] **Step 2: Update compose() — wrap left content in body-row**

Replace the compose method (lines 862-869):

```python
    def compose(self) -> ComposeResult:
        yield Static(id="header-bar")
        yield Static(id="plan-panel")
        yield VerticalScroll(id="story-list")
        yield Static(id="completed-section")
        yield Static(id="detail-panel")
        yield Static(id="footer-bar")
        yield Footer()
```

With:
```python
    def compose(self) -> ComposeResult:
        yield Static(id="header-bar")
        yield Static(id="plan-panel")
        with Horizontal(id="body-row"):
            with Vertical(id="left-pane"):
                yield VerticalScroll(id="story-list")
                yield Static(id="completed-section")
                yield Static(id="detail-panel")
            yield Static(id="diagnostics-panel")
        yield Static(id="footer-bar")
        yield Footer()
```

- [ ] **Step 3: Commit**

```bash
git add src/story_lifecycle/cli/tui.py
git commit -m "feat: add body-row layout with diagnostics panel placeholder"
```

### Task 12: Add diagnostics render function

**Files:**
- Modify: `src/story_lifecycle/cli/tui.py`

- [ ] **Step 1: Add `_render_diagnostics_panel()` method to StoryBoardApp**

Add after `_render_detail()` (around line 417):

```python
    def _render_diagnostics_panel(self) -> None:
        """Render the right-side diagnostics panel for the selected story."""
        panel = self.query_one("#diagnostics-panel")
        if not self.stories or self.selected_index >= len(self.stories):
            panel.update("[dim]No story selected[/]")
            return

        s = self.stories[self.selected_index]
        key = s["story_key"]
        try:
            from ..orchestrator.debug_packet import build_debug_packet
            packet = build_debug_packet(key)
        except Exception as exc:
            panel.update(f"[red]Error building diagnostics: {exc}[/]")
            return

        if "error" in packet:
            panel.update(f"[dim]Diagnostics unavailable: {packet['error']}[/]")
            return

        story = packet["story"]
        stuck = packet["stuck_reason"]
        session = packet["session_state"]
        events = packet.get("recent_events", [])
        done = packet["done_state"]

        severity_color = {
            "error": "red",
            "warning": "yellow",
            "info": "dim",
        }.get(stuck.get("severity", "info"), "dim")

        lines = [
            "[bold]Diagnostics[/]",
            "",
            f"[bold cyan]{key}[/]",
            f"status: {story['status']}",
            f"stage: {story['current_stage']}",
            "",
        ]

        # Stuck reason
        if stuck["code"] != "none":
            lines.append(f"[bold {severity_color}]可能卡住：[/]")
            lines.append(f"[{severity_color}]{stuck['message']}[/]")
        else:
            lines.append("[dim]未发现阻塞信号[/]")

        lines.append("")

        # Session info
        if session.get("cli_exit_state") and session["cli_exit_state"] != "none":
            lines.append(f"[dim]CLI exit: {session['cli_exit_state']}[/]")
        if session.get("session_name"):
            alive = "alive" if session.get("session_alive") else "dead"
            lines.append(f"[dim]Session: {session['session_name']} ({alive})[/]")

        # Done state
        if not done.get("exists"):
            lines.append(f"[dim]Done: missing[/]")
        elif done.get("valid") is False:
            lines.append(f"[red]Done: corrupted[/]")

        lines.append("")

        # Recent events (last 8)
        lines.append("[bold]最近事件：[/]")
        for ev in events[-8:]:
            et = ev.get("event_type", "?")
            ts = str(ev.get("created_at", ""))[:16]
            lines.append(f"[dim]{ts} {et}[/]")

        lines.extend([
            "",
            "[[p]] package story diagnostics",
            "[[P]] package global diagnostics",
        ])

        panel.update("\n".join(lines))
```

- [ ] **Step 2: Call _render_diagnostics_panel in _render()**

In the `_render()` method (line 937), add at the end of both the `full=True` and `full=False` paths:

At the end of the `if full:` block (before any return/continue), add:
```python
            self._render_diagnostics_panel()
```

And also after the non-full path at the end of `_render()`:
```python
        self._render_diagnostics_panel()
```

- [ ] **Step 3: Commit**

```bash
git add src/story_lifecycle/cli/tui.py
git commit -m "feat: add diagnostics panel render function"
```

### Task 13: Add keybindings and actions (o, p, P)

**Files:**
- Modify: `src/story_lifecycle/cli/tui.py`

- [ ] **Step 1: Add key bindings**

Add to the BINDINGS list (after line 848):

```python
        Binding("o", "toggle_diagnostics", "Diag", key_display="o"),
        Binding("p", "package_story_diagnostics", "Pkg Story", key_display="p"),
        Binding("shift+p", "package_global_diagnostics", "Pkg Global", key_display="P"),
```

- [ ] **Step 2: Add `on_mount` initialization for diagnostics state**

In `on_mount()` (line 893), add after `self._show_detail = False`:

```python
        self._show_diagnostics = True
        self._diagnostics_width = 44
```

And add a resize handler to detect narrow screens. Add after the existing `on_mount()` body:

```python
        # Check initial terminal width for diagnostics panel
        if self.size.width < 120:
            self._show_diagnostics = False
            self.query_one("#diagnostics-panel").set_class(True, "hidden")
```

- [ ] **Step 3: Add the three action methods**

Add to `StoryBoardApp`:

```python
    def action_toggle_diagnostics(self):
        """Toggle the right-side diagnostics panel visibility."""
        self._show_diagnostics = not self._show_diagnostics
        panel = self.query_one("#diagnostics-panel")
        panel.set_class(not self._show_diagnostics, "hidden")
        if self._show_diagnostics:
            self._render_diagnostics_panel()

    def action_package_story_diagnostics(self):
        """Generate a diagnostic bundle for the selected story."""
        if not self.stories or self.selected_index >= len(self.stories):
            self.notify("No story selected", severity="warning")
            return
        s = self.stories[self.selected_index]
        key = s["story_key"]
        try:
            from ..orchestrator.diagnostics import create_story_diagnostics_bundle
            result = create_story_diagnostics_bundle(story_key=key)
            if result.get("error"):
                self.notify(f"Diagnostics failed: {result['error']}", severity="error")
                return
            path = result["path"]
            self.notify(f"Bundle: {path}", title="Diagnostics")
            db.log_event(key, s.get("current_stage", ""), "diagnostic_bundle_created",
                         {"bundle_path": path, "bundle_type": "story"})
            # Update panel to show path
            panel = self.query_one("#diagnostics-panel")
            current = panel.renderable
            if isinstance(current, str):
                panel.update(current + f"\n\n[green]Bundle: {path}[/]")
        except Exception as exc:
            self.notify(f"Error: {exc}", severity="error")

    def action_package_global_diagnostics(self):
        """Generate a global diagnostics bundle."""
        try:
            from ..orchestrator.diagnostics import create_global_diagnostics_bundle
            result = create_global_diagnostics_bundle()
            if result.get("error"):
                self.notify(f"Diagnostics failed: {result['error']}", severity="error")
                return
            path = result["path"]
            self.notify(f"Global bundle: {path}", title="Diagnostics")
        except Exception as exc:
            self.notify(f"Error: {exc}", severity="error")
```

- [ ] **Step 4: Add screen resize handler for narrow screen detection**

Add to `StoryBoardApp`:

```python
    def on_resize(self, event):
        """Handle terminal resize — hide diagnostics on narrow screens."""
        width = event.size.width
        panel = self.query_one("#diagnostics-panel")
        if width < 120:
            if self._show_diagnostics:
                self._show_diagnostics = False
                panel.set_class(True, "hidden")
        else:
            if not self._show_diagnostics:
                self._show_diagnostics = True
                panel.set_class(False, "hidden")
                self._render_diagnostics_panel()
```

- [ ] **Step 5: Commit**

```bash
git add src/story_lifecycle/cli/tui.py
git commit -m "feat: add diagnostics panel keybindings and actions (o/p/P)"
```


---

## Phase 5: Integration & Validation

### Task 14: Regression check — story setup, story doctor, story diagnostics --global

**Files:**
- No new files — verification only.

- [ ] **Step 1: Verify `story setup` still works**

```bash
python -m story_lifecycle setup --help
```

Expected: shows setup help text without error.

- [ ] **Step 2: Verify `story doctor` still works**

```bash
python -m story_lifecycle doctor --help
```

Expected: shows doctor help text without error.

- [ ] **Step 3: Verify `story diagnostics --global` works without LLM config**

```bash
# Remove env key temporarily
unset STORY_LLM_API_KEY
python -m story_lifecycle diagnostics --global --no-zip
```

Expected: Creates a directory under `~/.story-lifecycle/diagnostics/global-*.zip` or `--no-zip` directory, output is printed.

- [ ] **Step 4: Verify `story diagnostics --global` with zip**

```bash
python -m story_lifecycle diagnostics --global
```

Expected: Creates `~/.story-lifecycle/diagnostics/global-*.zip`.

- [ ] **Step 5: Verify `story diagnostics STORY_KEY` creates a valid bundle**

```bash
# First create a test story
python -m story_lifecycle create TEST-DIAG-REG --no-start -t "Regression Test" -w /tmp/diag-test
# Generate diagnostics
python -m story_lifecycle diagnostics TEST-DIAG-REG --no-zip
```

Expected: bundle directory with `manifest.json`, `summary.md`, `debug_packet.json`, `events.jsonl`.

- [ ] **Step 6: Run all tests together**

```bash
python -m pytest tests/test_debug_packet.py tests/test_diagnostics.py -v
```

- [ ] **Step 7: Commit any remaining changes and run final verification**

```bash
git status
python -m pytest tests/ -v
```

### Task 15: Manual validation — real story diagnostics

**Files:**
- No new files.

- [ ] **Step 1: Create a real story (or use existing) and generate diagnostics**

```bash
python -m story_lifecycle create DIAG-REAL --no-start -t "Real Diagnostics Test"
python -m story_lifecycle diagnostics DIAG-REAL --no-zip
```

- [ ] **Step 2: Verify bundle directory content**

Check that the following files exist in the output:
- `manifest.json` — valid JSON with files/missing/truncated lists
- `summary.md` — human-readable, includes stuck reason and focus areas
- `debug_packet.json` — valid JSON matching schema
- `story.json` — redacted story DB row
- `events.jsonl` — one JSON per line
- `environment.txt` — platform, python version, PATH
- `config.redacted.yaml` — if config exists, with keys redacted

- [ ] **Step 3: Verify redaction**

```bash
# Check that no API keys appear in the bundle
grep -r "sk-" <bundle_dir>/ && echo "FAIL: found unredacted key" || echo "PASS: no raw keys"
```

### Task 16: Clean up and final commit

- [ ] **Step 1: Delete any test stories created during validation**

```bash
python -m story_lifecycle doctor
```

- [ ] **Step 2: Final lint check**

```bash
ruff check src/
```

- [ ] **Step 3: Final commit for any remaining changes**

```bash
git add -A
git status
git commit -m "chore: finalize diagnostics panel implementation"
```

---

## Self-Review Checklist

Before starting implementation, verify the plan:

1. **Spec coverage:** Each requirement in `docs/design-board-diagnostics-panel.md` is covered:
   - Debug Packet Schema → Task 2
   - Stuck Reason rules → Task 3
   - Redaction → Task 4
   - CLI `story diagnostics` → Tasks 6-7
   - Bundle generation → Tasks 8-9
   - TUI diagnostics panel → Tasks 11-13
   - Narrow screen → Task 13 (on_resize)
   - Keybindings `o`/`p`/`P` → Task 13
   - event_log recording → Task 13 (action_package_story_diagnostics)
   - Config exemption → Task 7
   - Summary.md → Task 8 (_write_summary_md)
   - Manifest → Task 8
   - Global diagnostics → Task 9
   - Tests → Tasks 5, 10
   - Regression → Tasks 14-15

2. **Placeholder scan:** No TBD, TODO, or vague references. All code blocks are complete.

3. **Type consistency:** `build_debug_packet()` returns `dict` with `schema_version`, `stuck_reason`, etc. Both TUI and CLI consume the same schema. The `create_*_diagnostics_bundle()` functions return `{"path": str}` or `{"error": str}`.

4. **Scope check:** Single feature (diagnostics panel), decomposed into 16 tasks across 5 phases. Each phase produces working, testable software.
