"""Claude Code adapter."""

from .base import BaseAdapter
from ...infra.terminal.platform_ops import resolve_executable


class ClaudeAdapter(BaseAdapter):
    """Adapter for Claude Code CLI (claude)."""

    name = "claude"

    # Readiness marker for PTY-injection paths (planner's autonomous claude).
    # NOTE: claude v2.1.195's prompt is ">", not "❯" — this never matches, so
    # _wait_ready falls through to the readiness_timeout fallback (180s) before
    # injecting. Acceptable for the autonomous path (no human waiting). The
    # interactive terminal path (api.py _ensure_story_agent_pty) no longer uses
    # PTY injection — it seeds the prompt via `claude "query"`
    # (interactive_launch_cmd), letting claude manage its own readiness. See
    # docs/handoff-design-hitl.md §10.
    readiness_marker = r"❯"

    def switch_provider(self, provider: str) -> str | None:
        # No-op: provider switching is not supported for the Claude CLI.
        # (Previously this shelled out to `cc use <provider>` via bash -c with an
        # unescaped provider argument — a command-injection vector, and the call
        # site was dead in production. Kept as a safe no-op to satisfy the
        # BaseAdapter abstract interface.)
        return None

    def launch_cmd(self, model: str) -> str:
        return "claude"

    def interactive_launch_cmd(self, model: str, prompt: str = "") -> list[str]:
        # `claude "query"` opens the interactive TUI with the prompt as the
        # initial user message (auto-submitted — claude processes it once its
        # own startup finishes). This seeds the design task WITHOUT a PTY
        # injection, sidestepping the readiness-detection problem (TUI shows
        # its prompt ~6s in but isn't ready to accept pasted input until
        # ~100s; no reliable output marker for that). Verified via
        # tmp_claude_query_test.py. See docs/handoff-design-hitl.md §10.
        cmd = [resolve_executable("claude")]
        if prompt:
            cmd.append(prompt)
        return cmd

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
