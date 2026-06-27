# Story 基础管理 — TAPD 可见性优先 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 TAPD 需求状态和本地 AI 开发进度统一到 Web Dashboard，实现需求可见性管理。

**Architecture:** 以 story 为中心实体，TAPD 作为数据源 enrichment 进 story 表。新增 `story sync` CLI 命令拉取 TAPD 数据，扩展 story 表字段存储 TAPD 元数据，重新设计 Web Dashboard 展示统一视图。

**Tech Stack:** Python 3.10+, SQLite, Click CLI, Rich, FastAPI, Pydantic, YAML

---

## File Structure

### 修改的文件
- `src/story_lifecycle/db/models.py` — 新增字段、迁移、VALID_COLUMNS 更新
- `src/story_lifecycle/orchestrator/api.py` — 新增/扩展 API 端点
- `src/story_lifecycle/cli/main.py` — 新增 `sync`、`list`、`show`、`advance`、`done` 命令
- `src/story_lifecycle/sources/tapd_source.py` — `_parse_story` 增加 deadline 等字段提取
- `src/story_lifecycle/sources/base.py` — `SourceItem` 增加 deadline 字段

### 新建的文件
- `src/story_lifecycle/cli/sync_cmd.py` — `story sync` 命令实现
- `src/story_lifecycle/cli/list_cmd.py` — `story list` 命令实现
- `src/story_lifecycle/orchestrator/sync_service.py` — sync 核心逻辑（CLI 和 API 共用）
- `tests/test_sync.py` — sync 功能测试
- `tests/test_story_list_cli.py` — CLI list/show/advance/done 测试

### 不变的文件
- Web 前端构建产物（`src/story_lifecycle/web/`）— 本次只做后端 API，前端由后续前端任务处理

---

## Task 1: 扩展 SourceItem 数据模型 + TAPD 解析增强

**Files:**
- Modify: `src/story_lifecycle/sources/base.py:8-18`
- Modify: `src/story_lifecycle/sources/tapd_source.py:109-148`
- Test: `tests/test_source_integration.py`

- [ ] **Step 1: 在 `SourceItem` 增加 `deadline` 字段**

在 `src/story_lifecycle/sources/base.py` 的 `SourceItem` dataclass 中增加：

```python
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
    deadline: str = ""          # ISO date string, e.g. "2026-06-15"
    parent_id: str | None = None
    extra: dict = field(default_factory=dict)
    fetched_at: float = 0.0
```

- [ ] **Step 2: 在 `_parse_story` 中提取 deadline**

在 `src/story_lifecycle/sources/tapd_source.py` 的 `_parse_story` 方法中，`SourceItem` 构造增加：

```python
deadline=raw.get("due_date", "") or raw.get("begin_date", ""),
```

同时在 `extra` 中增加 `url` 字段：

```python
extra={
    "short_id": short_id,
    "category": raw.get("category_name", ""),
    "iteration_id": raw.get("iteration_id", ""),
    "url": f"https://www.tapd.cn/{self._api.workspace_id}/prong/stories/view/{full_id}",
},
```

- [ ] **Step 3: 在 `_parse_bug` 中同样提取 deadline**

```python
deadline=raw.get("deadline", ""),
```

在 `extra` 中增加 `url` 和 `related_story_id`：

```python
extra={
    "severity": raw.get("severity", ""),
    "url": f"https://www.tapd.cn/{self._api.workspace_id}/bugtrace/bugs/view?bug_id={raw.get('id', '')}",
    "related_story_id": raw.get("story_id", ""),
},
```

- [ ] **Step 4: 写测试**

在 `tests/test_source_integration.py` 中追加（文件已存在，追加到末尾）：

```python
class TestSourceItemDeadline:
    def test_source_item_has_deadline_field(self):
        from story_lifecycle.sources.base import SourceItem
        item = SourceItem(
            id="123", source="tapd", item_type="requirement",
            title="Test", description="desc", deadline="2026-06-15"
        )
        assert item.deadline == "2026-06-15"

    def test_source_item_deadline_defaults_empty(self):
        from story_lifecycle.sources.base import SourceItem
        item = SourceItem(
            id="123", source="tapd", item_type="requirement",
            title="Test", description="desc"
        )
        assert item.deadline == ""
```

- [ ] **Step 5: 运行测试确认通过**

```bash
pytest tests/test_source_integration.py -v -k "deadline or SourceItem"
```

Expected: 2 passed

- [ ] **Step 6: Commit**

```bash
git add src/story_lifecycle/sources/base.py src/story_lifecycle/sources/tapd_source.py tests/test_source_integration.py
git commit -m "feat: SourceItem 增加 deadline 字段 + TAPD 解析增强"
```

---

## Task 2: story 表扩展字段 + DB 迁移

**Files:**
- Modify: `src/story_lifecycle/db/models.py:9-27, 61-196`

- [ ] **Step 1: 更新 VALID_COLUMNS**

在 `src/story_lifecycle/db/models.py` 的 `VALID_COLUMNS` 集合中增加：

```python
VALID_COLUMNS = frozenset(
    {
        "title",
        "workspace",
        "profile",
        "current_stage",
        "status",
        "complexity",
        "context_json",
        "execution_count",
        "last_error",
        "updated_at",
        "parent_key",
        "subtask_index",
        "sub_type",
        "source_type",
        "source_id",
        "deadline",
        "priority",
        "owner",
        "branches_json",
        "tapd_status",
        "tapd_url",
    }
)
```

- [ ] **Step 2: 在 `init_db` 中添加迁移语句**

在 `init_db()` 函数末尾（`idx_story_source` 创建之后）追加幂等迁移：

```python
        for col, default in [
            ("deadline", "TEXT"),
            ("priority", "TEXT"),
            ("owner", "TEXT"),
            ("branches_json", "TEXT DEFAULT '[]'"),
            ("tapd_status", "TEXT"),
            ("tapd_url", "TEXT"),
        ]:
            try:
                conn.execute(f"ALTER TABLE story ADD COLUMN {col} {default}")
            except sqlite3.OperationalError:
                pass
```

- [ ] **Step 3: 新增 `upsert_story_from_source` 辅助函数**

在 `models.py` 的 CRUD 区域（`upsert_story` 函数之后）添加：

```python
def upsert_story_from_source(
    source_type: str,
    source_id: str,
    title: str = "",
    workspace: str = "",
    profile: str = "minimal",
    current_stage: str = "design",
    status: str = "active",
    deadline: str = "",
    priority: str = "",
    owner: str = "",
    tapd_status: str = "",
    tapd_url: str = "",
) -> tuple[dict, bool]:
    """Insert or update a story from an external source.
    Returns (story_dict, was_created).
    """
    existing = find_by_source_id(source_type, source_id)
    if existing:
        update_story(
            existing["story_key"],
            title=title or None,
            deadline=deadline or None,
            priority=priority or None,
            owner=owner or None,
            tapd_status=tapd_status or None,
            tapd_url=tapd_url or None,
        )
        return get_story(existing["story_key"]), False
    else:
        import re
        key = f"{source_type}-{source_id}"
        create_story(
            story_key=key,
            title=title,
            workspace=workspace or str(Path.cwd()),
            profile=profile,
            current_stage=current_stage,
        )
        update_story(
            key,
            source_type=source_type,
            source_id=source_id,
            deadline=deadline,
            priority=priority,
            owner=owner,
            tapd_status=tapd_status,
            tapd_url=tapd_url,
        )
        return get_story(key), True
```

- [ ] **Step 4: 写测试**

在 `tests/test_service.py` 末尾追加：

```python
class TestStoryNewFields:
    def test_upsert_story_from_source_creates_new(self, isolated_story_home):
        from story_lifecycle.db import models as db
        story, created = db.upsert_story_from_source(
            source_type="tapd",
            source_id="1123456700001",
            title="TAPD 需求",
            deadline="2026-06-15",
            priority="高",
            owner="zhangsan",
            tapd_status="open",
            tapd_url="https://www.tapd.cn/1234/prong/stories/view/1123456700001",
        )
        assert created is True
        assert story["deadline"] == "2026-06-15"
        assert story["priority"] == "高"
        assert story["source_type"] == "tapd"

    def test_upsert_story_from_source_updates_existing(self, isolated_story_home):
        from story_lifecycle.db import models as db
        db.upsert_story_from_source(
            source_type="tapd", source_id="1123456700002", title="原始标题"
        )
        story, created = db.upsert_story_from_source(
            source_type="tapd",
            source_id="1123456700002",
            title="更新标题",
            tapd_status="progressing",
        )
        assert created is False
        assert story["title"] == "更新标题"
        assert story["tapd_status"] == "progressing"
```

- [ ] **Step 5: 运行测试**

```bash
pytest tests/test_service.py -v -k "StoryNewFields"
```

Expected: 2 passed

- [ ] **Step 6: Commit**

```bash
git add src/story_lifecycle/db/models.py tests/test_service.py
git commit -m "feat: story 表扩展 deadline/priority/owner/tapd_status 等字段"
```

---

## Task 3: Sync Service 核心逻辑

**Files:**
- Create: `src/story_lifecycle/orchestrator/sync_service.py`
- Test: `tests/test_sync.py`

- [ ] **Step 1: 写 sync service 的测试**

创建 `tests/test_sync.py`：

```python
"""Tests for TAPD sync service."""
import pytest
from unittest.mock import MagicMock, patch
from story_lifecycle.db import models as db


class TestSyncService:
    def test_sync_creates_new_stories(self, isolated_story_home):
        from story_lifecycle.orchestrator.sync_service import sync_tapd
        from story_lifecycle.sources.base import SourceItem

        items = [
            SourceItem(
                id="1001", source="tapd", item_type="requirement",
                title="用户登录", description="实现登录功能",
                priority="高", owner="zhangsan", deadline="2026-06-15",
                status="open",
                extra={"short_id": "1001", "url": "https://tapd.cn/1001"},
            ),
            SourceItem(
                id="bug_2001", source="tapd", item_type="bug",
                title="白屏问题", description="打开页面白屏",
                priority="紧急", owner="zhangsan", deadline="2026-06-11",
                status="new",
                extra={"severity": "严重", "url": "https://tapd.cn/bug/2001"},
            ),
        ]

        result = sync_tapd(items, workspace="/tmp/test-ws")

        assert result["created"] == 2
        assert result["updated"] == 0

        s1 = db.get_story("tapd-1001")
        assert s1 is not None
        assert s1["title"] == "用户登录"
        assert s1["deadline"] == "2026-06-15"
        assert s1["source_type"] == "tapd"

        s2 = db.get_story("tapd-bug_2001")
        assert s2 is not None
        assert s2["title"] == "白屏问题"

    def test_sync_updates_existing_stories(self, isolated_story_home):
        from story_lifecycle.orchestrator.sync_service import sync_tapd
        from story_lifecycle.sources.base import SourceItem

        db.upsert_story_from_source(
            source_type="tapd", source_id="1001",
            title="旧标题", tapd_status="open",
        )

        items = [
            SourceItem(
                id="1001", source="tapd", item_type="requirement",
                title="新标题", description="更新",
                priority="高", deadline="2026-06-20", status="progressing",
                extra={"url": "https://tapd.cn/1001"},
            ),
        ]

        result = sync_tapd(items, workspace="/tmp/test-ws")
        assert result["created"] == 0
        assert result["updated"] == 1

        s = db.get_story("tapd-1001")
        assert s["title"] == "新标题"
        assert s["tapd_status"] == "progressing"
        assert s["deadline"] == "2026-06-20"

    def test_sync_dry_run_does_not_write(self, isolated_story_home):
        from story_lifecycle.orchestrator.sync_service import sync_tapd
        from story_lifecycle.sources.base import SourceItem

        items = [
            SourceItem(
                id="1001", source="tapd", item_type="requirement",
                title="Dry run", description="",
                extra={},
            ),
        ]

        result = sync_tapd(items, workspace="/tmp/test-ws", dry_run=True)
        assert result["created"] == 1
        assert result["would_create"] == 1

        s = db.get_story("tapd-1001")
        assert s is None

    def test_sync_status_only_skips_new(self, isolated_story_home):
        from story_lifecycle.orchestrator.sync_service import sync_tapd
        from story_lifecycle.sources.base import SourceItem

        db.upsert_story_from_source(
            source_type="tapd", source_id="1001", title="已存在"
        )

        items = [
            SourceItem(
                id="1001", source="tapd", item_type="requirement",
                title="更新", description="", status="done",
                extra={},
            ),
            SourceItem(
                id="9999", source="tapd", item_type="requirement",
                title="新的", description="",
                extra={},
            ),
        ]

        result = sync_tapd(items, workspace="/tmp/test-ws", status_only=True)
        assert result["updated"] == 1
        assert result["skipped"] == 1
        assert db.get_story("tapd-9999") is None

    def test_sync_empty_items(self, isolated_story_home):
        from story_lifecycle.orchestrator.sync_service import sync_tapd

        result = sync_tapd([], workspace="/tmp/test-ws")
        assert result["created"] == 0
        assert result["updated"] == 0
```

- [ ] **Step 2: 运行测试确认失败**

```bash
pytest tests/test_sync.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'story_lifecycle.orchestrator.sync_service'`

- [ ] **Step 3: 实现 sync_service.py**

创建 `src/story_lifecycle/orchestrator/sync_service.py`：

```python
"""TAPD sync service — transform SourceItems into local stories."""
from __future__ import annotations

import logging
from pathlib import Path

from ..db import models as db

log = logging.getLogger(__name__)


def sync_tapd(
    items: list,
    workspace: str = "",
    profile: str = "minimal",
    dry_run: bool = False,
    status_only: bool = False,
) -> dict:
    """Sync TAPD SourceItems into local stories.

    Returns dict with counts: created, updated, skipped, would_create.
    """
    result = {"created": 0, "updated": 0, "skipped": 0, "would_create": 0}
    ws = workspace or str(Path.cwd())

    for item in items:
        existing = db.find_by_source_id(item.source, item.id)

        if dry_run:
            if existing:
                result["updated"] += 1
            else:
                result["would_create"] += 1
            continue

        if existing:
            db.update_story(
                existing["story_key"],
                title=item.title or None,
                deadline=item.deadline or None,
                priority=item.priority or None,
                owner=item.owner or None,
                tapd_status=item.status or None,
                tapd_url=item.extra.get("url") or None,
            )
            result["updated"] += 1
            log.info(f"Updated story for {item.source}:{item.id}")
        elif status_only:
            result["skipped"] += 1
        else:
            story, _ = db.upsert_story_from_source(
                source_type=item.source,
                source_id=item.id,
                title=item.title,
                workspace=ws,
                profile=profile,
                deadline=item.deadline,
                priority=item.priority,
                owner=item.owner,
                tapd_status=item.status,
                tapd_url=item.extra.get("url", ""),
            )
            result["created"] += 1
            log.info(f"Created story {story['story_key']} for {item.source}:{item.id}")

    return result
```

- [ ] **Step 4: 运行测试确认通过**

```bash
pytest tests/test_sync.py -v
```

Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/story_lifecycle/orchestrator/sync_service.py tests/test_sync.py
git commit -m "feat: sync service — TAPD SourceItem → local story 同步"
```

---

## Task 4: `story sync` CLI 命令

**Files:**
- Create: `src/story_lifecycle/cli/sync_cmd.py`
- Modify: `src/story_lifecycle/cli/main.py:369-374` (add_command 区域)

- [ ] **Step 1: 创建 sync_cmd.py**

创建 `src/story_lifecycle/cli/sync_cmd.py`：

```python
"""story sync — 拉取 TAPD 需求/缺陷同步为本地 story。"""
import click
from rich.console import Console
from rich.table import Table

console = Console()


@click.command("sync")
@click.option("--dry-run", is_flag=True, help="只显示会创建/更新哪些，不实际执行")
@click.option("--status-only", is_flag=True, help="只更新现有 story 的 TAPD 状态")
@click.option("--workspace", "-w", default=None, help="新 story 的工作区目录")
def sync_cmd(dry_run, status_only, workspace):
    """拉取 TAPD 待处理需求/缺陷，同步为本地 story。"""
    from ..db.models import init_db
    from ..sources.tapd_source import TapdSource

    init_db()

    config = _load_tapd_config()
    if not config:
        console.print("[red]TAPD 未配置。请先在 ~/.story-lifecycle/config.yaml 中添加 tapd 段。[/]")
        console.print("[dim]示例:\n  tapd:\n    workspace_id: \"12345\"\n    owner: \"zhangsan\"[/]")
        raise SystemExit(1)

    console.print("[bold cyan]正在拉取 TAPD 数据...[/]")

    source = TapdSource(config)
    try:
        items = source.fetch_pending()
    except Exception as e:
        console.print(f"[red]TAPD 拉取失败: {e}[/]")
        raise SystemExit(1)

    if not items:
        console.print("[green]没有待处理的需求或缺陷。[/]")
        return

    console.print(f"  拉取到 [cyan]{len(items)}[/] 个待处理项")

    if dry_run:
        _show_dry_run(items)
        return

    from ..orchestrator.sync_service import sync_tapd

    result = sync_tapd(
        items,
        workspace=workspace or ".",
        dry_run=dry_run,
        status_only=status_only,
    )

    console.print(
        f"\n[green]同步完成[/]: "
        f"新建 [cyan]{result['created']}[/] | "
        f"更新 [cyan]{result['updated']}[/] | "
        f"跳过 [dim]{result['skipped']}[/]"
    )


def _load_tapd_config() -> dict:
    """从 config.yaml 读取 TAPD 配置。"""
    from pathlib import Path
    import yaml

    config_file = Path.home() / ".story-lifecycle" / "config.yaml"
    if not config_file.exists():
        return {}

    with open(config_file, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    return data.get("tapd", {})


def _show_dry_run(items):
    """展示 dry-run 预览。"""
    from ..db import models as db

    table = Table(title="Dry Run 预览")
    table.add_column("ID", style="cyan")
    table.add_column("类型")
    table.add_column("标题")
    table.add_column("优先级")
    table.add_column("截止日期")
    table.add_column("操作", style="green")

    for item in items:
        existing = db.find_by_source_id(item.source, item.id)
        action = "更新" if existing else "新建"
        item_type = "缺陷" if item.item_type == "bug" else "需求"
        table.add_row(
            item.id[:20],
            item_type,
            item.title[:40],
            item.priority,
            item.deadline,
            action,
        )

    console.print(table)
```

- [ ] **Step 2: 注册到 CLI 主命令**

在 `src/story_lifecycle/cli/main.py` 的 `add_command` 区域（约 374 行附近），在 `from .project import project` 之前添加：

```python
from .sync_cmd import sync_cmd  # noqa: E402

cli.add_command(sync_cmd)
```

- [ ] **Step 3: 验证命令注册成功**

```bash
story sync --help
```

Expected: 显示 `Usage: story sync [OPTIONS]` 和选项说明

- [ ] **Step 4: Commit**

```bash
git add src/story_lifecycle/cli/sync_cmd.py src/story_lifecycle/cli/main.py
git commit -m "feat: story sync CLI — 拉取 TAPD 数据同步为本地 story"
```

---

## Task 5: `story list` / `story show` / `story advance` / `story done` CLI 命令

**Files:**
- Create: `src/story_lifecycle/cli/list_cmd.py`
- Modify: `src/story_lifecycle/cli/main.py` (注册新命令)
- Test: `tests/test_story_list_cli.py`

- [ ] **Step 1: 创建 list_cmd.py**

创建 `src/story_lifecycle/cli/list_cmd.py`：

```python
"""story list / show / advance / done — 基础 story 管理 CLI 命令。"""
from __future__ import annotations

import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

console = Console()


@click.command("list")
@click.option("--status", "-s", default=None, help="按状态筛选 (active/paused/completed/failed)")
@click.option("--overdue", is_flag=True, help="只显示已逾期的 story")
@click.option("--all", "show_all", is_flag=True, help="显示所有状态（含 completed/failed）")
def list_cmd(status, overdue, show_all):
    """列出所有 story。"""
    from ..db import models as db

    db.init_db()

    if show_all:
        stories = db.list_active_stories() + db.list_completed_stories(limit=100)
    else:
        stories = db.list_active_stories()

    if status:
        stories = [s for s in stories if s["status"] == status]

    if overdue:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        stories = [s for s in stories if s.get("deadline") and s["deadline"] < now]

    if not stories:
        console.print("[dim]没有 story。运行 [bold]story sync[/] 从 TAPD 拉取需求。[/]")
        return

    table = Table()
    table.add_column("KEY", style="cyan", max_width=20)
    table.add_column("标题", max_width=35)
    table.add_column("优先级", max_width=6)
    table.add_column("截止", max_width=10)
    table.add_column("阶段", max_width=10)
    table.add_column("状态", max_width=8)
    table.add_column("TAPD", max_width=10)

    from datetime import datetime, timezone
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for s in stories:
        deadline = s.get("deadline", "") or ""
        deadline_display = deadline[:10] if deadline else ""

        # 颜色标记
        stage = s["current_stage"]
        st = s["status"]
        tapd_st = s.get("tapd_status", "") or ""

        # 截止日期样式
        deadline_style = ""
        if deadline and deadline[:10] < now_str:
            deadline_style = "bold red"
        elif deadline:
            from datetime import timedelta
            try:
                dl = datetime.fromisoformat(deadline[:10])
                delta = (dl - datetime.now(timezone.utc)).days
                if delta <= 3:
                    deadline_style = "yellow"
            except ValueError:
                pass

        table.add_row(
            s["story_key"],
            s.get("title", "")[:35],
            s.get("priority", "")[:6],
            f"[{deadline_style}]{deadline_display}[/]" if deadline_style else deadline_display,
            stage,
            st,
            tapd_st[:10],
        )

    console.print(table)
    console.print(f"[dim]共 {len(stories)} 个 story[/]")


@click.command("show")
@click.argument("key")
def show_cmd(key):
    """查看 story 详情。"""
    from ..db import models as db
    import json

    db.init_db()
    s = db.get_story(key)
    if not s:
        console.print(f"[red]Story {key} 不存在[/]")
        raise SystemExit(1)

    lines = []
    lines.append(f"[bold cyan]{s['story_key']}[/]")
    lines.append(f"  标题: {s.get('title', '')}")
    lines.append(f"  状态: {s['status']}")
    lines.append(f"  阶段: {s['current_stage']}")
    lines.append(f"  Profile: {s.get('profile', '')}")
    lines.append(f"  工作区: {s.get('workspace', '')}")

    if s.get("deadline"):
        lines.append(f"  截止日期: {s['deadline']}")
    if s.get("priority"):
        lines.append(f"  优先级: {s['priority']}")
    if s.get("owner"):
        lines.append(f"  处理人: {s['owner']}")
    if s.get("tapd_status"):
        lines.append(f"  TAPD 状态: {s['tapd_status']}")
    if s.get("tapd_url"):
        lines.append(f"  TAPD 链接: {s['tapd_url']}")

    branches_raw = s.get("branches_json", "[]")
    if isinstance(branches_raw, str):
        try:
            branches = json.loads(branches_raw)
        except (json.JSONDecodeError, TypeError):
            branches = []
    else:
        branches = branches_raw or []
    if branches:
        lines.append("  关联分支:")
        for b in branches:
            lines.append(f"    - {b.get('repo', '')}/{b.get('branch', '')} ({b.get('status', '')})")

    if s.get("last_error"):
        lines.append(f"  [red]最后错误: {s['last_error'][:100]}[/]")

    console.print(Panel("\n".join(lines)))

    # 显示最近的 stage log
    logs = db.get_stage_logs(key, limit=10)
    if logs:
        console.print("\n[bold]最近操作:[/]")
        for log_entry in logs:
            console.print(
                f"  [{log_entry.get('created_at', '')[:16]}] "
                f"{log_entry['stage']} — {log_entry['action']}"
                + (f" ({log_entry['detail'][:50]})" if log_entry.get("detail") else "")
            )


@click.command("advance")
@click.argument("key")
def advance_cmd(key):
    """手动推进 story 到下一阶段。"""
    from ..db import models as db

    db.init_db()
    s = db.get_story(key)
    if not s:
        console.print(f"[red]Story {key} 不存在[/]")
        raise SystemExit(1)

    STAGE_ORDER = ["design", "implement", "test", "done"]
    current = s["current_stage"]

    if current == "done":
        console.print("[yellow]Story 已完成，无法继续推进。[/]")
        return

    try:
        idx = STAGE_ORDER.index(current)
    except ValueError:
        console.print(f"[red]未知阶段: {current}[/]")
        return

    next_stage = STAGE_ORDER[idx + 1] if idx + 1 < len(STAGE_ORDER) else "done"

    db.update_story(key, current_stage=next_stage)
    db.log_stage(key, next_stage, "advance", f"手动推进: {current} → {next_stage}")

    if next_stage == "done":
        db.update_story(key, status="completed")

    console.print(f"[green]{current} → {next_stage}[/]")


@click.command("done")
@click.argument("key")
def done_cmd(key):
    """标记 story 完成。"""
    from ..db import models as db

    db.init_db()
    s = db.get_story(key)
    if not s:
        console.print(f"[red]Story {key} 不存在[/]")
        raise SystemExit(1)

    db.update_story(key, current_stage="done", status="completed")
    db.log_stage(key, "done", "complete", "手动标记完成")

    console.print(f"[green]Story {key} 已标记完成[/]")

    if s.get("source_type") == "tapd" and s.get("source_id"):
        console.print(
            f"[dim]提示: TAPD 状态未自动同步。"
            f"可手动到 {s.get('tapd_url', 'TAPD')} 更新状态。[/]"
        )
```

- [ ] **Step 2: 注册到 CLI 主命令**

在 `src/story_lifecycle/cli/main.py` 的 `add_command` 区域添加（在 sync_cmd 注册之后）：

```python
from .list_cmd import list_cmd, show_cmd, advance_cmd, done_cmd  # noqa: E402

cli.add_command(list_cmd)
cli.add_command(show_cmd)
cli.add_command(advance_cmd)
cli.add_command(done_cmd)
```

- [ ] **Step 3: 写 CLI 测试**

创建 `tests/test_story_list_cli.py`：

```python
"""Tests for story list/show/advance/done CLI commands."""
import pytest
from click.testing import CliRunner
from story_lifecycle.db import models as db


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def seeded_db(isolated_story_home):
    db.init_db()
    db.upsert_story_from_source(
        source_type="tapd", source_id="1001",
        title="测试需求", deadline="2026-06-15",
        priority="高", tapd_status="open",
    )
    db.upsert_story_from_source(
        source_type="tapd", source_id="1002",
        title="已完成", status="completed",
    )
    db.update_story("tapd-1002", status="completed", current_stage="done")


class TestListCmd:
    def test_list_shows_stories(self, runner, seeded_db):
        from story_lifecycle.cli.list_cmd import list_cmd
        result = runner.invoke(list_cmd)
        assert result.exit_code == 0
        assert "1001" in result.output or "tapd" in result.output

    def test_list_empty(self, runner, isolated_story_home):
        from story_lifecycle.cli.list_cmd import list_cmd
        db.init_db()
        result = runner.invoke(list_cmd)
        assert result.exit_code == 0
        assert "没有 story" in result.output


class TestShowCmd:
    def test_show_existing(self, runner, seeded_db):
        from story_lifecycle.cli.list_cmd import show_cmd
        result = runner.invoke(show_cmd, ["tapd-1001"])
        assert result.exit_code == 0
        assert "测试需求" in result.output
        assert "2026-06-15" in result.output

    def test_show_nonexistent(self, runner, isolated_story_home):
        from story_lifecycle.cli.list_cmd import show_cmd
        db.init_db()
        result = runner.invoke(show_cmd, ["NOPE"])
        assert result.exit_code == 1


class TestAdvanceCmd:
    def test_advance_moves_stage(self, runner, seeded_db):
        from story_lifecycle.cli.list_cmd import advance_cmd
        s = db.get_story("tapd-1001")
        assert s["current_stage"] == "design"

        result = runner.invoke(advance_cmd, ["tapd-1001"])
        assert result.exit_code == 0

        s = db.get_story("tapd-1001")
        assert s["current_stage"] == "implement"

    def test_advance_to_done(self, runner, isolated_story_home):
        from story_lifecycle.cli.list_cmd import advance_cmd
        db.init_db()
        db.create_story("ADV-001", "推进测试", "/tmp", current_stage="test")

        runner.invoke(advance_cmd, ["ADV-001"])
        s = db.get_story("ADV-001")
        assert s["current_stage"] == "done"
        assert s["status"] == "completed"


class TestDoneCmd:
    def test_done_marks_completed(self, runner, seeded_db):
        from story_lifecycle.cli.list_cmd import done_cmd
        result = runner.invoke(done_cmd, ["tapd-1001"])
        assert result.exit_code == 0

        s = db.get_story("tapd-1001")
        assert s["status"] == "completed"
        assert s["current_stage"] == "done"
```

- [ ] **Step 4: 运行测试**

```bash
pytest tests/test_story_list_cli.py -v
```

Expected: 6 passed

- [ ] **Step 5: 验证所有 CLI 命令注册**

```bash
story --help
```

Expected: 输出中包含 `sync`、`list`、`show`、`advance`、`done` 命令

- [ ] **Step 6: Commit**

```bash
git add src/story_lifecycle/cli/list_cmd.py src/story_lifecycle/cli/main.py tests/test_story_list_cli.py
git commit -m "feat: story list/show/advance/done CLI 命令"
```

---

## Task 6: API 端点扩展

**Files:**
- Modify: `src/story_lifecycle/orchestrator/api.py`
- Test: `tests/test_api_integration.py` (追加)

- [ ] **Step 1: 扩展 story 列表 API — 增加逾期筛选和新字段**

在 `src/story_lifecycle/orchestrator/api.py` 中，修改 `list_stories` 端点：

将现有的 `list_stories` 函数替换为：

```python
@app.get("/api/story")
def list_stories(
    status: str = "",
    overdue: bool = False,
    show_all: bool = False,
):
    """List stories with optional filters.

    Query params:
        status: Filter by status (active, paused, completed, failed)
        overdue: Only show stories past their deadline
        show_all: Include completed/failed stories
    """
    if show_all:
        stories = db.list_active_stories() + db.list_completed_stories(limit=100)
    else:
        stories = db.list_active_stories()

    if status:
        stories = [s for s in stories if s["status"] == status]

    if overdue:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        stories = [
            s for s in stories
            if s.get("deadline") and s["deadline"][:10] < now
        ]

    return JSONResponse(
        [
            {
                "storyKey": s["story_key"],
                "title": s["title"],
                "currentStage": s["current_stage"],
                "status": s["status"],
                "complexity": s["complexity"],
                "workspace": s["workspace"],
                "profile": s["profile"],
                "executionCount": s["execution_count"],
                "updatedAt": s["updated_at"],
                "deadline": s.get("deadline"),
                "priority": s.get("priority"),
                "owner": s.get("owner"),
                "tapdStatus": s.get("tapd_status"),
                "tapdUrl": s.get("tapd_url"),
            }
            for s in stories
        ]
    )
```

- [ ] **Step 2: 扩展单个 story API — 返回新字段**

在 `get_story` 端点的返回值中增加新字段。在返回的 JSONResponse dict 中追加：

```python
"deadline": s.get("deadline"),
"priority": s.get("priority"),
"owner": s.get("owner"),
"branchesJson": s.get("branches_json", "[]"),
"tapdStatus": s.get("tapd_status"),
"tapdUrl": s.get("tapd_url"),
"sourceType": s.get("source_type"),
"sourceId": s.get("source_id"),
```

- [ ] **Step 3: 新增 sync API 端点**

在 api.py 中（`static frontend` 挂载之前）添加：

```python
# -------- TAPD Sync API --------


class SyncRequest(BaseModel):
    workspace: str = ""
    dry_run: bool = False
    status_only: bool = False


@app.post("/api/sync/tapd")
def api_sync_tapd(req: SyncRequest):
    """触发 TAPD 同步。"""
    from ..sources.tapd_source import TapdSource

    config = _load_tapd_config()
    if not config:
        raise HTTPException(400, "TAPD not configured. Add 'tapd' section to config.yaml.")

    source = TapdSource(config)
    try:
        items = source.fetch_pending()
    except Exception as e:
        raise HTTPException(502, f"TAPD fetch failed: {e}")

    from .sync_service import sync_tapd

    result = sync_tapd(
        items,
        workspace=req.workspace or ".",
        dry_run=req.dry_run,
        status_only=req.status_only,
    )
    return result


@app.get("/api/sync/tapd/status")
def api_sync_status():
    """获取 TAPD 配置状态（是否已配置）。"""
    config = _load_tapd_config()
    return {
        "configured": bool(config),
        "workspace_id": config.get("workspace_id", ""),
    }


def _load_tapd_config() -> dict:
    from pathlib import Path
    import yaml

    config_file = Path.home() / ".story-lifecycle" / "config.yaml"
    if not config_file.exists():
        return {}
    with open(config_file, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("tapd", {})
```

- [ ] **Step 4: 写 API 测试**

在 `tests/test_api_integration.py` 末尾追加：

```python
class TestSyncAPI:
    def test_sync_status_unconfigured(self, api_client, isolated_story_home):
        resp = api_client.get("/api/sync/tapd/status")
        assert resp.status_code == 200
        assert resp.json()["configured"] is False

    def test_sync_tapd_unconfigured_returns_400(self, api_client, isolated_story_home):
        resp = api_client.post("/api/sync/tapd", json={})
        assert resp.status_code == 400


class TestStoryListWithFilters:
    def test_list_with_overdue_filter(self, api_client, isolated_story_home):
        db.upsert_story_from_source(
            source_type="tapd", source_id="1001",
            title="逾期需求", deadline="2020-01-01",
        )
        db.upsert_story_from_source(
            source_type="tapd", source_id="1002",
            title="未来需求", deadline="2099-12-31",
        )

        resp = api_client.get("/api/story?overdue=true")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["title"] == "逾期需求"

    def test_list_returns_new_fields(self, api_client, isolated_story_home):
        db.upsert_story_from_source(
            source_type="tapd", source_id="1001",
            title="带字段", deadline="2026-06-15",
            priority="高", tapd_status="open",
        )

        resp = api_client.get("/api/story")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        item = data[0]
        assert item["deadline"] == "2026-06-15"
        assert item["priority"] == "高"
        assert item["tapdStatus"] == "open"

    def test_story_detail_returns_new_fields(self, api_client, isolated_story_home):
        db.upsert_story_from_source(
            source_type="tapd", source_id="1001",
            title="详情测试", tapd_status="progressing",
        )

        resp = api_client.get("/api/story/tapd-1001")
        assert resp.status_code == 200
        data = resp.json()
        assert data["tapdStatus"] == "progressing"
        assert data["sourceType"] == "tapd"
```

- [ ] **Step 5: 运行测试**

```bash
pytest tests/test_api_integration.py -v -k "SyncAPI or StoryListWithFilters"
```

Expected: 5 passed

- [ ] **Step 6: Commit**

```bash
git add src/story_lifecycle/orchestrator/api.py tests/test_api_integration.py
git commit -m "feat: API 扩展 — story 新字段 + sync 端点 + 逾期筛选"
```

---

## Task 7: 全量回归测试

**Files:** 无新文件

- [ ] **Step 1: 运行全量测试**

```bash
pytest --tb=short -q
```

Expected: 所有测试通过（包括已有的 611 个 + 新增的 ~13 个）

- [ ] **Step 2: 如有失败，修复并重跑**

- [ ] **Step 3: 最终确认 — CLI 命令可用**

```bash
story sync --help
story list --help
story show --help
story advance --help
story done --help
```

Expected: 所有命令显示帮助信息

- [ ] **Step 4: 最终 Commit**

```bash
git add -A
git commit -m "test: story 管理基础功能全量回归通过"
```
