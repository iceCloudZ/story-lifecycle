"""CLI adapters — abstract AI coding tools behind a uniform interface."""

from .base import BaseAdapter
from .claude import ClaudeAdapter

__all__ = ["BaseAdapter", "ClaudeAdapter", "get_adapter"]


def get_adapter(name: str) -> BaseAdapter:
    """Get adapter by name. Case-insensitive."""
    adapters = {
        "claude": ClaudeAdapter,
    }
    cls = adapters.get(name.lower())
    if not cls:
        raise ValueError(
            f"Unknown CLI adapter: {name}. Available: {list(adapters.keys())}"
        )
    return cls()
