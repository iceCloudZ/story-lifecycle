# Story 上下文关系：复制注入 + 回填 skill

- **日期**: 2026-06-15
- **状态**: 已批准，待写实施计划
- **作者**: zhaozihao + Claude

## 背景与动机

用户在 `D:\hc-all`（HappyCash 消费金融微服务聚合工作区）用多种 AI agent（Claude / Codex / Cursor）开发 TAPD 需求。痛点：

1. **AI 全自动开发"跑稳"周期长**，需求积压，想先用"半手动"方式（自己开 agent + 注入上下文）扛住这波。
2. 每个 story（需求）关联多种产物——代码分支、PRD、spec、plan、DDL、Nacos 变更、绑定项目——目前这些关系**没被有效收集和复用**。
3. 既存已开发的需求，这些关系基本是空的：`story_project` 表 11 条绑定，`worktree_state` 全 `unprepared`、`summary` / `evidence_ref` 全空。
4. hc-all 已有的 `.agents/skills/story-lifecycle` skill **过时**（指向旧命令 / PostgreSQL，与当前 SQLite 系统不符），会误导 agent。

## 目标

- **G1**：story 详情页提供「复制上下文资料包」按钮，一键复制一份**中性、混合浓度**的资料包——注入任何 agent 即可开干（开发 / 改 bug / 排查都适用）。
- **G2**：提供通用 agent skill（`.agents/skills/story-context`，不绑 claude），教任何 agent 在开发 / 改完 story 时把分支 / PRD / DDL / Nacos 关系**写回** story-lifecycle DB。
- **G3**：删除过时的 `.agents/skills/story-lifecycle`（及 `.claude` 版）+ 清理悬空引用。

## 非目标

- 不全面重写已删的 story-lifecycle skill（"后续稳定了再写"）。
- 不改 D:\story-lifecycle 的 stage 体系、TAPD sync 等已有功能。
- 不做全自动回填：回填靠 agent 智能 + skill 引导，不是规则扫描；现有 AutoDiscovery Scanner 保留为可选辅助，不在本次扩展。

## 现状（已有基础设施，本次复用）

**数据模型**（`db/models.py`，全部已建）：

| 表 | 关键字段 | 对应"关系" |
|---|---|---|
| `story` | `context_revision` | 乐观锁版本号 |
| `project` / `story_project` | `branch`, `worktree_path`, `base_branch`, `base_commit`, `summary` | 绑定项目 + 代码分支 |
| `story_document` | `kind`(prd/spec/plan), `ref`, `summary`, `evidence_ref` | PRD / spec / plan |
| `story_change_item` | `kind`(ddl/nacos), `ref`, `summary`, `evidence_ref`, `lifecycle_state`, `environment` | DDL / Nacos 变更 |
| `story_delivery_artifact` | `kind`, `url`, `target_branch` | PR / MR |

**模块**（`orchestrator/context/`，全部已实现）：
- `ContextResolver.resolve(key)` → `ContextBundle`（组装所有关系）
- `snapshot.py: generate_snapshot()` → 版本化 Markdown（**给人读**，本次不复用）
- `auto_discovery.Scanner / Decider / Handler` → 规则扫描回填（保留为可选辅助）

**API**（已实现）：
- `GET /api/story/{key}/context` → ContextBundle JSON
- `PUT /api/story/{key}/context` → **只 bump revision，不写数据**（gap，本次补）
- `POST /api/story/{key}/context/refresh` → 触发 AutoDiscovery
- `GET /api/story/{key}/context/snapshot` → snapshot Markdown

**db 层函数**：`get_story_documents` / `get_story_change_items` / `bump_context_revision` 已有；**`add_story_document` / `add_change_item` 缺失**（本次补）。

## 设计

### 整体闭环

```
hc-all 里任意 agent 开发 / 改 story
  → 读 hc-all/.agents/skills/story-context/SKILL.md
  → curl 写关系端点（分支 / PRD / DDL / Nacos）→ story-lifecycle DB
                                               ▲
story 详情页「Context」Tab → GET /context/pack ─┘
  → 复制混合浓度中性资料包
  → 手动开任意 agent 粘贴 → 开干
```

### Part A — 复制注入（读）

**后端**：新建 `orchestrator/context/pack.py`
- `generate_pack(story_key) -> dict`：复用 `ContextResolver().resolve()`，渲染**混合浓度中性** Markdown。
- 渲染规则：
  - 头部：`story_key`、`title`、TAPD url、`profile / stage`（中性陈述，**不含"请实现 / 请修复"指令**）
  - 项目绑定：每个 story_project 列 `branch`、`worktree_path`、`base_branch`
  - documents（PRD / spec / plan）：列 `kind`、`ref`（相对 worktree 路径）、`summary`；标注"worktree 内可读"
  - change_items：
    - **DDL**：列 `ref`（文件路径）、`summary`
    - **Nacos**：内联 `summary` + `evidence_ref` 正文（配置不在 worktree 本地文件，必须内联）
  - delivery_artifacts：PR / MR 的 `url`、`target_branch`
  - runtime_facts：列出
- **浓度原则**：本地文件（PRD / spec / plan / DDL）只给路径，agent 在 worktree 自己读；非本地（Nacos 正文、TAPD 摘要）内联。

**端点**：`GET /api/story/{key}/context/pack` → `{"content": <markdown>, "revision": N}`

**前端**：`StoryDetailPage` 新增 "Context" Tab（现详情页 6 个 Tab 未暴露 context）
- 调 `GET /context` 显示关系概览（分组：项目绑定 / 文档 / 变更项 / 交付）
- 「复制上下文资料包」按钮 → 调 `GET /context/pack` → `navigator.clipboard.writeText(content)`
- 复制后 toast 提示

### Part B — 回填 skill（写）

**后端**：补**写 context 关系**的 API 端点（现有 `PUT /context` 不写数据）
- `POST /api/story/{key}/context/documents` — body `{kind, ref, summary?}` → 加 `story_document`
- `POST /api/story/{key}/context/change-items` — body `{kind, ref, summary?, evidence_ref?, environment?}` → 加 `story_change_item`
- `PUT  /api/story/{key}/context/branch` — body `{project_id, branch, worktree_path?, base_branch?}` → upsert `story_project`
- 每次写入后 `bump_context_revision`
- db 层新增 `add_story_document` / `add_change_item`（`upsert_story_project` 复用现有或补）

**skill 文档**：新建 `D:\hc-all\.agents\skills\story-context\SKILL.md`
- frontmatter：`name: story-context`；`description` 含触发词（"维护 story 上下文"、"记录分支 / PRD / DDL / Nacos"、"回填 story 关系"…）
- 内容教任何 agent：
  - **何时做**：开始 / 改完一个 TAPD story 时
  - **怎么做**：curl 调上述写端点，把分支、PRD 路径、DDL、Nacos 配置写回
  - **curl 模板**（`API=http://127.0.0.1:8180/api/story`）
  - **约定**：DDL 给文件路径；Nacos 配置内容写进 `summary` + `evidence_ref`（pack 会内联）
  - **前置**：server 在 8180 跑（`story serve`）

### Part C — 清理（删旧 skill）

删除：
- `D:\hc-all\.agents\skills\story-lifecycle\`
- `D:\hc-all\.claude\skills\story-lifecycle\`

清理悬空引用：
- `.agents/skills/dev-workflow/SKILL.md:52` + `.claude/skills/dev-workflow/SKILL.md:52`：删"如果 story-lifecycle skill 可用…"那句
- `D:\hc-all\AGENTS.md` 第 57-64 行：删"Story Lifecycle 集成"整节（后续稳定重写时再加回）

## 关键决策记录

1. **renderer 新建（pack.py）而非复用 snapshot**：snapshot 是"给人读"的摘要；注入用要混合浓度 + 中性，语义不同，分开避免污染。
2. **写 DB 走 curl API，不新 CLI 命令**：现有 hc-all skill 全走 curl API（server 开发时跑着），跟随约定；CLI 命令多余。
3. **新建 story-context skill 而非扩展现有 story-lifecycle**：单一职责；旧 skill 过时已决定删除。
4. **删旧 story-lifecycle skill**：过时（旧命令 / PG），用户决定删，稳定后重写。
5. **Nacos 正文存 change_item.summary + evidence_ref**：两字段都已存在，无需 migration；够 pack 内联用。如后续需完整配置正文再单独存文件。
6. **资料包中性、无指令**：同一 story 复用于开发 / 改 bug / 排查多场景，不预设"请实现"。

## 数据流

```
[agent] --curl POST /context/documents-->  db.add_story_document  --> story_document
[agent] --curl POST /context/change-items--> db.add_change_item   --> story_change_item
[agent] --curl PUT  /context/branch-->      db.upsert_story_project--> story_project
                                                                      |
[前端] --GET /context/pack--> pack.generate_pack --> ContextResolver.resolve --> 读上述表 --> 渲染 markdown --> 复制
```

## 测试策略

- **后端**：
  - `pack.generate_pack` 单测：mock ContextBundle，断言渲染（本地文件给路径、Nacos 内联、中性无指令）
  - 写端点单测：POST document / change-item / branch → 查表确认写入 + revision bump
  - 复用 `isolated_story_home` fixture，并**修复为 autouse**——顺手解决"测试污染主库"根因（见附录）
- **前端**：Context Tab 渲染 + 复制按钮（mock fetch）
- **skill**：人工验证——在 hc-all 让 agent 跑一遍 story-context，确认关系写回 DB + 详情页 pack 复制可用

## 范围 / 实施顺序

一个 spec，建议按以下顺序实施（每步可独立验证）：

1. **Part A 后端**：`pack.py` + `GET /context/pack` 端点
2. **Part A 前端**：Context Tab + 复制按钮
3. **Part B 后端**：db 层 add 函数 + 写端点（documents / change-items / branch）
4. **Part B skill**：`hc-all/.agents/skills/story-context/SKILL.md`
5. **Part C 清理**：删旧 skill + 清 dev-workflow / AGENTS.md 引用

## 附录：附带修复（测试污染主库）

调查中发现的独立问题，顺带修复：

- **根因**：`tests/conftest.py` 的 `isolated_story_home` fixture 会把 DB 重定向到 tmp 目录，但它**不是 autouse**；漏用它的测试直接写主库 `~/.story-lifecycle/story.db`。
- **修复**：将 `isolated_story_home` 改为 autouse（或新增一个 autouse 的 DB 隔离 fixture 调用它），确保所有测试默认隔离。
- 验证：跑 `pytest` 后主库 `story` 表行数不变。
