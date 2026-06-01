"""Path helpers for .story/knowledge/ layout.

All runtime code must use these helpers instead of hand-building paths.

    .story/knowledge/
      product.yaml
      manifest.yaml
      search-catalog.md
      scenarios/<domain>/
      indexes/*.md
      indexes/by-domain/<domain>.md
      graph/product-context-graph.json
      playbooks/
      declarations/
      reviews/
      events/
      cache/
"""

from __future__ import annotations

from pathlib import Path


def knowledge_dir(workspace: str | Path) -> Path:
    return Path(workspace) / ".story" / "knowledge"


def manifest_path(workspace: str | Path) -> Path:
    return knowledge_dir(workspace) / "manifest.yaml"


def product_path(workspace: str | Path) -> Path:
    return knowledge_dir(workspace) / "product.yaml"


def search_catalog_path(workspace: str | Path) -> Path:
    return knowledge_dir(workspace) / "search-catalog.md"


def graph_dir(workspace: str | Path) -> Path:
    return knowledge_dir(workspace) / "graph"


def graph_json_path(workspace: str | Path) -> Path:
    return graph_dir(workspace) / "product-context-graph.json"


def scenarios_dir(workspace: str | Path) -> Path:
    return knowledge_dir(workspace) / "scenarios"


def indexes_dir(workspace: str | Path) -> Path:
    return knowledge_dir(workspace) / "indexes"


def index_by_domain_dir(workspace: str | Path) -> Path:
    return indexes_dir(workspace) / "by-domain"


def playbooks_dir(workspace: str | Path) -> Path:
    return knowledge_dir(workspace) / "playbooks"


def declarations_dir(workspace: str | Path) -> Path:
    return knowledge_dir(workspace) / "declarations"


def reviews_dir(workspace: str | Path) -> Path:
    return knowledge_dir(workspace) / "reviews"


def events_dir(workspace: str | Path) -> Path:
    return knowledge_dir(workspace) / "events"


def cache_dir(workspace: str | Path) -> Path:
    return knowledge_dir(workspace) / "cache"


def knowledge_done_file(workspace: str | Path) -> Path:
    """Done file for the PROJECT-KNOWLEDGE-INIT bootstrap."""
    return (
        Path(workspace)
        / ".story"
        / "done"
        / "PROJECT-KNOWLEDGE-INIT"
        / "knowledge_bootstrap.json"
    )


def knowledge_context_dir(workspace: str | Path, story_key: str) -> Path:
    return Path(workspace) / ".story" / "context" / story_key / "knowledge-context"
