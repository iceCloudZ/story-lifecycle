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
import sys

from ...infra.config import get_config
from .base import BaseStoryContextProvider

log = logging.getLogger("story-lifecycle.context_providers")

# module:class -> provider instance cache (config taken at first load)
_PROVIDERS: dict[str, BaseStoryContextProvider] = {}


def _load_provider(cfg: dict) -> BaseStoryContextProvider:
    """Import + instantiate the configured provider, cached by module:class.

    If ``path`` is set, it is prepended to sys.path so providers in packages
    that aren't pip-installed (e.g. a local transcript miner) can be imported.
    """
    key = f"{cfg.get('module')}:{cfg.get('class')}"
    cached = _PROVIDERS.get(key)
    if cached is not None:
        return cached
    extra = cfg.get("path")
    if extra and extra not in sys.path:
        sys.path.insert(0, extra)
    module = importlib.import_module(cfg["module"])
    cls = getattr(module, cfg["class"])
    provider = cls(config=cfg)
    _PROVIDERS[key] = provider
    return provider


def _default_provider_cfg() -> dict | None:
    """Auto-enable transcript context provider when story-miner is installed.

    If the user hasn't configured a provider, but ``miner`` is importable,
    default to ``TranscriptStoryContextProvider`` backed by ``miner.config.DB_PATH``.
    This keeps {transcript_context} injection on by default in the monorepo.
    """
    try:
        import miner.config as _mc

        return {
            "module": "miner.story_context_provider",
            "class": "TranscriptStoryContextProvider",
            "db_path": _mc.DB_PATH,
        }
    except Exception:
        return None


def get_transcript_context(story_key: str, workspace: str, stage: str) -> str | None:
    """Return historical transcript context for this story/stage, or None.

    If no provider is explicitly configured, falls back to the bundled
    ``miner.story_context_provider`` when ``story-miner`` is available.
    Any failure (missing config, import error, provider exception) returns None
    so prompt rendering is NEVER blocked by a provider.
    """
    cfg = get_config().get("context_provider") or _default_provider_cfg() or {}
    if not (cfg.get("module") and cfg.get("class")):
        return None
    try:
        provider = _load_provider(cfg)
        return provider.get_context(story_key, workspace, stage)
    except Exception as exc:  # noqa: BLE001 — provider must never block prompts
        log.warning("context provider failed for %s/%s: %s", story_key, stage, exc)
        return None


def get_knowledge_context(story_key: str, workspace: str, stage: str) -> str | None:
    """Return mined knowledge context for this story/stage, or None.

    Reads story-miner output artifacts and returns a task_type-specific
    summary. Any failure returns None so prompt rendering is never blocked.
    """
    try:
        from .knowledge_provider import KnowledgeContextProvider

        provider = KnowledgeContextProvider()
        return provider.get_context(story_key, workspace, stage)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "knowledge context provider failed for %s/%s: %s", story_key, stage, exc
        )
        return None


__all__ = [
    "BaseStoryContextProvider",
    "get_transcript_context",
    "get_knowledge_context",
]
