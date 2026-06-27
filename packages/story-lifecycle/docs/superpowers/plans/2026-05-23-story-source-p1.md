# Story Source Integration P1 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 P1 — 状态回写、BugContentProvider 聚合、TUI 手动选择父故事、Textual worker 轮询。

**Architecture:** 在 P0 基础上扩展。nodes.py 增加 sync_status 触发；新增 bug_providers.py 做聚合；tui.py 增加 ParentSelectDialog 和 worker 轮询。

**Tech Stack:** Python 3.11+, SQLite, Textual TUI

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `src/story_lifecycle/sources/bug_providers.py` | Create | BugContentProvider ABC + 聚合逻辑 |
| `src/story_lifecycle/orchestrator/nodes.py` | Modify | advance_node 增加 sync_status 触发 |
| `src/story_lifecycle/orchestrator/service.py` | Modify | create_story_from_source 处理 need_manual_select |
| `src/story_lifecycle/cli/tui.py` | Modify | ParentSelectDialog + Textual worker 轮询 |
| `tests/test_source_integration.py` | Modify | P1 测试 |

---

### Task 1: BugContentProvider 聚合

**Files:**
- Create: `src/story_lifecycle/sources/bug_providers.py`
- Modify: `tests/test_source_integration.py`

- [ ] **Step 1: Create bug_providers.py**

```python
# src/story_lifecycle/sources/bug_providers.py
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
    def can_handle(self, bug: SourceItem) -> bool:
        ...

    @abstractmethod
    def fetch_content(self, bug: SourceItem) -> BugContext | None:
        ...


class TapdBodyBugProvider(BugContentProvider):
    def can_handle(self, bug: SourceItem) -> bool:
        return bug.source == "tapd" and bug.item_type == "bug"

    def fetch_content(self, bug: SourceItem) -> BugContext | None:
        from .prd_providers import _html_to_markdown
        md = _html_to_markdown(bug.description)
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
        m = re.search(rf"(?:{pattern})[：:\s]*\n(.*?)(?=\n##|\n#|\Z)", md, re.DOTALL | re.IGNORECASE)
        return m.group(1).strip() if m else ""

    def _extract_images(self, md: str) -> list[str]:
        return re.findall(r"!\[.*?\]\((.*?)\)", md)


class TapdCommentsBugProvider(BugContentProvider):
    def __init__(self, api=None):
        self._api = api

    def can_handle(self, bug: SourceItem) -> bool:
        return bug.source == "tapd" and bug.item_type == "bug"

    def fetch_content(self, bug: SourceItem) -> BugContext | None:
        if not self._api:
            return None
        bug_id = bug.id.removeprefix("bug_")
        comments = self._api.get_comments(bug_id)
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
    TapdCommentsBugProvider(),
    FallbackBugProvider(),
]


def fetch_bug_content(
    bug: SourceItem,
    providers: list[BugContentProvider] | None = None,
) -> BugContext:
    """Aggregate results from all providers."""
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
                    combined.raw_markdown += ("\n\n---\n\n" if combined.raw_markdown else "") + partial.raw_markdown

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
```

- [ ] **Step 2: Write test**

Append to `tests/test_source_integration.py`:

```python
def test_fetch_bug_content_aggregation():
    """BugContext should aggregate from multiple providers."""
    from story_lifecycle.sources.base import SourceItem
    from story_lifecycle.sources.bug_providers import (
        fetch_bug_content, BugContentProvider, BugContext,
    )

    bug = SourceItem(
        id="bug_123",
        source="tapd",
        item_type="bug",
        title="登录后页面空白",
        description="<p>复现步骤：\n1. 登录\n2. 跳转首页</p><p>实际结果：空白</p>",
    )

    ctx = fetch_bug_content(bug)
    assert ctx.description == "登录后页面空白"
    assert ctx.source_type == "aggregated"
    # At least the fallback should have populated raw_markdown
    assert ctx.raw_markdown != ""
```

- [ ] **Step 3: Run tests + commit**

```bash
cd /d/story-lifecycle && python -m pytest tests/test_source_integration.py -v
git add src/story_lifecycle/sources/bug_providers.py tests/test_source_integration.py
git commit -m "feat: add BugContentProvider aggregation for bug context

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 2: 状态回写 sync_status

**Files:**
- Modify: `src/story_lifecycle/orchestrator/nodes.py`

- [ ] **Step 1: Read nodes.py, find advance_node or the completion logic**

Find where story status becomes "completed". Add sync_status trigger there.

Pattern to add (after status is set to completed):

```python
# Sync status to external source
story = db.get_story(state["story_key"])
if story:
    source_type = story.get("source_type")
    source_id = story.get("source_id")
    if source_type and source_id:
        try:
            from ..sources import get_source
            source = get_source(source_type)
            if source:
                source.sync_status(source_id, "completed")
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Failed to sync status to {source_type}: {e}")
```

The exact insertion point depends on the current code structure. Find where `status` transitions to `"completed"` and add the block after that.

- [ ] **Step 2: Run tests + commit**

```bash
cd /d/story-lifecycle && python -m pytest tests/ -v
git add src/story_lifecycle/orchestrator/nodes.py
git commit -m "feat: add sync_status trigger on story completion

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 3: TUI 手动选择父故事 (ParentSelectDialog)

**Files:**
- Modify: `src/story_lifecycle/cli/tui.py`

- [ ] **Step 1: Add ParentSelectDialog modal**

Add BEFORE StoryBoardApp class (after InboxScreen):

```python
class ParentSelectDialog(ModalScreen):
    """Bug 关联父故事的手动选择对话框。"""

    def __init__(self, bug_title: str, stories: list[dict]):
        self._bug_title = bug_title
        self._stories = stories
        self._cursor = 0
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical(id="parent-select-container"):
            yield Static(f"[bold]选择父故事[/]", id="parent-title")
            yield Static(f"Bug: {self._bug_title}", id="parent-desc")
            yield Static("", id="parent-list")
            with Horizontal(id="parent-btn-row"):
                yield Button("确认", variant="success", id="btn-parent-confirm")
                yield Button("独立创建", variant="warning", id="btn-parent-standalone")
                yield Button("取消", variant="default", id="btn-parent-cancel")

    def on_mount(self) -> None:
        self._render()

    def _render(self):
        lines = []
        for i, s in enumerate(self._stories):
            cursor = ">" if i == self._cursor else " "
            key = s.get("story_key", "")
            title = s.get("title", "")
            lines.append(f"  {cursor} {key}  {title}")
        self.query_one("#parent-list", Static).update("\n".join(lines))

    def key_up(self):
        if self._cursor > 0:
            self._cursor -= 1
            self._render()

    def key_down(self):
        if self._cursor < len(self._stories) - 1:
            self._cursor += 1
            self._render()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-parent-confirm" and self._stories:
            s = self._stories[self._cursor]
            self.dismiss(s.get("story_key"))
        elif event.button.id == "btn-parent-standalone":
            self.dismiss(None)  # None = create standalone
        else:
            self.dismiss("")  # Empty string = cancel

    def key_enter(self):
        if self._stories:
            s = self._stories[self._cursor]
            self.dismiss(s.get("story_key"))
```

- [ ] **Step 2: Update action_show_inbox to handle need_manual_select**

In the `_on_inbox_result` callback inside `action_show_inbox`, add handling for `need_manual_select`:

```python
def _on_inbox_result(result):
    if not result:
        return
    from ..orchestrator.service import create_story_from_source
    for item in result:
        r = create_story_from_source(item, auto_start=True)
        if r.status == "created":
            self.notify(f"已创建: {r.story_key}")
        elif r.status == "need_manual_select":
            # Show parent select dialog
            active = [s for s in self.stories if not s.get("parent_key")]
            def _on_parent_selected(parent_key):
                if parent_key == "":  # Cancel
                    return
                if parent_key is None:  # Standalone
                    r2 = create_story_from_source(item, auto_start=True)
                    if r2.status == "created":
                        self.notify(f"已创建独立故事: {r2.story_key}")
                else:
                    sub_key = create_sub_story(parent_key=parent_key, sub_type="bug-fix", description=item.description)
                    self.notify(f"已创建子故事: {sub_key}")
            self.push_screen(ParentSelectDialog(item.title, active), _on_parent_selected)
        else:
            self.notify(f"创建失败: {r.error}")
```

Make sure to import `create_sub_story` if needed: `from ..orchestrator.service import create_sub_story`.

- [ ] **Step 3: Run tests + commit**

```bash
cd /d/story-lifecycle && python -m pytest tests/ -v && ruff check src/
git add src/story_lifecycle/cli/tui.py
git commit -m "feat: add ParentSelectDialog for manual bug-parent association

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 4: Textual worker 轮询 + header 通知

**Files:**
- Modify: `src/story_lifecycle/cli/tui.py`

- [ ] **Step 1: Add poll setup to on_mount**

In `StoryBoardApp.on_mount`, add after existing interval setup:

```python
# Source polling
from ..cli.setup import get_config
config = get_config()
source_config = config.get("story_source", {})
if source_config.get("enabled"):
    self._source_enabled = True
    poll_interval = source_config.get("poll_interval", 300)
    self.set_interval(poll_interval, self._poll_source)
else:
    self._source_enabled = False
```

Add `_source_enabled` to `__init__` if it doesn't exist:
```python
self._source_enabled = False
```

- [ ] **Step 2: Add poll methods**

```python
def _poll_source(self) -> None:
    """Trigger background poll using Textual worker."""
    if not self._source_enabled:
        return
    self.run_worker(self._do_poll, thread=True, exclusive=True, group="source_poll")

def _do_poll(self) -> None:
    """Background thread: fetch pending items from source."""
    from ..sources import get_source
    from ..cli.setup import get_config
    from ..db import models as db

    config = get_config()
    source_name = config.get("story_source", {}).get("enabled", "")
    source = get_source(source_name)
    if not source:
        return

    try:
        items = source.fetch_pending()
    except Exception:
        return

    new_items = [i for i in items if not db.find_by_source_id(i.source, i.id)]
    if new_items:
        self._pending_items = new_items
        self.call_from_thread(self._update_inbox_notification, len(new_items))

def _update_inbox_notification(self, count: int):
    """Update header with inbox notification count."""
    try:
        header = self.query_one("#header-bar")
        if header:
            header.update(
                f"\n  [bold cyan]◆[/] [bold white]Story[/][bold cyan]Lifecycle[/] "
                f" [dim]│[/] [bold yellow]{count} 个新待办[/] "
                f"[dim]│[/] 按 [[i]] 查看"
            )
    except Exception:
        pass  # Header might not exist yet
```

- [ ] **Step 3: Run tests + lint + commit**

```bash
cd /d/story-lifecycle && python -m pytest tests/ -v && ruff check src/
git add src/story_lifecycle/cli/tui.py
git commit -m "feat: add Textual worker polling with header notification

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 5: 全量测试 + lint

- [ ] **Step 1: Run all tests**

Run: `cd /d/story-lifecycle && python -m pytest tests/ -v`

- [ ] **Step 2: Run lint**

Run: `cd /d/story-lifecycle && ruff check src/`

- [ ] **Step 3: Fix any issues**

- [ ] **Step 4: Final commit if needed**
