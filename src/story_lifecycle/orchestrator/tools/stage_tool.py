"""StageTool — default tool that launches a CLI adapter in a multiplexer session."""

from __future__ import annotations

from ...adapters import get_adapter
from ...db import models as db
from ...terminal import ttyd


class StageTool:
    """Execute a stage by launching an AI CLI in a multiplexer session."""

    def execute(self, state: dict, args: dict) -> dict:
        """Run the stage and return updated state.

        Args:
            state: StoryState dict.
            args: Resolved config with keys: adapter, provider, model, prompt.
        """
        import time

        key = state["story_key"]
        stage = state["current_stage"]
        workspace = state["workspace"]

        adapter_name = args.get("adapter", "claude")
        provider = args.get("provider")
        model = args.get("model", "sonnet")
        prompt = args.get("prompt", "")

        adapter = get_adapter(adapter_name)

        # 1. Switch provider
        if provider:
            adapter.switch_provider(provider)

        # 2. Ensure ttyd + session
        ttyd.ensure_ttyd(key, workspace)

        # 3. Stop existing CC
        session = ttyd.session_name(key)
        if ttyd.session_alive(session):
            ttyd.send_keys(session, "C-c")
            time.sleep(0.5)

        # 4. Create session if needed
        if not ttyd.session_alive(session):
            ttyd.create_session(session, workspace)

        # 5. Launch CLI
        launch = adapter.launch_cmd(model)
        ttyd.send_keys(session, launch, "Enter")
        time.sleep(8)

        # 6. Inject prompt
        ttyd.paste_text(session, prompt)
        ttyd.send_keys(session, "Enter")

        # 7. Track state
        state["execution_count"] = state.get("execution_count", 0) + 1
        state["stage_start_time"] = time.time()
        state["last_error"] = None

        db.log_stage(key, stage, "execute", f"Attempt {state['execution_count']}")
        db.log_event(key, stage, "execute", {
            "attempt": state["execution_count"],
            "adapter": adapter_name,
            "provider": provider,
            "model": model,
            "tool": "stage_tool",
        })
        db.update_story(key, execution_count=state["execution_count"], last_error=None)

        return state
