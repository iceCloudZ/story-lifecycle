# GitHub Issues Source Adapter 设计文档

## 概述

为 story-lifecycle 新增 GitHub Issues 数据源适配器，使用 `gh` CLI 代理 GitHub API 调用。从指定仓库拉取 open Issue 创建 Story，Story 阶段推进时同步更新 Issue 状态/标签。

## 决策记录

| 决策 | 选择 | 理由 |
|------|------|------|
| API 交互方式 | gh CLI 代理 | 零配置复用认证、Windows 原生支持、无新依赖 |
| 同步方向 | 双向 | Story 推进时自动回写 GitHub Issue 状态 |
| Issue 映射 | 1 Issue = 1 Story | 不做 parent-child，YAGNI |
| PRD 策略 | 现有 FallbackPrdProvider 兜底 | GitHub Issue body 天然 Markdown，质量足够 |
| 拉取范围 | 所有 open Issue | 简单明了，后续可按需加过滤 |

## 文件结构

```
src/story_lifecycle/sources/
├── base.py              # 已有，不动
├── __init__.py          # 新增 github 注册
├── github_source.py     # 新增，GithubSource 主类
├── github_cli.py        # 新增，gh CLI 封装
├── manual_source.py     # 已有
├── tapd_source.py       # 已有
├── tapd_api.py          # 已有
├── prd_providers.py     # 已有，FallbackPrdProvider 兜底 GitHub Issue
└── bug_providers.py     # 已有
```

## 配置

`~/.story-lifecycle/config.yaml` 新增：

```yaml
story_source:
  github:
    enabled: true
    repo: "owner/repo"       # 必填，GitHub 仓库
    poll_interval: 300       # 可选，默认 300 秒
```

`story setup` 命令新增 GitHub 源配置选项，提示输入 `owner/repo`。

## gh CLI 封装层 (`github_cli.py`)

`GithubCli` 类封装所有 `gh` CLI 调用：

```python
class GithubCli:
    def __init__(self, repo: str): ...

    def list_issues(self, state="open", label=None) -> list[dict]:
        # gh issue list -R {repo} --state {state} --json number,title,labels,body,assignees,state,milestone

    def get_issue(self, number: int) -> dict:
        # gh issue view {number} -R {repo} --json number,title,body,labels,assignees,state,milestone

    def close_issue(self, number: int) -> None:
        # gh issue close {number} -R {repo}

    def add_label(self, number: int, label: str) -> None:
        # gh issue edit {number} -R {repo} --add-label {label}

    def remove_label(self, number: int, label: str) -> None:
        # gh issue edit {number} -R {repo} --remove-label {label}

    def test_auth(self) -> bool:
        # gh auth status → 退出码判断
```

所有调用通过 `subprocess.run(["gh", ...], capture_output=True, text=True)` 执行，解析 JSON 输出。统一异常 `GithubCliError`。

## GithubSource 核心逻辑 (`github_source.py`)

### Issue → SourceItem 映射

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

### 接口实现

- **`fetch_pending()`**：调 `list_issues(state="open")`，逐个转 `SourceItem`
- **`get_detail(number)`**：调 `get_issue(number)`，返回完整 `SourceItem`
- **`sync_status(number, status)`**：
  - `"completed"` → `close_issue` + `remove_label("lifecycle:*")` + `add_label("lifecycle:done")`
  - `"started"` → `add_label("lifecycle:implementing")`
  - 其他状态 → 仅 add_label，不映射的不处理
- **`test_connection()`**：调 `test_auth()` 验证 gh 认证状态

### 注册

`sources/__init__.py` 新增：

```python
try:
    from .github_source import GithubSource
    register_source("github", lambda cfg: GithubSource(cfg))
except ImportError:
    pass
```

## 不做什么

- Issue 模板感知（idea/design/implementation 统一当 requirement）
- parent-child Issue 关系
- GraphQL 优化
- Webhook（只用轮询）
- 专属 GithubBodyPrdProvider（用 FallbackPrdProvider 兜底）

## 依赖

- 运行时依赖：`gh` CLI 已安装并认证
- 无新增 Python 包依赖
