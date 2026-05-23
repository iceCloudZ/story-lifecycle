"""CLI adapters — abstract AI coding tools behind a uniform interface."""

from .base import BaseAdapter
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .shell import ShellAdapter, _load_adapter_configs

__all__ = ["BaseAdapter", "ClaudeAdapter", "CodexAdapter", "ShellAdapter", "get_adapter"]


def get_adapter(name: str) -> BaseAdapter:
    """Get adapter by name. Checks builtins first, then adapters.yaml."""
    builtin = {"claude": ClaudeAdapter, "codex": CodexAdapter}
    cls = builtin.get(name.lower())
    if cls:
        return cls()

    # Try config-driven adapters from adapters.yaml
    configs = _load_adapter_configs()
    if name.lower() in configs:
        return ShellAdapter(config=configs[name.lower()], name=name.lower())

    raise ValueError(
        f"Unknown CLI adapter: {name}. "
        f"Built-in: {list(builtin.keys())}. "
        f"Configured: {list(configs.keys()) or 'none (create ~/.story-lifecycle/adapters.yaml)'}"
    )
