"""Claude Code adapter."""

import time
import subprocess
from .base import BaseAdapter
from ..terminal.platform_ops import resolve_executable


class ClaudeAdapter(BaseAdapter):
    """Adapter for Claude Code CLI (claude)."""

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
        return f"claude --model {model}"

    def headless_launch_cmd(self, model: str, prompt: str) -> list[str] | None:
        # Prompt is NOT included here — piped via stdin by _run_headless.
        # --permission-mode acceptEdits: allows file edits without prompting
        return [
            resolve_executable("claude"),
            "-p",
            "--model",
            model,
            "--allowedTools",
            "Bash,Read,Edit,Write,Glob,Grep",
            "--permission-mode",
            "acceptEdits",
        ]

    def inject_prompt(self, prompt: str, story_key: str, stage: str) -> str:
        return None

    def cleanup(self, story_key: str, stage: str):
        pass
