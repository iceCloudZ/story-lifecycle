> ⚠️ **历史快照（归档于 2026-07）**：描述的架构可能已被后续演进取代。当前架构见 [../../ARCHITECTURE.md](../../ARCHITECTURE.md)。本文件保留作决策记录（ADR），正文未修改。

---

# Idea: Orchestrator Agent — 从状态机到自主编排

## 背景

Story Lifecycle Manager 是一个 AI 编排工具，通过 LangGraph 状态机驱动多个 AI CLI（Claude Code、Codex 等）完成软件开发工作流。每个"Story"代表一个任务单元，按 profile 定义的阶段（design → implement → review）推进。

### 当前架构

```
LangGraph StateGraph（10 个固定节点）：

START → plan_stage ──→ execute_stage ──→ poll_completion ──→ review_stage ──→ router
              ↑              │                   │                              │
              │              │                   │                   ┌──────────┤
              │              │                   │                   ↓          ↓
           wait_confirm      │                   │              advance    retry (→ plan_stage)
              ↑              │                   │                ↓          ↓
              │              └──── (AI 执行) ←───┘          plan_stage    skip_stage
              │                                                          fail_stage → END
              └── (人工确认)
```

每个 stage 完整跑一遍：plan → execute → poll → review → router → advance，然后进入下一个 stage。

### 三角色模型

| 角色 | 节点 | 模型 | 职责 |
|---|---|---|---|
| 架构师/PM | plan_stage | DeepSeek（编排 LLM） | 规划方案、写任务书、选 adapter/model |
| 工程师 | execute_stage | Claude/Codex（执行 CLI） | 读任务书、写代码、产出结果 |
| QA/评审员 | review_stage | DeepSeek（编排 LLM） | 结构化质量审查、记录 issues |

### 已实现的"智能"

- **Adversarial Loop**：plan↔review 对抗循环（最多 3 轮），planner 和 reviewer 互相对抗收敛
- **Code Loop**：execute↔review 迭代重试（最多 3 轮）
- **Trajectory Score**：0-1 路径评分，< 0.3 直接 hard kill
- **Condenser**：历史上下文 LLM 压缩，防止 token 膨胀
- **知识库**：.story-knowledge/ 跨阶段积累决策记录、约束、设计要点
- **Tool 抽象**：Planner 动态选择执行工具（stage_tool 等）

### 核心局限

**当前系统本质是"用 LLM 做决策点的状态机"**，不是"LLM 驱动的自主 Agent"。

具体表现：

1. **Router 太被动**：只在出错时介入（retry/skip/fail），正常路径直接 advance，没有主动优化
2. **阶段是静态的**：profile 写死 stage 序列，无法根据执行情况动态调整（比如 implement 后发现需要额外 design）
3. **无跨 Story 实时学习**：同时跑 10 个 story，第 1 个发现某 provider 不稳定，其余 9 个不会自动避开
4. **无成本/时间感知**：不知道哪个 stage 花了多少 token、多少时间，无法做预算控制
5. **Planner 对每个 stage 独立规划**：上一个 stage 的经验教训没有实时反馈到下一个 stage 的规划
6. **无全局视野**：Planner 和 Reviewer 是独立的 LLM 调用，没有一个 agent 在统筹整个 Story 的生命周期

## 问题

如何设计一个 **Orchestrator Agent**，在保持当前系统确定性和可控性的前提下，具备全局视野和自主决策能力？

### 需要发散的方向

1. **全局 agent vs 决策点增强**：是加一个新的全局 agent 层，还是增强现有的 planner/reviewer 节点？
2. **动态阶段**：如何让 Story 的 stage 序列可以动态调整？agent 需要什么信息才能做出合理决策？
3. **实时学习**：cross-story 的实时信息共享怎么做？共享什么信息？（provider 稳定性、model 能力、常见错误模式）
4. **成本/质量权衡**：如何在 token 预算有限的情况下，智能地分配资源（哪些 stage 值得用贵 model，哪些可以跳过）？
5. **渐进式增强**：如何确保新能力可以逐步引入，不会破坏现有的确定性流程？
6. **人机协作**：agent 在什么情况下应该自主决策，什么情况下应该停下来问人？

### 约束

- 编排 LLM 是 DeepSeek（成本低），执行 LLM 是 Claude/Codex（能力强），要利用这个分层
- 不能丢失确定性——用户需要能理解、预测、干预 agent 的决策
- 性能开销要可控——每个 stage 多 1-2 次 LLM 调用可以接受，但不能指数级增长
- 要兼容现有的 profile YAML、Tool 抽象、知识库体系

### 相关文档

- `docs/design-smart-orchestrator.md` — 当前 Smart Orchestrator 完整设计
- `docs/idea-plan-review-adversarial-loop.md` — 对抗循环设计
- `docs/idea-dual-flywheel-domain-and-engine.md` — 双飞轮（领域+引擎）知识体系
- `docs/idea-project-intelligence-pipeline.md` — 项目智能管道
- `docs/roadmap-v0.5-to-v1.0.md` — 版本路线图

## 核心判断

Orchestrator Agent 不应该替换现有 LangGraph 状态机。更合理的方向是：在确定性的 workflow kernel 之上增加一个智能控制平面。

```text
Deterministic Workflow Kernel
  负责状态推进、DB 更新、工具启动、done 消费、review gate、wait_confirm

Orchestrator Agent Control Plane
  负责观察全局上下文、提出策略、建议 graph patch、分配预算、识别风险

Policy Engine
  负责判断 agent 提案是否允许执行、是否需要降级、是否需要问人、是否拒绝
```

底线原则：

```text
LLM proposes, Policy disposes.
```

也就是说，LLM 可以提出“插入 architecture_review”“切换 model”“拆 sub-story”“暂停问人”等建议，但不能直接修改 DB、启动执行工具、删除文件或推进 story 状态。所有实际副作用仍由确定性代码执行。

这个设计保留当前状态机的可预测性，同时给系统增加全局视野和自主优化能力。

## 目标形态

Orchestrator Agent 的目标不是做一个“万能聊天式 Agent”，而是成为 Story Lifecycle 的 Mission Control：

```text
Story = process
Stage = thread
Tool = syscall
Model = compute resource
Budget = quota
Review gate = kernel guard
Human = privileged operator
Blackboard = shared runtime memory
Trace = audit log
Flywheel = scheduler optimizer
```

系统能力从：

```text
按 profile 跑完一个 story
```

升级为：

```text
在预算、风险、上下文、历史成功率、生产约束之间，动态选择最合适的工程路径
```

## 总体架构

```text
Story Start
  -> Meta-Planner 生成 StrategyEnvelope
  -> Policy Engine 校验 strategy
  -> Graph 按 profile route 启动

Each Stage
  -> deterministic node 执行
  -> event_log 记录事实
  -> 更新 Working Memory / Budget Ledger / Runtime Blackboard
  -> Rule Router 判断是否可以正常 advance
  -> 若异常、高风险或低置信度:
       Strategic Router 生成 DecisionEnvelope
       Policy Engine 校验 decision
       apply / downgrade / reject / wait_confirm
```

核心组件：

| 组件 | 职责 |
|---|---|
| Meta-Planner | Story 级策略制定，输出 StrategyEnvelope |
| Strategic Router | Stage 后的战术调整，输出 DecisionEnvelope |
| Policy Engine | 约束校验、权限控制、预算控制、人机协作触发 |
| Stage Library | 所有合法原子 stage 的定义 |
| Stage Graph | stage 之间允许的依赖和跳转 |
| Graph Patch Registry | 运行时允许的 graph 修改动作 |
| Working Memory | 单个 story 的持续上下文 |
| Runtime Blackboard | 跨 story 的短期运行态共享 |
| Budget Ledger | token、时间、重试、贵模型、人类注意力预算 |
| EventBus | 所有 story / stage / LLM / tool 事件的结构化事实流 |

## Meta-Planner 与 Strategic Router

### Meta-Planner

Meta-Planner 是“战役计划”制定者。它不关心某个 stage 的具体执行细节，而是在 story 开始或重大偏离时生成全局策略。

触发时机：

- Story START。
- 进入 design 前的 scope/decomposition 判断。
- 连续 retry 无进展。
- trajectory_score 显著下降。
- 预算即将耗尽。
- provider/model 健康度降级。
- 架构审查触发。
- 生产风险升级。
- domain constraint 与 engine strategy 冲突。

输出是 StrategyEnvelope：

```json
{
  "strategy_id": "strat-001",
  "mode": "quality_first",
  "initial_route": ["design", "implement", "review"],
  "allowed_graph_patches": ["insert_stage", "repeat_stage", "split_sub_story"],
  "model_policy": {
    "planning": "deepseek",
    "execution": "codex",
    "review": "deepseek"
  },
  "budget": {
    "max_llm_calls": 12,
    "max_expensive_model_calls": 3,
    "max_minutes": 60,
    "max_human_interrupts": 2
  },
  "fallback_plan": {
    "on_execution_timeout": "switch_executor_or_split_task",
    "on_repeated_review_failure": "insert_architecture_review"
  },
  "human_interrupt_policy": {
    "ask_on_production_risk": true,
    "ask_on_budget_overrun": true,
    "ask_on_low_confidence_below": 0.6
  },
  "router_thresholds": {
    "min_trajectory_score_for_advance": 0.75,
    "review_score_revise_threshold": 0.65,
    "max_retry_without_progress": 2,
    "trigger_on_budget_burn_rate": 0.8
  }
}
```

Meta-Planner 还有一个不可后移到 design 末尾的职责：判断当前输入到底是单个 story，还是需要拆分的 epic。这个判断不能等到 design 阶段末尾再做，否则一个大需求已经消耗了大量设计成本，后续 implement 也会变成大爆炸修改。

但是这个判断也不应该在 START 阶段做完整深读。START 阶段如果读完整 PRD、扫描代码、分析影响面，然后 plan 阶段再读一次，会造成 token 爆炸。更合理的方式是：

```text
START
  cheap intake only
  不深读 PRD
  不做完整代码影响分析
  只选择 profile / budget / autonomy level / 是否需要 deep plan
  可运行 Complexity Classifier，识别 trivial/S/M/L/Epic

PLAN
  做一次深度理解
  完成 scope sizing
  如果是 L/Epic，则拆分子任务并生成上下文包
```

因此 Orchestrator Agent 的职责不是“START 阶段提前深读并拆分”，而是保证 **design/execute 之前已经完成 scope sizing 和 context sharding**。

### Complexity Classifier 与 Simple Execution Path

START 阶段可以做极轻量 Complexity Classifier，但不能做深度分析。它只消费标题、来源元数据、PRD 摘要、文件大小、关键词和少量 repo hints。

输出：

```json
{
  "complexity_hint": "trivial",
  "confidence": 0.82,
  "recommended_path": "simple_execution",
  "reason": "single-file documentation wording change; no code/database/API impact detected"
}
```

复杂度分流：

```text
trivial
  -> Simple Execution Path
  -> 不调用完整 Meta-Planner
  -> 不做 decomposition
  -> 可跳过 review 或使用轻量 review

S/M
  -> normal plan

L/Epic
  -> decomposition-aware plan
```

Simple Execution Path 适用场景：

- typo / 文案修复。
- 单文件低风险变更。
- 无 DB/API/生产影响。
- 无跨模块依赖。
- 用户明确要求快速处理。

即使走 Simple Execution Path，也必须保留 done 协议、event_log 和最小 review/verification 证据。

Simple Execution Path 必须有熔断机制。Classifier 的误判成本是不对称的：把 trivial 判成 S/M 只是多花成本；把 Epic 判成 trivial 会造成严重错误。

熔断信号：

- 修改文件数超过阈值，例如 3 个。
- 触及 DB migration、配置中心、权限、安全、支付、理赔、发布脚本等高风险路径。
- 出现 review revise/fail。
- 出现新的跨模块依赖。
- done JSON 声明 complexity 从 trivial/S 升级到 M/L。
- 执行耗时或 retry 超过 simple path budget。

熔断后：

```text
simple_execution
-> circuit_break
-> return_to_plan
-> normal plan / decomposition-aware plan
```

```text
intake / story created
-> START cheap intake
-> PLAN deep analysis + scope sizing
-> decide:
     S/M story: enter design
     L/epic: create decomposition proposal
-> human_confirm or auto-create sub-stories by autonomy level
-> per-sub-story design
```

判断信号：

- 是否跨多个业务域或多个 bounded context。
- 是否同时包含数据模型、计算逻辑、管理端 API、前端交互、迁移、回归测试。
- 是否需要多个团队或多类 reviewer。
- 是否存在可独立交付的功能切片。
- 是否有明显依赖顺序，例如模型/DDL 先于计算策略，API 先于前端。
- 是否设计文档产出会覆盖过多模块，导致 review 无法聚焦。

Scope & Decomposition Gate 输出：

```json
{
  "scope_decision": "decompose",
  "complexity": "L",
  "reason": "PRD touches domain model, DDL, calculation engine, admin API, frontend config, migration, quote simulation and claim regression.",
  "recommended_next_stage": "confirm_decomposition",
  "sub_stories": [
    {
      "key": "001-A",
      "title": "分段赔付领域模型与数据结构设计",
      "scope": ["domain model", "DDL", "migration"],
      "depends_on": []
    },
    {
      "key": "001-B",
      "title": "分段赔付计算策略设计",
      "scope": ["claim calculation", "segment algorithm", "edge cases"],
      "depends_on": ["001-A"]
    },
    {
      "key": "001-C",
      "title": "保障因子管理端配置 API",
      "scope": ["CRUD API", "validation", "OpenAPI"],
      "depends_on": ["001-A"]
    },
    {
      "key": "001-D",
      "title": "前端分段规则配置交互",
      "scope": ["dynamic rows", "interval validation", "UX constraints"],
      "depends_on": ["001-C"]
    },
    {
      "key": "001-E",
      "title": "报价试算与理赔链路回归",
      "scope": ["integration tests", "compatibility", "regression cases"],
      "depends_on": ["001-B", "001-C"]
    }
  ],
  "human_interrupt_required": true
}
```

这类拆分建议不应该由 design stage 的执行 Agent 在文档末尾“顺手建议”。执行 Agent 已经进入具体设计上下文，天然会倾向继续展开细节。拆分是编排层职责，应该由 Orchestrator Agent 在任务生命周期入口做。

更精确地说：拆分可以发生在 plan 阶段，但必须由 Orchestrator Agent / Meta-Planner 作为编排决策完成，而不是由 design executor 在产出设计文档后才附带建议。

## Plan-stage Decomposition 与 Context Sharding

Story Lifecycle 的一个天然优势是：不同 stage 默认在新窗口/新进程中运行，上下文天然隔离。共享上下文必须显式写入文件。这非常适合做可控的多 Agent 调度。

因此，推荐的拆分模型是：

```text
PLAN
  -> 一次性深读 PRD + repo signals + domain hints
  -> 判断 S/M/L/Epic
  -> 如果 S/M：生成单一 Task Packet
  -> 如果 L/Epic：
       生成 Decomposition Plan
       生成 Dependency Graph
       生成 per-task Task Packets

EXECUTE
  -> 读取 Decomposition Plan + Task Packets
  -> 校验依赖图和冲突
  -> 按拓扑层确定性调度
  -> 能并行的 task 并行执行
  -> 每个 task 新开隔离 agent/window
  -> 每个 agent 只读取自己的 Task Packet

REVIEW
  -> 审查每个 task artifact
  -> 审查集成风险
  -> 决定 retry specific task / retry merge / return_to_plan
```

核心原则：

```text
全局深读只发生一次。
拆分后的子任务必须上下文隔离。
子 Agent 不继承完整 PRD、完整代码分析和完整历史上下文。
每个子 Agent 只读取自己的 Task Packet。
```

### Decomposition Plan

建议写入：

```text
.story/context/{story_key}/plan/decomposition.json
```

示例：

```json
{
  "story_key": "001",
  "strategy": "decompose",
  "complexity": "L",
  "tasks": [
    {
      "id": "A",
      "title": "分段赔付领域模型与数据结构",
      "packet_path": ".story/context/001/plan/tasks/A.md",
      "depends_on": [],
      "can_parallel": true,
      "expected_artifacts": ["docs/specs/001-A-domain-model.md"],
      "risk": "high"
    },
    {
      "id": "B",
      "title": "分段赔付计算策略",
      "packet_path": ".story/context/001/plan/tasks/B.md",
      "depends_on": ["A"],
      "can_parallel": false,
      "expected_artifacts": ["docs/specs/001-B-calculation.md"]
    },
    {
      "id": "C",
      "title": "保障因子管理端配置 API",
      "packet_path": ".story/context/001/plan/tasks/C.md",
      "depends_on": ["A"],
      "can_parallel": true,
      "expected_artifacts": ["docs/specs/001-C-admin-api.md"]
    },
    {
      "id": "D",
      "title": "前端分段规则配置交互",
      "packet_path": ".story/context/001/plan/tasks/D.md",
      "depends_on": ["C"],
      "can_parallel": false,
      "expected_artifacts": ["docs/specs/001-D-frontend.md"]
    },
    {
      "id": "E",
      "title": "报价试算与理赔链路回归",
      "packet_path": ".story/context/001/plan/tasks/E.md",
      "depends_on": ["B", "C"],
      "can_parallel": false,
      "expected_artifacts": ["docs/specs/001-E-regression.md"]
    }
  ]
}
```

### Task Packet

每个子任务上下文包建议写入：

```text
.story/context/{story_key}/plan/tasks/{task_id}.md
```

Task Packet 应包含：

- 子任务目标。
- 明确范围。
- 非目标。
- 依赖任务。
- 可读取文件。
- 禁止修改范围。
- 资源锁。
- 输入契约。
- 输出契约。
- 预期 artifact。
- 测试要求。
- 风险提示。
- 完成 done JSON 协议。

示例结构：

```markdown
# Task A: 分段赔付领域模型与数据结构

## Goal
设计分段赔付所需的领域模型、值对象、数据库结构和迁移策略。

## Scope
- 领域实体和值对象
- DDL 草案
- 兼容旧数据
- 回滚方案

## Non-goals
- 不设计前端交互
- 不实现理赔计算算法
- 不修改代码

## Relevant Files
- backend/.../coverage/...
- db/migration/...

## Resource Locks
- write:domain:claim_calculation
- write:db_table:coverage_rule
- write:file_glob:backend/**/CoverageRuleService.java

## Output
- docs/specs/001-A-domain-model.md

## Done JSON
{
  "status": "success",
  "task_id": "A",
  "artifacts": ["docs/specs/001-A-domain-model.md"],
  "summary": "...",
  "open_questions": []
}
```

Resource Locks 是并行调度的关键。`depends_on` 只能表达先后关系，不能表达资源冲突。两个 task 即使没有显式依赖，只要写锁冲突，也不能并行。

资源锁类型示例：

```json
{
  "resource_locks": [
    {
      "type": "file_glob",
      "value": "backend/**/CoverageRuleService.java",
      "mode": "write"
    },
    {
      "type": "domain_area",
      "value": "claim_calculation",
      "mode": "write"
    },
    {
      "type": "db_table",
      "value": "coverage_rule",
      "mode": "write"
    },
    {
      "type": "api_prefix",
      "value": "/api/v1/coverage-factors",
      "mode": "write"
    }
  ]
}
```

调度条件：

```text
depends_on satisfied
+ no conflicting resource_locks
= can run parallel
```

### Execute 阶段职责

execute 阶段不应该重新做完整依赖分析，也不应该重新深读 PRD。它的职责是确定性调度：

```text
load decomposition.json
validate dependency graph
validate packet_path exists
validate no cycle
validate parallel conflict hints
validate resource_locks
validate lock wait timeout
topological sort
run ready tasks by layer
collect task done files
merge task summaries
emit execute summary
```

如果 execute 发现依赖图无效或并行冲突，不应该自行重拆，而应该返回 plan：

```json
{
  "status": "blocked",
  "reason": "invalid_decomposition",
  "details": "Task B and C both modify CoverageRuleService.java but are marked parallel",
  "recommended_action": "return_to_plan"
}
```

调度器还需要防止资源锁死锁或长期等待。第一阶段可以不做复杂死锁图算法，先采用保守策略：

- 每一层调度前先计算完整 runnable set。
- 只启动 resource_locks 互不冲突的 task。
- 如果某个 task 因锁等待超过阈值，标记为 `lock_wait_timeout`。
- 如果连续 N 次调度轮询没有新 task 启动且仍有未完成 task，标记为 `scheduler_no_progress`。
- 这类情况不由 execute 自行重排，应返回 plan 或 wait_confirm。

示例：

```json
{
  "status": "blocked",
  "reason": "lock_wait_timeout",
  "details": "Task C waited 10 minutes for write lock api_prefix:/api/v1/coverage-factors",
  "recommended_action": "return_to_plan"
}
```

### 并行策略

并行不由子 Agent 自由决定，而由 plan 产出的 dependency graph 和 execute 的确定性调度器决定。

```text
layer 1: A
layer 2: B + C
layer 3: D + E
merge/review
```

若某个 task 失败：

- 依赖它的 task 阻塞。
- 不依赖它的 task 可以继续。
- 失败 task 的 packet、stdout、done、artifact 汇总给 review/router。
- router 决定 retry task、return_to_plan 或 fail story。

### Context Sharding 的收益

这个模型解决的问题：

- 避免每个子 Agent 重读完整 PRD。
- 避免执行阶段重复做全局推理。
- 保持 stage/window 的天然隔离优势。
- 让并行边界可审计。
- 让失败传播清楚。
- 让 review 可以按 task 精准定位问题。

### Strategic Router

Strategic Router 是“前线战术指挥”。它在 stage 结束后，根据 StrategyEnvelope、当前 stage 产物、review finding、budget、blackboard 和 policy 约束提出下一步建议。

它不应该每个 stage 都调用 LLM。默认仍由 rule router 处理正常 happy path；只有出现以下信号时才调用 Strategic Router：

- review 结果为 revise/fail。
- stage output 缺关键 artifact。
- retry 多轮无进展，超过 StrategyEnvelope.router_thresholds。
- trajectory_score 低于 StrategyEnvelope.router_thresholds。
- provider/model health 降级。
- budget burn rate 超过 StrategyEnvelope.router_thresholds。
- domain/engine constraint 冲突。
- story 进入生产风险区。

输出是 DecisionEnvelope：

```json
{
  "decision_id": "dec-019",
  "decision": "insert_stage",
  "stage": "architecture_review",
  "reason": "same boundary failure repeated across related findings",
  "confidence": 0.78,
  "expected_value": "reduce repeated implementation retries",
  "budget_delta": {
    "llm_calls": 1,
    "minutes": 10
  },
  "risk": "delays current story but reduces architectural drift",
  "requires_human": false
}
```

## Stage Library、Stage Graph 与 Graph Patch

profile 不应该被废弃。profile 仍然表示默认路线；Stage Graph 表示允许运行时偏离的空间；Graph Patch 表示某次具体偏离。

```text
profile = planned route
stage_graph = allowed route universe
graph_patch = runtime deviation
```

### Stage Library

Stage Library 定义所有合法原子阶段：

```text
design
implement
review
test
research
architecture_review
security_scan
performance_test
integration_test
finalize
deploy_check
rollback_plan
```

每个 stage 必须声明：

- 输入要求。
- 输出 contract。
- 可用 tools。
- 默认 model policy。
- 预算上限。
- 是否允许自动插入。
- 是否需要人工确认。

### Stage Graph

Stage Graph 定义 stage 之间允许的边，而不是写死线性序列。

示例：

```text
design -> implement
implement -> test
implement -> review
review -> implement
review -> architecture_review
architecture_review -> design
test -> finalize
finalize -> END
```

### Graph Patch Registry

Orchestrator Agent 只能从已注册的 graph patch 中选择：

```text
insert_stage(stage, reason)
repeat_stage(stage, reason)
skip_stage(stage, reason)
split_sub_story(scope, reason)
merge_sub_story(result, reason)
switch_model(stage, model, reason)
pause_for_human(question, options)
```

每个 patch 都必须有：

- schema。
- precondition。
- budget_delta。
- risk_level。
- rollback behavior。
- policy check。

## Working Memory

当前 planner/reviewer 是离散 LLM 调用。Orchestrator Agent 需要一个 story 级 Working Memory，作为跨 stage 的持续上下文。

```json
{
  "story_key": "TAPD-1065520",
  "current_hypothesis": "审批状态不一致可能来自消息回调和状态枚举不一致",
  "confirmed_facts": [
    "hc-email-gov contains approval status mapping",
    "review found missing callback error handling"
  ],
  "open_risks": [
    "production config impact unknown",
    "rollback plan missing"
  ],
  "discarded_paths": [
    "frontend-only fix rejected by review"
  ],
  "latest_review_findings": [],
  "budget_status": {
    "remaining_llm_calls": 8,
    "remaining_minutes": 35
  }
}
```

每个 stage 可以读取 Working Memory，也必须以结构化方式更新它。这样 planner 不再每次从零开始，reviewer 也能知道哪些风险已经被确认、哪些路径已经被放弃。

## EventBus 与 Runtime Blackboard

### EventBus

EventBus 记录事实，不做决策。所有 story 的关键事件都进入 event_log：

```json
{
  "event_type": "llm_error",
  "story_key": "S-001",
  "stage": "execute",
  "adapter": "codex",
  "model": "gpt-5-codex",
  "mode": "headless",
  "error_type": "timeout",
  "duration_ms": 120000,
  "created_at": "2026-05-26T10:00:00Z"
}
```

### Runtime Blackboard

Runtime Blackboard 是短期运行态共享，不是长期知识库。它消费 event_log，聚合最近一段时间的系统状态。

```json
{
  "provider_health": {
    "codex/headless/windows": {
      "status": "degraded",
      "error_rate_15m": 0.4,
      "ttl_minutes": 60
    }
  },
  "recent_failure_signatures": [
    {
      "signature": "malformed_done_json",
      "stage": "implement",
      "count_15m": 3
    }
  ],
  "workspace_pressure": {
    "running_stories": 5,
    "blocked_workspaces": 2
  }
}
```

Blackboard 信息必须有 TTL、滑动窗口和指数衰减，防止旧故障长期污染决策。

## Budget Ledger

Orchestrator Agent 如果没有预算意识，会天然倾向于多问 LLM、多 review、多 retry。预算必须是一等公民。

每个 story 维护 Budget Ledger：

```json
{
  "story_key": "S-001",
  "budget": {
    "max_minutes": 60,
    "max_llm_calls": 12,
    "max_expensive_model_calls": 3,
    "max_retries": 4,
    "max_human_interrupts": 2
  },
  "used": {
    "minutes": 18,
    "llm_calls": 5,
    "expensive_model_calls": 1,
    "retries": 1,
    "human_interrupts": 0
  }
}
```

任何 graph patch、model switch、extra review、architecture_review 都必须声明 budget_delta。Policy Engine 可以据此：

- 允许。
- 降级模型。
- 降低 retry 次数。
- 转入 wait_confirm。
- 拒绝提案。

## Policy Engine

Policy Engine 是系统安全边界。它消费 StrategyEnvelope / DecisionEnvelope，但只执行符合约束的提案。

Policy Engine 不能等到后期才实现。哪怕 P0 只有硬编码规则，也必须先形成闭环：

```text
proposal -> policy_check -> allow / reject / needs_confirm / shadow_only
```

否则 DecisionEnvelope 只是日志，无法验证 `LLM proposes, Policy disposes`。

校验维度：

- 是否在 autonomy level 允许范围内。
- 是否在 Stage Graph 允许边内。
- 是否超过预算。
- 是否触发生产风险。
- 是否涉及 destructive action。
- 是否与 domain constraint 冲突。
- confidence 是否足够。
- 是否需要 human approval。

输出：

```json
{
  "policy_result": "needs_confirm",
  "reason": "production risk and confidence below threshold",
  "allowed_actions": ["pause_for_human"],
  "blocked_actions": ["auto_apply_graph_patch"]
}
```

P0 的 Policy Engine Skeleton 可以只包含最小规则：

```text
1. destructive action 一律 needs_confirm。
2. production risk 一律 needs_confirm。
3. budget_delta 超过剩余预算则 reject 或 downgrade。
4. confidence 低于 autonomy level 阈值则 needs_confirm。
5. graph patch 不在 allowlist 内则 reject。
6. L0/L1 autonomy 下所有 apply 类动作转为 shadow_only 或 needs_confirm。
```

后续再逐步从硬编码规则演进为 profile/config 驱动。即使未来引入规则 DSL 或 LLM 辅助解释 policy，最终裁决仍必须由确定性 policy check 产出。

## 自主等级

不要用一个布尔开关控制 Agent 自主性。建议定义 Autonomy Level：

```text
L0 manual
  所有关键决策问人。

L1 suggest
  Agent 只生成建议，不改变流程。

L2 shadow_apply
  Agent 计算新决策，但实际仍走旧路径，用于 A/B 和审计。

L3 guarded_apply
  低风险决策自动执行，高风险问人。

L4 budget_autonomy
  允许在预算内调模型、调 retry、插入低风险 stage。

L5 full_profile_autonomy
  仅在 SWE-bench 或显式授权 profile 中启用，允许更大范围 graph patch。
```

生产 hotfix 默认 L1/L2；普通本地 story 可以 L3；SWE-bench 可以 L4/L5。

## 人机协作协议

Agent 什么时候必须问人：

- 生产相关变更。
- 删除、迁移、回滚、发布。
- 预算超限。
- 低置信度但高影响。
- domain constraint 与 engine strategy 冲突。
- 连续 N 次无进展。
- 安全、权限、数据风险。
- 用户明确要求手动确认。

问人必须结构化，不允许只问“怎么办”。

```json
{
  "question": "当前连续 3 次实现都触发同一状态机问题，是否插入 architecture_review？",
  "recommendation": "insert_architecture_review",
  "options": [
    {
      "id": "insert_architecture_review",
      "label": "先做架构审查",
      "risk": "延迟当前修复，但降低继续打补丁风险"
    },
    {
      "id": "continue_retry",
      "label": "继续局部修复",
      "risk": "可能继续扩大状态不一致"
    },
    {
      "id": "fail_story",
      "label": "终止 story",
      "risk": "当前问题不解决"
    }
  ]
}
```

## 可观测性与审计

每次 Orchestrator Agent 决策都必须可追踪：

```json
{
  "decision_id": "dec-019",
  "story_key": "S-001",
  "stage": "review",
  "input_summary": {},
  "available_actions": ["advance", "retry", "insert_stage", "wait_confirm"],
  "chosen_action": "insert_stage",
  "rejected_actions": ["advance", "retry"],
  "policy_result": "allowed",
  "human_override": null,
  "outcome": "pending"
}
```

后续双飞轮可以直接消费这些 trace，用于：

- router preference dataset。
- strategy success/failure attribution。
- graph patch 效果评估。
- budget policy 调优。
- human override 分析。

## Blackboard 异步契约

EventBus 是事实日志，Blackboard 是聚合快照。两者不能阻塞主执行路径。

```text
stage emits event
-> append event_log synchronously and quickly
-> background aggregator consumes event_log
-> update blackboard snapshot asynchronously
-> router reads latest snapshot if available
```

约束：

- event_log 写入可以在主流程同步完成，但必须足够快。
- blackboard 聚合必须异步，不能成为 story 执行关键路径。
- router 读取 blackboard 时必须容忍过期数据。
- blackboard snapshot 必须带 `updated_at`、`ttl`、`staleness_ms`。
- blackboard 不可用时，router 降级为不使用该信号，而不是阻塞。
- TUI / debug panel 应显示 snapshot 新鲜度，让用户知道决策使用的是多久之前的全局状态。

示例：

```json
{
  "snapshot_id": "bb-20260527-100000",
  "updated_at": "2026-05-27T10:00:00Z",
  "staleness_ms": 4200,
  "provider_health": {
    "codex/headless/windows": {
      "status": "degraded",
      "ttl_seconds": 3600
    }
  }
}
```

TUI 展示示例：

```text
Blackboard: provider health degraded (snapshot 4.2s old, ttl 3600s)
```

## Graph Patch Shadow / Sandbox Validation

Graph Patch 是高风险能力。尤其是以下动作：

```text
insert_stage
switch_model
split_sub_story
parallel_dispatch
skip_stage
```

在低自治等级下，这些 patch 应先进入 shadow mode：

```json
{
  "patch_id": "patch-001",
  "proposal": "insert_stage:architecture_review",
  "actual_path": "retry:implement",
  "shadow_path": "architecture_review -> design -> implement",
  "applied": false,
  "reason": "L2 autonomy only allows shadow",
  "later_outcome": "retry_failed_same_boundary_issue"
}
```

shadow mode 的目标不是立即改变流程，而是积累反事实数据：

- Agent 想怎么干。
- Policy 为什么没让它干。
- 实际路径结果如何。
- 如果用户/评审认为提案更好，记录 counterfactual label。

反事实标签不能完全自动推断。业务项目中如果 shadow proposal 与实际路径分歧，应在 TUI 提供非阻塞标注入口：

```text
AI 曾建议插入 architecture_review，系统实际选择 retry implement。
如果当前 retry 结果不理想，可标记：AI 建议更合理 / 实际路径更合理 / 无法判断。
```

这些标签进入后续 router preference dataset。

对于 SWE-bench 或安全沙箱，可以进一步做 sandbox validation：真实跑一条分支，比较 pass rate、成本、时间和失败类型。业务项目默认不做真实分支执行，除非显式授权。

Scope & Decomposition Gate 也必须进入审计日志。后续如果某个大 story 因为未拆分导致 implement 失败、review 疲劳或回归爆炸，系统可以追溯是 scope sizing 判断错误，还是用户覆盖了拆分建议。

```json
{
  "decision_id": "scope-001",
  "decision_type": "scope_decomposition",
  "story_key": "001",
  "input_signals": [
    "ddl_change",
    "calculation_engine_change",
    "admin_api_change",
    "frontend_config_change",
    "migration_required"
  ],
  "chosen_action": "decompose",
  "complexity": "L",
  "sub_story_count": 5,
  "policy_result": "needs_confirm",
  "human_override": null
}
```

## 与双飞轮的关系

Orchestrator Agent 是双飞轮的执行控制面。

```text
Engine Flywheel
  学到哪些编排策略有效：
  - 何时插入 architecture_review
  - 何时切换 model
  - 何时停止 retry
  - 哪类 graph patch 提升成功率

Domain Flywheel
  学到业务项目的生产约束：
  - 哪些改动必须问人
  - 哪些模块必须查配置
  - 哪些风险不能自动推进

Orchestrator Agent
  在当前 story 中综合 engine strategy 与 domain constraint，
  输出可审计的 StrategyEnvelope / DecisionEnvelope。
```

冲突仲裁沿用双飞轮优先级：

```text
safety constraint > domain production constraint > engine execution constraint > domain pattern > engine pattern
```

## 风险

### 1. Agent 越权

风险：LLM 直接改变状态或执行副作用，导致不可预测。

控制：LLM 只能输出 envelope，Policy Engine 和 deterministic handler 才能执行。

### 2. 动态阶段造成流程不可理解

风险：用户无法理解 story 为什么突然跳回 design 或插入 architecture_review。

控制：所有 graph patch 必须记录 reason、confidence、budget_delta、policy_result，并在 TUI 可见。

### 3. Blackboard 污染

风险：短期故障信号长期影响决策。

控制：TTL、滑动窗口、指数衰减、人工清除入口。

### 4. 成本膨胀

风险：Meta-Planner、Strategic Router、Condenser、Reviewer 都调用 LLM，成本失控。

控制：默认 rule router；Strategic Router 只在异常点触发；每个 story 有 Budget Ledger。

### 5. 人类被频繁打断

风险：Agent 过度 wait_confirm，用户体验变差。

控制：human_interrupt_budget；低风险自动处理；重复问题合并提问。

### 6. 多 Agent 群聊化

风险：系统退化成 planner、reviewer、router、meta-planner 多个 LLM 互相聊天，成本高且难审计。

控制：所有 agent 输出结构化 envelope；禁止自由对话式协调；所有状态由 event_log 和 working memory 承载。

## 落地路线

### P0：DecisionEnvelope + Policy Engine Skeleton

- 统一 planner/router/reviewer 的决策输出结构。
- 增加 decision_id、confidence、reason、budget_delta、requires_human、policy_result。
- 增加最小 policy check：allow / reject / needs_confirm / shadow_only。
- 不改变当前行为，除明确高风险动作进入 needs_confirm。

### P1：Complexity Classifier + Simple Execution Path

- START 阶段做 cheap intake，不深读。
- 规则或小模型判断 trivial/S/M/L/Epic。
- trivial 走 Simple Execution Path，避免完整 Meta-Planner 成本。
- Simple Execution Path 增加 circuit breaker，发现高风险或范围膨胀时 return_to_plan。
- 保留 done、event_log 和最小 verification。

### P1.5：Resource Lock Dry-run

- 在真正开启并行调度前，execute 仍串行运行。
- 后台模拟 resource_locks 争用。
- 输出“如果并行会冲突”的 dry-run 报告。
- 用 dry-run 数据校准锁粒度：太粗会导致无法并行，太细会漏掉冲突。

### P2：Working Memory + Budget Ledger

- 每个 story 建立 `.story/context/{story_key}/working_memory.json`。
- 每个 story 建立 budget ledger。
- 每个 stage 读取并更新 memory / budget。

### P3：Strategic Router Shadow Mode

- 只在异常点生成 Strategic Router 建议。
- 不执行建议，只记录 old_decision vs proposed_decision。
- 统计 proposed decision 与后续 outcome 的关系。
- 引入反事实评估字段：human_label、later_outcome、counterfactual_note。
- TUI 提供非阻塞 human counterfactual label 入口。

### P4：Runtime Blackboard

- 从 event_log 聚合 provider/model/stage 健康度。
- 支持 TTL 和滑动窗口。
- planner/router 可读取 blackboard，但只能作为低优先级证据。
- blackboard 聚合异步执行，主流程只读取可用 snapshot。
- TUI/debug panel 显示 blackboard snapshot staleness。

### P5：Meta-Planner + Plan-stage Decomposition

- Story START 生成 StrategyEnvelope。
- START 只做 cheap intake，完整 StrategyEnvelope 可推迟到 plan。
- 在 plan 阶段执行 Scope & Decomposition Gate。
- 对 L/epic 输入输出子故事拆分建议，而不是让 design stage 末尾才建议拆分。
- 生成 Decomposition Plan、Dependency Graph、Task Packets、Resource Locks。
- 先只作为 prompt/context，不直接改 graph。
- major checkpoint 可触发 re-plan。

### P6：Stage Graph + Graph Patch Registry + Sandbox Validation

- 定义 Stage Library。
- 定义 Stage Graph。
- 注册 insert_stage、repeat_stage、split_sub_story、pause_for_human 等 patch。
- Policy Engine 校验后才允许执行。
- 高风险 patch 先进入 shadow mode。
- SWE-bench 或显式授权环境可做 sandbox validation。

### P7：Guarded Apply

- 根据 autonomy level 启用自动应用。
- L1/L2 用于真实业务默认模式。
- L4/L5 仅用于 SWE-bench 或显式授权 profile。

## 推荐结论

Orchestrator Agent 的正确方向不是“把状态机变成自由 Agent”，而是：

```text
确定性状态机作为 workflow kernel
LLM Orchestrator 作为 policy proposer
Policy Engine 作为安全边界
Graph Patch 作为合法行动空间
Blackboard / Budget / Memory 作为决策上下文
Human Interrupt Contract 作为最终控制权
Trace-driven Flywheel 作为持续进化机制
```

这样系统可以逐步获得全局视野、自主决策和实时学习能力，同时保留 Story Lifecycle 最重要的工程属性：确定性、可解释、可审计、可干预。

## 讨论记录与设计演进

### 1. 拆分时机：不是 design 末尾

真实任务书中出现了一个典型问题：design 阶段任务要求完整阅读 PRD、分析领域模型、设计 DDL、API、算法、前端交互、测试策略，最后才要求“如果 complexity=L，建议后续拆分子 Task”。

这个时机过晚。对于 L/Epic 级需求，等 design executor 写完大设计再建议拆分，已经消耗了大量上下文和设计成本，也会让后续 implement 走向大爆炸修改。

结论：

```text
拆分不能放在 design 末尾作为附带建议。
拆分是编排层职责，必须在进入具体执行前完成。
```

### 2. START 阶段不能深读

随后讨论发现：如果在 START 阶段让 Orchestrator Agent 深读 PRD、扫描代码、分析影响面，然后 plan 阶段又深读一次，会导致 token 爆炸。

结论：

```text
START 只做 cheap intake。
START 不做完整拆分。
START 不做完整技术方案。
START 只选择 profile、budget、autonomy level 和是否需要 deep plan。
```

### 3. Plan 阶段拆分是合理的

结合 superpowers / sub-agent 模式，plan 阶段可以做一次深度理解，然后拆分子任务。关键不在于“越早越好”，而在于：

```text
全局深读只发生一次。
拆分后的子 Agent 不共享完整上下文。
每个子 Agent 只读取自己的 Task Packet。
```

这与 Claude Code subagent、Anthropic orchestrator-worker、OpenAI handoff、LangGraph supervisor/subagent 等方向一致：主 Agent 负责理解、拆分、派发、汇总；子 Agent 用隔离上下文执行局部任务。

### 4. Execute 阶段负责调度，不负责重拆

进一步讨论后，确定 execute 阶段不应该直接看到多个上下文包就自由开多 Agent，也不应该重新做完整依赖分析。

职责边界应是：

```text
PLAN:
  deep analysis
  decomposition
  dependency graph
  task packets

EXECUTE:
  validate graph
  deterministic scheduling
  spawn isolated agents
  collect results

REVIEW:
  per-task review
  integration review
  retry/merge/return_to_plan decision
```

这样既保留上下文隔离，又能安全利用并行。

### 5. 当前设计结论

最终收敛为：

```text
START cheap intake
-> PLAN deep analysis + decomposition + context sharding
-> EXECUTE deterministic parallel scheduler
-> REVIEW task-level + integration-level review
-> ROUTER retry specific task / return_to_plan / advance
```

这比“START 提前深读拆分”更省 token，也比“execute 临时自由多 Agent”更可控。

### 6. Policy 与调度细节补强

后续评审指出：如果 P0 只有 DecisionEnvelope，没有 Policy Engine，那么 `LLM proposes, Policy disposes` 只是日志，不是闭环。因此 P0 调整为 `DecisionEnvelope + Policy Engine Skeleton`。哪怕规则先硬编码，也必须先有 allow/reject/needs_confirm/shadow_only 的裁决接口。

评审还指出，Task Packet 只靠 `depends_on` 不能保证并行安全。两个任务可能没有显式依赖，但会修改同一个服务、同一张表或同一个 API 前缀。因此增加 Resource Locks，execute 调度时必须同时满足：

```text
depends_on satisfied
+ no conflicting resource_locks
= can run parallel
```

Strategic Router 的异常触发阈值也不应该写死。不同策略下阈值不同：

```text
quality_first: 更早触发 router
speed_first: 更晚触发 router
production_hotfix: 低风险容忍度
swebench: 更激进探索
```

因此 `router_thresholds` 进入 StrategyEnvelope。

最后，Blackboard 不能阻塞主流程。EventBus 负责同步写事实，Blackboard 由异步聚合器生成 snapshot。Router 可以读取轻微过期的 snapshot，但不能等待 blackboard 更新。

这些补强让设计从“概念完整”进一步变成“工程可落地”。

### 7. V2 评审补充

进一步评审后补充了四类落地细节：

1. Resource Locks 需要死锁/等待保护。第一阶段不做复杂锁管理，但 execute 必须有 `lock_wait_timeout` 和 `scheduler_no_progress`，避免并行调度卡死。
2. Simple Execution Path 需要 circuit breaker。Classifier 误把 Epic 判成 trivial 的风险远高于把 trivial 判成 S/M，因此一旦发现范围膨胀或触及高风险资源，必须 return_to_plan。
3. Blackboard snapshot 的新鲜度应在 TUI/debug panel 展示。用户需要知道 router 使用的是 4 秒前还是 4 分钟前的全局健康信号。
4. Shadow Mode 的反事实数据不能只靠自动推断。业务项目需要低成本 human label 入口；SWE-bench 才适合真实 sandbox branch 对比。

这些补充的共同目标是：让系统在变聪明之前，先保证不会因为并行、缓存、误分类或反事实幻觉而变得不可靠。
