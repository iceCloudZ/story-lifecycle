# 三期实施交接（分窗口执行）

> 三期分别在不同窗口执行，最后开汇总窗口收口。每个窗口零上下文启动，靠本文件 + 设计文档自包含交接。
> 设计文档：`10-test-framework-integration-design.md`（Phase 1）、`11-workspace-entity-design.md`（Phase 2/3）。

## 执行顺序与窗口间冲突提示

**建议按 1 → 2 → 3 顺序开窗口，不要并行。** 已知的文件级重叠：

- `orchestrator/engine/planner.py` + `prompt_sections.py`：Phase 1 改（scenario_catalog 注入、StagePlan 加字段），Phase 2 也改（术语切换 Sandbox、初始化管线触发）——并行必冲突
- `frontend/`：Phase 1 加 StoryDetailPage tab，Phase 2 新建 WorkspacePage——路由文件可能重叠
- Phase 3 依赖 Phase 2 的 WorkspacePage 和 workspace 表

每期结束**必须**做两件事再关窗口：

1. 跑该期验收 checklist（在对应设计文档的「验收标准」节）并如实记录结果
2. 写本目录下的 `PHASE-N-RESULT.md`（模板见下）并 `git commit`（只暂存本期相关文件）

## 窗口 1 启动 prompt（Phase 1：测试框架接入）

```text
读 D:/github/story-lifecycle/docs/project-intelligence/10-test-framework-integration-design.md，
按它实施 Phase 1：测试框架接入。文档自包含，严格按「改动总览」的 1-4 执行（改动 5 在
hc-pytest 仓库，不在本仓库，只做契约对齐不写 hc 侧代码）。

边界：
- 只做文档 10 的范围，不要做 Workspace 实体化 / wiki（那是 Phase 2/3，别的窗口做）
- 遵守文档末尾 7 条不变量（特别是：异步产物模式为主、外部 FAIL 计 reject budget、
  PASS 不跳 confirm-gate、不在 poll loop 同步跑长测试、扫描器不进 miner）
- planner.py 只改 StagePlan(:322) 和 scenario_catalog 注入(:188 附近)，
  不要碰 infra/schemas.py:20 那个废弃 PlanResult
- 前端 tab 图标用 SVG 组件，遵守 frontend/AGENTS.md

完成后：
- 逐项跑文档「验收标准」checklist
- 写 docs/project-intelligence/phases/PHASE-1-RESULT.md（模板见该目录 README）
- git commit，只暂存本期相关文件
```

## 窗口 2 启动 prompt（Phase 2：Workspace 实体化）

```text
读 D:/github/story-lifecycle/docs/project-intelligence/11-workspace-entity-design.md，
按它实施 Phase 2：Workspace 实体化（§1 实体模型、§3 初始化管线、§8 Phase 2 行）。
另读 docs/project-intelligence/phases/PHASE-1-RESULT.md 了解上一期落地的边界，
不要改动 Phase 1 已交付的 verify_providers 机制（本期的集成登记只往
workspace.integrations_json 里存 config key）。

范围（只做这些）：
- workspace 表 + project.workspace_id 迁移（infra/db/models.py，raw SQL 无 ORM）
- 初始化管线 5 步（独立 pipeline，不复用 story 引擎；幂等、单步可重跑）
- 术语切换：prompt/文档中 per-story 目录改称 Sandbox（workspace_path 字符串
  语义和 15+ 消费点零改动，只改措辞）
- WorkspacePage 只读版：旅程 / Stories / 概览三个 tab（Wiki tab 是 Phase 3）
- 开源零配置路径：不建 Workspace 时行为与今天完全一致

完成后：
- 逐项跑文档 §9 Phase 2 验收 checklist
- 写 docs/project-intelligence/phases/PHASE-2-RESULT.md
- git commit，只暂存本期相关文件
```

## 窗口 3 启动 prompt（Phase 3：Wiki + draft 管线）

```text
读 D:/github/story-lifecycle/docs/project-intelligence/11-workspace-entity-design.md，
按它实施 Phase 3：wiki 条目 + draft 管线 + review 收件箱（§4 wiki 条目设计、
§5 多源探测模型的核心侧、§8 Phase 3 行）。
另读 docs/project-intelligence/phases/PHASE-1-RESULT.md 和 PHASE-2-RESULT.md
了解前两期边界。

范围：
- packages/knowledge 加 WikiEntry（type: wiki）+ parser + INDEX 索引
- 核心侧 BaseWikiProbe 缝 + CodeScanProbe（L1）；hc 侧 probe（DMS/ES/Mongo）
  不在本仓库实现，只保证缝可用
- agent 注入只取 summary + related，检索降权，stale 标注
- WorkspacePage Wiki tab + review 收件箱（人写直接生效；AI/probe 产出一律
  draft → review → merge；journey last_status 回写是事实字段，自动）
- PII 红线：probe 只产聚合统计

完成后：
- 逐项跑文档 §9 Phase 3 验收 checklist
- 写 docs/project-intelligence/phases/PHASE-3-RESULT.md
- git commit，只暂存本期相关文件
```

## 汇总窗口 启动 prompt（三期完成后）

```text
读 docs/project-intelligence/phases/ 下的 PHASE-1-RESULT.md、PHASE-2-RESULT.md、
PHASE-3-RESULT.md，对照设计文档 10 / 11 的验收标准做三期汇总：
1. 核对每期验收 checklist 的实际结果，标出未过项和跳过项
2. 检查三期交界处的一致性：verify_provider 与 workspace.integrations_json 的
   config 是否同源、术语 Sandbox 是否无残留三义、wiki 注入与 scenario_catalog
   注入是否都降权/限量得当
3. 跑全量 pytest（仓库根）确认无回归
4. 产出汇总结论：三期各自状态、遗留问题清单（排优先级）、文档 10/11 需要
   回写的勘误（实际实现与设计有出入处，直接修订文档并 commit）
```

## PHASE-N-RESULT.md 模板

```markdown
# Phase N 实施结果（<日期>）

## 交付清单
- <文件/功能，一行一条，带路径>

## 验收 checklist 结果
- [x] <过项>
- [ ] <未过项——原因>

## 与设计文档的偏差
- <实现时不得不偏离设计处，及理由；没有就写"无">

## 遗留问题（给汇总窗口）
- <已知未解/风险，一行一条>

## 关键 commit
- <hash 一行一个>
```
