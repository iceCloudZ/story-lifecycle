from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from .base import SourceItem


@dataclass
class PrdContent:
    source_type: str
    markdown: str
    file_path: str | None = None
    attachments: list[str] = field(default_factory=list)


class PrdProvider(ABC):
    @abstractmethod
    def can_handle(self, item: SourceItem) -> bool:
        ...

    @abstractmethod
    def fetch_content(self, item: SourceItem) -> PrdContent | None:
        ...


class TapdBodyPrdProvider(PrdProvider):
    def can_handle(self, item: SourceItem) -> bool:
        return item.source == "tapd" and bool(item.description.strip())

    def fetch_content(self, item: SourceItem) -> PrdContent | None:
        md = _html_to_markdown(item.description)
        return PrdContent(source_type="tapd_body", markdown=md)


class LocalFilePrdProvider(PrdProvider):
    def can_handle(self, item: SourceItem) -> bool:
        return bool(re.search(r"(?:^|\n)(/\S+\.md|[A-Z]:\\\S+\.md)", item.description))

    def fetch_content(self, item: SourceItem) -> PrdContent | None:
        m = re.search(r"(?:^|\n)(/\S+\.md|[A-Z]:\\\S+\.md)", item.description)
        if not m:
            return None
        p = Path(m.group(1))
        if not p.exists():
            return None
        return PrdContent(source_type="local_file", markdown=p.read_text(encoding="utf-8"), file_path=str(p))


class FallbackPrdProvider(PrdProvider):
    def can_handle(self, item: SourceItem) -> bool:
        return True

    def fetch_content(self, item: SourceItem) -> PrdContent | None:
        md = (
            f"# {item.title}\n\n"
            f"**来源**: {item.source} ({item.id})\n"
            f"**优先级**: {item.priority}\n"
            f"**处理人**: {item.owner}\n\n"
            f"## 需求描述\n\n{item.description}\n"
        )
        return PrdContent(source_type="fallback", markdown=md)


DEFAULT_PRD_PROVIDERS = [
    TapdBodyPrdProvider(),
    LocalFilePrdProvider(),
    FallbackPrdProvider(),
]


def fetch_prd_content(
    item: SourceItem,
    providers: list[PrdProvider] | None = None,
) -> PrdContent | None:
    chain = providers or DEFAULT_PRD_PROVIDERS
    for provider in chain:
        if provider.can_handle(item):
            content = provider.fetch_content(item)
            if content:
                return content
    return None


def save_prd(story_key: str, prd_content: PrdContent, workspace: str) -> str:
    prd_dir = Path(workspace) / "prd" if workspace else Path("prd")
    prd_dir.mkdir(parents=True, exist_ok=True)
    if prd_content.file_path and Path(prd_content.file_path).exists():
        return prd_content.file_path
    prd_file = prd_dir / f"{story_key}.md"
    prd_file.write_text(prd_content.markdown, encoding="utf-8")
    return str(prd_file)


def _html_to_markdown(html: str) -> str:
    text = html
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<p>", "\n", text)
    text = re.sub(r"</p>", "\n", text)
    text = re.sub(r"<strong>(.*?)</strong>", r"**\1**", text, flags=re.DOTALL)
    text = re.sub(r"<b>(.*?)</b>", r"**\1**", text, flags=re.DOTALL)
    text = re.sub(r"<em>(.*?)</em>", r"*\1*", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()
