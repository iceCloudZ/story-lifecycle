"""Claude Code adapter."""

import time
import subprocess
from .base import BaseAdapter


class ClaudeAdapter(BaseAdapter):
    """Adapter for Claude Code CLI (claude)."""

    def switch_provider(self, provider: str) -> str | None:
        try:
            subprocess.run(
                ["bash", "-c", f"cc use {provider}"], capture_output=True, timeout=30
            )
        except FileNotFoundError:
            # bash not available (e.g. Windows without Git Bash in PATH) — skip
            pass
        time.sleep(2)  # provider switch takes effect next launch
        return None

    def launch_cmd(self, model: str) -> str:
        return f"claude --model {model}"

    def inject_prompt(self, prompt: str, story_key: str, stage: str) -> str:
        """Prompt injection is handled by ttyd.paste_text() in nodes.py."""
        return None

    def cleanup(self, story_key: str, stage: str):
        pass
