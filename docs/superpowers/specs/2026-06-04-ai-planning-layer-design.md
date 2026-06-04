# Phase 2: AI 规划层 — 路线图生成 + Issue 自动创建

## 概述

在 Phase 1（数据源 + 双写）基础上，将 AI 介入点从"执行阶段"前移到"规划阶段"：
1. **路线图生成**：AI 读取项目需求，生成 phased roadmap，人确认
2. **里程碑拆解**：AI 将 roadmap item 拆解成具体 GitHub Issue，人确认
3. **Issue 批量创建**：用 `gh issue create` 自动创建，人确认后提交

使 story-lifecycle 成为真正的"全生命周期"管理工具——从项目出生到完成。

## 完整生命周期视图

```
  ┌─────────────────────────────────────────────────────────┐
  │                  Phase 2: AI 规划层                      │
  │                                                         │
  │  requirements.md                                        │
  │       ↓ AI 生成，人确认                                  │
  │  roadmap.md（phased）                                    │
  │       ↓ AI 拆解，人确认                                  │
  │  Issue 草稿列表                                          │
  │       ↓ 批量创建                                         │
  │  GitHub Issues (lifecycle:idea / lifecycle:accepted)     │
  │                                                         │
  ├─────────────────────────────────────────────────────────┤
  │                  Phase 1: 执行层                         │
  │                                                         │
  │  lifecycle:accepted Issue → Story                       │
  │       ↓ 自动执行                                        │
  │  design → implement → test                              │
  │       ↓ 双写同步                                        │
  │  Issue comment + labels 更新                            │
  │       ↓ 完成                                            │
  │  lifecycle:done                                          │
  └─────────────────────────────────────────────────────────┘
```

## 决策记录

| 决策 | 选择 | 理由 |
|------|------|------|
| AI 介入方式 | 每步 AI 提案 + 人确认 | 人做判断题，不做问答题 |
| 路线图输入 | requirements.md + 现有代码结构 | 需求文档是源头，代码结构提供约束 |
| Issue 模板 | 复用项目已有模板 | stock-research 已有 idea/design/implementation 模板 |
| 交互方式 | TUI 内嵌交互 | 复用现有 TUI，不新增 UI |
| LLM 调用 | 复用 story-lifecycle 现有 LLM 配置 | STORY_LLM_API_KEY / STORY_LLM_MODEL |

## 三步流程

### Step 1：路线图生成

**输入**：`docs/requirements.md`（或用户指定文件）+ 仓库代码结构
**输出**：`docs/roadmap.md`（草稿）

```bash
story plan roadmap --from docs/requirements.md
```

**AI 做什么**：
1. 读取 requirements.md
2. 扫描现有代码结构（已有什么模块、什么阶段）
3. 生成 phased roadmap（参考 stock-research 的 6-phase 结构）
4. 每个 phase 包含：名称、目标、功能列表、依赖关系、验证标准

**人的角色**：
- TUI 展示生成的 roadmap
- 人审查每个 phase，可修改、删除、重新排序
- 确认后写入 `docs/roadmap.md`

### Step 2：里程碑拆解

**输入**：`docs/roadmap.md` + 选中某个 phase
**输出**：Issue 草稿列表

```bash
story plan decompose --phase 3
```

**AI 做什么**：
1. 读取指定 phase 的功能列表
2. 根据项目 Issue 模板，拆解成具体 Issue：
   - 复杂功能 → design Issue（`type:design`, `lifecycle:draft`）
   - 明确任务 → implementation Issue（`type:implementation`, `lifecycle:implementing`）
   - 已有设计 → 直接 implementation Issue（`lifecycle:accepted`）
3. 每个 Issue 填写模板字段（Tasks、Verification 等）
4. 标注 Issue 间依赖关系

**人的角色**：
- TUI 展示 Issue 草稿列表
- 人审查每个 Issue：调整粒度、增删、修改字段
- 标记哪些 Issue 直接 `lifecycle:accepted`（可以立即开发）

### Step 3：Issue 批量创建

**输入**：确认后的 Issue 草稿列表
**输出**：GitHub Issues（已创建）

```bash
story plan publish
```

**做什么**：
1. 逐个调用 `gh issue create` 创建 Issue
2. 自动填写 title、body、labels
3. 创建完成后输出 Issue number 列表
4. 更新 roadmap.md 中对应 item 关联 Issue number

**人的角色**：
- 确认创建结果
- 后续可在 GitHub 上继续调整

## 文件结构

```
src/story_lifecycle/
├── planner/                  # 新增模块
│   ├── __init__.py
│   ├── roadmap.py            # 路线图生成（LLM 调用）
│   ├── decomposer.py         # 里程碑拆解（LLM 调用）
│   └── publisher.py          # Issue 批量创建（gh CLI）
├── cli/
│   └── plan_cmd.py           # 新增 `story plan` 命令
└── sources/
    └── github_cli.py         # Phase 1 已有，新增 create_issue 方法
```

## CLI 命令

```bash
story plan roadmap --from <file>          # 生成路线图草稿
story plan decompose --phase <n>          # 拆解指定 phase 为 Issue 列表
story plan publish                        # 批量创建 Issue
story plan publish --dry-run              # 预览不实际创建
```

## 配置

`~/.story-lifecycle/config.yaml` 新增：

```yaml
story_source:
  github:
    repo: "owner/repo"
    # Phase 1 配置...

planning:
  roadmap_template: "docs/templates/roadmap-template.md"   # 可选，路线图模板
  issue_templates_dir: ".github/ISSUE_TEMPLATE"            # 可选，读取项目 Issue 模板
  default_phase: "current"                                  # 可选，默认拆解哪个 phase
```

## AI Prompt 设计（概要）

### 路线图生成 Prompt

```
你是项目规划师。根据以下需求文档和现有代码结构，生成 phased roadmap。

要求：
1. 每个阶段有明确的目标和交付物
2. 阶段之间有合理的依赖关系
3. 先基础设施后业务功能
4. 每个 phase 列出具体功能点

输入：
- 需求文档：{requirements_content}
- 现有代码结构：{code_structure}

输出格式：
- phase 名称、目标、功能列表、依赖、验证标准
```

### 里程碑拆解 Prompt

```
你是项目拆解师。将以下 roadmap phase 拆解成 GitHub Issues。

要求：
1. 复杂功能先用 design Issue，再 implementation Issue
2. 每个 Issue 有明确的 Tasks 和 Verification
3. 标注 Issue 间依赖
4. 使用项目已有 Issue 模板

输入：
- Phase 内容：{phase_content}
- 项目 Issue 模板：{templates}
- 现有 docs/ 内容：{existing_docs}

输出格式：
- Issue 列表，每个包含：title, body, labels, template_type, dependencies
```

## 与 Phase 1 的关系

- Phase 2 创建的 Issue 最终进入 Phase 1 的数据源
- Phase 2 使用 Phase 1 的 `GithubCli.create_issue()` 方法
- Phase 2 的 `story plan` 命令独立于 Phase 1 的 `story serve`，不依赖服务器运行
- Phase 2 的 LLM 调用复用现有 `STORY_LLM_API_KEY` / `STORY_LLM_MODEL` 配置

## 不做什么

- 自动推进 Issue 生命周期（idea → accepted 仍由人手动改标签）
- 自动更新 docs/ 文件（人负责同步）
- 替代 GitHub Projects 看板（不涉及 project board 自动化）
- 多项目路线图（先支持单项目）

## 依赖

- Phase 1 的 `github_cli.py`（create_issue 方法）
- 项目已有 Issue 模板
- LLM API 配置（复用现有）
