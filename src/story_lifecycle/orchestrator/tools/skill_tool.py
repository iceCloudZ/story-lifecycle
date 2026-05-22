"""SkillTool — execute a pure skill command without full stage orchestration."""

from __future__ import annotations

from ...db import models as db
from ...terminal import ttyd


class SkillTool:
    """Execute a skill-only stage (no adapter launch, just skill invocation)."""

    def execute(self, state: dict, args: dict) -> dict:
        """Run a skill in the multiplexer session.

        Args:
            state: StoryState dict.
            args: Config with keys: skill, prompt, adapter, provider, model.
        """
        import time

        key = state["story_key"]
        stage = state["current_stage"]
        workspace = state["workspace"]

        adapter_name = args.get("adapter", "claude")
        model = args.get("model", "sonnet")
        skill = args.get("skill", "")
        prompt = args.get("prompt", "")

        # Ensure session exists
        ttyd.ensure_ttyd(key, workspace)
        session = ttyd.session_name(key)

        if not ttyd.session_alive(session):
            ttyd.create_session(session, workspace)

        # Build skill command
        from ...adapters import get_adapter

        adapter = get_adapter(adapter_name)
        launch = adapter.launch_cmd(model)
        ttyd.send_keys(session, launch, "Enter")
        time.sleep(8)

        # Inject skill instruction
        skill_prompt = f"请执行 skill: `{skill}`\n\n{prompt}"
        ttyd.paste_text(session, skill_prompt)
        ttyd.send_keys(session, "Enter")

        state["execution_count"] = state.get("execution_count", 0) + 1
        state["stage_start_time"] = time.time()
        state["last_error"] = None

        db.log_stage(key, stage, "execute_skill", f"Skill: {skill}")
        db.log_event(key, stage, "execute", {
            "attempt": state["execution_count"],
            "tool": "skill_tool",
            "skill": skill,
        })
        db.update_story(key, execution_count=state["execution_count"], last_error=None)

        return state
