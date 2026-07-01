"""BaseTool - shared CLI execution logic."""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path

from ...adapters import get_adapter
from ...db import models as db
from ...terminal import pty as pty_terminal
from ...terminal.platform_ops import subprocess_needs_shell
from ..engine.execution import ExecutionMode, parse_execution_mode

log = logging.getLogger("story-lifecycle.base-tool")

_HEADLESS_TIMEOUT = 3600


class BaseTool:
    """Base class for all execution tools."""

    def _launch_in_session(self, state: dict, args: dict, prompt: str) -> dict:
        """Launch a CLI in the explicitly selected execution mode."""
        key = state["story_key"]
        workspace = str(Path(state["workspace"]).resolve())
        adapter_name = args.get("adapter", "claude")
        model = args.get("model", "sonnet")
        execution_mode = parse_execution_mode(args.get("execution_mode"))
        tool_name = getattr(self, "_tool_name", self.__class__.__name__)

        adapter = get_adapter(adapter_name)

        provider = args.get("provider")
        if provider:
            try:
                adapter.switch_provider(provider)
            except Exception:
                pass

        if execution_mode is ExecutionMode.HEADLESS:
            cmd = adapter.headless_launch_cmd(model, prompt)
            if cmd is None:
                raise RuntimeError(
                    f"Adapter {adapter_name!r} does not support headless execution"
                )
            state["_execution_mode"] = execution_mode.value
            state["_waiting_for_agent"] = False
            return self._run_headless(
                state,
                cmd,
                prompt,
                workspace,
                adapter_name,
                model,
                tool_name,
            )

        state["execution_count"] = state.get("execution_count", 0) + 1
        state["stage_start_time"] = time.time()
        state["last_error"] = None
        state["_execution_mode"] = execution_mode.value
        state["_waiting_for_agent"] = True
        marker = {
            "stage": state["current_stage"],
            "mode": execution_mode.value,
            "adapter": adapter_name,
            "model": model,
            "attempt": state["execution_count"],
        }
        state.setdefault("context", {})["_active_execution"] = marker

        interactive_fn = getattr(adapter, "interactive_launch_cmd", None)
        command = (
            interactive_fn(model)
            if interactive_fn is not None
            else [adapter.launch_cmd(model)]
        )
        try:
            pty_terminal.ensure_agent_pty(
                key,
                command,
                workspace,
                prompt,
            )
        except Exception as exc:
            state["_waiting_for_agent"] = False
            state["context"].pop("_active_execution", None)
            state["last_error"] = (
                f"Interactive PTY launch failed for {adapter_name}: {exc}"
            )
            db.log_event(
                key,
                state["current_stage"],
                "execute_failed",
                {
                    "tool": tool_name,
                    "adapter": adapter_name,
                    "model": model,
                    "execution_mode": execution_mode.value,
                    "error": str(exc),
                },
            )
            db.update_story(
                key,
                execution_count=state["execution_count"],
                last_error=state["last_error"],
                context_json=json.dumps(state["context"], ensure_ascii=False),
            )
            return state

        db.log_event(
            key,
            state["current_stage"],
            "execute",
            {
                "attempt": state["execution_count"],
                "tool": tool_name,
                "adapter": adapter_name,
                "provider": provider,
                "model": model,
                "execution_mode": execution_mode.value,
            },
        )
        db.update_story(
            key,
            execution_count=state["execution_count"],
            last_error=None,
            context_json=json.dumps(state["context"], ensure_ascii=False),
        )
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
        """Run a CLI synchronously via its explicit headless command."""
        key = state["story_key"]
        state["execution_count"] = state.get("execution_count", 0) + 1
        state["stage_start_time"] = time.time()
        state["last_error"] = None

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
                "execution_mode": ExecutionMode.HEADLESS.value,
                "headless": True,
            },
        )
        db.update_story(key, execution_count=state["execution_count"], last_error=None)

        log.info(
            "Headless mode: running %s for %s (timeout=%ds)",
            adapter_name,
            key,
            timeout,
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
            log.error("Headless CLI timed out (%ds) for %s", timeout, key)
            state["last_error"] = f"Headless CLI timed out after {timeout}s"
            db.update_story(key, last_error=state["last_error"])

        return state

    def _synth_done_file(self, state: dict, stdout: str) -> None:
        """Write a synthetic done file from headless CLI stdout."""
        key = state["story_key"]
        stage = state["current_stage"]
        workspace = state["workspace"]
        done_dir = Path(workspace) / ".story" / "done" / key
        done_dir.mkdir(parents=True, exist_ok=True)
        done_path = done_dir / f"{stage}.json"

        data = None
        if stdout and stdout.strip():
            import re

            match = re.search(r"```json\s*\n(.*?)```", stdout, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group(1))
                except (json.JSONDecodeError, ValueError):
                    pass
            if data is None:
                try:
                    data = json.loads(stdout.strip())
                except (json.JSONDecodeError, ValueError):
                    pass

        if not isinstance(data, dict):
            data = {"output": (stdout or "")[:2000], "synthetic": True}

        if stage == "design" and "spec_path" not in data:
            docs_dir = Path(workspace) / "docs"
            if docs_dir.is_dir():
                candidates = sorted(docs_dir.glob("design*.md"))
                if candidates:
                    data["spec_path"] = f"docs/{candidates[-1].name}"

        done_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.info("Synthesized .story/done/%s/%s.json for %s", key, stage, key)

    def describe(self) -> str:
        return self.__doc__ or self.__class__.__name__
