"""Claude Code adapter."""

from .base import BaseAdapter
from ...infra.terminal.platform_ops import resolve_executable


class ClaudeAdapter(BaseAdapter):
    """Adapter for Claude Code CLI (claude)."""

    name = "claude"

    def switch_provider(self, provider: str) -> str | None:
        # No-op: provider switching is not supported for the Claude CLI.
        # (Previously this shelled out to `cc use <provider>` via bash -c with an
        # unescaped provider argument — a command-injection vector, and the call
        # site was dead in production. Kept as a safe no-op to satisfy the
        # BaseAdapter abstract interface.)
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
