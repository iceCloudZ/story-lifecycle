"""BaseTool — shared session launch and state update logic."""

from __future__ import annotations

import time

from ...adapters import get_adapter
from ...db import models as db
from ...terminal import ttyd


class BaseTool:
    """Base class for all execution tools. Provides common session management."""

    def _launch_in_session(self, state: dict, args: dict, prompt: str) -> dict:
        """Launch a CLI adapter in a multiplexer session with the given prompt.

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
            adapter.switch_provider(provider)

        ttyd.ensure_ttyd(key, workspace)
        session = ttyd.session_name(key)
        if ttyd.session_alive(session):
            ttyd.send_keys(session, "C-c")
            time.sleep(0.5)
        if not ttyd.session_alive(session):
            ttyd.create_session(session, workspace)

        launch = adapter.launch_cmd(model)
        ttyd.send_keys(session, launch, "Enter")
        time.sleep(8)
        ttyd.paste_text(session, prompt)
        ttyd.send_keys(session, "Enter")

        state["execution_count"] = state.get("execution_count", 0) + 1
        state["stage_start_time"] = time.time()
        state["last_error"] = None

        db.log_event(key, state["current_stage"], "execute", {
            "attempt": state["execution_count"],
            "tool": tool_name,
            "adapter": adapter_name,
            "provider": args.get("provider"),
            "model": model,
        })
        db.update_story(key, execution_count=state["execution_count"], last_error=None)
        return state

    def describe(self) -> str:
        return self.__doc__ or self.__class__.__name__
