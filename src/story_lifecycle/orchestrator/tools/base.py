"""BaseTool — shared session launch and state update logic."""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import time
from pathlib import Path

from ...adapters import get_adapter
from ...db import models as db
from ...terminal import ttyd
from ...terminal.platform_ops import subprocess_needs_shell

log = logging.getLogger("story-lifecycle.base-tool")

_HEADLESS_TIMEOUT = 3600


class BaseTool:
    """Base class for all execution tools. Provides common session management."""

    def _launch_in_session(self, state: dict, args: dict, prompt: str) -> dict:
        """Launch a CLI adapter with the given prompt.

        Tries multiplexer session first; falls back to launching in a new
        terminal window (reliable on all platforms including Windows).

        In headless mode (_tui_app is None), runs the CLI via subprocess.run
        with the adapter's headless_launch_cmd — no terminal window needed.

        Returns updated state with execution_count, stage_start_time, etc.
        """
        key = state["story_key"]
        workspace = str(Path(state["workspace"]).resolve())
        adapter_name = args.get("adapter", "claude")
        model = args.get("model", "sonnet")
        tool_name = getattr(self, "_tool_name", self.__class__.__name__)

        adapter = get_adapter(adapter_name)

        provider = args.get("provider")
        if provider:
            try:
                adapter.switch_provider(provider)
            except Exception:
                pass

        launch = adapter.launch_cmd(model)
        session = ttyd.session_name(key)

        # Headless first: if no TUI app, always use subprocess — never
        # inject into Zellij sessions even if one happens to exist.
        from ..graph import _tui_app

        if _tui_app is None:
            headless_fn = getattr(adapter, "headless_launch_cmd", None)
            cmd = headless_fn(model, prompt) if headless_fn else None
            if cmd is not None:
                return self._run_headless(
                    state,
                    cmd,
                    prompt,
                    workspace,
                    adapter_name,
                    model,
                    tool_name,
                )
            # Adapter doesn't support headless — fall through to session launch
            log.warning(
                "Adapter %s does not support headless, falling back to launch_cli",
                adapter_name,
            )

        # TUI mode: try injecting into an existing healthy session first.
        # Never create_session + send_keys on Windows/Zellij — the background
        # pane may be empty (no ConPTY), causing prompt to disappear.
        injected = False
        if ttyd.session_alive(session):
            ttyd.send_keys(session, "C-c")
            time.sleep(0.5)
            ttyd.send_keys(session, launch, "Enter")
            time.sleep(8)
            ttyd.paste_text(session, prompt)
            ttyd.send_keys(session, "Enter")
            injected = True
            ttyd._mplex_launched.add(key)

        foreground_zellij = False
        if not injected:
            tmp = (
                Path(tempfile.gettempdir())
                / f"story-prompt-{key}-{state['current_stage']}.md"
            )
            tmp.write_text(prompt, encoding="utf-8")

            # TUI running — try zellij foreground, else terminal window
            zellij_args = ttyd.zellij_execution_args(key, workspace, launch, str(tmp))
            if zellij_args is not None:
                from ..graph import emit_terminal_request

                emit_terminal_request(key, zellij_args)
                foreground_zellij = True
            else:
                ttyd.launch_cli(key, workspace, launch, str(tmp))
                ttyd._mplex_launched.add(key)

        from ..graph import emit_terminal_opened

        if not foreground_zellij:
            emit_terminal_opened(key)

        state["execution_count"] = state.get("execution_count", 0) + 1
        state["stage_start_time"] = time.time()
        state["last_error"] = None

        db.log_event(
            key,
            state["current_stage"],
            "execute",
            {
                "attempt": state["execution_count"],
                "tool": tool_name,
                "adapter": adapter_name,
                "provider": args.get("provider"),
                "model": model,
            },
        )
        db.update_story(key, execution_count=state["execution_count"], last_error=None)
        return state

    def _run_headless(
        self,
        state: dict,
        cmd: list[str],
        prompt: str,
        workspace: str,
        adapter_name: str,
        model: str,
        tool_name: str,
    ) -> dict:
        """Run CLI synchronously via subprocess in headless mode.

        Pipes *prompt* via stdin (avoids OS command-line length limits).
        Blocks until the CLI exits.  Updates state with execution metadata
        and returns.  The caller should return this state immediately
        (early return from _launch_in_session).
        """
        key = state["story_key"]
        state["execution_count"] = state.get("execution_count", 0) + 1
        state["stage_start_time"] = time.time()
        state["last_error"] = None

        # Budget-aware timeout: read from context (swebench manifest stores it)
        timeout = _HEADLESS_TIMEOUT
        ctx = state.get("context") or {}
        budget = ctx.get("budget")
        if isinstance(budget, dict) and budget.get("timeout_seconds"):
            timeout = budget["timeout_seconds"]

        db.log_event(
            key,
            state["current_stage"],
            "execute",
            {
                "attempt": state["execution_count"],
                "tool": tool_name,
                "adapter": adapter_name,
                "model": model,
                "headless": True,
            },
        )
        db.update_story(key, execution_count=state["execution_count"], last_error=None)

        log.info(
            "Headless mode: running %s for %s (timeout=%ds)", adapter_name, key, timeout
        )
        try:
            result = subprocess.run(
                cmd,
                cwd=workspace,
                input=prompt,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=subprocess_needs_shell(),
            )
            if result.returncode != 0:
                stderr_snippet = (result.stderr or "")[:500]
                log.warning(
                    "Headless CLI exited %d for %s: %s",
                    result.returncode,
                    key,
                    stderr_snippet,
                )
                state["last_error"] = (
                    f"Headless CLI exited {result.returncode}: {stderr_snippet}"
                )
                db.update_story(key, last_error=state["last_error"])
            else:
                log.info("Headless CLI completed for %s", key)
                self._synth_done_file(state, result.stdout)
        except subprocess.TimeoutExpired:
            log.error("Headless CLI timed out (%ds) for %s", _HEADLESS_TIMEOUT, key)
            state["last_error"] = f"Headless CLI timed out after {_HEADLESS_TIMEOUT}s"
            db.update_story(key, last_error=state["last_error"])

        return state

    def _synth_done_file(self, state: dict, stdout: str) -> None:
        """Write a synthetic .story/done file from headless CLI stdout.

        The CLI (e.g. ``claude -p``) does not reliably write the handshake
        file itself.  We parse its stdout for JSON output; if that fails we
        write a minimal marker so ``poll_completion_node`` can proceed.
        """
        key = state["story_key"]
        stage = state["current_stage"]
        workspace = state["workspace"]
        done_dir = Path(workspace) / ".story" / "done" / key
        done_dir.mkdir(parents=True, exist_ok=True)
        done_path = done_dir / f"{stage}.json"

        # Try to find JSON blob in stdout (claude --output-format json)
        data = None
        if stdout and stdout.strip():
            import re as _re

            # Look for fenced JSON block first (```json ... ```)
            m = _re.search(r"```json\s*\n(.*?)```", stdout, _re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group(1))
                except (json.JSONDecodeError, ValueError):
                    pass
            # Try the whole stdout as JSON
            if data is None:
                try:
                    data = json.loads(stdout.strip())
                except (json.JSONDecodeError, ValueError):
                    pass

        if not isinstance(data, dict):
            data = {"output": (stdout or "")[:2000], "synthetic": True}

        # Auto-discover artifacts the CLI may have written but not reported.
        # Design stage often writes docs/design*.md without setting spec_path.
        if stage == "design" and "spec_path" not in data:
            docs_dir = Path(workspace) / "docs"
            if docs_dir.is_dir():
                candidates = sorted(docs_dir.glob("design*.md"))
                if candidates:
                    data["spec_path"] = f"docs/{candidates[-1].name}"

        done_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log.info("Synthesized .story/done/%s/%s.json for %s", key, stage, key)

    def describe(self) -> str:
        return self.__doc__ or self.__class__.__name__
