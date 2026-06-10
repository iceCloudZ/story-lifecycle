# Story 基础管理 — TAPD 可见性优先

**日期**: 2026-06-10
**状态**: 设计已确认

## 背景

用户痛点：
1. TAPD 需求多，进度更新和工时计算是日常负担
2. AI 写了代码和分支，但代码管理散乱，没跟需求关联
3. TAPD 快到期了才发现（截止日期盲区）
4. 测试要东西时发现没测过（质量不可控）

目标：先做**可见性**——把 TAPD 需求状态和本地 AI 开发进度统一到一个 Web 面板，一目了然。

## 架构决策

**方案：Story 为中心，TAPD 作为数据源**

- 每个 TAPD 需求/缺陷 = 一个本地 story
- TAPD 元数据（截止日期、优先级、状态）enrichment 进 story 表
- Web Dashboard 按 TAPD 需求组织视图
- 后续再补操作能力（双向同步、工时、自动检查）

选择理由：story 表已有 `source_type`/`source_id` 字段，TAPD 数据自然映射为 story，改动最小。

## 数据模型

### story 表新增字段

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `deadline` | TEXT | NULL | TAPD 需求截止日期（ISO 格式） |
| `priority` | TEXT | NULL | TAPD 优先级 |
| `owner` | TEXT | NULL | TAPD 当前处理人 |
| `branches_json` | TEXT | `'[]'` | 关联的 git 分支列表（JSON 数组） |
| `tapd_status` | TEXT | NULL | TAPD 侧最新状态 |
| `tapd_url` | TEXT | NULL | TAPD 需求链接 |

`branches_json` 结构：
```json
[
  {"repo": "project-a", "branch": "feat/FEAT-001-login", "status": "active"},
  {"repo": "project-b", "branch": "feat/FEAT-001-api", "status": "merged"}
]
```

### VALID_COLUMNS 更新

`db/models.py` 中 `VALID_COLUMNS` 需要添加新字段名。

### 迁移

在 `init_db()` 中用 `ALTER TABLE ... ADD COLUMN` 做幂等迁移（已有模式）。

## TAPD Sync 命令

### CLI

```bash
story sync                    # 拉取 TAPD 待处理需求/缺陷，创建或更新本地 story
story sync --dry-run          # 只显示会创建/更新哪些，不实际执行
story sync --status-only      # 只更新现有 story 的 TAPD 状态，不创建新的
```

### Sync 逻辑

1. 读取 TAPD 配置（`~/.story-lifecycle/config.yaml` 中的 tapd 段）
2. 调用 `TapdSource.fetch_pending()` 拉取需求 + 缺陷
3. 对每个 `SourceItem`：
   - 通过 `db.find_by_source_id("tapd", item.id)` 查找本地 story
   - 存在 → 更新 `tapd_status`、`deadline`、`priority`、`owner`、`title`
   - 不存在 → 自动创建 story（key 用 TAPD short_id 或 `tapd-{id}`，title 用 TAPD 标题，设置 `source_type="tapd"`、`source_id=item.id`）
4. 输出汇总：新建 N 个、更新 M 个、跳过 K 个

### 配置

TAPD 配置在 `~/.story-lifecycle/config.yaml` 中：
```yaml
tapd:
  workspace_id: "12345"
  owner: "zhangsan"
  story_status: "open,progressing,reopened"
  bug_status: "new,reopened,assigned,resolving"
```

## CLI 补充命令

```bash
story list                    # 列出所有 story
story list --status active    # 按状态筛选
story list --overdue          # 只显示已逾期的
story show <key>              # 查看详情（TAPD 状态 + 本地阶段 + 分支）
story advance <key>           # 手动推进一个阶段（design→implement→test→done）
story done <key>              # 标记完成（可选：同步回 TAPD）
```

`story list` 输出格式（Rich 表格）：
```
KEY          TITLE              PRIORITY   DEADLINE    STAGE       STATUS     BRANCH
FEAT-001     用户登录           高         06-15       implement   开发中     feat/FEAT-001
BUG-042      白屏问题           紧急       06-11       test        待测       fix/BUG-042
```

逾期项用红色标记，即将到期（3天内）用黄色标记。

## Web Dashboard 重新设计

### 主面板 — 需求总览表格

列：
- TAPD 需求（标题 + 链接到 TAPD）
- 优先级（高/中/低，颜色标记）
- 截止日期（逾期红色，3天内黄色）
- 本地阶段（进度条：design → implement → test → done）
- 开发状态（active/paused/completed/failed）
- 关联分支
- 操作（查看详情）

### 顶部统计卡片

- 总需求数 / 开发中 / 待测试 / 已完成
- 即将到期（3天内）/ 已逾期
- 上次同步时间

### 排序和筛选

- 默认按截止日期排序（最近的在前）
- 筛选：全部 / 开发中 / 待测试 / 已完成 / 已逾期

### API 端点

```
GET /api/sync/tapd           # 执行 TAPD 同步（对应 story sync）
GET /api/sync/tapd/status    # 获取上次同步状态
GET /api/stories?overdue=true # 支持逾期筛选
```

Story 详情 API 扩展（`GET /api/story/{key}`）增加返回 `deadline`、`priority`、`owner`、`branches_json`、`tapd_status`、`tapd_url`。

## 不做的事（本次范围外）

- 工时记录
- 自动检查（分支是否创建、测试是否通过）
- 双向 TAPD 状态回写（`story done` 可选触发）
- AI 开发自动推进阶段
- 多项目 workspace 管理
