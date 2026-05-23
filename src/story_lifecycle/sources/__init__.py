from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import StorySource

_registry: dict[str, Callable[[dict], StorySource]] = {}


def register_source(name: str, factory: Callable[[dict], StorySource]):
    _registry[name] = factory


def get_source(name: str, config: dict | None = None) -> StorySource | None:
    factory = _registry.get(name)
    if config is None:
        from ..cli.setup import get_config
        config = get_config().get("story_source", {}).get(name, {})
    return factory(config or {}) if factory else None


def get_available_sources() -> list[str]:
    return list(_registry.keys())
