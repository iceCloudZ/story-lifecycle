"""Claude Code adapter."""

from .base import BaseAdapter, SessionSpec
from ...infra.terminal.platform_ops import resolve_executable


class ClaudeAdapter(BaseAdapter):
    """Adapter for Claude Code CLI (claude)."""

    name = "claude"

    # Readiness marker for PTY-injection paths (planner's autonomous claude).
    # BUG #21: 兼容 claude v2.1.195 的 ">" prompt(旧值 "❯" 永不匹配 →
    # _wait_ready 空等 30s 才注入)。用 alternation 兼容多版本。
    readiness_marker = r"[>❯]"

    def switch_provider(self, provider: str) -> str | None:
        # No-op: provider switching is not supported for the Claude CLI.
        # (Previously this shelled out to `cc use <provider>` via bash -c with an
        # unescaped provider argument — a command-injection vector, and the call
        # site was dead in production. Kept as a safe no-op to satisfy the
        # BaseAdapter abstract interface.)
        return None

    def launch_cmd(self, model: str) -> str:
        return "claude"

    def interactive_launch_cmd(
        self,
        model: str,
        prompt: str = "",
        session_id: str = "",
        session_name: str = "",
        resume: bool = False,
    ) -> list[str]:
        # Deprecated: use start_session() — it returns a SessionSpec that makes
        # the prompt-delivery strategy explicit. Kept for backward compat with
        # tests that assert on the raw command.
        # `claude "query"` opens the interactive TUI with the prompt as the
        # initial user message (auto-submitted — claude manages its own readiness,
        # no PTY injection / readiness guessing). Session persistence:
        #   NEW    → claude --session-id <uuid> --name <name> "<prompt>"
        #   RESUME → claude --resume <uuid> "<prompt>"   (loads transcript, continues)
        # Both must run with the same cwd — --resume lookup is cwd-scoped.
        cmd = [resolve_executable("claude")]
        if resume and session_id:
            cmd += ["--resume", session_id]
        else:
            if session_id:
                cmd += ["--session-id", session_id]
            if session_name:
                cmd += ["--name", session_name]
        if prompt:
            cmd.append(prompt)
        return cmd

    def start_session(
        self,
        model: str,
        prompt: str = "",
        session_id: str = "",
        session_name: str = "",
        resume: bool = False,
    ) -> SessionSpec:
        # Claude bakes the seed prompt into the launch command itself
        # (`claude "query"`), so the spawner must NOT do PTY injection —
        # claude manages its own readiness. Return a spec that says so.
        return SessionSpec(
            command=self.interactive_launch_cmd(
                model,
                prompt=prompt,
                session_id=session_id,
                session_name=session_name,
                resume=resume,
            ),
            pty_prompt="",  # already in command
            readiness_marker=None,  # claude "query" self-manages readiness
            session_id=session_id,
            resume=resume,
        )

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
