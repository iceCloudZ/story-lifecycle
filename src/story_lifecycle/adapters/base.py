"""Base adapter — defines the interface all CLI adapters must implement."""

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

    def cleanup(self, story_key: str, stage: str):
        """Clean up temp files after stage completion. Override if needed."""
        pass

    def enter_session_cmd(self, session_name: str, workspace: str) -> str:
        """Command to create and enter a multiplexer session."""
        from ..terminal import ttyd

        return ttyd.enter_session_cmd(session_name, workspace)
