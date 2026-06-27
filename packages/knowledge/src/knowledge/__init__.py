"""Unified knowledge layer for story-lifecycle + story-miner.

Public API:
    from knowledge import KnowledgeIndex
    index = KnowledgeIndex("/path/to/.story/knowledge")
    results = index.retrieve(story_key="...", workspace="...", stage="...", query="...")
"""
from .index import KnowledgeIndex
from .models import FailureEntry, KnowledgeEntry, PlaybookEntry, ScenarioEntry

__all__ = [
    "KnowledgeIndex",
    "KnowledgeEntry",
    "ScenarioEntry",
    "PlaybookEntry",
    "FailureEntry",
]
