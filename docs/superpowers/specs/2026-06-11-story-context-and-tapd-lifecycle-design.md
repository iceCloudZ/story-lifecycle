# Story 长期资料与 TAPD 生命周期设计

**日期**：2026-06-11  
**状态**：设计已确认，待用户评审文档  
**范围**：Story 长期资料、多仓库 Worktree、Session 上下文注入、TAPD 与本地 Story 生命周期

## 背景

当前系统已经能够：

- 将 TAPD 需求或缺陷同步为本地 Story。
- 保存 `source_type`、`source_id`、`tapd_status`、`tapd_url` 等外部来源字段。
- 使用 `context_json` 保存阶段输出和运行时上下文。
- 在 Story 执行时渲染 Prompt，并通过 PTY、Session 或 Headless 模式运行 AI。
- 使用 `branches_json` 展示简单分支信息。

但以下长期事实没有稳定模型：

- Story 影响哪些项目或代码仓库。
- 每个项目实际使用哪个分支和 Worktree。
- 对应的 PRD、设计文档及其摘要。
- 每个项目涉及哪些 DDL、Nacos 变更。
- AI 自动发现的事实来自什么证据，是否已经人工确认。
- 重新开启 Session 时，如何稳定注入上述资料。

`context_json` 同时承担阶段结果、临时运行标记和部分业务上下文，不适合继续承载需要长期维护、局部编辑和审计的资料。

此外，TAPD Story、本地 Story 和 AI Session 是三套不同生命周期。同步 TAPD 数据不能等价于启动 AI，本地开发完成也不能直接等价于 TAPD 工作流完成。

## 目标

1. 为每个 Story 保存结构化的影响项目、PRD、设计文档、分支、DDL 和 Nacos 变更。
2. 支持一个 Story 影响多个本地仓库。
3. 支持不同 Story 并行编辑同一个仓库，并通过独立 Worktree 隔离。
4. 用户和 AI 都可维护资料，所有 AI 更新必须带来源和证据。
5. 每次新建、重开或恢复 Session 时，注入轻量上下文目录，由 AI 按需读取原文。
6. TAPD 同步只创建候选 Story，不自动消耗 AI 资源。
7. 本地完成后只生成 TAPD 回写建议，用户确认后按实际工作流执行。
8. 所有不可执行分支提供用户可见反馈和诊断事件。

## 非目标

本期不实现：

- TAPD Webhook 实时同步。
- 自动判断生产环境是否已经上线。
- 自动执行 DDL。
- 自动发布 Nacos。
- 自动删除 Worktree。
- 跨机器同步本地仓库绝对路径。
- 通用 CMDB、发布平台或配置中心管理系统。

## 架构决策

采用“结构化事实 + 运行时快照”方案：

- 数据库结构化保存项目、文档和变更项。
- Resolver 只读取数据库和文件系统事实。
- Decider 使用纯函数输出是否可启动、如何合并资料、是否可回写 TAPD。
- Handler 才允许更新数据库、创建 Worktree、启动 Session 或调用 TAPD。
- Session 启动前生成带 revision 的 Markdown 快照。
- Prompt 只注入引用、摘要、状态和证据路径，不自动展开完整正文。

不采用以下方案：

### 扩展 `context_json`

实现简单，但会继续混合长期事实与运行时状态，并导致并发覆盖、局部编辑和审计困难。

### 单一 `story_context` JSON 文档

能够隔离运行上下文，但项目、DDL、Nacos 和确认项仍难以独立查询、校验和展示。

## 数据模型

### `story` 扩展

新增字段：

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `intake_state` | TEXT | `ready` | `candidate` 或 `ready` |
| `context_revision` | INTEGER | `0` | 长期资料版本，防止并发覆盖 |

规则：

- 现有 Story 迁移后设为 `ready`。
- TAPD 同步新建的 Story 设为 `candidate`。
- 手工创建的 Story 默认设为 `ready`。
- `candidate` 不允许启动 AI。
- 用户启动候选 Story 时，系统先验证项目绑定，再将其转为 `ready`。

`story.status` 继续表达本地执行状态：

```text
idle | active | paused | blocked | waiting_subtasks |
completed | failed | aborted
```

迁移时现有状态保持不变；新同步的候选 Story 使用 `idle`。

### `project`

共享仓库注册表。不同 Story 可以引用同一个项目。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 项目 ID |
| `name` | TEXT UNIQUE | 用户可识别的项目名 |
| `repo_path` | TEXT UNIQUE | 主仓库规范化绝对路径 |
| `default_branch` | TEXT | 默认基线分支 |
| `remote_url` | TEXT | Git 远程地址，可空 |
| `availability` | TEXT | `available/missing/not_git/unknown` |
| `created_at` | TIMESTAMP | 创建时间 |
| `updated_at` | TIMESTAMP | 更新时间 |

`repo_path` 写入前使用 `Path.resolve()` 规范化。路径不存在时允许保存，但必须标记为 `missing`，启动 Story 时拒绝执行。

### `story_project`

Story 对项目的执行绑定。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 绑定 ID |
| `story_key` | TEXT FK | 本地 Story |
| `project_id` | INTEGER FK | 共享项目 |
| `branch` | TEXT | Story 专用分支 |
| `base_branch` | TEXT | 创建分支使用的基线 |
| `base_commit` | TEXT | 准备 Worktree 时锁定的基线提交 |
| `worktree_path` | TEXT UNIQUE | Story 实际执行目录 |
| `workspace_type` | TEXT | `worktree/main` |
| `worktree_state` | TEXT | `unprepared/available/missing/stale/conflict/unknown` |
| `summary` | TEXT | 该项目在 Story 中的影响摘要 |
| `source` | TEXT | `user/ai/import` |
| `evidence_ref` | TEXT | 证据路径、commit 或外部引用 |
| `created_at` | TIMESTAMP | 创建时间 |
| `updated_at` | TIMESTAMP | 更新时间 |

唯一约束：

- `(story_key, project_id)` 唯一。
- `worktree_path` 非空时全局唯一。
- 活动 Story 不能共享同一分支执行绑定。

### `story_document`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 文档 ID |
| `story_key` | TEXT FK | 本地 Story |
| `project_id` | INTEGER FK NULL | 文档可属于整个 Story 或单个项目 |
| `kind` | TEXT | `prd/design` |
| `ref` | TEXT | 文件路径或 URL |
| `summary` | TEXT | 简短摘要 |
| `source` | TEXT | `user/ai/tapd/import` |
| `evidence_ref` | TEXT | 证据引用 |
| `verification_state` | TEXT | 可信状态 |
| `created_at` | TIMESTAMP | 创建时间 |
| `updated_at` | TIMESTAMP | 更新时间 |

### `story_change_item`

DDL 和 Nacos 使用统一的变更项模型。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 变更项 ID |
| `story_key` | TEXT FK | 本地 Story |
| `project_id` | INTEGER FK | 所属项目 |
| `kind` | TEXT | `ddl/nacos` |
| `ref` | TEXT | SQL 文件、配置文件、Data ID 或外部 URL |
| `summary` | TEXT | 变更摘要 |
| `lifecycle_state` | TEXT | 变更进度 |
| `verification_state` | TEXT | 证据可信状态 |
| `environment` | TEXT | 环境，可空 |
| `source` | TEXT | `user/ai/import` |
| `evidence_ref` | TEXT | commit、diff、流水线记录或人工说明 |
| `created_at` | TIMESTAMP | 创建时间 |
| `updated_at` | TIMESTAMP | 更新时间 |

`lifecycle_state`：

```text
proposed | detected | implemented | released
```

`verification_state`：

```text
unverified | evidence_backed | user_confirmed | contradicted
```

两套状态必须分开。例如 AI 可以根据 Git diff 将 DDL 标记为：

```text
lifecycle_state = implemented
verification_state = evidence_backed
```

但不能仅凭代码将其标记为已执行：

```text
lifecycle_state = released
```

### 事件日志

复用现有 `event_log`，新增事件类型：

```text
context_changed
context_refresh_started
context_refresh_completed
context_refresh_failed
worktree_prepare_started
worktree_prepare_completed
worktree_prepare_failed
worktree_cleanup_previewed
worktree_cleanup_completed
tapd_writeback_suggested
tapd_writeback_confirmed
tapd_writeback_failed
context_snapshot_created
```

事件记录 `context_revision`、变更来源、受影响实体 ID 和 reason code。

## 资料写入规则

### 用户写入

- 用户通过 Web Story 详情页编辑。
- API 是统一写入边界。
- CLI 使用同一服务层，不直接拼 SQL。
- 用户写入的事实默认标记 `user_confirmed`。

### AI 写入

AI 可以主动发现和更新：

- 当前分支、commit 和 Worktree 状态。
- Git diff 中的 SQL 或约定 DDL 文件。
- 配置文件或代码中出现的 Nacos Data ID 和配置项。
- 已存在的 PRD、设计文档和测试报告。

AI 写入必须同时提供：

- `source=ai`
- `evidence_ref`
- 发现时间
- `evidence_backed` 或 `unverified`

AI 不得仅凭推断更新：

- DDL 已执行。
- Nacos 已发布。
- 测试或生产环境已部署。
- TAPD 应该完成。

### 并发写入

所有资料更新请求携带 `context_revision`。

处理流程：

1. Handler 在事务内读取当前 revision。
2. 请求 revision 不一致时返回 HTTP 409。
3. 响应包含最新 revision、最新资料和冲突实体。
4. 保存项目、文档、变更项和事件日志。
5. `context_revision + 1`。
6. 提交事务。

禁止后写入者静默覆盖先写入者。

## Worktree 模型

### 为什么需要 Worktree

不同 Story 可能同时编辑同一个项目：

```text
hc-order 主仓库：D:\code\hc-order

Story A：
  D:\worktrees\TAPD-101\hc-order
  codex/TAPD-101-order

Story B：
  D:\worktrees\TAPD-102\hc-order
  codex/TAPD-102-order
```

`project` 表示共享仓库身份，`story_project` 表示 Story 专属执行目录。

### 默认命名

配置增加：

```yaml
worktree_root: D:\worktrees
branch_prefix: codex/
```

默认 Worktree：

```text
<worktree_root>/<story_key>/<project_name>
```

默认分支：

```text
codex/<story_key>-<project-slug>
```

分支创建前必须执行：

```text
git check-ref-format --branch <branch>
```

### Worktree Resolver

使用：

```text
git worktree list --porcelain -z
```

解析稳定的机器可读输出，不解析人类展示文本。

Resolver 输出：

```text
WorktreeState =
  unprepared | available | missing | stale | conflict | unknown
```

Resolver 只读，不创建、移动、删除或修复 Worktree。

### Worktree 决策表

| 事实 | 决策 |
|---|---|
| 目标 Worktree 不存在，分支不存在 | 创建分支和 Worktree |
| Worktree 存在、分支匹配、未被其他 Story 占用 | 复用 |
| 路径存在但不是登记的 Git Worktree | 拒绝启动 |
| 分支已在其他 Worktree checkout | 拒绝启动，不使用 `--force` |
| Worktree 分支与登记分支不一致 | 标记 `stale`，拒绝启动 |
| 主仓库有未提交修改 | 从明确的 `base_commit` 创建，不修改主仓库 |
| Story 完成或终止 | 保留 Worktree，等待人工确认清理 |
| Worktree 非 clean | 禁止自动删除，不提供 force 清理 |

### 启动协议

```text
用户启动 Story
-> 读取项目事实
-> 验证全部 repo_path
-> 解析 Git Worktree 事实
-> 纯函数生成准备计划
-> 获取项目级文件锁
-> 锁内重新解析事实
-> 创建全部 Story Worktree
-> 生成 Context Snapshot
-> 启动 AI Session
-> Story 进入 active
```

只有全部项目准备成功后才允许启动 Session。

若部分项目创建失败：

- 本次新建且 clean 的 Worktree 可以补偿清理。
- 已存在的 Worktree 不动。
- 非 clean Worktree 不自动删除。
- Story 不进入 `active`。
- 返回 `worktree_prepare_partial`。

### 清理协议

清理必须由用户确认：

1. Resolver 检查 Worktree 是否存在、是否 clean、分支是否已合并。
2. Decider 生成 cleanup preview。
3. 页面展示将删除的路径和保留的分支。
4. 用户确认后 Handler 执行 `git worktree remove`。
5. P0 不自动删除分支。
6. P0 不使用 `--force` 删除 dirty Worktree。

## Session 上下文注入

### 触发时机

每次以下操作前生成最新快照：

- 新建 Session。
- 重新开启 Session。
- 恢复暂停或阻塞的 Story。
- 阶段重试需要启动新的执行上下文。

已存在健康 Session 时，用户进入 Session 只 attach，不重复启动或重复注入。

### Context Resolver

Resolver 读取：

- Story 标识、标题和当前阶段。
- TAPD 镜像字段和外部链接。
- 所有 `story_project` 及实际 Worktree。
- PRD 和设计文档引用与摘要。
- DDL/Nacos 的状态、可信度和证据。
- 当前 `context_revision`。

Resolver 校验：

- 项目和 Worktree 路径是否存在。
- Worktree 当前分支是否匹配。
- 本地文件引用是否可读。
- URL 格式是否有效。
- 状态值是否合法。

Resolver 不执行：

- Git checkout。
- Worktree 创建或删除。
- 数据库更新。
- Session 启动。
- TAPD 回写。

### 快照格式

快照保存到：

```text
.story/context/<story_key>/story-context-r<revision>.md
```

内容示例：

```markdown
## Story 长期上下文

- Story: TAPD-123
- Context Revision: 7
- TAPD: https://...

### 项目：hc-order

- 主仓库：D:\code\hc-order
- 执行目录：D:\worktrees\TAPD-123\hc-order
- 分支：codex/TAPD-123-hc-order
- 基线：master@abc123
- 影响摘要：订单状态扩展

文档：
- PRD：prd/TAPD-123.md
  摘要：增加订单取消原因

DDL：
- db/migration/V123__order_reason.sql
  状态：implemented / evidence_backed
  证据：commit abc456

Nacos：
- Data ID: hc-order.yaml
  状态：detected / unverified
  证据：src/main/resources/application.yml:42
```

Prompt 只注入快照内容，不自动读取所有原文，并明确要求 AI 按需打开引用。

每次 Session 启动记录：

- `context_revision`
- 快照路径
- Session ID
- 当前阶段
- 项目和资料数量

这样可以追踪 AI 当时看到的事实。

## 自动发现

### 触发条件

自动发现只在单 Story 范围触发：

1. 新建或重新开启 Session 前。
2. 阶段完成并消费 done 文件后。
3. 用户点击“刷新 Story 资料”。
4. 项目绑定、分支或 Worktree 发生修改后。

禁止后台无界全量扫描所有 Story。

### 三层结构

#### Scanner

只读取目标 Worktree：

- 当前分支和 HEAD。
- 与 `base_commit` 的 diff。
- 约定 SQL、migration 和配置文件。
- PRD、设计、测试报告路径。
- 配置项和 Data ID 引用。

Git 子进程必须：

- 使用参数数组，不使用 shell 字符串拼接。
- 设置超时。
- 捕获 stdout/stderr 和返回码。
- 对失败输出长度设上限。

#### Decider

纯函数比较当前资料与扫描候选：

```text
existing facts + scan candidates -> context mutations
```

输出新增、更新、矛盾和忽略项，不直接写 DB。

#### Handler

在短事务内：

- 应用 mutation。
- 记录事件。
- 增加 `context_revision`。

Scanner 不得：

- 启动 AI。
- 执行 SQL。
- 发布配置。
- 更新 TAPD。

## TAPD 与本地 Story 生命周期

### 三套权威状态

#### TAPD Story

业务协作状态由 TAPD 项目的实际工作流定义，例如：

```text
规划中 -> 实现中 -> 已实现
```

也可能使用自定义状态或并行工作流。

#### 本地 Story

```text
intake_state:
  candidate | ready

status:
  idle | active | paused | blocked | waiting_subtasks |
  completed | failed | aborted
```

#### AI Session

```text
missing | starting | live | exited | unknown
```

Session 存在不代表本地 Story 正在执行；本地 Story 完成也不代表 TAPD 已完成。

### TAPD 同步

同步流程：

```text
TAPD fetch
-> 根据 source_type/source_id 查找本地 Story
-> 新项创建 candidate + idle
-> 已存在项只更新 TAPD 权威字段
-> 不启动线程
-> 不创建 Session
-> 不创建 Worktree
```

TAPD 权威字段：

- 标题。
- 负责人。
- 优先级。
- 截止日期。
- TAPD 状态。
- TAPD URL。

本地权威字段：

- 项目绑定。
- Worktree 和分支。
- PRD/设计引用及本地摘要。
- DDL/Nacos 资料。
- 本地执行阶段和状态。
- AI 证据与人工确认。

TAPD 同步不得覆盖本地权威字段。

### 状态与动作决策表

| 事实状态 | 用户动作 | 行为 |
|---|---|---|
| TAPD 新项，无本地 Story | sync | 创建 `candidate + idle`，不启动 AI |
| `candidate` | start | 校验项目，转 `ready`，准备 Worktree 后启动 |
| `candidate` 且项目路径无效 | start | 拒绝并返回 `project_path_missing` |
| `idle/paused/blocked` | open session | 刷新资料、生成快照、启动或恢复 |
| `active + live session` | open session | 只 attach |
| `completed` | generate suggestion | 读取 TAPD 工作流并生成回写建议 |
| 回写建议待确认 | confirm | 调用 TAPD API 并记录结果 |
| 任意非终态 | refresh context | 单 Story 扫描，不启动 AI |

### TAPD 回写建议

本地 Story 完成时：

1. Resolver 获取 TAPD 当前状态。
2. Resolver 获取状态映射和合法流转。
3. Decider 根据本地证据生成建议目标状态。
4. 保存 `pending` 建议并展示给用户。
5. 用户确认后 Handler 调用 TAPD 更新。

禁止硬编码：

```text
local completed -> tapd resolved
```

并行工作流或需要附加字段时，建议必须展示缺失字段，不能执行不完整回写。

### 状态漂移

示例：

```text
TAPD 已关闭，但本地 Story 仍 active。
```

系统行为：

- 显示 drift 警告。
- 记录诊断事件。
- 提供“继续、终止、标记完成”选项。
- 不自动杀死 Session。
- 不自动覆盖本地状态。

P1 可以通过 TAPD Webhook 增量更新外部事实，但 Webhook 事件仍不得自动启动本地 Story。

## API 设计

### 资料

```text
GET    /api/story/{key}/context
PUT    /api/story/{key}/context
POST   /api/story/{key}/context/refresh
GET    /api/story/{key}/context/snapshot
```

`PUT` 请求包含：

```json
{
  "revision": 7,
  "projects": [],
  "documents": [],
  "changeItems": []
}
```

### 项目注册表

```text
GET    /api/projects
POST   /api/projects
PUT    /api/projects/{id}
```

### Worktree

```text
POST   /api/story/{key}/worktrees/prepare
GET    /api/story/{key}/worktrees/cleanup-preview
POST   /api/story/{key}/worktrees/cleanup
```

### 生命周期

```text
POST   /api/story/{key}/start
GET    /api/story/{key}/tapd-writeback-suggestion
POST   /api/story/{key}/tapd-writeback-confirm
```

所有失败响应包含：

```json
{
  "ok": false,
  "reasonCode": "branch_checked_out_elsewhere",
  "message": "分支已被另一个 Worktree 使用",
  "details": {}
}
```

## Web Story 详情页

新增“影响与发布资料”区域，按项目分组展示：

- 项目名。
- 主仓库路径。
- 实际 Worktree。
- 分支和基线。
- Worktree 状态。
- 项目影响摘要。
- PRD 和设计文档。
- DDL 变更。
- Nacos 变更。
- 来源、证据和可信状态。
- AI 自动发现的待确认项。

页面操作：

- 编辑并保存资料。
- 添加共享项目或新项目。
- 刷新 AI 发现。
- 准备 Worktree。
- 查看清理预览并确认清理。
- 查看 TAPD 回写建议并确认。

版本冲突时页面不得覆盖数据，应展示最新 revision 和冲突项。

## 错误处理

| 失败 | 行为 | Reason Code |
|---|---|---|
| 项目路径不存在 | 不创建 Worktree，不启动 Session | `project_path_missing` |
| 路径不是 Git 仓库 | 拒绝启动 | `project_not_git` |
| Git 命令超时 | 保留原资料 | `git_command_timeout` |
| Git 命令失败 | 保留原资料 | `git_command_failed` |
| Worktree 路径冲突 | 拒绝启动 | `worktree_path_conflict` |
| 分支被其他 Worktree 使用 | 拒绝启动 | `branch_checked_out_elsewhere` |
| Worktree 分支不匹配 | 标记 stale，拒绝启动 | `worktree_branch_mismatch` |
| 多项目部分准备失败 | 补偿本次 clean 新建项，拒绝启动 | `worktree_prepare_partial` |
| Revision 冲突 | HTTP 409，返回最新资料 | `context_revision_conflict` |
| TAPD 拉取失败 | 使用 stale 缓存，不改本地状态 | `tapd_fetch_failed` |
| TAPD 回写失败 | 建议保持 pending | `tapd_writeback_failed` |
| Session 启动失败 | 保留 Worktree，Story 不进入 active | `session_start_failed` |

## 测试策略

### 单元测试

- `StoryContextResolver` 的排序、校验和快照渲染。
- 资料 mutation 合并和冲突判断。
- Worktree 状态解析。
- Worktree 启动决策表。
- Cleanup 决策表。
- TAPD 状态映射和回写建议。
- DDL/Nacos 进度与可信状态不可混淆。

### 数据库测试

- 新表和迁移可重复执行。
- 外键、唯一约束和索引。
- 删除 Story 时资料级联清理。
- 批量更新失败时事务整体回滚。
- `context_revision` 冲突不产生部分写入。
- 事件日志与 revision 同事务提交。

SQLite 每个连接必须显式启用：

```text
PRAGMA foreign_keys=ON
```

### Git 集成测试

使用临时仓库验证：

- 两个 Story 为同一项目创建独立 Worktree。
- 不同 Worktree 使用不同分支。
- 分支已被 checkout 时拒绝。
- 路径冲突时拒绝。
- Worktree 分支不匹配时标记 stale。
- dirty Worktree 不允许清理。
- 多项目准备失败时只补偿本次新建的 clean Worktree。

### API 测试

- Story 资料 CRUD。
- Revision 冲突返回 409。
- Refresh 不启动 AI。
- Candidate Start 的项目验证。
- Worktree prepare 和 cleanup preview/confirm。
- TAPD 建议生成和确认回写。

### Prompt 回归测试

- 新建、恢复和重试 Session 都注入最新 revision。
- 健康 Session attach 不重复注入。
- Prompt 包含所有项目目录和资料摘要。
- Prompt 不自动展开 PRD、设计、DDL 或 Nacos 全文。
- 快照路径和 revision 被记录到事件日志。

### 生命周期回归测试

- TAPD sync 创建 `candidate + idle`。
- TAPD sync 不调用 `start_story_async`。
- 本地完成不直接调用 TAPD 更新。
- TAPD 已关闭不自动停止本地 Story。
- 非执行分支都有 reason code 和诊断事件。

## P0 交付范围

P0 包含：

- 结构化数据模型与迁移。
- 共享项目注册表。
- Story 资料 API 和 Web 编辑。
- Worktree Resolver、Decider 和自动创建。
- 人工确认清理。
- Session Context Snapshot 和 Prompt 注入。
- 单 Story 自动发现。
- Candidate/Ready 接纳状态。
- TAPD 回写建议与确认。
- 回归测试。

P1 候选：

- TAPD Webhook 增量同步。
- CI/CD 和部署平台证据接入。
- Nacos 平台只读验证。
- 数据库变更平台只读验证。
- Worktree 合并状态自动识别与批量清理辅助。

## 验收标准

1. TAPD 同步新项后，页面能看到候选 Story，但不会创建线程、Session 或 Worktree。
2. 用户可为 Story 添加多个影响项目，每个项目保存主仓库、分支和独立 Worktree。
3. 两个活动 Story 可以安全编辑同一项目，且不会共享 Worktree 或分支。
4. AI 能根据 Git 证据更新分支、文档、DDL 和 Nacos 资料。
5. AI 无外部证据时不能把 DDL、Nacos 或部署状态标记为已发布。
6. 重新开启 Session 时，AI 收到最新资料目录和 revision，并能按需读取原文。
7. Worktree 或分支冲突时，系统拒绝启动并显示明确原因。
8. 本地完成后只生成 TAPD 回写建议；用户确认后才执行。
9. 并发编辑不会静默覆盖资料。
10. 所有新增历史风险都有自动化回归测试。

## 参考资料

- [TAPD API 接口文档](https://open.tapd.cn/document/api-doc/API%E6%96%87%E6%A1%A3/api_reference/)
- [TAPD 工作流状态映射](https://open.tapd.cn/document/api-doc/API%E6%96%87%E6%A1%A3/api_reference/workflow/get_workflow_status_map.html)
- [TAPD 工作流流转细则](https://open.tapd.cn/document/api-doc/API%E6%96%87%E6%A1%A3/api_reference/workflow/get_workflow_all_transitions.html)
- [TAPD 更新需求](https://open.tapd.cn/document/api-doc/API%E6%96%87%E6%A1%A3/api_reference/story/update_story.html)
- [TAPD Webhook](https://open.tapd.cn/document/api-doc/%E5%BF%AB%E9%80%9F%E5%85%A5%E9%97%A8/%E5%BC%80%E5%8F%91%E5%BA%94%E7%94%A8/%E4%BD%BF%E7%94%A8Webhook-%E4%BA%91%E7%AB%AF.html)
- [Git Worktree](https://git-scm.com/docs/git-worktree)
- [Git Branch Name Validation](https://git-scm.com/docs/git-check-ref-format)
- [SQLite Foreign Keys](https://sqlite.org/foreignkeys.html)
- [SQLite Transactions](https://sqlite.org/lang_transaction.html)
- [SQLite Isolation](https://sqlite.org/isolation.html)
- [Python pathlib](https://docs.python.org/3/library/pathlib.html)
- [Python subprocess](https://docs.python.org/3/library/subprocess.html)
