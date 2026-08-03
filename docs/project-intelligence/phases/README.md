# 三期实施交接（并行窗口执行）

> 三期在**三个并行窗口**同时执行，最后开汇总窗口 merge + 收口。
> 每个窗口零上下文启动，靠本文件 + 设计文档自包含交接。
> 设计文档：`10-test-framework-integration-design.md`（Phase 1）、`11-workspace-entity-design.md`（Phase 2/3）。

## 并行隔离机制

三个窗口共享同一工作目录会互相覆盖文件，因此**每个窗口在独立 git worktree + 分支上工作**：

| 窗口 | worktree 路径 | 分支 |
|---|---|---|
| 1 | `D:/worktrees/sl-phase1` | `feature/ice/phase1-verify-provider` |
| 2 | `D:/worktrees/sl-phase2` | `feature/ice/phase2-workspace-entity` |
| 3 | `D:/worktrees/sl-phase3` | `feature/ice/phase3-wiki-knowledge` |

（worktree 由主仓提前建好；各窗口在对应路径下打开，正常开发、commit 到各自分支，**不 merge、不 push**。）

## 边界重切（为并行消除冲突）

与文档 10/11 的原始分期相比，三处调整，**这两处重叠从各期中摘出、统一交给汇总窗口**：

- ~~Sandbox 术语切换（planner.py prompt 措辞）~~ → 汇总窗口（Phase 1 也改 planner.py，并行必撞）
- ~~Wiki tab 挂载进 WorkspacePage~~ → 汇总窗口（Phase 3 只做独立的 `WikiTab` 组件新文件，挂载一行由汇总做）

剩余可自动 merge 的重叠：`packages/knowledge` 的 `models.py`/`parser.py`（Phase 1 改 ScenarioEntry 既有类、Phase 3 追加 WikiEntry 新类/新函数，不同区域）。

## 窗口 1 启动 prompt（Phase 1：测试框架接入）

```text
你在 D:/worktrees/sl-phase1（story-lifecycle 仓库的 git worktree，分支
feature/ice/phase1-verify-provider）。所有改动 commit 到此分支，不 merge 不 push。

读 docs/project-intelligence/10-test-framework-integration-design.md，按它实施
Phase 1：测试框架接入。文档自包含，严格按「改动总览」的 1-4 执行（改动 5 在
hc-pytest 仓库，不在本仓库，只做契约对齐不写 hc 侧代码）。

边界（并行窗口，另外两个窗口在做 Phase 2/3，勿越界）：
- 只做文档 10 的范围，不做 Workspace 实体化 / wiki
- 遵守文档末尾 7 条不变量（异步产物模式为主、外部 FAIL 计 reject budget、
  PASS 不跳 confirm-gate、不在 poll loop 同步跑长测试、扫描器不进 miner）
- planner.py 只改 StagePlan(:322) 和 scenario_catalog 注入(:188 附近)，
  不碰 infra/schemas.py:20 的废弃 PlanResult
- 前端 tab 图标用 SVG 组件，遵守 frontend/AGENTS.md
- packages/knowledge 里只改 ScenarioEntry 既有类和 parse_scenario 既有函数
  （Phase 3 窗口在同文件追加新类，保持区域分离才能自动 merge）

完成后：
- 逐项跑文档「验收标准」checklist（hc 侧依赖的项标注"需 hc 侧联调"）
- 写 docs/project-intelligence/phases/PHASE-1-RESULT.md（模板见该目录 README）
- git commit 到当前分支，只暂存本期相关文件
```

## 窗口 2 启动 prompt（Phase 2：Workspace 实体化）

```text
你在 D:/worktrees/sl-phase2（story-lifecycle 仓库的 git worktree，分支
feature/ice/phase2-workspace-entity）。所有改动 commit 到此分支，不 merge 不 push。

读 docs/project-intelligence/11-workspace-entity-design.md，按它实施 Phase 2：
Workspace 实体化（§1 实体模型、§2 WorkspacePage、§3 初始化管线、§8 Phase 2 行）。

边界（并行窗口，另两个窗口在做 Phase 1/3，勿越界）：
- **不做 Sandbox 术语切换的代码改动**（planner.py/prompt_sections.py 由 Phase 1
  窗口占用，术语统一由汇总窗口做）；本期只在新代码里用 Sandbox 措辞
- **不改 planner.py / prompt_sections.py / unified_gate.py**
- **不做 Wiki tab**（Phase 3 做 WikiTab 组件，挂载由汇总窗口做）
- workspace 表 + project.workspace_id（infra/db/models.py，raw SQL 无 ORM）
- 初始化管线 5 步（独立 pipeline，不复用 story 引擎；幂等、单步可重跑）
- WorkspacePage 只读版：旅程 / Stories / 概览三个 tab + 路由注册
  （路由文件改动控制在追加式，不改既有 StoryDetailPage 相关行）
- 开源零配置路径：不建 Workspace 时行为与今天完全一致
- verify_provider 的集成登记只往 workspace.integrations_json 存 config key，
  不动 verify_providers 机制本身（Phase 1 的地盘）

完成后：
- 逐项跑文档 §9 Phase 2 验收 checklist
- 写 docs/project-intelligence/phases/PHASE-2-RESULT.md
- git commit 到当前分支，只暂存本期相关文件
```

## 窗口 3 启动 prompt（Phase 3：Wiki 知识层 + draft 管线）

```text
你在 D:/worktrees/sl-phase3（story-lifecycle 仓库的 git worktree，分支
feature/ice/phase3-wiki-knowledge）。所有改动 commit 到此分支，不 merge 不 push。

读 docs/project-intelligence/11-workspace-entity-design.md，按它实施 Phase 3：
wiki 条目 + draft 管线（§4 wiki 条目设计、§5 多源探测模型的核心侧、§8 Phase 3 行）。

边界（并行窗口，另两个窗口在做 Phase 1/2，勿越界）：
- packages/knowledge 加 WikiEntry（type: wiki）+ parser + INDEX 索引：
  **只追加新类/新函数**（WikiEntry、parse_wiki），不改 ScenarioEntry 等既有
  类和函数（Phase 1 窗口在改那些，区域分离才能自动 merge）
- 核心侧 BaseWikiProbe 缝 + CodeScanProbe（L1）；hc 侧 probe 不在本仓库实现
- agent 注入只取 summary + related，检索降权，stale 标注
- draft → review → merge 后端管线 + API（人写直接生效；AI/probe 产出 draft；
  journey last_status 回写是事实字段，自动）
- 前端只做独立的 WikiTab 组件新文件（frontend/src/components/WikiTab.tsx），
  **不挂载**、不改 WorkspacePage/路由（Phase 2 在建页面，挂载由汇总窗口做）
- 不依赖 workspace 表是否存在：wiki 条目走 knowledge_root 文件层，
  review 状态存独立新表 wiki_draft（不碰 workspace/project 表）
- PII 红线：probe 只产聚合统计

完成后：
- 逐项跑文档 §9 Phase 3 验收 checklist（依赖 WorkspacePage 挂载的项标注
  "待汇总窗口挂载后验证"）
- 写 docs/project-intelligence/phases/PHASE-3-RESULT.md
- git commit 到当前分支，只暂存本期相关文件
```

## 汇总窗口 启动 prompt（三期分支都完成后）

```text
三个并行窗口已完成三期开发，分支：feature/ice/phase1-verify-provider、
feature/ice/phase2-workspace-entity、feature/ice/phase3-wiki-knowledge。

1. 读 docs/project-intelligence/phases/ 下三份 PHASE-N-RESULT.md，了解各期
   交付、验收结果、遗留问题
2. 按 1 → 2 → 3 顺序 merge 三个分支回主分支，解决冲突（预期只有
   packages/knowledge 的 models.py/parser.py 可能有冲突，区域分离应可自动合）
3. 做两件被摘出来的交界工作：
   a. Sandbox 术语切换：planner.py / prompt_sections.py 及文档中 per-story
      目录措辞统一为 Sandbox（workspace_path 字符串语义不动）
   b. 把 Phase 3 的 WikiTab 组件挂载进 Phase 2 的 WorkspacePage（一个 tab 一行）
4. 交界一致性核对：verify_provider 与 workspace.integrations_json 的 config
   同源；wiki 注入与 scenario_catalog 注入的降权/限量；review API 与前端对齐
5. 全量 pytest（仓库根）+ 前端 build，确认无回归
6. 对照文档 10/11 验收标准做三期汇总：未过项、遗留问题排优先级、
   文档勘误回写（实现与设计有出入处直接修订文档）
7. git commit（merge + 交界工作）；push 需用户明确要求
```

## PHASE-N-RESULT.md 模板

```markdown
# Phase N 实施结果（<日期>）

## 交付清单
- <文件/功能，一行一条，带路径>

## 验收 checklist 结果
- [x] <过项>
- [ ] <未过项——原因（hc 联调依赖/待挂载等）>

## 与设计文档的偏差
- <实现时不得不偏离设计处，及理由；没有就写"无">

## 遗留问题（给汇总窗口）
- <已知未解/风险/merge 时注意点，一行一条>

## 关键 commit
- <hash 一行一个>
```
