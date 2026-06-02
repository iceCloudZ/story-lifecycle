# Simple Command Layer 设计

本文定义 `story-lifecycle` 的简单命令入口层。

背景判断：`story-lifecycle` 的内核天然复杂，因为它管理的是完整研发工作流：

```text
需求 -> 理解 -> 计划 -> 编码 -> 验证 -> 复盘 -> 知识沉淀
```

它不像 CodeGraph 只处理：

```text
代码 -> 索引 -> 查询
```

所以目标不是让内核简单，而是让用户入口简单。复杂能力可以保留在底层协议、run 状态、artifact registry、knowledge pack、context builder 和 flywheel 里；用户日常只需要少量好记命令。

## 核心结论

`story-lifecycle` 应该增加一层 Simple Command Layer。

```text
用户看到：
  story init
  story start
  story status
  story ask
  story next
  story done
  story scan

系统内部：
  artifact registry
  init-knowledge
  scenario review
  scenario scan
  context builder
  planner/reviewer/executor
  quality flywheel
  knowledge sync
```

这层不是替换现有命令，而是给复杂命令提供更低心智成本的别名、组合和引导。

## Benchmark 视角

Simple Command Layer 不应该只对标 CodeGraph。CodeGraph 只代表“代码索引 / 上下文查询”这一类工具，而 `story-lifecycle` 同时覆盖需求推进、项目知识、质量飞轮、AI 工作流和未来远端 skill 平台。

因此 09 的设计需要持续参考多类开源项目：

| 类别 | 代表项目 | 主要学习点 | 对 `story-lifecycle` 的启发 |
| --- | --- | --- | --- |
| 代码索引 / Code Context | CodeGraph, CodeRLM | `query`、`context`、`callers`、`impact` 这类短命令；用结构化索引减少 agent 盲搜。 | `story ask`、`story scan`、`story impact` 应该像代码索引工具一样直接。 |
| 项目记忆 / Memory | MemCode, OpenZero, Agent Memory System | 本地持久记忆、自动 checkpoint、跨会话恢复上下文。 | 数据飞轮不能全靠用户手动总结，要支持被动增长和自动沉淀。 |
| Agent 工作流 CLI | Weft, Nika | 固定流程、人工确认、每步产物可审计。 | `story start/next/done/review` 要强调流程可重复、可审计、人控。 |
| 多 Agent 编排 / Worktree | Ruah, Optio | worktree 隔离、任务 scope、队列、状态机、冲突控制。 | 后续多 agent 并行时，要先有任务边界和隔离机制。 |
| 协议 / 编辑器入口 | ECA | protocol-first、rules、skills、commands、hooks。 | 本地 skill、MCP、ys-agent 需要走协议化扩展，而不是绑死某个客户端。 |
| 代码搜索 / IDE 级索引 | Sourcegraph, OpenGrok, LSP-based tools | 大型代码库检索、跨仓、符号导航。 | `story knowledge find/context` 要尊重已有索引工具，不重复造底层搜索轮子。 |

当前已知参考链接：

- CodeGraph: https://github.com/colbymchenry/codegraph
- CodeRLM: https://getreinforcement.com/
- MemCode: https://www.memcode.pro/
- Ruah: https://www.ruah.sh/
- Optio: https://optio.host/
- ECA: https://eca.dev/
- Weft: https://weftcli.com/

这张表不是最终结论。后续需要让其他 AI 继续补充开源项目、验证 star / 活跃度 / 文档质量 / 架构可借鉴性，再反向修正本设计。

## 设计原则

### 1. 常用动作短命令

用户每天高频使用的命令必须短。

推荐：

```bash
story status
story next
story ask "提现影响哪些服务"
story done
```

不推荐让用户日常记：

```bash
story project context compose --scenario order.withdraw --stage implement
story project index-assets --refresh --include-registry
```

### 2. 复杂概念后台化

用户不需要先理解这些概念：

```text
Artifact Registry
Knowledge Pack
Context Builder
Scenario Knowledge Layer
Quality Flywheel
KnowledgeRunState
```

这些概念应该在系统内部工作。CLI 可以在必要时展示摘要，但不能把它们作为首次使用门槛。

### 3. 渐进展开

简单命令先给结果，再告诉用户如果想深入可以用什么专业命令。

示例：

```text
story ask "提现场景"

Answer built from:
  - scenario: order.withdraw
  - services: hc-order, hc-limit, hc-user
  - confidence: medium

More:
  story scenario scan order.withdraw
  story knowledge context order.withdraw --verbose
```

### 4. 不破坏底层协议

Simple Command Layer 只做路由、组合、默认值和解释，不重写内核。

```text
simple command
  -> existing command / service
  -> existing artifacts
  -> existing run state
```

### 5. Local-first

简单入口先服务本地。远端 `ys-agent`、公司级 skill 平台和远程 MCP 是未来增强，不能成为简单入口的前置条件。

## 命令分层

### Layer 1：用户日常入口

面向普通开发、测试、产品同学。

```bash
story init
story start <story-id>
story status
story ask "<question>"
story next
story done
story scan <scenario-or-keyword>
```

特点：

- 命令短。
- 参数少。
- 默认值明确。
- 输出下一步建议。
- 失败时给可执行修复命令。

### Layer 2：专业入口

面向熟悉系统的开发、测试负责人、AI 编排者。

```bash
story knowledge init
story knowledge status
story knowledge find <keyword>
story knowledge context "<task>"
story scenario review
story scenario scan <scenario-id>
story test run
story review
```

特点：

- 仍然人可用。
- 显式区分 knowledge、scenario、test、review。
- 可作为 Layer 1 的展开命令。

### Layer 3：系统入口

面向自动化、agent、CI、未来 `ys-agent` 集成。

```bash
story project init-knowledge
story project index-assets
story project context compose
story project scenario scan <scenario-id>
story project knowledge lint
story project knowledge merge <run-id>
story quality seed
story flywheel sync
```

特点：

- 保留完整参数。
- 产物协议稳定。
- 支持非交互执行。
- 适合被 Simple Command Layer 调用。

## 核心命令设计

### story init

用途：初始化项目知识和工作区。

内部映射：

```text
story init
  -> story project init-knowledge
```

默认行为：

- 检测项目结构。
- 展示扫描概览。
- 推荐 P0 scope。
- 用户确认后生成 `.story/knowledge`。
- 输出下一步建议。

示例：

```bash
story init
```

输出：

```text
Project knowledge initialized.

Next:
  story scenario review
  story scan order.withdraw
  story ask "提现影响哪些服务"
```

### story start

用途：开始或进入一个需求。

内部映射：

```text
story start TAPD-1001234
  -> story new / story enter
  -> context builder
  -> planner
```

行为：

- 如果 story 不存在，创建。
- 如果 story 已存在，进入。
- 如果有 PRD/spec，关联 Artifact Registry。
- 自动尝试构建初始 context packet。

示例：

```bash
story start TAPD-1001234
```

### story status

用途：查看当前工作状态。

内部映射：

```text
story status
  -> current story state
  -> current stage
  -> knowledge status
  -> pending runs
```

输出必须短：

```text
Current story: TAPD-1001234
Stage: planning
Knowledge: available, 2 stale scenarios
Next: story next
```

详细模式：

```bash
story status --verbose
```

### story ask

用途：问项目、需求、业务场景、影响范围。

内部映射：

```text
story ask "<question>"
  -> describe_project_knowledge
  -> search_project_knowledge
  -> expand_project_context
  -> compose context
  -> answer
```

示例：

```bash
story ask "提现影响哪些服务和表"
```

输出要求：

- 先给结论。
- 给 source_refs。
- 标注 confidence。
- 给下一步建议。

示例输出：

```text
提现大概率涉及：
  services: hc-order, hc-limit, hc-user, hc-third-party
  tables: t_loan_order, t_loan_sub_order
  mq: happyCash / RISK_ORDER

Confidence: medium
Reason: based on scenario candidate + codegraph facts

Next:
  story scan order.withdraw
```

### story scan

用途：对业务场景或关键词做深扫。

内部映射：

```text
story scan order.withdraw
  -> story project scenario scan order.withdraw

story scan "提现"
  -> find candidate scenario
  -> ask user to confirm
  -> scenario scan
```

行为：

- 如果参数是已确认 scenario id，直接 scan。
- 如果参数是关键词，先搜索候选场景。
- 如果没有匹配，提示 `story scenario add`。

示例：

```bash
story scan order.withdraw
story scan "提现"
```

### story next

用途：让系统告诉用户下一步该做什么，并可执行。

内部映射：

```text
story next
  -> current story state
  -> gate decision
  -> planner recommendation
```

输出：

```text
Recommended next step:
  Write implementation plan for TAPD-1001234.

Run:
  story plan
```

如果可以自动执行，询问用户确认。

### story done

用途：完成当前阶段。

内部映射：

```text
story done
  -> mark current stage done
  -> write done artifact
  -> update artifact registry
  -> maybe update quality/knowledge flywheel
```

行为：

- 自动记录 stage 输出。
- 如果发现可沉淀 finding/pattern，进入 pending review。
- 输出下一步。

## Knowledge 简单入口

为了借鉴 CodeGraph 的易用性，Project Intelligence 可以提供一组更直接的 knowledge 命令。

```bash
story knowledge init
story knowledge status
story knowledge find withdraw
story knowledge context "提现场景"
story knowledge scan order.withdraw
story knowledge sync
```

映射关系：

| 简单命令 | 内部命令 |
| --- | --- |
| `story knowledge init` | `story project init-knowledge` |
| `story knowledge status` | read manifest, runs, stale summary |
| `story knowledge find <keyword>` | search project knowledge |
| `story knowledge context "<task>"` | describe + search + expand + compose |
| `story knowledge scan <scenario>` | `story project scenario scan <scenario>` |
| `story knowledge sync` | `sync-knowledge` |

## Scenario 简单入口

```bash
story scenario review
story scenario add order.withdraw
story scenario scan order.withdraw
story scenario status order.withdraw
```

这些命令比 `story project scenarios review` 更短，但可以继续调用现有底层实现。

## 输出体验规范

### 1. 先结论，后细节

不要一上来输出内部过程。

推荐：

```text
Found 3 likely scenarios for "提现":
  1. order.withdraw
  2. audit.withdraw-review
  3. user.mgm-withdrawal
```

再展示来源和下一步。

### 2. 每次输出下一步

每个命令最后都应该有 `Next`。

```text
Next:
  story scan order.withdraw
```

### 3. 错误要给修复命令

错误不能只说失败。

```text
No knowledge pack found.

Run:
  story init
```

### 4. 默认短输出，支持 verbose

```bash
story status
story status --verbose
```

### 5. 显示 confidence 和 source_refs

涉及业务、影响范围、风险判断时必须显示可信度和证据。

```text
Confidence: medium
Sources:
  hc-order/.../LoanOrderController.java:36
  .story/knowledge/indexes/by-scenario/order.withdraw.json
```

## 与开源项目的关系

`story-lifecycle` 不应该对标某一个工具，而应该吸收不同工具的强项。

### CodeGraph / CodeRLM

可吸收：

- 短命令入口：`query`、`context`、`callers`、`impact`。
- 精确上下文：按 symbol、caller、callee、file scope 找证据。
- 让 agent 少扫文件，多查索引。

不能照抄：

- 不能把 code index 当知识本体。
- 不能把业务场景等同于代码调用图。

对 Simple Command Layer 的影响：

```bash
story ask "提现影响哪些服务"
story scan order.withdraw
story impact "LoanOrderMapper"
```

### MemCode / OpenZero / Agent Memory

可吸收：

- 本地持久记忆。
- commit、阶段完成、review 后自动 checkpoint。
- 跨会话恢复上下文。

不能照抄：

- 不能把所有历史记忆无差别注入 prompt。
- 不能让 memory 绕过 evidence-first 和 review gate。

对 Simple Command Layer 的影响：

```bash
story memory status
story memory recall "为什么提现要查风控"
```

P0 可以不实现 `story memory`，但 `story done` 应预留自动沉淀资产的接口。

### Weft / Nika

可吸收：

- 固定流程。
- 人工 review。
- 每步产物可审计。
- 不自动 merge。

不能照抄：

- 不要把 `story-lifecycle` 变成单纯的“AI 生成代码流水线”。
- 不要让 agent 自动推进高风险阶段。

对 Simple Command Layer 的影响：

```bash
story start TAPD-1001234
story next
story review
story done
```

### Ruah / Optio

可吸收：

- task lifecycle。
- worktree isolation。
- 文件 scope / ownership。
- 多 agent 队列和冲突控制。

不能照抄：

- P0 不做复杂多 agent 平台。
- 不把 Kubernetes / pod / 远端执行作为本地前置条件。

对 Simple Command Layer 的影响：

```bash
story task status
story task claim hc-order/**
story task split
```

这些是 P2 之后能力，不进入 P0。

### ECA

可吸收：

- protocol-first。
- rules、skills、commands、hooks 的清晰分层。
- 跨编辑器 / 跨 agent 统一体验。

不能照抄：

- 不把 `story-lifecycle` 做成编辑器插件本体。
- 不依赖单一 assistant 客户端。

对 Simple Command Layer 的影响：

```bash
story hooks status
story skills list
story commands list
```

P0 只需要保留协议化扩展边界。

## P0 范围

P0 只实现最有价值的简单入口：

```bash
story init
story status
story ask "<question>"
story scan <scenario-or-keyword>
story knowledge status
story knowledge find <keyword>
```

P0 不做：

- 不重构底层状态机。
- 不重写现有 project 命令。
- 不接远端 `ys-agent`。
- 不做复杂 TUI。
- 不做完整自然语言命令解析。

P0 还需要完成一次外部 benchmark review。实现前不要求 benchmark 完美，但至少要让其他 AI 对本设计做一次对标审阅，避免只从 CodeGraph 单点学习。

## P1 范围

P1 增加：

```bash
story start <story-id>
story next
story done
story scenario review
story scenario scan <scenario-id>
story knowledge context "<task>"
story impact "<symbol-or-scenario>"
```

P1 开始把需求执行、知识注入和质量飞轮串得更紧。

## P2 范围

P2 增加：

- 更完整的自然语言意图识别。
- 多项目 profile。
- company skill / `ys-agent` 远端桥接。
- 统一 dashboard。
- 自动化建议和定期巡检。

## 给其他 AI 的审阅任务

在实现 Simple Command Layer 前，安排一个独立 AI 对本文做 benchmark review。

审阅目标：

```text
不要只评价本文写得好不好。
要主动寻找更多开源项目，比较它们的命令入口、状态模型、记忆机制、工作流设计和扩展协议。
然后反向修正 story-lifecycle 的简单命令层。
```

建议 prompt：

```text
你是一个 AI coding workflow / developer tool 产品架构审阅者。

请审阅 docs/project-intelligence/09-simple-command-layer-design.md。

要求：
1. 上网查找更多开源项目，不限于 CodeGraph。
2. 至少覆盖这些类别：
   - code index / code context
   - coding agent memory
   - AI workflow CLI
   - multi-agent orchestration
   - task lifecycle / worktree isolation
   - editor/protocol/skill systems
3. 对每个项目给出：
   - 项目名和链接
   - 核心命令或核心交互
   - star / 活跃度 / 文档成熟度的简要判断
   - story-lifecycle 可以吸收什么
   - 不应该照抄什么
4. 重点评估本文的 P0/P1/P2 是否合理。
5. 给本文打分，指出最重要的 5 个修改建议。
6. 如果你认为需要新增命令，请说明它映射到底层哪个已有能力。
7. 不要建议一上来做重型平台、GraphDB、向量库或完整 TUI，除非能证明 P0 必须要。
```

期望输出：

```text
1. Benchmark matrix
2. Findings
3. Recommended changes to 09
4. P0/P1/P2 priority adjustment
5. Score and rationale
```

审阅通过标准：

- 至少新增 5 个本文未列出的开源项目。
- 能区分“可吸收能力”和“不能照抄能力”。
- 能把建议落到具体命令或具体输出体验。
- 能指出哪些建议属于 P0，哪些应该推迟。

## 验收标准

P0 完成标准：

- 用户可以通过 `story init` 初始化知识包。
- 用户可以通过 `story status` 看当前 story/knowledge 状态。
- 用户可以通过 `story ask "..."` 获取项目知识回答。
- 用户可以通过 `story scan order.withdraw` 触发场景深扫。
- 简单命令内部复用现有底层命令，不复制核心逻辑。
- 所有输出都有清晰 Next。
- 没有 knowledge pack 时，命令会提示 `story init`。
- 已完成一次外部开源项目 benchmark review，并把必要修改吸收到 09。

P1 完成标准：

- `story start` 能创建或进入需求。
- `story next` 能基于状态给出下一步。
- `story done` 能完成当前阶段并更新资产。
- `story scenario review` 能生成或确认业务场景。
- `story knowledge context` 能输出 context packet。

## 实现建议

不要一上来做复杂智能路由。

推荐顺序：

1. 建立 simple command alias 层。
2. 把 `story init` 映射到 `project init-knowledge`。
3. 把 `story knowledge status/find` 做成薄封装。
4. 把 `story ask` 映射到 Context Builder。
5. 把 `story scan` 映射到 scenario scan。
6. 再接 `story start/next/done`。

Simple Command Layer 的价值在于降低入口心智，不在于替代现有架构。

## 与其他文档的关系

- `03-bootstrap-design.md` 定义知识包。
- `07-scenario-knowledge-workflow-design.md` 定义场景知识层。
- `08-init-knowledge-interaction-design.md` 定义 `init-knowledge` 的交互。
- 本文定义更上层的简单命令入口，让复杂能力可以被普通用户自然使用。
