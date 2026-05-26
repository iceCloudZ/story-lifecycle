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
  }
}
```

### Strategic Router

Strategic Router 是“前线战术指挥”。它在 stage 结束后，根据 StrategyEnvelope、当前 stage 产物、review finding、budget、blackboard 和 policy 约束提出下一步建议。

它不应该每个 stage 都调用 LLM。默认仍由 rule router 处理正常 happy path；只有出现以下信号时才调用 Strategic Router：

- review 结果为 revise/fail。
- stage output 缺关键 artifact。
- retry 多轮无进展。
- trajectory_score 下降。
- provider/model health 降级。
- budget 异常。
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

### P0：DecisionEnvelope

- 统一 planner/router/reviewer 的决策输出结构。
- 增加 decision_id、confidence、reason、budget_delta、requires_human、policy_result。
- 不改变当前行为，只增加记录。

### P1：Working Memory + Budget Ledger

- 每个 story 建立 `.story/context/{story_key}/working_memory.json`。
- 每个 story 建立 budget ledger。
- 每个 stage 读取并更新 memory / budget。

### P2：Strategic Router Shadow Mode

- 只在异常点生成 Strategic Router 建议。
- 不执行建议，只记录 old_decision vs proposed_decision。
- 统计 proposed decision 与后续 outcome 的关系。

### P3：Runtime Blackboard

- 从 event_log 聚合 provider/model/stage 健康度。
- 支持 TTL 和滑动窗口。
- planner/router 可读取 blackboard，但只能作为低优先级证据。

### P4：Meta-Planner

- Story START 生成 StrategyEnvelope。
- 先只作为 prompt/context，不直接改 graph。
- major checkpoint 可触发 re-plan。

### P5：Stage Graph + Graph Patch Registry

- 定义 Stage Library。
- 定义 Stage Graph。
- 注册 insert_stage、repeat_stage、split_sub_story、pause_for_human 等 patch。
- Policy Engine 校验后才允许执行。

### P6：Guarded Apply

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
