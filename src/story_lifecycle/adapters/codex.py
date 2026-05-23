"""Codex CLI adapter."""

import time
import subprocess
from .base import BaseAdapter


class CodexAdapter(BaseAdapter):
    """Adapter for OpenAI Codex CLI (codex)."""

    def switch_provider(self, provider: str) -> str | None:
        return None

    def launch_cmd(self, model: str) -> str:
        return "codex"

    def inject_prompt(self, prompt: str, story_key: str, stage: str) -> str | None:
        """Prompt injection is handled by ttyd.paste_text() in nodes.py."""
        return None

    def cleanup(self, story_key: str, stage: str):
        pass
