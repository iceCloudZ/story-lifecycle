from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class SourceItem:
    id: str
    source: str
    item_type: str  # "requirement" | "bug"
    title: str
    description: str
    priority: str = ""
    owner: str = ""
    status: str = ""
    deadline: str = ""
    parent_id: str | None = None
    extra: dict = field(default_factory=dict)
    fetched_at: float = 0.0


@dataclass
class ResolveResult:
    parent_key: str | None = None
    need_manual_select: bool = False
    parent_source_id: str | None = None
    need_import_parent: bool = False


class StorySource(ABC):
    @abstractmethod
    def fetch_pending(self) -> list[SourceItem]: ...

    @abstractmethod
    def get_detail(self, item_id: str) -> SourceItem | None: ...

    @abstractmethod
    def sync_status(self, item_id: str, status: str): ...

    @abstractmethod
    def test_connection(self) -> bool: ...


class BugParentResolver(ABC):
    @abstractmethod
    def resolve(
        self, bug: SourceItem, existing_stories: list[dict]
    ) -> ResolveResult | None: ...


class TapdRelationResolver(BugParentResolver):
    def resolve(
        self, bug: SourceItem, existing_stories: list[dict]
    ) -> ResolveResult | None:
        if not bug.extra.get("related_story_id"):
            return None
        tapd_id = bug.extra["related_story_id"]
        for s in existing_stories:
            if s.get("source_type") == bug.source and s.get("source_id") == tapd_id:
                return ResolveResult(parent_key=s["story_key"])
        return ResolveResult(parent_source_id=tapd_id, need_import_parent=True)


class TitlePatternResolver(BugParentResolver):
    PATTERN = r"\[([A-Z]+-\d+)\]"

    def resolve(
        self, bug: SourceItem, existing_stories: list[dict]
    ) -> ResolveResult | None:
        import re

        m = re.search(self.PATTERN, bug.title)
        if not m:
            return None
        story_key = m.group(1)
        for s in existing_stories:
            if s["story_key"] == story_key:
                return ResolveResult(parent_key=story_key)
        return None


class ManualResolver(BugParentResolver):
    def resolve(
        self, bug: SourceItem, existing_stories: list[dict]
    ) -> ResolveResult | None:
        return ResolveResult(need_manual_select=True)


DEFAULT_BUG_PARENT_RESOLVERS = [
    TapdRelationResolver(),
    TitlePatternResolver(),
    ManualResolver(),
]


def resolve_bug_parent(
    bug: SourceItem,
    existing_stories: list[dict],
    resolvers: list[BugParentResolver] | None = None,
) -> ResolveResult:
    chain = resolvers or DEFAULT_BUG_PARENT_RESOLVERS
    for resolver in chain:
        result = resolver.resolve(bug, existing_stories)
        if result is None:
            continue
        if result.parent_key or result.need_import_parent or result.need_manual_select:
            return result
    return ResolveResult()
