"""Claude Code adapter."""

import time
import subprocess
from pathlib import Path
from .base import BaseAdapter


class ClaudeAdapter(BaseAdapter):
    """Adapter for Claude Code CLI (claude)."""

    def switch_provider(self, provider: str) -> str | None:
        # cc use {provider} switches the active API provider
        subprocess.run(["bash", "-c", f"cc use {provider}"],
                       capture_output=True, timeout=30)
        time.sleep(2)  # provider switch takes effect next launch
        return None  # already executed, no need to return a command

    def launch_cmd(self, model: str) -> str:
        return f"claude --model {model}"

    def inject_prompt(self, prompt: str, story_key: str, stage: str) -> str:
        """Write prompt to file, inject via tmux load-buffer + paste-buffer.
           Uses tmux buffer to avoid shell escaping issues."""
        prompt_file = Path(f"/tmp/storypilot-{story_key}-{stage}.md")
        prompt_file.write_text(prompt, encoding="utf-8")

        buf_name = f"sp-{story_key}"
        # Load file content into tmux buffer, then paste
        return (
            f"tmux load-buffer -b {buf_name} {prompt_file} && "
            f"tmux paste-buffer -b {buf_name}"
        )

    def cleanup(self, story_key: str, stage: str):
        prompt_file = Path(f"/tmp/storypilot-{story_key}-{stage}.md")
        prompt_file.unlink(missing_ok=True)
