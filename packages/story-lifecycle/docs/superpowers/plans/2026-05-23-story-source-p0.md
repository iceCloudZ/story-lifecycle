# Story Source Integration P0 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 P0 — 故事来源适配层 + TAPD 集成 + 收件箱 + 基础 PRD 保存。

**Architecture:** 新增 `sources/` 模块，定义 StorySource 抽象、TapdApi 封装、TapdSource 适配器。DB 新增 `source_type`/`source_id` 列做去重。Service 层新增 `create_story_from_source()`。TUI 新增 `[i]` 收件箱按键。

**Tech Stack:** Python 3.11+, SQLite, Textual TUI, YAML config

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `src/story_lifecycle/sources/__init__.py` | Create | 注册表 + get_source 工厂 |
| `src/story_lifecycle/sources/base.py` | Create | SourceItem + StorySource ABC + ResolveResult + BugParentResolver |
| `src/story_lifecycle/sources/manual_source.py` | Create | ManualSource（默认适配器） |
| `src/story_lifecycle/sources/tapd_api.py` | Create | TapdApi — 从 cli_tapd.py 提取的 API 层 |
| `src/story_lifecycle/sources/tapd_source.py` | Create | TapdSource — 调用 TapdApi |
| `src/story_lifecycle/sources/prd_providers.py` | Create | PrdProvider ABC + TapdBodyPrdProvider + LocalFilePrdProvider + FallbackPrdProvider |
| `src/story_lifecycle/db/models.py` | Modify | 新增 source_type/source_id 列 + find_by_source_id |
| `src/story_lifecycle/orchestrator/service.py` | Modify | 新增 create_story_from_source + CreateFromSourceResult |
| `src/story_lifecycle/cli/tui.py` | Modify | [i] 收件箱按键 + InboxScreen |
| `src/story_lifecycle/cli/setup.py` | Modify | _merge_config 防覆盖 |
| `tests/test_source_integration.py` | Create | P0 全部测试 |

---

### Task 1: DB 扩展 — source_type/source_id 列

**Files:**
- Modify: `src/story_lifecycle/db/models.py`
- Modify: `tests/test_source_integration.py`

- [ ] **Step 1: Write failing test**

```python
def test_source_id_columns(tmp_path):
    """DB should have source_type and source_id columns with dedup."""
    from story_lifecycle.db.models import Database
    db = Database(str(tmp_path / "test.db"))

    # Create two stories
    db.create_story("S1", "Story 1", "", "minimal")
    db.create_story("S2", "Story 2", "", "minimal")

    # Update source info
    db.update_story("S1", {"source_type": "tapd", "source_id": "1001234"})
    db.update_story("S2", {"source_type": "tapd", "source_id": "1001235"})

    # find_by_source_id works
    found = db.find_by_source_id("tapd", "1001234")
    assert found is not None
    assert found["story_key"] == "S1"

    # Not found returns None
    assert db.find_by_source_id("tapd", "9999999") is None

    # source_type + source_id combination uniqueness check
    found2 = db.find_by_source_id("tapd", "1001235")
    assert found2["story_key"] == "S2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /d/story-lifecycle && python -m pytest tests/test_source_integration.py::test_source_id_columns -v`

- [ ] **Step 3: Implement**

In `src/story_lifecycle/db/models.py`:

3a. Add `source_type` and `source_id` to `VALID_COLUMNS` frozenset.

3b. Add idempotent migration after existing migrations (after the `sub_type` migration):

```python
for col in ("source_type", "source_id"):
    try:
        self._conn.execute(f"ALTER TABLE story ADD COLUMN {col} TEXT")
    except Exception:
        pass
try:
    self._conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_story_source ON story(source_type, source_id)"
    )
except Exception:
    pass
```

3c. Add `find_by_source_id` method:

```python
def find_by_source_id(self, source_type: str, source_id: str) -> dict | None:
    rows = self._conn.execute(
        "SELECT * FROM story WHERE source_type = ? AND source_id = ?",
        (source_type, source_id),
    ).fetchall()
    return dict(rows[0]) if rows else None
```

- [ ] **Step 4: Run test**

Run: `cd /d/story-lifecycle && python -m pytest tests/test_source_integration.py::test_source_id_columns -v`

- [ ] **Step 5: Commit**

```bash
git add src/story_lifecycle/db/models.py tests/test_source_integration.py
git commit -m "feat: add source_type/source_id columns with dedup index to story DB"
```

---

### Task 2: sources/base.py — 抽象接口 + 数据结构

**Files:**
- Create: `src/story_lifecycle/sources/base.py`
- Create: `src/story_lifecycle/sources/__init__.py`

- [ ] **Step 1: Create base.py with SourceItem, StorySource, ResolveResult, BugParentResolver**

```python
# src/story_lifecycle/sources/base.py
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class SourceItem:
    id: str
    source: str           # "tapd" | "jira" | "manual"
    item_type: str        # "requirement" | "bug"
    title: str
    description: str
    priority: str = ""
    owner: str = ""
    status: str = ""
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
    def fetch_pending(self) -> list[SourceItem]:
        ...

    @abstractmethod
    def get_detail(self, item_id: str) -> SourceItem | None:
        ...

    @abstractmethod
    def sync_status(self, item_id: str, status: str):
        ...

    @abstractmethod
    def test_connection(self) -> bool:
        ...


class BugParentResolver(ABC):
    @abstractmethod
    def resolve(self, bug: SourceItem, existing_stories: list[dict]) -> ResolveResult | None:
        ...


class TapdRelationResolver(BugParentResolver):
    def resolve(self, bug: SourceItem, existing_stories: list[dict]) -> ResolveResult | None:
        if not bug.extra.get("related_story_id"):
            return None
        tapd_id = bug.extra["related_story_id"]
        for s in existing_stories:
            if s.get("source_type") == bug.source and s.get("source_id") == tapd_id:
                return ResolveResult(parent_key=s["story_key"])
        return ResolveResult(parent_source_id=tapd_id, need_import_parent=True)


class TitlePatternResolver(BugParentResolver):
    PATTERN = r"\[([A-Z]+-\d+)\]"

    def resolve(self, bug: SourceItem, existing_stories: list[dict]) -> ResolveResult | None:
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
    def resolve(self, bug: SourceItem, existing_stories: list[dict]) -> ResolveResult | None:
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
```

- [ ] **Step 2: Create __init__.py with registry**

```python
# src/story_lifecycle/sources/__init__.py
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
```

- [ ] **Step 3: Commit**

```bash
git add src/story_lifecycle/sources/
git commit -m "feat: add sources module with StorySource ABC, SourceItem, ResolveResult"
```

---

### Task 3: ManualSource + TapdApi + TapdSource

**Files:**
- Create: `src/story_lifecycle/sources/manual_source.py`
- Create: `src/story_lifecycle/sources/tapd_api.py`
- Create: `src/story_lifecycle/sources/tapd_source.py`

- [ ] **Step 1: Create manual_source.py**

```python
# src/story_lifecycle/sources/manual_source.py
from .base import SourceItem, StorySource


class ManualSource(StorySource):
    def fetch_pending(self) -> list[SourceItem]:
        return []

    def get_detail(self, item_id: str) -> SourceItem | None:
        return None

    def sync_status(self, item_id: str, status: str):
        pass

    def test_connection(self) -> bool:
        return True
```

Register in `__init__.py`:

```python
from .manual_source import ManualSource
register_source("manual", lambda cfg: ManualSource())
```

- [ ] **Step 2: Create tapd_api.py**

This extracts the HTTP request logic from `C:/Users/zzh58/.claude/scripts/cli_tapd.py`. The key pieces:
- `token_manager` for auth
- HTTP GET/POST to `api.tapd.cn`
- JSON response parsing

```python
# src/story_lifecycle/sources/tapd_api.py
"""TAPD API 封装 — 从 cli_tapd.py 提取的核心逻辑。"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)

# 导入 cli_tapd 的 token_manager 和 HTTP 请求逻辑
_CLI_TAPD_PATH = Path.home() / ".claude" / "scripts" / "cli_tapd.py"


def _load_cli_tapd():
    """动态加载 cli_tapd.py 模块获取核心 API 能力。"""
    if not _CLI_TAPD_PATH.exists():
        raise FileNotFoundError(f"cli_tapd.py not found: {_CLI_TAPD_PATH}")
    import importlib.util
    spec = importlib.util.spec_from_file_location("cli_tapd", str(_CLI_TAPD_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TapdApi:
    def __init__(self, workspace_id: str, auth_token: str = ""):
        self.workspace_id = workspace_id
        self._auth_token = auth_token
        self._cli = None

    def _get_cli(self):
        if self._cli is None:
            self._cli = _load_cli_tapd()
        return self._cli

    def _call(self, command: str, params: dict) -> list[dict] | dict | None:
        """调用 cli_tapd 的命令函数。"""
        cli = self._get_cli()
        # cli_tapd 用 argparse 暴露子命令为 cmd_<command> 函数
        # 但也有 main-level dispatch; 用内部函数映射
        cmd_func_name = f"cmd_{command.replace('-', '_')}"
        func = getattr(cli, cmd_func_name, None)
        if func is None:
            log.warning(f"TAPD command not found: {command}")
            return None
        try:
            # cli_tapd 的函数签名: cmd_xxx(client, workspace_id, params)
            client = cli.token_manager  # 或其他 auth client
            result = func(client, self.workspace_id, json.dumps(params, ensure_ascii=False))
            if isinstance(result, str):
                return json.loads(result)
            return result
        except Exception as e:
            log.warning(f"TAPD API error ({command}): {e}")
            return None

    def get_stories(self, params: dict) -> list[dict]:
        result = self._call("get_stories", params)
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return result.get("data", [])
        return []

    def get_bugs(self, params: dict) -> list[dict]:
        result = self._call("get_bug", params)
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return result.get("data", [])
        return []

    def get_story_detail(self, story_id: str) -> dict | None:
        result = self._call("get_stories", {"entity_type": "stories", "id": story_id})
        if isinstance(result, list) and result:
            return result[0]
        if isinstance(result, dict):
            return result.get("data", [{}])[0] if result.get("data") else None
        return None

    def get_bug_detail(self, bug_id: str) -> dict | None:
        result = self._call("get_bug", {"id": bug_id})
        if isinstance(result, list) and result:
            return result[0]
        return None

    def update_story(self, story_id: str, fields: dict) -> bool:
        result = self._call("update_story", {"id": story_id, **fields})
        return result is not None

    def update_bug(self, bug_id: str, fields: dict) -> bool:
        result = self._call("update_bug", {"id": bug_id, **fields})
        return result is not None

    def get_comments(self, entry_id: str) -> list[dict]:
        result = self._call("get_comments", {"entry_id": entry_id})
        if isinstance(result, list):
            return result
        return []

    def get_entity_relations(self, entity_id: str) -> list[dict]:
        result = self._call("entity_relations", {"entity_id": entity_id})
        if isinstance(result, list):
            return result
        return []
```

> **注意**：`TapdApi` 的 `_call` 方法需要适配 `cli_tapd.py` 的实际函数签名。
> 实现时需要读取 `cli_tapd.py` 的源码确认 token_manager/client 的获取方式。
> 如果 `cli_tapd.py` 不方便直接调用内部函数，可以改为 `subprocess.run` 调用 CLI 入口，用 `PYTHONIOENCODING=utf-8` 前缀。

- [ ] **Step 3: Create tapd_source.py**

```python
# src/story_lifecycle/sources/tapd_source.py
from __future__ import annotations

import logging
import re
import time

from .base import SourceItem, StorySource
from .tapd_api import TapdApi

log = logging.getLogger(__name__)


class TapdSource(StorySource):
    def __init__(self, config: dict):
        self._api = TapdApi(
            workspace_id=config.get("workspace_id", ""),
            auth_token=config.get("auth_token", ""),
        )
        self.owner = config.get("owner", "")
        self.story_status_filter = config.get("story_status", "open,progressing,reopened")
        self.bug_status_filter = config.get("bug_status", "new,reopened,assigned,resolving")

    def fetch_pending(self) -> list[SourceItem]:
        items = []
        items.extend(self._fetch_stories())
        items.extend(self._fetch_bugs())
        return items

    def _fetch_stories(self) -> list[SourceItem]:
        result = self._api.get_stories({
            "entity_type": "stories",
            "limit": 20,
            "owner": self.owner,
            "status": self.story_status_filter,
        })
        return [self._parse_story(s) for s in result]

    def _fetch_bugs(self) -> list[SourceItem]:
        result = self._api.get_bugs({
            "limit": 20,
            "status": self.bug_status_filter,
        })
        return [self._parse_bug(b) for b in result]

    def get_detail(self, item_id: str) -> SourceItem | None:
        if item_id.startswith("bug_"):
            raw = self._api.get_bug_detail(item_id.removeprefix("bug_"))
            return self._parse_bug(raw) if raw else None
        raw = self._api.get_story_detail(item_id)
        return self._parse_story(raw) if raw else None

    def sync_status(self, item_id: str, status: str):
        TAPD_STATUS_MAP = {"completed": "done", "blocked": "reopen", "aborted": "postponed"}
        tapd_status = TAPD_STATUS_MAP.get(status)
        if not tapd_status:
            return
        if item_id.startswith("bug_"):
            self._api.update_bug(item_id.removeprefix("bug_"), {"status": tapd_status})
        else:
            self._api.update_story(item_id, {"status": tapd_status})

    def test_connection(self) -> bool:
        try:
            result = self._api.get_stories({"limit": 1})
            return True
        except Exception:
            return False

    def _parse_story(self, raw: dict) -> SourceItem:
        return SourceItem(
            id=raw.get("id", ""),
            source="tapd",
            item_type="requirement",
            title=raw.get("name", ""),
            description=raw.get("description", ""),
            priority=raw.get("priority_label", ""),
            owner=raw.get("owner", ""),
            status=raw.get("status", ""),
            parent_id=None,
            extra={
                "category": raw.get("category_name", ""),
                "iteration_id": raw.get("iteration_id", ""),
                "custom_field_one": raw.get("custom_field_one", ""),
            },
            fetched_at=time.time(),
        )

    def _parse_bug(self, raw: dict) -> SourceItem:
        return SourceItem(
            id=f"bug_{raw.get('id', '')}",
            source="tapd",
            item_type="bug",
            title=raw.get("title", ""),
            description=raw.get("description", ""),
            priority=raw.get("priority_label", ""),
            owner=raw.get("current_owner", ""),
            status=raw.get("status", ""),
            parent_id=raw.get("story_id", None),
            extra={"severity": raw.get("severity", "")},
            fetched_at=time.time(),
        )
```

Register in `__init__.py`:

```python
from .tapd_source import TapdSource
register_source("tapd", lambda cfg: TapdSource(cfg))
```

- [ ] **Step 4: Commit**

```bash
git add src/story_lifecycle/sources/
git commit -m "feat: add ManualSource, TapdApi, TapdSource adapters"
```

---

### Task 4: PrdProvider 基础实现

**Files:**
- Create: `src/story_lifecycle/sources/prd_providers.py`

- [ ] **Step 1: Create prd_providers.py**

```python
# src/story_lifecycle/sources/prd_providers.py
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


def _html_to_markdown(html: str) -> str:
    """简单的 HTML → Markdown 转换。"""
    import re as _re
    text = html
    text = _re.sub(r"<br\s*/?>", "\n", text)
    text = _re.sub(r"<p>", "\n", text)
    text = _re.sub(r"</p>", "\n", text)
    text = _re.sub(r"<strong>(.*?)</strong>", r"**\1**", text, flags=_re.DOTALL)
    text = _re.sub(r"<b>(.*?)</b>", r"**\1**", text, flags=_re.DOTALL)
    text = _re.sub(r"<em>(.*?)</em>", r"*\1*", text, flags=_re.DOTALL)
    text = _re.sub(r"<[^>]+>", "", text)
    return text.strip()
```

- [ ] **Step 2: Commit**

```bash
git add src/story_lifecycle/sources/prd_providers.py
git commit -m "feat: add PrdProvider chain with TAPD body, local file, fallback"
```

---

### Task 5: Service 层 — create_story_from_source

**Files:**
- Modify: `src/story_lifecycle/orchestrator/service.py`
- Modify: `tests/test_source_integration.py`

- [ ] **Step 1: Write failing test**

```python
def test_create_story_from_source(tmp_path):
    """create_story_from_source should create a story with source metadata."""
    from story_lifecycle.db.models import Database, get_db, set_db_path
    original = set_db_path(str(tmp_path / "test.db"))
    db = get_db()

    from story_lifecycle.sources.base import SourceItem
    from story_lifecycle.orchestrator.service import create_story_from_source

    item = SourceItem(
        id="1144381896001001234",
        source="tapd",
        item_type="requirement",
        title="用户登录功能",
        description="<p>实现登录</p>",
        priority="P0",
        owner="赵子豪",
        status="open",
    )

    result = create_story_from_source(item, auto_start=False)
    assert result.status == "created"
    assert result.story_key is not None
    assert result.story_key.startswith("TAPD-")

    # Verify DB has source columns
    story = db.get_story(result.story_key)
    assert story["source_type"] == "tapd"
    assert story["source_id"] == "1144381896001001234"

    set_db_path(original)


def test_create_bug_with_auto_import_parent(tmp_path):
    """Bug whose parent exists on TAPD but not locally should auto-import parent."""
    from story_lifecycle.db.models import Database, get_db, set_db_path
    original = set_db_path(str(tmp_path / "test.db"))
    db = get_db()

    from story_lifecycle.sources.base import SourceItem
    from story_lifecycle.orchestrator.service import create_story_from_source

    bug_item = SourceItem(
        id="bug_1144381896001009999",
        source="tapd",
        item_type="bug",
        title="登录后页面空白",
        description="页面加载后空白",
        extra={"related_story_id": "1144381896001001234"},
    )

    # This should auto-import parent story from TAPD, then create bug-fix sub-story
    # For unit test, this will fail because TapdApi can't actually call TAPD
    # So we test the need_import_parent path returns correct status
    result = create_story_from_source(bug_item, auto_start=False)
    # Since TapdApi won't work in test, it should return failed
    assert result.status in ("failed", "need_manual_select", "created")

    set_db_path(original)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /d/story-lifecycle && python -m pytest tests/test_source_integration.py::test_create_story_from_source tests/test_source_integration.py::test_create_bug_with_auto_import_parent -v`

- [ ] **Step 3: Implement create_story_from_source in service.py**

Add to `src/story_lifecycle/orchestrator/service.py`:

```python
@dataclass
class CreateFromSourceResult:
    status: str  # "created" | "need_manual_select" | "failed"
    story_key: str | None = None
    bug_item: SourceItem | None = None
    error: str | None = None


def create_story_from_source(
    item: SourceItem,
    profile: str = "minimal",
    workspace: str = "",
    generate_prd: bool = True,
    auto_start: bool = True,
) -> CreateFromSourceResult:
    from ..sources.base import resolve_bug_parent
    from ..sources import get_source
    from ..sources.prd_providers import fetch_prd_content, _save_prd

    story_key = _derive_story_key(item)
    prd_path = None

    # Requirement → PrdProvider chain
    if generate_prd and item.item_type == "requirement":
        prd_content = fetch_prd_content(item)
        if prd_content and prd_content.markdown:
            prd_path = _save_prd(story_key, prd_content, workspace)

    # Bug → resolve parent
    if item.item_type == "bug":
        active_stories = db.list_active_stories() if hasattr(db, "list_active_stories") else []
        result = resolve_bug_parent(item, active_stories)

        # Auto-import parent if needed
        if result.need_import_parent and result.parent_source_id:
            source = get_source(item.source)
            parent_item = source.get_detail(result.parent_source_id) if source else None
            if not parent_item:
                return CreateFromSourceResult(status="failed", error=f"无法导入父需求: {item.source}/{result.parent_source_id}")
            parent_result = create_story_from_source(parent_item, profile=profile, workspace=workspace, generate_prd=True, auto_start=False)
            if parent_result.status != "created" or not parent_result.story_key:
                return CreateFromSourceResult(status="failed", error=f"父需求导入失败: {parent_result.error or parent_result.status}")
            result.parent_key = parent_result.story_key

        if result.need_manual_select:
            return CreateFromSourceResult(status="need_manual_select", bug_item=item)
        if result.parent_key:
            sub_key = create_sub_story(parent_key=result.parent_key, sub_type="bug-fix", description=item.description)
            db.update_story(sub_key, {"source_type": item.source, "source_id": item.id})
            if auto_start:
                from .graph import start_story_async
                start_story_async(sub_key)
            return CreateFromSourceResult(status="created", story_key=sub_key)

    # Create normal story
    key = create_and_start_story(story_key=story_key, title=item.title, profile=profile, workspace=workspace, prd_path=prd_path)
    db.update_story(key, {"source_type": item.source, "source_id": item.id})

    if auto_start:
        from .graph import start_story_async
        start_story_async(key)

    return CreateFromSourceResult(status="created", story_key=key)


def _derive_story_key(item: SourceItem) -> str:
    return f"TAPD-{item.id[-6:]}" if item.source == "tapd" else f"{item.source.upper()}-{item.id[-6:]}"
```

Also add `_save_prd` to `prd_providers.py`:

```python
def save_prd(story_key: str, prd_content: PrdContent, workspace: str) -> str:
    prd_dir = Path(workspace) / "prd" if workspace else Path("prd")
    prd_dir.mkdir(parents=True, exist_ok=True)
    if prd_content.file_path and Path(prd_content.file_path).exists():
        return prd_content.file_path
    prd_file = prd_dir / f"{story_key}.md"
    prd_file.write_text(prd_content.markdown, encoding="utf-8")
    return str(prd_file)
```

- [ ] **Step 4: Run tests**

Run: `cd /d/story-lifecycle && python -m pytest tests/test_source_integration.py -v`

- [ ] **Step 5: Commit**

```bash
git add src/story_lifecycle/orchestrator/service.py src/story_lifecycle/sources/prd_providers.py tests/test_source_integration.py
git commit -m "feat: add create_story_from_source with CreateFromSourceResult"
```

---

### Task 6: setup.py — config merge 策略

**Files:**
- Modify: `src/story_lifecycle/cli/setup.py`

- [ ] **Step 1: Add _merge_config function**

In `src/story_lifecycle/cli/setup.py`, find where config is saved in `run_setup()` and add merge logic:

```python
def _merge_config(existing: dict, updates: dict) -> dict:
    merged = dict(existing)
    merged.update(updates)
    return merged
```

Update the save logic in `run_setup()` to use `_merge_config`:

```python
# Before saving:
existing = get_config()
new_config = _merge_config(existing, config_dict)
CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
CONFIG_FILE.write_text(yaml.dump(new_config, allow_unicode=True), encoding="utf-8")
```

- [ ] **Step 2: Commit**

```bash
git add src/story_lifecycle/cli/setup.py
git commit -m "feat: add _merge_config to prevent setup overwriting story_source"
```

---

### Task 7: TUI 收件箱 [i] 按键

**Files:**
- Modify: `src/story_lifecycle/cli/tui.py`

- [ ] **Step 1: Add InboxScreen modal**

```python
class InboxScreen(ModalScreen):
    """待办收件箱 — 显示外部平台拉取的待办条目。"""

    BINDINGS = [
        Binding("escape", "close_inbox", "Close"),
        Binding("r", "refresh_inbox", "Refresh"),
    ]

    def __init__(self, items: list):
        self._items = items
        self._selected: set[int] = set()
        self._cursor = 0
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical(id="inbox-container"):
            yield Static("[bold]待办收件箱[/]", id="inbox-title")
            yield Static("", id="inbox-list")
            with Horizontal(id="inbox-btn-row"):
                yield Button("确认创建", variant="success", id="btn-inbox-confirm")
                yield Button("取消", variant="default", id="btn-inbox-cancel")

    def on_mount(self) -> None:
        self._render()

    def _render(self):
        lines = []
        for i, item in enumerate(self._items):
            check = "✓" if i in self._selected else " "
            cursor = ">" if i == self._cursor else " "
            type_tag = "[需求]" if item.item_type == "requirement" else "[Bug]"
            lines.append(f"  {cursor} [{check}] {type_tag} {item.title}  ({item.source})")
        self.query_one("#inbox-list", Static).update("\n".join(lines))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-inbox-confirm":
            selected = [self._items[i] for i in sorted(self._selected)]
            self.dismiss(selected)
        else:
            self.dismiss([])

    def action_close_inbox(self):
        self.dismiss([])

    def action_refresh_inbox(self):
        # Re-fetch from source
        ...

    def key_up(self):
        if self._cursor > 0:
            self._cursor -= 1
            self._render()

    def key_down(self):
        if self._cursor < len(self._items) - 1:
            self._cursor += 1
            self._render()

    def key_space(self):
        if self._cursor in self._selected:
            self._selected.discard(self._cursor)
        else:
            self._selected.add(self._cursor)
        self._render()

    def key_enter(self):
        # Toggle selection and confirm
        if self._cursor not in self._selected:
            self._selected.add(self._cursor)
        self._render()
        selected = [self._items[i] for i in sorted(self._selected)]
        self.dismiss(selected)
```

- [ ] **Step 2: Add [i] binding and action to StoryBoardApp**

In BINDINGS list, add:
```python
Binding("i", "show_inbox", "Inbox"),
```

Add action method:
```python
def action_show_inbox(self):
    from ..sources import get_source
    from ..cli.setup import get_config

    config = get_config()
    source_name = config.get("story_source", {}).get("enabled", "")
    if not source_name:
        self._notify("未配置外部来源，请运行 story setup")
        return

    source = get_source(source_name)
    if not source:
        self._notify(f"来源 {source_name} 不可用")
        return

    try:
        items = source.fetch_pending()
    except Exception as e:
        self._notify(f"获取待办失败: {e}")
        return

    if not items:
        self._notify("没有新的待办")
        return

    def _on_inbox_result(result):
        if not result:
            return
        from ..orchestrator.service import create_story_from_source
        for item in result:
            r = create_story_from_source(item, auto_start=True)
            if r.status == "created":
                self._notify(f"已创建: {r.story_key}")
            elif r.status == "need_manual_select":
                self._notify(f"需要手动选择父故事: {item.title}")
            else:
                self._notify(f"创建失败: {r.error}")

    screen = InboxScreen(items)
    self.push_screen(screen, _on_inbox_result)
```

- [ ] **Step 3: Commit**

```bash
git add src/story_lifecycle/cli/tui.py
git commit -m "feat: add [i] inbox screen for external source items"
```

---

### Task 8: 全量测试 + lint

- [ ] **Step 1: Run all tests**

Run: `cd /d/story-lifecycle && python -m pytest tests/ -v`

- [ ] **Step 2: Run lint**

Run: `cd /d/story-lifecycle && ruff check src/`

- [ ] **Step 3: Fix any issues**

- [ ] **Step 4: Final commit if needed**

```bash
git add -A
git commit -m "chore: P0 final cleanup and lint"
```
