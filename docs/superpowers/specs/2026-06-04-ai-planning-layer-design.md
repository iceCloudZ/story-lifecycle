# Phase 2: AI 规划层 — 自适应项目启动

## 概述

将 AI 介入点前移到项目规划阶段。根据项目当前状态自适应：可能只是一个 idea，可能是已有仓库但没有路线图，也可能是做了一半需要重新规划。AI 在每个节点先提案，人做判断题。

## 完整生命周期视图

```
  ┌─────────────────────────────────────────────────────────────┐
  │                  Phase 2: AI 规划层（自适应起点）             │
  │                                                             │
  │  ┌─ 起点探测 ─┐                                             │
  │  │ 当前状态？  │                                             │
  │  └──┬───┬───┬─┘                                             │
  │     │   │   │                                               │
  │  只有 idea  有仓库+需求    做了一部分                         │
  │     │   没路线图          没路线图                            │
  │     ↓   ↓               ↓                                   │
  │  Step 0a  Step 1       Step 1                               │
  │  idea →   roadmap生成   分析现状                             │
  │  requirements             + roadmap生成                      │
  │     ↓   ↓               ↓                                   │
  │     └───┴───────────────┘                                   │
  │              ↓                                               │
  │     .story/planning/roadmap.md                               │
  │              ↓ AI 拆解，人确认                                │
  │     .story/planning/issues.json                              │
  │              ↓ 批量创建                                       │
  │         GitHub Issues                                        │
  │                                                             │
  ├─────────────────────────────────────────────────────────────┤
  │                  Phase 1: 执行层                             │
  │                                                             │
  │  lifecycle:accepted → design → implement → test → done      │
  └─────────────────────────────────────────────────────────────┘
```

## 起点：项目状态探测

`story plan` 命令首先探测项目状态：

```bash
story plan init
```

AI 检查当前目录，判断项目处于哪个阶段：

| 状态 | 探测信号 | AI 建议的下一步 |
|------|---------|---------------|
| **空项目（只有 idea）** | 无 `.story/` 目录、无 Git 仓库、无代码文件 | 从 Step 0a 开始：idea → requirements |
| **有仓库，无规划** | 有 `.story/` 或 Git 仓库，有代码文件（`**/*.{py,ts,java,go}`），但无 `.story/planning/roadmap.md` | 从 Step 1 开始：生成 requirements + roadmap |
| **有需求，无路线图** | 有 `.story/planning/requirements.md`，但无 roadmap.md | 从 Step 1 开始：requirements → roadmap |
| **有路线图，未拆解** | 有 roadmap.md，但没有对应 Issues | 从 Step 2 开始：拆解 phase → issues |
| **已有 Issues** | roadmap + issues 都有 | 直接进 Phase 1 执行，或补充缺失的 issues |

探测信号优先级：`.story/` 存在 → Git 仓库存在 → 代码文件存在 → 文档文件存在。以 `.story/` 和 Git 状态为主信号，文档目录为辅助信号。

AI 向用户确认判断是否正确，然后进入对应步骤。

## 断点续传

规划流程可能被中断（LLM 报错、用户 Ctrl+C）。每完成一个步骤，往 `.story/planning/state.json` 写入进度：

```json
{
  "current_step": "step_2",
  "completed_steps": ["step_0a", "step_1"],
  "last_updated": "2026-06-05T10:30:00",
  "context": {
    "requirements_file": ".story/planning/requirements.md",
    "roadmap_file": ".story/planning/roadmap.md",
    "selected_phase": 1
  }
}
```

下次运行 `story plan init` 时：
1. 读取 `state.json`，发现未完成的规划流程
2. 提示用户："上次你完成了 Step 1（roadmap 生成），要继续 Step 2（里程碑拆解）吗？"
3. 用户确认后跳到对应步骤，已完成的文件直接复用

`story plan` 各子命令也检查 `state.json`，防止跳步执行。

## 流程步骤

### Step 0a：idea → requirements（空项目专属）

**触发条件**：无 Git 仓库、无任何文档，用户只有一个 idea

**AI 做什么**：
1. 与用户对话式澄清：目标用户、核心功能、技术偏好、约束条件
2. 生成 `.story/planning/requirements.md`

**人的角色**：回答 AI 提问，审查生成的需求文档

```bash
story plan init
# AI: "检测到这是一个空项目。请描述你的 idea："
# 用户: "我想做一个股票研究平台，帮助个人投资者分析财务数据"
# AI: 追问澄清问题...
# AI: 生成 .story/planning/requirements.md 草稿 → 人确认
```

### Step 0b：项目初始化（可选，Step 0a 之后）

**触发条件**：Step 0a 完成，用户选择让 AI 初始化项目

**AI 做什么**：
1. 根据技术方案生成目录结构
2. 初始化 Git 仓库
3. 生成基础配置（.gitignore、CI、Issue 模板等）
4. 提交初始结构

**人的角色**：确认技术选型，审查生成的结构

**注**：这一步可能直接调用 AI CLI（Claude Code）执行，相当于 Phase 1 的一个特殊 Story。

### Step 1：roadmap 生成

**触发条件**：有 requirements（Step 0a 生成或已存在），需要路线图

**输入**：`.story/planning/requirements.md` + 现有代码结构（如果有的话）
**输出**：`.story/planning/roadmap.md`（草稿）

```bash
story plan roadmap                    # 自动找 .story/planning/requirements.md
story plan roadmap --from <file>      # 指定输入文件
```

**AI 做什么**：
1. 读取需求文档
2. 扫描现有代码结构（如果有）
3. 生成 phased roadmap（写入 `.story/planning/roadmap.md`）
4. 每个 phase：名称、目标、功能列表、依赖、验证标准

**人的角色**：审查每个 phase，可修改、删除、重排

### Step 2：里程碑拆解

**触发条件**：有 `.story/planning/roadmap.md`，需要拆成 Issue

**输入**：`.story/planning/roadmap.md` + 选中某个 phase
**输出**：`.story/planning/issues.json`（Issue 草稿列表）

```bash
story plan decompose                  # 拆解当前 phase
story plan decompose --phase 3        # 拆解指定 phase
```

**AI 做什么**：
1. 读取 phase 内容
2. 检查项目中已有的 Issue 模板（`.github/ISSUE_TEMPLATE/`）
3. 拆解成 Issue 列表，每个包含：title、body、labels、template_type、dependencies
4. 复杂功能 → design Issue，明确任务 → implementation Issue

**人的角色**：审查每个 Issue 草稿，调整粒度，标记哪些直接 `lifecycle:accepted`

### Step 3：Issue 批量创建

**触发条件**：Issue 草稿确认完毕

```bash
story plan publish                    # 创建 Issues
story plan publish --dry-run          # 预览不创建
```

**做什么**：
1. 逐个调用 `gh issue create`
2. 输出 Issue number 列表
3. 更新 `.story/planning/roadmap.md` 关联 Issue number

**人的角色**：确认创建结果

## 决策记录

| 决策 | 选择 | 理由 |
|------|------|------|
| 起点假设 | 不假设，自适应探测 | 项目状态千差万别，不能固定起点 |
| AI 介入方式 | 每步 AI 提案 + 人确认 | 人做判断题，不做问答题 |
| 交互方式 | CLI 对话式 | 先做最简方案，TUI 集成后续考虑 |
| LLM 调用 | 复用 story-lifecycle 现有配置 | STORY_LLM_API_KEY / STORY_LLM_MODEL |
| 项目初始化 | 可选，Step 0a 之后由用户决定 | 不是所有项目都需要 AI 初始化 |
| 规划文件路径 | 统一收敛到 `.story/planning/` | 避免污染业务仓库；`.story/` 已在 .gitignore 中或用户可控；与 Phase 1 的 `.story/` 约定一致 |
| CLI 职能边界 | `story setup` = 运行时配置（LLM key、数据源），`story plan` = 项目级规划 | 两者目标用户和生命周期不同：setup 全局配置一次，plan 按项目/阶段反复使用 |

## 文件结构

```
src/story_lifecycle/
├── planner/                  # 新增模块
│   ├── __init__.py
│   ├── probe.py              # 项目状态探测
│   ├── idea_expander.py      # idea → requirements（LLM 对话）
│   ├── roadmap.py            # 路线图生成（LLM）
│   ├── decomposer.py         # 里程碑拆解（LLM）
│   ├── publisher.py          # Issue 批量创建（gh CLI）
│   └── state.py              # 断点续传状态管理
├── cli/
│   └── plan_cmd.py           # 新增 `story plan` 命令组
└── sources/
    └── github_cli.py         # Phase 1 已有，create_issue 方法
```

项目运行时生成的文件：

```
.story/planning/
├── state.json              # 断点续传进度
├── requirements.md         # Step 0a 生成
├── roadmap.md              # Step 1 生成
└── issues.json             # Step 2 生成，Step 3 消费
```

## CLI 命令

```bash
story plan init                        # 探测项目状态，引导进入对应步骤（含断点续传检测）
story plan roadmap [--from <file>]     # 生成路线图
story plan decompose [--phase <n>]     # 拆解 phase → Issue 草稿
story plan publish [--dry-run]         # 批量创建 Issue
```

`story plan init` 是入口命令，它会：
1. 检查 `.story/planning/state.json` 是否有未完成的规划流程
2. 如有，提示用户从断点续传
3. 如无，探测项目状态，告诉用户"你现在到哪了"
4. 建议下一步应该跑哪个子命令
5. 如果是空项目，直接进入 idea → requirements 对话

## 配置

`~/.story-lifecycle/config.yaml` 新增：

```yaml
story_source:
  github:
    repo: "owner/repo"
    # Phase 1 配置...

planning:
  issue_templates_dir: ".github/ISSUE_TEMPLATE"  # 可选
```

## 与 Phase 1 的关系

- Phase 2 创建的 Issue 进入 Phase 1 数据源
- Phase 2 使用 Phase 1 的 `GithubCli.create_issue()`
- `story plan` 独立于 `story serve`，不依赖服务器
- LLM 配置复用

## 不做什么

- 自动推进 Issue 生命周期（idea → accepted 由人决定）
- TUI 集成（Phase 2 先用 CLI 对话式，跑通后再考虑 TUI）
- 替代 GitHub Projects 看板
- 多项目路线图
- 将规划文件写到 `.story/` 之外的目录（用户可手动移动，工具只管 `.story/planning/`）

## 依赖

- Phase 1 的 `github_cli.py`（create_issue）
- 项目 Issue 模板（可选，没有则用默认模板）
- LLM API 配置（复用现有）
