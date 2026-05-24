"""BaseTool — shared session launch and state update logic."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from ...adapters import get_adapter
from ...db import models as db
from ...terminal import ttyd


class BaseTool:
    """Base class for all execution tools. Provides common session management."""

    def _launch_in_session(self, state: dict, args: dict, prompt: str) -> dict:
        """Launch a CLI adapter with the given prompt.

        Tries multiplexer session first; falls back to launching in a new
        terminal window (reliable on all platforms including Windows).

        Returns updated state with execution_count, stage_start_time, etc.
        """
        key = state["story_key"]
        workspace = state["workspace"]
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

        # Only inject into an already-existing healthy session.
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
            # Try foreground Zellij execution (Windows + Zellij)
            tmp = (
                Path(tempfile.gettempdir())
                / f"story-prompt-{key}-{state['current_stage']}.md"
            )
            tmp.write_text(prompt, encoding="utf-8")

            zellij_args = ttyd.zellij_execution_args(key, workspace, launch, str(tmp))
            if zellij_args is not None:
                # Request TUI to hand over real terminal for foreground Zellij
                from ..graph import emit_terminal_request

                emit_terminal_request(key, zellij_args)
                foreground_zellij = True
            else:
                # Fallback: launch in a new terminal window
                ttyd.launch_cli(key, workspace, launch, str(tmp))

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

    def describe(self) -> str:
        return self.__doc__ or self.__class__.__name__
