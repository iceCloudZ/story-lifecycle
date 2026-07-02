from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .base import SourceItem


@dataclass
class BugContext:
    source_type: str
    description: str
    steps_to_reproduce: str = ""
    expected_behavior: str = ""
    actual_behavior: str = ""
    environment: str = ""
    screenshots: list[str] = field(default_factory=list)
    logs: str = ""
    raw_markdown: str = ""


class BugContentProvider(ABC):
    @abstractmethod
    def can_handle(self, bug: SourceItem) -> bool: ...

    @abstractmethod
    def fetch_content(self, bug: SourceItem) -> BugContext | None: ...


class TapdBodyBugProvider(BugContentProvider):
    def can_handle(self, bug: SourceItem) -> bool:
        return bug.source == "tapd" and bug.item_type == "bug"

    def fetch_content(self, bug: SourceItem) -> BugContext | None:
        from .prd_providers import _html_to_markdown

        md = _html_to_markdown(bug.description)

        # Try LLM semantic extraction first
        try:
            from ...orchestrator.evaluation.semantic import extract_bug_context

            result = extract_bug_context(md, title=bug.title)
            data = result["data"]
            return BugContext(
                source_type="tapd_body",
                description=data.get("description", bug.title),
                steps_to_reproduce=data.get("steps_to_reproduce", ""),
                expected_behavior=data.get("expected_behavior", ""),
                actual_behavior=data.get("actual_behavior", ""),
                environment=data.get("environment", ""),
                screenshots=self._extract_images(md),
                logs=data.get("logs", ""),
                raw_markdown=md,
            )
        except Exception:
            # Fallback to regex
            pass

        # Regex fallback (original logic)
        return BugContext(
            source_type="tapd_body",
            description=bug.title,
            steps_to_reproduce=self._extract_section(md, "复现步骤|步骤|重现"),
            expected_behavior=self._extract_section(md, "预期|期望|期望结果"),
            actual_behavior=self._extract_section(md, "实际|实际结果|现象"),
            environment=self._extract_section(md, "环境|版本|设备"),
            screenshots=self._extract_images(md),
            logs=self._extract_section(md, "日志|log|堆栈|stack"),
            raw_markdown=md,
        )

    def _extract_section(self, md: str, pattern: str) -> str:
        m = re.search(
            rf"(?:{pattern})[：:\s]*\n(.*?)(?=\n##|\n#|\Z)",
            md,
            re.DOTALL | re.IGNORECASE,
        )
        return m.group(1).strip() if m else ""

    def _extract_images(self, md: str) -> list[str]:
        return re.findall(r"!\[.*?\]\((.*?)\)", md)


class TapdCommentsBugProvider(BugContentProvider):
    def __init__(self, api=None):
        self._api = api

    def can_handle(self, bug: SourceItem) -> bool:
        return bug.source == "tapd" and bug.item_type == "bug" and self._api is not None

    def fetch_content(self, bug: SourceItem) -> BugContext | None:
        if not self._api:
            return None
        bug_id = bug.id.removeprefix("bug_")
        try:
            comments = self._api.get_comments(bug_id)
        except Exception:
            return None
        if not comments:
            return None
        combined = "\n\n".join(
            f"**{c.get('author', '')}** ({c.get('created', '')}):\n{c.get('description', '')}"
            for c in comments
        )
        return BugContext(
            source_type="tapd_comments",
            description=bug.title,
            raw_markdown=combined,
        )


class FallbackBugProvider(BugContentProvider):
    def can_handle(self, bug: SourceItem) -> bool:
        return True

    def fetch_content(self, bug: SourceItem) -> BugContext | None:
        return BugContext(
            source_type="fallback",
            description=bug.title,
            raw_markdown=bug.description or bug.title,
        )


DEFAULT_BUG_CONTENT_PROVIDERS = [
    TapdBodyBugProvider(),
    FallbackBugProvider(),
]


def fetch_bug_content(
    bug: SourceItem,
    providers: list[BugContentProvider] | None = None,
) -> BugContext:
    chain = providers or DEFAULT_BUG_CONTENT_PROVIDERS
    combined = BugContext(source_type="aggregated", description=bug.title)

    for provider in chain:
        if provider.can_handle(bug):
            partial = provider.fetch_content(bug)
            if partial:
                if not combined.steps_to_reproduce and partial.steps_to_reproduce:
                    combined.steps_to_reproduce = partial.steps_to_reproduce
                if not combined.expected_behavior and partial.expected_behavior:
                    combined.expected_behavior = partial.expected_behavior
                if not combined.actual_behavior and partial.actual_behavior:
                    combined.actual_behavior = partial.actual_behavior
                if not combined.environment and partial.environment:
                    combined.environment = partial.environment
                if not combined.logs and partial.logs:
                    combined.logs = partial.logs
                if partial.screenshots:
                    combined.screenshots.extend(partial.screenshots)
                if partial.raw_markdown:
                    combined.raw_markdown += (
                        "\n\n---\n\n" if combined.raw_markdown else ""
                    ) + partial.raw_markdown

    return combined


def format_bug_context(ctx: BugContext) -> str:
    parts = [ctx.description]
    if ctx.steps_to_reproduce:
        parts.append(f"\n复现步骤:\n{ctx.steps_to_reproduce}")
    if ctx.expected_behavior:
        parts.append(f"\n预期行为: {ctx.expected_behavior}")
    if ctx.actual_behavior:
        parts.append(f"\n实际行为: {ctx.actual_behavior}")
    if ctx.environment:
        parts.append(f"\n环境: {ctx.environment}")
    if ctx.logs:
        parts.append(f"\n相关日志:\n{ctx.logs}")
    return "\n".join(parts)
