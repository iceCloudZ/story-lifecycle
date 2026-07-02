"""Load knowledge template files from package resources."""

from __future__ import annotations

import importlib.resources as _ir


def load_template(name: str) -> str:
    """Load a template file by name from the templates/ directory."""
    ref = _ir.files("story_lifecycle.knowledge.knowledge_store.templates").joinpath(name)
    return ref.read_text(encoding="utf-8")
