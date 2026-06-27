"""KnowledgeIndex — load unified INDEX.json and retrieve relevant knowledge."""
from __future__ import annotations

import json
import os
from typing import Any

from .generator import generate_index, write_index
from .models import KnowledgeEntry
from .parser import (
    CommandRef,
    FailureEntry,
    FileRef,
    FailureRef,
    PlaybookEntry,
    ScenarioEntry,
)


class KnowledgeIndex:
    """Unified index over a project knowledge directory."""

    def __init__(self, knowledge_dir: str):
        self.knowledge_dir = os.path.abspath(knowledge_dir)
        self._entries: list[KnowledgeEntry] = []
        self._by_id: dict[str, KnowledgeEntry] = {}
        self._load()

    def _load(self) -> None:
        index_path = os.path.join(self.knowledge_dir, "INDEX.json")
        if not os.path.exists(index_path):
            write_index(self.knowledge_dir)
        with open(index_path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        self._entries = [_entry_from_dict(e) for e in payload.get("entries", [])]
        self._by_id = {e.id: e for e in self._entries}

    def refresh(self) -> str:
        """Regenerate INDEX.json and reload."""
        path = write_index(self.knowledge_dir)
        self._load()
        return path

    def get(self, entry_id: str) -> KnowledgeEntry | None:
        return self._by_id.get(entry_id)

    def all(self) -> list[KnowledgeEntry]:
        return list(self._entries)

    def retrieve(
        self,
        story_key: str = "",
        workspace: str = "",
        stage: str = "",
        query: str = "",
        domain: str = "",
        top_k: int = 10,
    ) -> list[KnowledgeEntry]:
        """Retrieve knowledge entries relevant to the current task context."""
        scored: list[tuple[float, KnowledgeEntry]] = []
        query_lower = (query or "").lower()
        ws_lower = (workspace or "").lower()

        for entry in self._entries:
            score = 0.0

            # 1. exact story match for by-story playbook
            if (
                entry.type == "playbook"
                and getattr(entry, "linked_story", "")
                and entry.linked_story == story_key
            ):
                score += 1000

            # 2. domain match
            if domain and entry.domain == domain:
                score += 100
            elif domain and entry.domain and entry.domain in domain:
                score += 50

            # 3. stage trigger
            trigger = entry.trigger or {}
            if stage and trigger.get("stage") == stage:
                score += 80

            # 4. workspace keyword match
            if ws_lower and any(k in ws_lower for k in (trigger.get("workspace_keyword") or "").lower().split()):
                score += 60

            # 5. query keyword match in title/tags
            if query_lower:
                text = " ".join(
                    [entry.title, " ".join(entry.tags), " ".join(entry.must_read)]
                ).lower()
                if query_lower in text:
                    score += 40
                for word in query_lower.split():
                    if word in text:
                        score += 10

            if score > 0:
                scored.append((score, entry))

        scored.sort(key=lambda x: (-x[0], x[1].title))
        return [e for _, e in scored[:top_k]]


def _entry_from_dict(data: dict[str, Any]) -> KnowledgeEntry:
    type_ = data.get("type")
    if type_ == "scenario":
        return ScenarioEntry(
            id=data["id"],
            type="scenario",
            title=data["title"],
            source=data["source"],
            domain=data.get("domain", ""),
            status=data.get("status", "extracted"),
            trigger=data.get("trigger", {}),
            must_read=data.get("must_read", []),
            roles=data.get("roles", []),
            tags=data.get("tags", []),
            source_refs=data.get("source_refs", []),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            path=data.get("path", ""),
            links=data.get("links", []),
            participating_services=data.get("participating_services", []),
            main_flow=data.get("main_flow", []),
            apis=data.get("apis", []),
            tables=data.get("tables", []),
            mq_topics=data.get("mq_topics", []),
            state_machines=data.get("state_machines", []),
            known_risks=data.get("known_risks", []),
        )
    if type_ == "playbook":
        return PlaybookEntry(
            id=data["id"],
            type="playbook",
            title=data["title"],
            source=data["source"],
            domain=data.get("domain", ""),
            status=data.get("status", "extracted"),
            trigger=data.get("trigger", {}),
            must_read=data.get("must_read", []),
            roles=data.get("roles", []),
            tags=data.get("tags", []),
            source_refs=data.get("source_refs", []),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            path=data.get("path", ""),
            links=data.get("links", []),
            theme=data.get("theme", ""),
            session_count=data.get("session_count", 0),
            top_files=[FileRef.from_dict(f) for f in data.get("top_files", [])],
            common_commands=[CommandRef.from_dict(c) for c in data.get("common_commands", [])],
            common_failures=[FailureRef.from_dict(f) for f in data.get("common_failures", [])],
            linked_scenarios=data.get("linked_scenarios", []),
            linked_story=data.get("linked_story", ""),
        )
    if type_ == "failure":
        return FailureEntry(
            id=data["id"],
            type="failure",
            title=data["title"],
            source=data["source"],
            domain=data.get("domain", ""),
            status=data.get("status", "extracted"),
            trigger=data.get("trigger", {}),
            must_read=data.get("must_read", []),
            roles=data.get("roles", []),
            tags=data.get("tags", []),
            source_refs=data.get("source_refs", []),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            path=data.get("path", ""),
            links=data.get("links", []),
            category=data.get("category", ""),
            display_category=data.get("display_category", ""),
            detail=data.get("detail", ""),
            frequency=data.get("frequency", {}),
            common_tools=data.get("common_tools", []),
            stages_affected=data.get("stages_affected", []),
            mitigations=data.get("mitigations", []),
            counterfactuals=data.get("counterfactuals", []),
        )
    return KnowledgeEntry.from_dict(data)
