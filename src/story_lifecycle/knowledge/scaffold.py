# src/story_lifecycle/knowledge/scaffold.py
"""Create .story/knowledge/ directory structure with .gitignore."""

from __future__ import annotations

from pathlib import Path

from .paths import (
    cache_dir,
    declarations_dir,
    events_dir,
    graph_dir,
    index_by_domain_dir,
    indexes_dir,
    knowledge_dir,
    playbooks_dir,
    reviews_dir,
    scenarios_dir,
)

_SUBDIRS = [
    scenarios_dir,
    indexes_dir,
    index_by_domain_dir,
    graph_dir,
    playbooks_dir,
    declarations_dir,
    reviews_dir,
    events_dir,
    cache_dir,
]

_GITIGNORE = """\
/indexes/
/graph/
/events/
/cache/
/reviews/pending-review-items.md
"""


def scaffold_knowledge_dir(workspace: str | Path) -> Path:
    """Create .story/knowledge/ with all subdirs and .gitignore. Idempotent."""
    root = knowledge_dir(workspace)
    root.mkdir(parents=True, exist_ok=True)

    for fn in _SUBDIRS:
        fn(workspace).mkdir(parents=True, exist_ok=True)

    # done dir for bootstrap handshake
    done = Path(workspace) / ".story" / "done" / "PROJECT-KNOWLEDGE-INIT"
    done.mkdir(parents=True, exist_ok=True)

    gi = root / ".gitignore"
    if not gi.exists():
        gi.write_text(_GITIGNORE, encoding="utf-8")

    return root
