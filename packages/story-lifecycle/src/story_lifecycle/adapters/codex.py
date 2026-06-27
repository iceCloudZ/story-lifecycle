"""Codex CLI adapter."""

from .base import BaseAdapter


class CodexAdapter(BaseAdapter):
    """Adapter for OpenAI Codex CLI (codex)."""

    name = "codex"

    def switch_provider(self, provider: str) -> str | None:
        return None

    def launch_cmd(self, model: str) -> str:
        return "codex"

    def inject_prompt(self, prompt: str, story_key: str, stage: str) -> str | None:
        """Prompt injection is handled by ttyd.paste_text() in nodes.py."""
        # I2: record anchor (best-effort, paste path still returns None).
        self.write_anchor(prompt, story_key, stage)
        return None

    def cleanup(self, story_key: str, stage: str):
        pass
