"""Base adapter — defines the interface all CLI adapters must implement."""

import os
import shlex
from abc import ABC, abstractmethod


class BaseAdapter(ABC):
    """Abstract interface for AI coding CLI tools."""

    @abstractmethod
    def switch_provider(self, provider: str) -> str | None:
        """Return the shell command to switch provider, or None if not needed."""
        ...

    @abstractmethod
    def launch_cmd(self, model: str) -> str:
        """Return the command to launch the CLI interactively in a session."""
        ...

    @abstractmethod
    def inject_prompt(self, prompt: str, story_key: str, stage: str) -> str | None:
        """Return the shell command to inject a prompt into the running CLI,
        or None if prompt injection is handled by ttyd.paste_text()."""
        ...

    def headless_launch_cmd(self, model: str, prompt: str) -> list[str] | None:
        """Return command args for non-interactive headless execution.

        The prompt is piped via stdin, NOT passed as a CLI argument —
        avoids OS command-line length limits on long prompts.

        Returns None if the adapter does not support headless mode.
        Subclasses should override when their CLI has a native
        non-interactive execution flag (e.g. claude -p, codex -q).
        """
        return None

    def interactive_launch_cmd(self, model: str) -> list[str]:
        """Return argv for an interactive PTY process."""
        return shlex.split(self.launch_cmd(model), posix=os.name != "nt")

    def cleanup(self, story_key: str, stage: str):
        """Clean up temp files after stage completion. Override if needed."""
        pass

    def enter_session_cmd(self, session_name: str, workspace: str) -> str:
        """Command to create and enter a multiplexer session."""
        from ..terminal import ttyd

        return ttyd.enter_session_cmd(session_name, workspace)
