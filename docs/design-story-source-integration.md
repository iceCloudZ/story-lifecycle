# Story Source Integration 设计文档

> 日期：2026-05-23
> 状态：评审完成，已整合改进
> 作者：zhaozihao

---

## 1. 背景与问题

当前创建故事完全依赖手动输入：用户按 `[n]` → 填 key、标题、PRD 路径。实际开发中，需求的来源是外部平台（TAPD、Jira 等），用户需要手动在平台和工具之间复制粘贴。

**痛点**：
- 需求/bug 信息分散在 TAPD，手动同步容易遗漏
- 创建 bug-fix 子故事时，bug 描述需要手动从 TAPD 复制
- 故事完成后需要手动回 TAPD 更新状态，经常忘记
- 无法快速了解"当前有哪些待办"

**目标**：设计一个可扩展的故事来源适配层，让 Story Lifecycle 自动对接外部平台，用户只需确认即可创建故事。

## 2. 核心概念

```
外部平台 (TAPD / Jira / ...)
  │
  │  轮询 / 手动拉取
  ▼
StorySource Adapter (适配层)
  │
  │  统一的 SourceItem
  ▼
TUI 待办确认界面
  │
  │  用户选择 + 确认
  ▼
Story Lifecycle (创建故事 / 子故事)
  │
  │  执行完成
  ▼
状态回写 (更新外部平台状态)
```

**关键原则**：
- **有平台就自动，没平台就手动**：无配置时行为与现在完全一致
- **轮询走轻量 API，交互走 skill**：轮询用 TAPD CLI 直调（快速确定性），PRD 生成走 skill（AI 增强）
- **来源可扩展**：TAPD 只是第一个，未来加 Jira/GitHub Issues 只需新增 adapter

## 3. 适配器抽象层

### 3.1 SourceItem

平台条目的统一数据结构：

```python
@dataclass
class SourceItem:
    id: str               # 平台原始 ID，如 "1144381896001001234"
    source: str           # 来源标识，如 "tapd"、"jira"、"manual"
    item_type: str        # "requirement" | "bug"
    title: str
    description: str      # 原始描述（HTML 或纯文本）
    priority: str         # P0/P1/P2/P3 或平台原始优先级
    owner: str            # 处理人
    status: str           # 平台当前状态
    parent_id: str | None # 关联的父需求 ID（bug 关联 story 时有值）
    extra: dict           # 平台特有字段（TAPD 的 category、iteration_id 等）
    fetched_at: float     # 拉取时间戳
```

### 3.2 StorySource 接口

```python
class StorySource(ABC):
    """故事来源适配器"""

    @abstractmethod
    def fetch_pending(self) -> list[SourceItem]:
        """拉取待处理的条目（需求 + bug）"""

    @abstractmethod
    def get_detail(self, item_id: str) -> SourceItem | None:
        """获取单个条目详情（用于生成 PRD）"""

    @abstractmethod
    def sync_status(self, item_id: str, status: str):
        """回写状态到外部平台"""

    @abstractmethod
    def test_connection(self) -> bool:
        """测试连接是否可用"""
```

### 3.3 适配器注册

```python
# src/story_lifecycle/sources/__init__.py

_registry: dict[str, type[StorySource]] = {}

def register_source(name: str, cls: type[StorySource]):
    _registry[name] = cls

def get_source(name: str) -> StorySource | None:
    cls = _registry.get(name)
    return cls() if cls else None

def get_available_sources() -> list[str]:
    return list(_registry.keys())
```

### 3.4 Bug 父故事解析（BugParentResolver）

Bug 创建为子故事时需要知道关联哪个父故事。不同团队的关联方式不同，需要独立抽象。

> **评审改进**：原设计使用 `__manual_select__` 魔法字符串区分"需要手动选择"和"找到了父故事"。
> 改为 `ResolveResult` dataclass，显式表达解析结果，避免字符串魔法值泄漏到 Service/TUI 层。

```python
@dataclass
class ResolveResult:
    """Bug 父故事解析结果"""
    parent_key: str | None     # 找到的父故事 key，None 表示无法自动关联
    need_manual_select: bool   # True 表示需要 TUI 弹出选择框


class BugParentResolver(ABC):
    """Bug 关联父故事的解析策略"""

    @abstractmethod
    def resolve(self, bug: SourceItem, existing_stories: list[dict]) -> ResolveResult | None:
        """解析结果，返回 None 表示此 resolver 无法处理（跳过）。"""


class TapdRelationResolver(BugParentResolver):
    """TAPD 方式 — 通过 entity_relations API 查询 bug 关联的 story。"""

    def resolve(self, bug: SourceItem, existing_stories: list[dict]) -> ResolveResult | None:
        if not bug.extra.get("related_story_id"):
            return None  # 无法处理，跳过
        tapd_id = bug.extra["related_story_id"]
        for s in existing_stories:
            ctx = json.loads(s.get("context_json") or "{}")
            if ctx.get("source_id") == tapd_id:
                return ResolveResult(parent_key=s["story_key"], need_manual_select=False)
        # 有关联 ID 但找不到对应故事 → 需要手动选择
        return ResolveResult(parent_key=None, need_manual_select=True)


class TitlePatternResolver(BugParentResolver):
    """标题模式 — 从 bug 标题中提取 story ID，如 [STORY-123] 修复登录。"""

    PATTERN = r'\[([A-Z]+-\d+)\]'

    def resolve(self, bug: SourceItem, existing_stories: list[dict]) -> ResolveResult | None:
        import re
        m = re.search(self.PATTERN, bug.title)
        if not m:
            return None  # 无法处理，跳过
        story_key = m.group(1)
        for s in existing_stories:
            if s["story_key"] == story_key:
                return ResolveResult(parent_key=story_key, need_manual_select=False)
        return None  # 标题有 ID 但故事不存在，跳过让下一个 resolver 处理


class ManualResolver(BugParentResolver):
    """手动选择 — TUI 弹出故事列表让用户选择。"""

    def resolve(self, bug: SourceItem, existing_stories: list[dict]) -> ResolveResult | None:
        return ResolveResult(parent_key=None, need_manual_select=True)
```

### Resolver 链调度

```python
DEFAULT_BUG_PARENT_RESOLVERS = [
    TapdRelationResolver(),    # 先查平台关联关系
    TitlePatternResolver(),    # 再从标题提取
    ManualResolver(),          # 兜底：让用户手动选
]


def resolve_bug_parent(
    bug: SourceItem,
    existing_stories: list[dict],
    resolvers: list[BugParentResolver] | None = None,
) -> ResolveResult:
    """解析 bug 应关联的父故事。短路逻辑：找到 parent_key 立即返回，need_manual_select=True 也立即返回。"""
    chain = resolvers or DEFAULT_BUG_PARENT_RESOLVERS
    for resolver in chain:
        result = resolver.resolve(bug, existing_stories)
        if result is None:
            continue  # 此 resolver 无法处理，跳过
        if result.parent_key or result.need_manual_select:
            return result
    # 所有 resolver 都无法处理 → 创建独立故事
    return ResolveResult(parent_key=None, need_manual_select=False)
```

配置化：

```yaml
story_source:
  bug_parent_resolver:
    - tapd_relation     # 先查 TAPD 关联关系
    - title_pattern     # 再从标题提取
    - manual            # 兜底：手动选择
```

### TUI 手动选择交互

当 `ResolveResult.need_manual_select=True` 时，TUI 弹出选择框：

```
┌─ 选择父故事 ─────────────────────────────────┐
│ Bug: 登录后页面空白                            │
│                                               │
│ 关联到哪个故事？                               │
│  > FEATURE-001  用户登录功能                   │
│    FEATURE-002  支付模块重构                   │
│    [不关联，创建独立故事]                       │
│                                               │
│              [确认]    [取消]                   │
└───────────────────────────────────────────────┘
```

## 4. TAPD 适配器

### 4.1 概览

TAPD 适配器使用已有的 `cli_tapd.py`，通过**进程内 import** 调用（避免 subprocess 开销）。

```python
# src/story_lifecycle/sources/tapd_source.py

class TapdSource(StorySource):
    """TAPD 故事来源 — 进程内调用 cli_tapd 模块"""

    def __init__(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("cli_tapd", self.cli_path)
        self._cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self._cli)
```

### 4.2 配置化

适配器参数从 `config.yaml` 读取，不硬编码：

```yaml
# ~/.story-lifecycle/config.yaml
story_source:
  enabled: tapd          # 启用的来源，多个用逗号分隔；"manual" 或空则纯手动
  poll_interval: 300     # 轮询间隔（秒），默认 5 分钟
  tapd:
    cli_path: "C:/Users/zzh58/.claude/scripts/cli_tapd.py"
    workspace_id: "44381896"
    owner: "赵子豪"
    # 轮询时过滤的状态
    story_status: "open,progressing,reopened"
    bug_status: "new,reopened,assigned,resolving"
```

### 4.3 fetch_pending — 拉取待办

```python
def fetch_pending(self) -> list[SourceItem]:
    """拉取分配给当前用户的待处理需求和 bug。"""
    items = []
    items.extend(self._fetch_stories())
    items.extend(self._fetch_bugs())
    return items

def _fetch_stories(self) -> list[SourceItem]:
    result = self._run_cli("get-stories", {
        "entity_type": "stories",
        "limit": 20,
        "owner": self.owner,
        "status": self.story_status_filter,
    })
    return [self._parse_story(s) for s in result]

def _fetch_bugs(self) -> list[SourceItem]:
    result = self._run_cli("get-bug", {
        "limit": 20,
        "status": self.bug_status_filter,
    })
    return [self._parse_bug(b) for b in result]
```

### 4.4 get_detail — 获取详情

拉取完整需求描述（含子任务、图片），为 PRD 生成做准备：

```python
def get_detail(self, item_id: str) -> SourceItem | None:
    if item_id.startswith("bug_"):
        return self._get_bug_detail(item_id)
    return self._get_story_detail(item_id)
```

### 4.5 sync_status — 状态回写

```python
# 故事完成 → 更新 TAPD 需求状态
TAPD_STATUS_MAP = {
    "completed": "done",
    "blocked": "reopen",
    "aborted": "postponed",
}

def sync_status(self, item_id: str, status: str):
    tapd_status = TAPD_STATUS_MAP.get(status)
    if not tapd_status:
        return
    if item_id.startswith("bug_"):
        self._run_cli("update-bug", {"id": item_id, "status": tapd_status})
    else:
        self._run_cli("update-story", {"id": item_id, "status": tapd_status})
```

### 4.6 _call — 进程内调用封装

> **评审改进**：原设计使用 `subprocess.run` 调用 CLI 脚本，每次起子进程开销大且阻塞 TUI。
> 改为进程内 import cli_tapd 模块，直接调用函数，避免 Python 解释器重复启动。

```python
def _call(self, command: str, params: dict) -> list[dict]:
    """进程内调用 cli_tapd 模块，返回解析后的数据。"""
    import json
    # cli_tapd 模块通过 importlib 已加载到 self._cli
    # 直接调用对应函数，避免 subprocess 开销
    try:
        func = getattr(self._cli, command)
        result = func(
            workspace_id=self.workspace_id,
            params=json.dumps(params, ensure_ascii=False),
        )
        if isinstance(result, str):
            return json.loads(result)
        return result if isinstance(result, list) else []
    except Exception as e:
        log.warning(f"TAPD call error ({command}): {e}")
        return []
```

> **注意**：如果 cli_tapd.py 不方便 import（如依赖 argparse 做了全局操作），
> 可将其核心 API 请求逻辑提取为独立模块 `tapd_api.py`，CLI 入口只做参数解析后调用 API 模块。
> TapdSource 直接 import `tapd_api` 模块即可。

## 5. 手动适配器（默认）

```python
# src/story_lifecycle/sources/manual_source.py

class ManualSource(StorySource):
    """手动创建 — 现有行为的封装，不拉取任何外部数据。"""

    def fetch_pending(self) -> list[SourceItem]:
        return []

    def get_detail(self, item_id: str) -> SourceItem | None:
        return None

    def sync_status(self, item_id: str, status: str):
        pass  # 无需回写

    def test_connection(self) -> bool:
        return True
```

## 5.5 PRD 内容提供者（PrdProvider）

### 问题

PRD 来源和故事来源是**两个正交维度**：

- 故事来源：TAPD / Jira / Manual
- PRD 来源：TAPD 正文 / 钉钉文档 / Confluence / 本地文件 / 用户手动

同一个 TAPD 需求，PRD 可能在 TAPD 正文里，也可能在钉钉文档链接里，也可能在本地文件里。不同团队/用户的 PRD 获取方式不同，需要独立抽象。

### PrdContent 数据结构

```python
@dataclass
class PrdContent:
    source_type: str        # "tapd_body" | "dingtalk_doc" | "local_file" | "manual"
    markdown: str           # PRD 正文（已转为 markdown）
    file_path: str | None   # 保存到本地的路径（如果有）
    attachments: list[str]  # 附件/图片路径
```

### PrdProvider 接口

```python
class PrdProvider(ABC):
    """PRD 内容提供者 — 从不同来源获取 PRD 内容"""

    @abstractmethod
    def can_handle(self, item: SourceItem) -> bool:
        """判断是否能处理这个条目的 PRD 获取"""

    @abstractmethod
    def fetch_content(self, item: SourceItem) -> PrdContent | None:
        """提取 PRD 内容，返回 None 表示获取失败"""
```

### 内置 Provider（链式尝试）

按优先级尝试，第一个成功的就用：

```python
# src/story_lifecycle/sources/prd_providers.py

class TapdBodyPrdProvider(PrdProvider):
    """从 TAPD 正文直接提取 PRD — 适用于需求内容写在 TAPD 里的情况。"""

    def can_handle(self, item: SourceItem) -> bool:
        return item.source == "tapd" and bool(item.description.strip())

    def fetch_content(self, item: SourceItem) -> PrdContent | None:
        # HTML → Markdown 转换
        md = html_to_markdown(item.description)
        return PrdContent(source_type="tapd_body", markdown=md, file_path=None, attachments=[])


class DingTalkLinkPrdProvider(PrdProvider):
    """检测钉钉文档链接 → 抓取内容 → 转 MD。

    钉钉文档 URL 格式: https://dingtalk.com/doc/xxx 或 https://doc.dingtalk.com/xxx
    """

    DINGTALK_PATTERNS = [
        r"https?://(?:doc\.)?dingtalk\.com/\S+",
        r"https?://(?:[\w-]+\.)?dingtalk\.com/document/\S+",
    ]

    def can_handle(self, item: SourceItem) -> bool:
        return bool(self._extract_dingtalk_url(item.description))

    def fetch_content(self, item: SourceItem) -> PrdContent | None:
        url = self._extract_dingtalk_url(item.description)
        if not url:
            return None
        # 抓取钉钉文档内容
        md = self._fetch_dingtalk_doc(url)
        if not md:
            return None
        return PrdContent(source_type="dingtalk_doc", markdown=md, file_path=None, attachments=[])

    def _extract_dingtalk_url(self, text: str) -> str | None:
        import re
        for pattern in self.DINGTALK_PATTERNS:
            m = re.search(pattern, text)
            if m:
                return m.group(0)
        return None

    def _fetch_dingtalk_doc(self, url: str) -> str | None:
        """抓取钉钉文档。可能需要认证 token。"""
        # 方案 1: web_reader MCP（如果可访问）
        # 方案 2: 钉钉开放平台 API
        # 方案 3: 用户手动提供（返回 None，由 FallbackPrdProvider 处理）
        ...


class LocalFilePrdProvider(PrdProvider):
    """从本地文件路径读取 PRD — 适用于用户已下载/准备好的文件。"""

    def can_handle(self, item: SourceItem) -> bool:
        # 检测 description 中是否有本地文件路径
        import re
        return bool(re.search(r'(?:^|\n)(/\S+\.md|[A-Z]:\\\S+\.md)', item.description))

    def fetch_content(self, item: SourceItem) -> PrdContent | None:
        import re
        m = re.search(r'(?:^|\n)(/\S+\.md|[A-Z]:\\\S+\.md)', item.description)
        if not m:
            return None
        path = m.group(1)
        p = Path(path)
        if not p.exists():
            return None
        return PrdContent(
            source_type="local_file",
            markdown=p.read_text(encoding="utf-8"),
            file_path=str(p),
            attachments=[],
        )


class FallbackPrdProvider(PrdProvider):
    """兜底 — 所有 Provider 都无法处理时，用条目基本信息生成简易 PRD。"""

    def can_handle(self, item: SourceItem) -> bool:
        return True  # 始终可处理

    def fetch_content(self, item: SourceItem) -> PrdContent | None:
        md = (
            f"# {item.title}\n\n"
            f"**来源**: {item.source} ({item.id})\n"
            f"**优先级**: {item.priority}\n"
            f"**处理人**: {item.owner}\n\n"
            f"## 需求描述\n\n{item.description}\n"
        )
        return PrdContent(source_type="fallback", markdown=md, file_path=None, attachments=[])
```

### 5.5.1 Bug 内容提供者（BugContentProvider）

Bug 创建为子故事时，需要获取完整的 bug 上下文（复现步骤、截图、预期行为等），让 AI CLI 能有效修复。

```python
@dataclass
class BugContext:
    source_type: str            # "tapd_body" | "tapd_comments" | "dingtalk_doc" | "fallback"
    description: str            # Bug 现象描述
    steps_to_reproduce: str     # 复现步骤
    expected_behavior: str      # 预期行为
    actual_behavior: str        # 实际行为
    environment: str            # 环境信息
    screenshots: list[str]      # 截图文件路径
    logs: str                   # 相关日志/堆栈
    raw_markdown: str           # 原始完整内容


class BugContentProvider(ABC):
    """Bug 内容提供者 — 从不同来源获取 bug 的完整上下文"""

    @abstractmethod
    def can_handle(self, bug: SourceItem) -> bool:
        """判断是否能处理这个 bug 的内容获取"""

    @abstractmethod
    def fetch_content(self, bug: SourceItem) -> BugContext | None:
        """提取 bug 完整上下文"""
```

### 内置 BugContentProvider

```python
class TapdBodyBugProvider(BugContentProvider):
    """从 TAPD bug 正文提取 — 解析标准 bug 报告格式。"""

    def can_handle(self, bug: SourceItem) -> bool:
        return bug.source == "tapd" and bug.item_type == "bug"

    def fetch_content(self, bug: SourceItem) -> BugContext | None:
        md = html_to_markdown(bug.description)
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
        """从 markdown 中提取特定章节内容。"""
        import re
        m = re.search(rf'(?:{pattern})[：:\s]*\n(.*?)(?=\n##|\n#|\Z)', md, re.DOTALL | re.IGNORECASE)
        return m.group(1).strip() if m else ""

    def _extract_images(self, md: str) -> list[str]:
        """提取 markdown 中的图片路径，下载到本地。"""
        import re
        return re.findall(r'!\[.*?\]\((.*?)\)', md)


class TapdCommentsBugProvider(BugContentProvider):
    """从 TAPD bug 评论中提取补充信息 — 复现步骤常在评论里。"""

    def can_handle(self, bug: SourceItem) -> bool:
        return bug.source == "tapd" and bug.item_type == "bug"

    def fetch_content(self, bug: SourceItem) -> BugContext | None:
        # 通过 cli_tapd.py get-comments 拉取评论
        comments = self._fetch_comments(bug.id)
        if not comments:
            return None
        # 合并评论内容作为补充上下文
        combined = "\n\n".join(
            f"**{c['author']}** ({c['created']}):\n{c['description']}"
            for c in comments
        )
        return BugContext(
            source_type="tapd_comments",
            description=bug.title,
            steps_to_reproduce="",
            expected_behavior="",
            actual_behavior="",
            environment="",
            screenshots=[],
            logs="",
            raw_markdown=combined,
        )


class FallbackBugProvider(BugContentProvider):
    """兜底 — 只有标题和原始描述。"""

    def can_handle(self, bug: SourceItem) -> bool:
        return True

    def fetch_content(self, bug: SourceItem) -> BugContext | None:
        return BugContext(
            source_type="fallback",
            description=bug.title,
            steps_to_reproduce="",
            expected_behavior="",
            actual_behavior="",
            environment="",
            screenshots=[],
            logs="",
            raw_markdown=bug.description or bug.title,
        )
```

### Bug 内容聚合调度

> **评审改进**：原设计使用短路责任链（正文成功则跳过评论），但正文和评论是互补信息，应该**聚合**而非二选一。

```python
DEFAULT_BUG_CONTENT_PROVIDERS = [
    TapdBodyBugProvider(),      # 正文解析
    TapdCommentsBugProvider(),  # 评论补充
    FallbackBugProvider(),      # 兜底
]


def fetch_bug_content(
    bug: SourceItem,
    providers: list[BugContentProvider] | None = None,
) -> BugContext:
    """聚合所有 Provider 的结果。正文和评论都是有效上下文，需要合并而非二选一。"""
    chain = providers or DEFAULT_BUG_CONTENT_PROVIDERS
    combined = BugContext(
        source_type="aggregated",
        description=bug.title,
        steps_to_reproduce="",
        expected_behavior="",
        actual_behavior="",
        environment="",
        screenshots=[],
        logs="",
        raw_markdown="",
    )

    for provider in chain:
        if provider.can_handle(bug):
            partial = provider.fetch_content(bug)
            if partial:
                # 聚合：空字段用 provider 结果填充，非空字段追加
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
```

### Bug 上下文注入到子故事 Prompt

创建 bug-fix 子故事时，将 `BugContext` 结构化信息注入 `sub_description`：

```python
def _format_bug_context(ctx: BugContext) -> str:
    """将 BugContext 格式化为注入 prompt 的描述文本。"""
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

### 配置化

```yaml
story_source:
  bug_content_providers:
    - tapd_body         # 正文解析
    - tapd_comments     # 评论补充
    - fallback          # 兜底
```

### Provider 链调度

```python
# src/story_lifecycle/sources/prd_providers.py

DEFAULT_PRD_PROVIDERS = [
    TapdBodyPrdProvider(),
    DingTalkLinkPrdProvider(),
    LocalFilePrdProvider(),
    FallbackPrdProvider(),      # 兜底，永远在最后
]


def fetch_prd_content(
    item: SourceItem,
    providers: list[PrdProvider] | None = None,
) -> PrdContent | None:
    """按优先级尝试各 Provider，返回第一个成功的结果。"""
    chain = providers or DEFAULT_PRD_PROVIDERS
    for provider in chain:
        if provider.can_handle(item):
            content = provider.fetch_content(item)
            if content:
                return content
    return None
```

### 用户自定义 Provider

用户可在 config.yaml 中配置 Provider 链，或通过插件注册：

```yaml
story_source:
  enabled: tapd
  prd_providers:
    - dingtalk_link    # 先检测钉钉链接
    - tapd_body        # 再尝试 TAPD 正文
    - local_file       # 再尝试本地文件
    - fallback         # 兜底
```

### 钉钉文档的具体实现路径

钉钉文档获取有多种方案，需要根据团队实际情况选择：

| 方案 | 实现 | 适用场景 |
|------|------|---------|
| web_reader MCP | `mcp__web_reader__webReader` 直接抓 URL | 钉钉文档公开可访问时 |
| 钉钉开放平台 API | 调用文档 API，需 appKey/appSecret | 企业有钉钉开发权限时 |
| 用户手动下载 | 返回 None → FallbackPrdProvider → TUI 提示用户提供路径 | 以上都不可用时 |

**P0 实现**：先跳过钉钉自动抓取，`DingTalkLinkPrdProvider` 检测到钉钉链接后返回 None，由 FallbackPrdProvider 兜底。用户可在 TUI 中手动指定本地文件路径。

**P1 实现**：根据团队实际情况选择钉钉集成方案。

## 6. Service 层扩展

### 6.1 创建故事的来源感知

```python
# service.py 扩展

def create_story_from_source(
    item: SourceItem,
    profile: str = "minimal",
    workspace: str = "",
    generate_prd: bool = True,
) -> str:
    """从外部平台条目创建故事。"""
    story_key = _derive_story_key(item)
    title = item.title
    prd_path = None

    # 需求类型 → 通过 PrdProvider 链获取 PRD
    if generate_prd and item.item_type == "requirement":
        from ..sources.prd_providers import fetch_prd_content
        prd_content = fetch_prd_content(item)
        if prd_content and prd_content.markdown:
            prd_path = _save_prd(story_key, prd_content, workspace)

    # Bug 类型 → 解析父故事关联 → 创建子故事或独立故事
    if item.item_type == "bug":
        from ..sources.base import resolve_bug_parent
        active_stories = db.list_active_stories()
        result = resolve_bug_parent(item, active_stories)

        if result.need_manual_select:
            return {"need_manual_select": True, "bug": item}
        if result.parent_key:
            return create_sub_story(
                parent_key=result.parent_key,
                sub_type="bug-fix",
                description=item.description,
            )
        # 无父故事 → 创建独立故事（继续往下执行）

    # 创建普通故事
    key = create_and_start_story(
        story_key=story_key,
        title=title,
        profile=profile,
        workspace=workspace,
        prd_path=prd_path,
    )

    # 记录来源映射
    db.update_context(key, "source_id", item.id)
    db.update_context(key, "source_type", item.source)

    return key


def _derive_story_key(item: SourceItem) -> str:
    """从平台条目生成 story key。"""
    if item.source == "tapd":
        # TAPD ID 最后 6 位作为 key
        return f"TAPD-{item.id[-6:]}"
    return f"{item.source.upper()}-{item.id[-6:]}"


def _save_prd(story_key: str, prd_content: PrdContent, workspace: str) -> str:
    """将 PrdContent 保存为本地 PRD 文件。"""
    prd_dir = Path(workspace) / "prd"
    prd_dir.mkdir(exist_ok=True)

    # 如果 PrdProvider 已经指定了文件路径（如 LocalFilePrdProvider），直接用
    if prd_content.file_path and Path(prd_content.file_path).exists():
        return prd_content.file_path

    prd_file = prd_dir / f"{story_key}.md"
    prd_file.write_text(prd_content.markdown, encoding="utf-8")
    return str(prd_file)
```

### 6.2 状态回写触发

在故事的 `advance_node` 完成时（状态变为 `completed`），触发来源回写：

```python
# nodes.py advance_node 中，故事完成后添加：
if state["status"] == "completed":
    ctx = state.get("context", {})
    source_id = ctx.get("source_id")
    source_type = ctx.get("source_type")
    if source_id and source_type:
        from ..sources import get_source
        source = get_source(source_type)
        if source:
            try:
                source.sync_status(source_id, "completed")
            except Exception as e:
                log.warning(f"Failed to sync status to {source_type}: {e}")
```

## 7. TUI 交互设计

### 7.1 待办收件箱

新增 `[i]` 按键，打开待办收件箱：

```
┌─ 待办收件箱 (TAPD) ──────────────────────────┐
│                                               │
│  需求:                                        │
│  [✓] STORY-1234  用户登录功能      P0  前端   │
│  [✓] STORY-1235  支付模块重构      P1  后端   │
│  [ ] STORY-1236  数据导出优化      P2  后端   │
│                                               │
│  Bug:                                         │
│  [✓] BUG-401    登录后页面空白                │
│    → 关联: STORY-1234 (已存在)                │
│  [ ] BUG-402    注册校验不生效                │
│                                               │
│  [Enter] 确认创建选中项  [r] 刷新  [Esc] 关闭 │
└───────────────────────────────────────────────┘
```

交互流程：
1. 按 `[i]` 打开收件箱
2. 上/下移动，空格选择/取消
3. Enter 确认创建选中的条目
4. 每个选中项自动调用 `create_story_from_source`

### 7.2 创建来源选择

修改 `[n]` 新建故事的流程，增加来源选择：

```
按 [n] 后:
  有配置来源? → 弹出选择:
    ┌─ 创建故事 ──────────────────┐
    │ 来源:                       │
    │  > 从 TAPD 拉取 (推荐)      │
    │    手动输入                  │
    └─────────────────────────────┘
  无配置来源? → 直接打开 ManualSource 对话框（现有行为）
```

### 7.3 按键绑定扩展

```python
Binding("i", "show_inbox", "Inbox"),    # 打开待办收件箱
```

## 8. 轮询调度

### 8.1 TUI 内轮询

在 `StoryBoardApp.on_mount` 中，如果配置了来源，启动定时轮询：

```python
def on_mount(self):
    ...
    from ..sources import get_available_sources
    from ..cli.setup import get_config
    config = get_config()
    source_config = config.get("story_source", {})
    poll_interval = source_config.get("poll_interval", 300)

    if source_config.get("enabled"):
        self._source_enabled = True
        self._poll_interval = poll_interval
        self.set_interval(poll_interval, self._poll_source)
    else:
        self._source_enabled = False

async def _poll_source(self):
    """后台轮询外部来源，发现新条目时显示通知。"""
    from ..sources import get_source
    from ..cli.setup import get_config
    from ..db.models import get_db

    config = get_config()
    source_name = config.get("story_source", {}).get("enabled", "")
    source = get_source(source_name)
    if not source:
        return

    items = source.fetch_pending()
    # 通过 DB 查询已导入的 source_id，避免重复导入
    db = get_db()
    new_items = [i for i in items if not db.find_by_source_id(i.id)]

    if new_items:
        self._pending_items = new_items
        # 在 header 显示通知
        header = self.query_one("#header-bar")
        header.update(
            f"\n  [bold cyan]◆[/] [bold white]Story[/][bold cyan]Lifecycle[/] "
            f" [dim]│[/] [bold yellow]{len(new_items)} 个新待办[/] "
            f"[dim]│[/] 按 [[i]] 查看"
        )
```

### 8.2 轮询频率

| 场景 | 频率 | 说明 |
|------|------|------|
| 默认 | 5 分钟 | 平衡实时性和 API 压力 |
| 有活跃故事时 | 5 分钟 | 不需要更频繁 |
| 用户手动刷新 | 立即 | 收件箱内按 `[r]` |

## 9. PRD 生成策略

### 9.1 交互式场景（skill 驱动）

用户在收件箱确认创建时，如果是需求类型，可以调用 `prd-generator` skill 生成完整 PRD：

```
用户选中 STORY-1234 → 确认创建
  → 调 tapd skill 拉完整详情（含图片、子任务）
  → 调 prd-generator skill 生成结构化 PRD
  → 创建故事，PRD 路径写入 context
```

这个流程由 **agent** 驱动（不是代码直调），因为 PRD 生成涉及图片识别、AI 补充等需要 agent 能力的步骤。

### 9.2 快速创建场景（跳过 PRD）

用户也可以选择快速创建，跳过 PRD 生成：

```
用户选中 STORY-1234 → 快速创建
  → 用平台描述作为初始 PRD（简单写入）
  → 直接创建故事，进入设计阶段
```

### 9.3 Bug 类型

Bug 不需要 PRD，直接用 bug 描述创建 bug-fix 子故事。

## 10. 数据模型

### 10.1 来源映射 + 去重

不需要新增 DB 表。来源信息存在 `context_json` 中：

```json
{
  "source_id": "1144381896001001234",
  "source_type": "tapd",
  "source_status": "progressing"
}
```

去重通过查询 `context_json` 中的 `source_id` 实现，不需要额外的 `imported.json` 文件：

```python
# db/models.py 新增
def find_by_source_id(self, source_id: str) -> dict | None:
    """通过 source_id 查找已导入的故事（避免重复导入）。"""
    rows = self._conn.execute(
        "SELECT * FROM story WHERE context_json LIKE ?",
        (f'%"source_id": "{source_id}"%',),
    ).fetchall()
    return dict(rows[0]) if rows else None
```

> **评审改进**：原设计使用 `imported.json` 做去重，但这引入了额外的状态文件和同步问题。
> 改为直接查询 DB 中 `context_json` 的 `source_id` 字段，单一数据源，无文件管理开销。

## 11. 实现范围与优先级

### P0 — 基础适配层 + TAPD 集成

1. StorySource 抽象接口 + ManualSource
2. TapdSource（fetch_pending + sync_status）
3. Service 层 `create_story_from_source()`
4. TUI 收件箱 `[i]` 按键 + 待办列表
5. config.yaml 来源配置
6. DB 去重（find_by_source_id）

### P1 — 增强

7. PRD 生成集成（prd-generator skill）
8. 状态回写（故事完成 → 更新 TAPD 状态）
9. 父子关联（bug 自动关联已有故事）
10. 轮询通知

### P2 — 扩展

11. Bug 自动创建子故事（review 发现 → 自动创建 bug-fix 子故事）
12. Webhook 模式（替代轮询）
13. 其他平台适配器（Jira、GitHub Issues）

## 12. 文件结构

```
src/story_lifecycle/
├── sources/                    # 新增模块
│   ├── __init__.py            # 注册表 + get_source
│   ├── base.py                # StorySource 抽象类 + SourceItem
│   ├── prd_providers.py       # PrdProvider 抽象 + 内置 Provider 链
│   ├── manual_source.py       # 手动适配器（默认）
│   └── tapd_source.py         # TAPD 适配器
├── orchestrator/
│   ├── service.py             # 扩展 create_story_from_source
│   ├── nodes.py               # 扩展 advance_node 状态回写
│   └── api.py                 # 新增 /api/inbox 端点
└── cli/
    ├── tui.py                 # 收件箱界面 + 来源选择
    └── setup.py               # config.yaml 来源配置
```

## 13. 风险与约束

| 风险 | 缓解措施 |
|------|---------|
| TAPD API 限流 | 轮询间隔不低于 5 分钟，请求限制 limit=20 |
| CLI 调用失败（网络/认证） | 捕获异常，降级为手动模式，TUI 提示错误 |
| 平台状态不一致 | 回写失败只记录日志，不影响故事执行 |
| TAPD CLI 路径因用户不同变化 | 从 config.yaml 读取，setup 向导中配置 |
| PRD 生成耗时（图片识别等） | 异步执行，不阻塞 TUI |

## 14. 验收标准

1. 配置 `story_source.enabled: tapd` 后，TUI 出现 `[i]` 收件箱入口
2. 收件箱正确显示 TAPD 待处理的需求和 bug
3. 选择并确认后，自动创建故事/子故事，PRD 自动生成
4. 故事完成后，TAPD 状态自动更新
5. 已导入的条目不重复显示
6. 未配置来源时，行为与现有完全一致（手动创建）
7. TAPD CLI 连接失败时，降级为手动模式，TUI 显示提示

## 15. 开放问题

| # | 问题 | 选项 | 当前决策 |
|---|------|------|---------|
| 1 | PRD 生成是同步还是异步？ | A) 创建前同步 B) 创建后异步 | 待定 |
| 2 | Bug 关联父故事靠什么字段？ | A) TAPD 关联关系 B) 用户手动选 | A 优先，B 兜底 |
| 3 | 多来源同时启用？ | A) 只启用一个 B) 可同时启用多个 | A — 简化实现 |
| 4 | ~~imported.json 清理策略？~~ | — | **已废弃** — 评审改为 DB 去重（find_by_source_id），无需管理额外文件 |
| 5 | 自定义类型是否需要专属内容获取路径？ | A) 内容跟着来源走，不跟着类型走 B) 每个类型可配独立 Provider | **A** — 类型是标签，内容由 StorySource + item_type 决定。当前够用，但如果有场景需要（如 perf-tuning 需要拉 benchmark 数据），可能需要 B。待评审确认。 |
| 6 | integration/refinement/redo 子故事来源？ | A) 全部手动创建 B) refinement 可从 TAPD story 变更检测 C) redo 从 review 评审结果自动触发 | 待定 — 变更检测和 review 自动触发留到后续迭代 |
| 7 | 钉钉文档 P0 如何处理？ | A) 跳过，Fallback 兜底 B) web_reader MCP 尝试抓取 | A — P0 先跳过 |
