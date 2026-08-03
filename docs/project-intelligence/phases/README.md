# 三期实施交接（1+2 并行 → 3 → 汇总）

> **执行节奏：窗口 1、2 并行同开（同一工作目录）→ 都完成后开窗口 3 → 最后汇总窗口收口。**
> 每个窗口零上下文启动，靠本文件 + 设计文档自包含交接。
> 设计文档：`10-test-framework-integration-design.md`（Phase 1）、`11-workspace-entity-design.md`（Phase 2/3）。

## 为什么 1、2 可以同目录并行，3 必须等

边界已按并行重切过，窗口 1、2 的改动面**无实质重叠**：

- 窗口 1：`orchestrator/verify_providers/`、`unified_gate.py`、`planner.py`（StagePlan + scenario_catalog）、`prompt_sections.py`、knowledge 的 ScenarioEntry、`stale.py`、StoryDetailPage tab
- 窗口 2：`infra/db/models.py`（workspace 表）、初始化管线（新模块）、WorkspacePage（新页面）——**明确不碰 planner.py / prompt_sections.py / unified_gate.py**

窗口 3 必须等 1、2 完成：它要在知识层追加 WikiEntry（等窗口 1 的 ScenarioEntry 改动落定避免同文件冲突），WikiTab 挂载需要窗口 2 的 WorkspacePage 已存在。

**同目录并行的纪律（窗口 1、2 都要遵守）**：

- 勤 commit，每次只 `git add` 自己任务的文件（本仓库常有并行会话，未提交改动可能被冲掉）
- 前端路由/App 文件若两边都要碰：只追加、不改对方行；撞上时后到者 rebase
- `git push` 需用户明确要求

**两处交界工作统一留给汇总窗口**（避免三向纠缠）：

- Sandbox 术语切换（`planner.py`/`prompt_sections.py` 中 per-story 目录措辞统一为 Sandbox；`workspace_path` 字符串语义不动）
- WikiTab 组件挂载进 WorkspacePage（一个 tab 一行）

## 窗口 1 启动 prompt（Phase 1：测试框架接入）

```text
读 docs/project-intelligence/10-test-framework-integration-design.md，按它实施
Phase 1：测试框架接入。文档自包含，严格按「改动总览」的 1-4 执行（改动 5 在
hc-pytest 仓库，不在本仓库，只做契约对齐不写 hc 侧代码）。

边界（另一并行窗口在做 Phase 2，勿越界）：
- 只做文档 10 的范围，不做 Workspace 实体化 / wiki
- 遵守文档末尾 7 条不变量（异步产物模式为主、外部 FAIL 计 reject budget、
  PASS 不跳 confirm-gate、不在 poll loop 同步跑长测试、扫描器不进 miner）
- planner.py 只改 StagePlan(:322) 和 scenario_catalog 注入(:188 附近)，
  不碰 infra/schemas.py:20 的废弃 PlanResult
- 前端 tab 图标用 SVG 组件，遵守 frontend/AGENTS.md
- packages/knowledge 里只改 ScenarioEntry 既有类和 parse_scenario 既有函数
  （后续 Phase 3 会在同文件追加新类，保持区域分离）
- 同目录有并行会话：勤 commit，每次只暂存自己任务的文件

完成后：
- 逐项跑文档「验收标准」checklist（hc 侧依赖的项标注"需 hc 侧联调"）
- 写 docs/project-intelligence/phases/PHASE-1-RESULT.md（模板见本目录 README）
- git commit，只暂存本期相关文件
```

## 窗口 2 启动 prompt（Phase 2：Workspace 实体化）

```text
读 docs/project-intelligence/11-workspace-entity-design.md，按它实施 Phase 2：
Workspace 实体化（§1 实体模型、§2 WorkspacePage、§3 初始化管线、§8 Phase 2 行）。

边界（另一并行窗口在做 Phase 1，勿越界）：
- **不改 planner.py / prompt_sections.py / unified_gate.py**（Phase 1 的地盘；
  Sandbox 术语切换由汇总窗口统一做，本期新代码里直接用 Sandbox 措辞即可）
- **不做 Wiki tab**（Phase 3 做组件，挂载由汇总窗口做）
- workspace 表 + project.workspace_id（infra/db/models.py，raw SQL 无 ORM）
- 初始化管线 5 步（独立 pipeline，不复用 story 引擎；幂等、单步可重跑）
- WorkspacePage 只读版：旅程 / Stories / 概览三个 tab + 路由注册
  （路由文件追加式改动，不改 Phase 1 碰的 StoryDetailPage 相关行）
- 开源零配置路径：不建 Workspace 时行为与今天完全一致
- verify_provider 的集成登记只往 workspace.integrations_json 存 config key，
  不动 verify_providers 机制本身
- 同目录有并行会话：勤 commit，每次只暂存自己任务的文件

完成后：
- 逐项跑文档 §9 Phase 2 验收 checklist
- 写 docs/project-intelligence/phases/PHASE-2-RESULT.md
- git commit，只暂存本期相关文件
```

## 窗口 3 启动 prompt（Phase 3：Wiki 知识层 + draft 管线）

> 启动前提：窗口 1、2 均已完成（两份 RESULT 文件存在）。

```text
读 docs/project-intelligence/11-workspace-entity-design.md，按它实施 Phase 3：
wiki 条目 + draft 管线（§4 wiki 条目设计、§5 多源探测模型的核心侧、§8 Phase 3 行）。
另读 docs/project-intelligence/phases/PHASE-1-RESULT.md 和 PHASE-2-RESULT.md
了解前两期边界。

范围：
- packages/knowledge 加 WikiEntry（type: wiki）+ parser + INDEX 索引：
  只追加新类/新函数（WikiEntry、parse_wiki），不改既有类和函数的行为
- 核心侧 BaseWikiProbe 缝 + CodeScanProbe（L1）；hc 侧 probe 不在本仓库实现
- agent 注入只取 summary + related，检索降权，stale 标注
- draft → review → merge 后端管线 + API（人写直接生效；AI/probe 产出 draft；
  journey last_status 回写是事实字段，自动）
- 前端只做独立的 WikiTab 组件新文件（frontend/src/components/WikiTab.tsx），
  **不挂载**（挂载由汇总窗口做，避免与 WorkspacePage 收尾纠缠）
- PII 红线：probe 只产聚合统计

完成后：
- 逐项跑文档 §9 Phase 3 验收 checklist（依赖挂载的项标注"待汇总窗口挂载后验证"）
- 写 docs/project-intelligence/phases/PHASE-3-RESULT.md
- git commit，只暂存本期相关文件
```

## 汇总窗口 启动 prompt（三期都完成后）

```text
三期已完成，读 docs/project-intelligence/phases/ 下三份 PHASE-N-RESULT.md，
了解各期交付、验收结果、遗留问题。然后：

1. 做两件交界工作：
   a. Sandbox 术语切换：planner.py / prompt_sections.py 及文档中 per-story
      目录措辞统一为 Sandbox（workspace_path 字符串语义和 15+ 消费点不动）
   b. 把 Phase 3 的 WikiTab 组件挂载进 Phase 2 的 WorkspacePage（一个 tab）
2. 交界一致性核对：verify_provider 与 workspace.integrations_json 的 config
   同源；wiki 注入与 scenario_catalog 注入的降权/限量；review API 与前端对齐；
   "workspace" 三义无残留
3. 全量 pytest（仓库根）+ 前端 build，确认无回归
4. 对照文档 10/11 验收标准做三期汇总：未过项、遗留问题排优先级、
   文档勘误回写（实现与设计有出入处直接修订文档）
5. git commit；push 需用户明确要求
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
- <已知未解/风险/交界注意点，一行一条>

## 关键 commit
- <hash 一行一个>
```
