# Phase 1: GitHub Issues 数据源 + 双写同步

## 概述

为 story-lifecycle 新增 GitHub Issues 数据源适配器（试点项目：stock-research）。包含两部分：
1. **数据源**：从 GitHub 仓库拉取 `lifecycle:accepted` 标签的 Issue 创建 Story
2. **双写同步**：执行层面继续用文件协议（`.story/` 目录），阶段推进后自动将任务书、评审、完成状态同步到 Issue comment/labels，GitHub Issue 成为可视化仪表盘

## 决策记录

| 决策 | 选择 | 理由 |
|------|------|------|
| API 交互方式 | gh CLI 代理 | 零配置复用认证、Windows 原生支持、无新依赖 |
| 同步方向 | 双向（数据源拉取 + 状态/内容回写） | Story 推进时自动回写 GitHub Issue 状态和内容 |
| sync_context 不入基类 | 留在 GithubSource 内，isinstance 判断 | YAGNI — 只有 GitHub 需要双写，过早抽象会增加无意义的多态分支；接入 GitLab/Jira 时再抽接口 |
| Issue 映射 | 1 Issue = 1 Story | 不做 parent-child，YAGNI |
| PRD 策略 | 现有 FallbackPrdProvider 兜底 | GitHub Issue body 天然 Markdown，质量足够 |
| 拉取范围 | `lifecycle:accepted` 标签的 open Issue | 只拉人审核过的、准备开发的 Issue |
| 执行协议 | 文件协议不变 + Issue 双写 | 文件协议快速可靠，Issue 提供可视化 |
| 试点项目 | stock-research | 先跑通再推广到其他项目 |

## 文件结构

```
src/story_lifecycle/sources/
├── base.py              # 已有，不动
├── __init__.py          # 新增 github 注册
├── github_source.py     # 新增，GithubSource（数据源 + 双写同步）
├── github_cli.py        # 新增，gh CLI 封装
├── manual_source.py     # 已有
├── tapd_source.py       # 已有
├── tapd_api.py          # 已有
├── prd_providers.py     # 已有
└── bug_providers.py     # 已有
```

## 配置

`~/.story-lifecycle/config.yaml` 新增：

```yaml
story_source:
  github:
    enabled: true
    repo: "owner/repo"           # 必填
    poll_interval: 300           # 可选，默认 300 秒
    sync_to_issue: true          # 可选，默认 true
    accept_label: "lifecycle:accepted"  # 可选，拉取哪些 Issue
```

`story setup` 命令新增 GitHub 源配置选项。

## gh CLI 封装层 (`github_cli.py`)

```python
class GithubCli:
    def __init__(self, repo: str): ...

    def list_issues(self, state="open", label=None) -> list[dict]:
        # gh issue list -R {repo} --state {state} --label {label}
        #   --json number,title,labels,body,assignees,state,milestone

    def get_issue(self, number: int) -> dict:
        # gh issue view {number} -R {repo}
        #   --json number,title,body,labels,assignees,state,milestone

    def create_issue(self, title: str, body: str, label: list[str] | None = None) -> int:
        # gh issue create -R {repo} --title {title} --body {body} --label ...
        # → 返回 issue number

    def close_issue(self, number: int) -> None:
        # gh issue close {number} -R {repo}

    def add_label(self, number: int, label: str) -> None:
        # gh issue edit {number} -R {repo} --add-label {label}

    def remove_label(self, number: int, label: str) -> None:
        # gh issue edit {number} -R {repo} --remove-label {label}

    def comment_issue(self, number: int, body: str) -> None:
        # gh issue comment {number} -R {repo} --body {body}

    def test_auth(self) -> bool:
        # gh auth status → 退出码判断
```

所有调用通过 `subprocess.run(["gh", ...], capture_output=True, text=True)` 执行。统一异常 `GithubCliError`。

## 异常约定

**核心原则：文件协议是 source of truth，Issue 双写是 best-effort 仪表盘。**

1. **gh 未安装或未认证** — `story serve` 启动时 `test_connection()` fail-fast，报错退出，不进入双写流程
2. **网络超时 / rate limit** — `GithubCli` 内部统一 catch `subprocess.CalledProcessError`，包装为 `GithubCliError` 抛出
3. **双写失败不阻断 Story 流转** — `sync_status()` 和 `sync_context()` 外层 try/except，失败时 `logging.warning` 记录后继续。理由：Story 的进度已持久化在 `.story/` 和 DB 中，Issue 只是可视化
4. **拉取失败** — `fetch_pending()` 失败时返回空列表 + warning，不阻塞其他数据源
5. **重试策略** — 暂不做自动重试。避免在 Story 流转中引入延迟。用户可在 `.story-done/` 中看到真实状态，手动触发同步

## GithubSource 核心逻辑 (`github_source.py`)

### Part 1：数据源（Issue → Story）

#### Issue → SourceItem 映射

| GitHub Issue 字段 | SourceItem 字段 | 说明 |
|---|---|---|
| `number` (str) | `id` | "123" |
| "github" | `source` | 固定值 |
| labels 解析 | `item_type` | `type:bug` → "bug"，其余 → "requirement" |
| `title` | `title` | 直接映射 |
| `body` | `description` | 直接做 PRD 内容 |
| labels 解析 | `priority` | 取 `priority:*` 标签，默认空 |
| `assignees[0].login` | `owner` | 第一个 assignee |
| `state` | `status` | "open" / "closed" |
| - | `parent_id` | None |
| labels + milestone | `extra` | 存原始 labels、milestone |

#### 接口实现

- **`fetch_pending()`**：调 `list_issues(state="open", label=self.accept_label)`，逐个转 `SourceItem`
- **`get_detail(number)`**：调 `get_issue(number)`，返回完整 `SourceItem`（含完整 body）
- **`sync_status(number, status)`**：见 Part 2
- **`test_connection()`**：调 `test_auth()` 验证 gh 认证状态

### Part 2：双写同步（Story 进度 → Issue 可视化）

```python
class GithubSource(StorySource):
    def sync_status(self, item_id: str, status: str):
        number = int(item_id)
        STATUS_MAP = {
            "completed": ("close", "lifecycle:done"),
            "started": ("label", "lifecycle:implementing"),
            "blocked": ("label", "lifecycle:blocked"),
            "paused": ("label", "lifecycle:paused"),
        }
        action, label = STATUS_MAP.get(status, (None, None))
        if action == "close":
            self._cli.close_issue(number)
        if label:
            self._remove_lifecycle_labels(number)
            self._cli.add_label(number, label)

    def sync_context(self, item_id: str, stage: str, context: dict):
        number = int(item_id)
        parts = []
        if "plan_summary" in context:
            parts.append(f"## 任务书: {stage}\n{context['plan_summary']}")
        if "review_summary" in context:
            parts.append(f"## 评审: {stage}\n{context['review_summary']}")
        if "done_data" in context:
            parts.append(f"## 完成信号: {stage}\n```json\n{json.dumps(context['done_data'], ensure_ascii=False, indent=2)}\n```")
        if parts:
            self._cli.comment_issue(number, "\n\n---\n\n".join(parts))
```

#### 双写触发点

在 `graph_nodes.py` 关键节点中：

1. **`advance_node`**（阶段完成）→ `source.sync_context()`，同步任务书摘要 + 完成数据
2. **`advance_node`**（Story 全部完成）→ 已有 `source.sync_status(source_id, "completed")` 调用点
3. **`review_stage_node`**（评审完成）→ `source.sync_context()`，同步评审摘要

`sync_context` 是 `GithubSource` 专属方法，通过 `isinstance(source, GithubSource)` 判断是否支持。

## 注册

```python
# sources/__init__.py
try:
    from .github_source import GithubSource
    register_source("github", lambda cfg: GithubSource(cfg))
except ImportError:
    pass
```

## 不做什么

- Issue 模板感知（统一当 requirement）
- parent-child Issue 关系
- GraphQL 优化
- Webhook（只用轮询）
- 专属 GithubBodyPrdProvider
- 替换文件协议
- 将 `sync_context` 加入基类接口

## 依赖

- 运行时依赖：`gh` CLI 已安装并认证
- 无新增 Python 包依赖
