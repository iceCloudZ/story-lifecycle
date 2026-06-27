"""Context provider loader.

Reads ``~/.story-lifecycle/config.yaml``'s ``context_provider`` section and
dynamically loads the configured provider class via importlib — mirroring the
adapter plugin pattern. story-lifecycle never imports a concrete provider at
module level; providers are optional plugins.

Config schema::

    context_provider:
      module: "miner.story_context_provider"
      class: "TranscriptStoryContextProvider"
      db_path: "/path/to/transcripts.db"   # provider-specific kwargs

Any failure (missing config, import error, provider exception) returns None so
prompt rendering is NEVER blocked by a provider.
"""

import importlib
import logging

from ..cli.setup import get_config
from .base import BaseStoryContextProvider

log = logging.getLogger("story-lifecycle.context_providers")

# module:class -> provider instance cache (config taken at first load)
_PROVIDERS: dict[str, BaseStoryContextProvider] = {}


def _load_provider(cfg: dict) -> BaseStoryContextProvider:
    """Import + instantiate the configured provider, cached by module:class."""
    key = f"{cfg.get('module')}:{cfg.get('class')}"
    cached = _PROVIDERS.get(key)
    if cached is not None:
        return cached
    module = importlib.import_module(cfg["module"])
    cls = getattr(module, cfg["class"])
    provider = cls(config=cfg)
    _PROVIDERS[key] = provider
    return provider


def get_transcript_context(
    story_key: str, workspace: str, stage: str
) -> str | None:
    """Return historical transcript context for this story/stage, or None.

    Returns None when no provider is configured (default — injection off), or
    when the provider could not be loaded / raised. Never raises.
    """
    cfg = get_config().get("context_provider") or {}
    if not (cfg.get("module") and cfg.get("class")):
        return None
    try:
        provider = _load_provider(cfg)
        return provider.get_context(story_key, workspace, stage)
    except Exception as exc:  # noqa: BLE001 — provider must never block prompts
        log.warning("context provider failed for %s/%s: %s", story_key, stage, exc)
        return None


__all__ = ["BaseStoryContextProvider", "get_transcript_context"]
