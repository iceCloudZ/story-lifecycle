"""Claude Code adapter."""

import time
import subprocess
from .base import BaseAdapter
from ..terminal.platform_ops import resolve_executable


class ClaudeAdapter(BaseAdapter):
    """Adapter for Claude Code CLI (claude)."""

    name = "claude"

    def switch_provider(self, provider: str) -> str | None:
        try:
            subprocess.run(
                ["bash", "-c", f"cc use {provider}"], capture_output=True, timeout=30
            )
        except FileNotFoundError:
            pass
        time.sleep(2)
        return None

    def launch_cmd(self, model: str) -> str:
        return "claude"

    def interactive_launch_cmd(self, model: str) -> list[str]:
        return [resolve_executable("claude")]

    def headless_launch_cmd(self, model: str, prompt: str) -> list[str] | None:
        return [
            resolve_executable("claude"),
            "-p",
            "--allowedTools",
            "Bash,Read,Edit,Write,Glob,Grep",
            "--permission-mode",
            "acceptEdits",
        ]

    def inject_prompt(self, prompt: str, story_key: str, stage: str) -> str:
        # I2: record a story<->session anchor for miner.link (best-effort,
        # never affects the returned None / paste-based injection).
        self.write_anchor(prompt, story_key, stage)
        return None

    def cleanup(self, story_key: str, stage: str):
        pass
