# 模块架构图(Business Module Architecture)

> 按 **业务流程** 划分 story-lifecycle 的大模块,配 Mermaid 架构图(GitHub 原生渲染)。

## 与代码分层文档的关系(正交)

本目录有两套视角,**都看才能真正定位代码**:

| 视角 | 在哪 | 回答什么 |
|---|---|---|
| **业务模块**(本目录 01–04) | `docs/module-architecture/` | "从需求到交付,系统提供哪些业务能力?"(流程,时序) |
| **代码分层**(真相源) | [`packages/story-lifecycle/docs/ARCHITECTURE.md`](../../packages/story-lifecycle/docs/ARCHITECTURE.md) | "代码怎么组织才不循环依赖?"(结构,分层) |

两套划分**正交**:一个业务模块横跨多个代码层,一个代码层服务多个业务模块。**改任何代码前,两边定位都要看。** 双向映射见 [05-business-to-code-mapping.md](05-business-to-code-mapping.md)。

> 代码分层文档留在包内(按 [`AGENTS.md`](../../AGENTS.md) 约定"包级文档留包内"),且被 50+ 文件引用(含 48 个 ADR)。本目录不移动/复制它,而是建立映射指向它——单一真相源、零断链。

## 文件导航

| 文档 | 内容 | 看这个如果你想知道… |
|---|---|---|
| [01-business-flow.md](01-business-flow.md) | 端到端业务流程 + 时序图 | 一个需求从进来到交付经过哪些步骤 |
| [02-modules-overview.md](02-modules-overview.md) | 7 大模块划分 + 关系图 | 系统整体有哪些业务能力域 |
| [03-module-details.md](03-module-details.md) | 各模块详解(职责/IO/代码落点/API) | 某个模块具体做什么、代码在哪 |
| [04-knowledge-flywheel.md](04-knowledge-flywheel.md) | 跨包知识飞轮详解 | 系统"越用越聪明"怎么实现 |
| [05-business-to-code-mapping.md](05-business-to-code-mapping.md) | **业务模块 ↔ 代码分层双向映射** | 从业务找代码落点,或从代码找它服务什么业务 |

## 七大业务模块速查

按一个 Story 的生命周期时序:

| # | 模块 | 一句话 | 核心 API 前缀 |
|---|---|---|---|
| ① | **需求接入** Intake | 把外部需求转化成 Story + PRD | `/api/story` POST、`/api/intake`、`/api/sync/tapd` |
| ② | **上下文装配** Context Assembly | AI 开工前喂什么(知识 + transcript + 项目画像) | `/api/story/{key}/context*` |
| ③ | **执行编排** Execution | 驱动 AI CLI 走完一个阶段(系统心脏) | `/plan/stream`、`/pty/spawn`、`/ws/*` |
| ④ | **质量闸** Quality Gate | 硬闸 + 对抗审查,决定 advance/retry/fail | `/gate-results`、`/findings`、`/quality` |
| ⑤ | **交付收尾** Delivery | 产出交付包 + worktree 清理 + 上游回写 | `/delivery-artifacts`、`/worktrees/*` |
| ⑥ | **知识飞轮** Knowledge Flywheel | 跨包沉淀经验,反哺下次(monorepo 灵魂) | 跨包,I1–I4 |
| ⑦ | **人机协同** HITL | 人怎么介入(plan 确认 / clarify / 交互终端) | `/plan/confirm`、`/clarify`、`/approvals` |

## 一句话定位

> **把一个需求(Story)交给 AI,让它走完 设计→实现→验证 的完整生命周期,过程中沉淀/复用知识,人可随时介入纠偏。**

四包分工是业务骨架:

| 包 | 业务角色 |
|---|---|
| `story-lifecycle` | 编排引擎 + 知识消费者(模块①–⑤、⑦ 的主体) |
| `story-miner` | 知识生产者(模块⑥的生产侧) |
| `knowledge` | 统一知识契约(模块⑥的 schema) |
| `testing` | 真 AI E2E 评测(度量引擎自身) |
