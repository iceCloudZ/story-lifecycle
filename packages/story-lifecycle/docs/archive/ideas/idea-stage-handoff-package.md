> ⚠️ **历史快照（归档于 2026-07）**：描述的架构可能已被后续演进取代。当前架构见 [../../ARCHITECTURE.md](../../ARCHITECTURE.md)。本文件保留作决策记录（ADR），正文未修改。

---

# Idea: Stage Handoff Package

日期：2026-05-24

## 背景

Story Lifecycle Manager 的核心目标，是把一个 story 拆成多个阶段，让不同 AI coding assistant 按阶段推进工作。当前系统已经有 profile、prompt template、graph state、`.story-done` 握手机制和 review/router 流程：

- profile 定义阶段顺序，例如 `design -> implement -> test`。
- prompt template 为每个 stage 提供通用指令。
- graph 负责调度 stage、等待 `.story-done/{story_key}/{stage}.json`、执行 review/router/advance。
- AI CLI 通过 terminal/session 运行，完成后写 `.done` 作为阶段完成信号。

这套机制能跑通基本流程，但在最近排查 Windows + Zellij + TUI 的问题时，暴露出一个更深层的设计问题：**相邻阶段之间缺少明确、结构化、可审查的交接契约**。

以当前 terminal lifecycle 调试为例：

1. 我们先排查出 `e`、`r`、创建 story、`.done`、Zellij session、Claude 启动和 prompt 注入之间的职责混乱。
2. 继续讨论后，形成了 `n/r/e/.done/session` 的状态决策表。
3. 又通过外部 AI 评审，补充了 `DONE_CORRUPTED`、`STORY_FINISHED`、纯决策函数、副作用边界等设计约束。
4. 这些内容最终沉淀成 `docs/design-terminal-entry-lifecycle.md`，可以作为后续 code LLM 实现的明确输入。

这个过程说明：当一个阶段完成后，真正对下游有价值的，不只是“我完成了”这个 `.done` JSON，也不只是固定 prompt 模板，而是一份面向下游阶段的交接包：

- 当前问题的背景是什么？
- 哪些事实已经确认？
- 哪些边界不能越过？
- 哪些文件/模块应该改？
- 哪些状态必须覆盖？
- 哪些测试和验证命令必须跑？
- 哪些决策来自用户确认，哪些只是推测？

如果这些内容只散落在对话上下文、prompt 模板或 `.done.summary` 里，下游 code LLM 很容易重新猜语义、漏掉约束、重复踩坑。

## 当前问题

### 1. Prompt template 太通用

当前 `prompts/` 下的 stage prompt 更像“协议模板”，它能告诉 AI：

- 当前是什么阶段。
- 完成后要写 `.done`。
- 大致要输出什么字段。

但它不擅长表达当前 story 的具体上下文和阶段间约束。比如 terminal lifecycle 这类任务，真正关键的是：

- `e` 必须是观察入口，不是执行入口。
- `.done` 优先级高于 running/session 状态。
- Windows + Zellij 下不能后台 create 执行用 session。
- 损坏 `.done` 不能触发重启 AI。
- resolver/decision/TUI handler 必须分离副作用边界。

这些内容不适合硬编码进所有通用 prompt，也不应该依赖 code LLM 从历史对话里自行恢复。

### 2. `.done` 只表达完成结果，不表达下游执行契约

`.done` 是很好的完成信号，但它偏向结果：

```json
{
  "status": "success",
  "summary": "...",
  "changed_files": [],
  "tests": []
}
```

它不适合承载完整的下游执行说明。即使扩展 `.done` 字段，也会遇到两个问题：

- JSON 不适合写长篇背景、权衡、状态表和测试说明。
- `.done` 会被 graph 消费并删除，不适合作为长期可审查的交接文档。

### 3. 下游 LLM 容易重复理解和重复决策

如果 `design` 阶段只在 `.done.summary` 里说“已完成设计”，`implement` 阶段仍然要重新读取 PRD、推断架构、判断风险、猜哪些建议已经被用户确认。

这会导致：

- 设计阶段的推理价值没有被充分复用。
- Code LLM 可能绕过已确认的设计决策。
- Review LLM 无法精确判断实现是否满足上游设计，只能做泛泛代码审查。
- 多轮 retry 时，问题清单和禁止改动项容易丢失。

### 4. 不是所有阶段都需要同样重的交接

如果每个 stage 都强制生成长文档，会带来额外成本：

- 小任务会显得笨重。
- 文档可能退化成模板噪音。
- LLM 生成和读取成本增加。

因此需要一个机制，让系统能在“明确需要生成”和“可以跳过”之间取得平衡，并且这个判断应基于语义而不是静态规则。

## 核心想法

引入 **Stage Handoff Package**：任意两个相邻 stage 之间，如果上游产物会显著影响下游执行，上游 stage 可以生成一份结构化交接包，作为下游 stage 的主要输入之一。

它不是替代 `.done`，而是补充 `.done`：

- `.done`：机器可读的完成信号，驱动 graph 状态推进。
- handoff package：人和 LLM 都可读的阶段交接契约，驱动下游执行质量。

它也不是替代 prompt template：

- prompt template 继续保留协议层要求，例如读取 handoff、执行任务、写 `.done`。
- handoff package 承载当前 story 的具体背景、设计决策、实施边界和测试要求。

## 适用范围

Stage Handoff Package 不限于 `design -> implement`，可以用于任意相邻阶段。

示例：

| 上游 -> 下游 | Handoff 类型 | 主要内容 |
| --- | --- | --- |
| `idea -> design` | requirement_brief | 用户意图、已确认需求、非目标、待澄清点 |
| `design -> implement` | execution_brief | 设计决策、状态规则、修改范围、测试要求 |
| `implement -> test` | test_brief | 变更摘要、风险点、测试重点、手动验证步骤 |
| `test -> review` | review_brief | 测试结果、跳过项、残余风险 |
| `review -> retry` | fix_brief | 必修问题、禁止改动项、验收条件 |
| `parent -> sub-story` | delegation_brief | 子任务目标、依赖、边界、完成条件 |

## 是否由编排 LLM 判断

建议由 **编排 LLM 在 stage 边界做语义判断**：当前上游产物是否需要生成 handoff package，生成给哪个下游 stage，使用什么类型，以及为什么。

规则不直接替代 LLM 判断业务语义。规则只做护栏：

- 校验 LLM 的决定是否自洽。
- 要求 LLM 对跳过 handoff 的风险给出解释。
- 在低置信度、用户明确要求、或输出字段缺失时阻止静默跳过。

### 判断触发点

handoff 判断应由编排 LLM 触发，发生在上游 stage 完成、graph 准备进入下游 stage 之前。

典型位置：

1. 上游 `.done` 被 graph 消费后。
2. router/advance 决定下一阶段之前或之后。
3. review 决定 retry 时。
4. parent story 拆 sub-story 时。

编排 LLM 的输入应包含：

- 当前 story 和 stage。
- 上游 stage 输出摘要。
- 即将进入的目标 stage。
- 当前 profile 的阶段定义。
- 已有 context、plan、review、finding、用户确认信息。
- 可能影响下游的风险信号。

### 风险信号

以下不是强制规则，而是编排 LLM 判断时必须显式考虑的风险信号：

- 下游阶段会修改代码。
- 上游阶段产出了设计、计划、评审结论或 retry 指令。
- 涉及多个文件、模块、服务或状态机。
- 涉及 `.done`、terminal/session、部署、数据迁移、权限、外部系统等高风险逻辑。
- review 要求 retry。
- parent story 拆分 sub-story。
- 用户明确要求生成交接文档。

### 决策输出

编排 LLM 应输出结构化决策：

```json
{
  "handoff_decision": "create",
  "handoff_type": "execution_brief",
  "target_stage": "implement",
  "reasoning": "下游实现需要遵守 n/r/e/.done/session 的状态语义，且已有用户确认的设计决策需要传递。",
  "risk_if_skipped": "code LLM 可能把 e 重新实现成执行入口，或忽略 .done 优先级。",
  "confidence": "high",
  "sections": [
    "goal",
    "confirmed_decisions",
    "state_rules",
    "tests_required",
    "verification"
  ]
}
```

跳过时也必须解释：

```json
{
  "handoff_decision": "skip",
  "handoff_type": "none",
  "target_stage": "test",
  "reasoning": "上游只修改了文案且下游测试阶段已有固定检查，不需要额外交接包。",
  "risk_if_skipped": "低；下游可直接依据变更文件和固定测试协议执行。",
  "confidence": "medium",
  "sections": []
}
```

### Policy Validation

policy validation 不决定是否生成 handoff，只校验 LLM 决策是否可接受：

- 必须有 `handoff_decision`、`reasoning`、`risk_if_skipped`、`confidence`。
- 如果 `handoff_decision=create`，必须有 `handoff_type`、`target_stage`、`sections`。
- 如果 `handoff_decision=skip` 且 `confidence=low`，进入人工确认或默认要求生成 handoff。
- 如果用户明确要求生成 handoff，LLM 不能 skip。
- 如果 LLM 决策字段缺失或理由空泛，要求重新判断。

## Handoff Package 建议结构

不同 handoff 类型可以有不同字段，但建议使用 **YAML Frontmatter + Markdown 正文** 的混合格式：

- YAML Frontmatter 承载机器可校验的硬约束、类型、目标 stage、必须字段和禁止项。
- Markdown 正文承载背景、推理、上下文和给下游 LLM 的说明。

示例：

```md
---
type: execution_brief
source_stage: design
target_stage: implement
story_key: "1065520"
decision:
  handoff_decision: create
  reasoning: "实现阶段需要遵守 n/r/e/.done/session 的状态语义。"
  risk_if_skipped: "Code LLM 可能把 e 重新实现成执行入口。"
  confidence: high
constraints:
  - "e 必须是观察入口，不能启动 AI 或消费 .done"
  - ".done 优先级高于 running/session 状态"
must_not_change:
  - "不要引入无关 UI/样式改动"
done_contract:
  must_include:
    - changed_files
    - tests
    - handoff_deviations
---

# Stage Handoff: 1065520 / design -> implement

## Goal
...
```

共享正文结构：

```md
# Stage Handoff: <story_key> / <source_stage> -> <target_stage>

## Goal
下游阶段要完成什么。

## Background
为什么需要做这件事，已确认的上下文是什么。

## Confirmed Decisions
用户或上游阶段已经确认的设计决策。

## Constraints
不能违反的边界、非目标、环境限制。

## Required Behavior
下游必须实现或验证的行为。

## State Rules
状态机、文件协议、异常组合、优先级规则。

## Suggested Implementation
建议修改的模块、函数、接口或数据结构。

## Tests Required
必须覆盖的测试场景。

## Verification Commands
需要执行的命令。

## Done Contract
下游完成后 `.done` 应包含哪些字段。

## Open Questions
仍需用户或后续阶段确认的问题。
```

对于 `review -> retry`，结构可以更偏向问题清单：

```md
## Must Fix
必须修复的问题。

## Must Not Change
禁止顺手改动的范围。

## Acceptance Criteria
重试完成的验收标准。
```

## `.done` 中的引用方式

上游 stage 完成时，可以在 `.done` 里引用 handoff 文件，而不是把全文塞进 JSON。

示例：

```json
{
  "status": "success",
  "summary": "已完成 terminal entry lifecycle 设计",
  "handoff": {
    "created": true,
    "path": "docs/story-briefs/1065520/design-to-implement.md",
    "type": "execution_brief",
    "source_stage": "design",
    "target_stage": "implement"
  }
}
```

Graph 消费 `.done` 后，将 handoff path 写入 story context。下游 stage 渲染 prompt 时读取该 handoff，并把它作为主要上下文之一。

下游 stage 如果偏离 handoff，必须在 `.done` 中显式声明：

```json
{
  "status": "success",
  "summary": "...",
  "handoff_deviations": [
    {
      "section": "Constraints",
      "item": "不要改动 TUI 样式",
      "reason": "测试无法通过，必须调整一个状态提示文案",
      "approved": false
    }
  ]
}
```

Review stage 应优先审查 `handoff_deviations`：

- 偏离是否真实必要。
- 是否违反用户已确认决策。
- 是否需要人工确认。
- 是否影响验收标准。

## Profile 配置建议

可以在 profile 中加入 handoff 配置，但它不应替代编排 LLM 的语义判断。profile 只提供默认偏好、可用类型和校验要求。

```yaml
handoff:
  mode: auto  # never | auto | required
  defaults:
    design->implement:
      type: execution_brief
      preferred_sections: [goal, confirmed_decisions, state_rules, tests_required]
    implement->test:
      type: test_brief
      preferred_sections: [changed_behavior, risk_points, verification]
    review->retry:
      type: fix_brief
      preferred_sections: [must_fix, must_not_change, acceptance_criteria]
  validation:
    require_reasoning: true
    require_risk_if_skipped: true
    low_confidence: ask_user
```

含义：

- `never`：该 profile 不启用 handoff。
- `auto`：由编排 LLM 判断是否生成，policy validation 做护栏。
- `required`：用户或 profile 明确要求编排 LLM 生成；如果 LLM 认为不应生成，必须进入人工确认，不能静默 skip。

## 编排 LLM 的职责变化

引入 handoff 后，编排 LLM 不应该只是选择下一个 stage 或 provider。它还需要判断阶段间是否需要交接包。

建议新增职责：

1. 判断当前 stage 输出是否足够让下游直接执行。
2. 显式考虑风险信号，并说明跳过 handoff 的风险。
3. 判断是否生成 handoff。
4. 如果需要 handoff，选择 handoff 类型和章节。
5. 确保 handoff 中区分：
   - confirmed decisions
   - assumptions
   - open questions
   - non-goals
6. 在 `.done` 中记录 handoff path。

为降低延迟和成本，优先采用“一次调用完成判断与生成”的形式：编排 LLM 直接输出带 YAML Frontmatter 的 handoff 文档；如果决定 skip，则输出结构化 skip 决策即可，不再进行第二次生成调用。

可选优化：上游执行 LLM 在写 `.done` 前生成 handoff 初稿，编排 LLM 只负责审查、修剪和确认。这可以降低编排 LLM 从零生成 handoff 的成本，但不能让执行 LLM 取代编排 LLM 的 stage 边界判断职责。

## 下游 Stage Prompt 的变化

固定 prompt 不再承载大量业务细节，只保留协议层：

1. 读取当前 stage 输入。
2. 如果 context 中有 handoff package，必须先读取并遵守。
3. 不得违反 handoff 的 constraints/non-goals。
4. 执行当前 stage。
5. 写 `.done`。
6. 如有必要，为下游生成新的 handoff package。

这样 prompt template 更稳定，story-specific 内容由 handoff package 承载。

## 落地路径

### P0：文档协议和手动引用

- 定义 handoff package 文档格式。
- 允许 design stage 手动输出 `docs/story-briefs/{story_key}/...md`。
- `.done` 中记录 handoff path。
- 下游 prompt 读取 handoff path 并纳入执行上下文。
- 使用 `docs/design-terminal-entry-lifecycle.md` 作为第一个 execution_brief 原型，把其中的状态决策表、`DONE_CORRUPTED`、`STORY_FINISHED`、纯函数边界和 Windows + Zellij 约束填入 handoff 的 constraints/state rules/tests。
- 用同一个 terminal lifecycle 实现任务对比验证：有 handoff 时，code LLM 是否更少偏离 `e` 纯观察、`.done` 最高优先级和 Windows 禁止后台 create 的约束。

### P1：自动判断是否生成

- 在 profile 中加入 handoff 配置。
- 编排 LLM 在 stage 边界输出 `handoff_decision`、`handoff_type`、`target_stage`、`reasoning`、`risk_if_skipped`。
- policy validation 校验决策字段和置信度，但不替代 LLM 做语义判断。

### P2：审查和 retry 闭环

- review stage 针对失败项生成 `fix_brief`。
- retry stage 必须按 `fix_brief` 执行。
- review LLM 审查实现是否满足 handoff。
- 下游 `.done` 必须支持 `handoff_deviations`，用于声明无法遵守 handoff 的原因。
- 对同一个 story/stage 的 retry，新的 `fix_brief` 覆盖旧的 active fix brief；context 中只保留当前最新 fix brief，历史 brief 可归档但不作为下游默认输入。

### P3：质量和检索增强

- 将 handoff 存入 `.story-knowledge` 或可检索索引。
- 在后续相关 story 中召回相似 handoff。
- 统计 handoff 对 retry 次数、失败率和 review 质量的影响。

## 风险与约束

### 文档过重

小任务不应生成长 handoff。需要通过编排 LLM 的语义判断和 policy validation 控制生成频率。

### Handoff 过时

如果实现阶段偏离 handoff，必须在 `.done` 中说明偏离原因，并由 review 阶段检查。

### Handoff Drift

Handoff 可能因为实现阶段发现新事实而过时。系统不能假设 handoff 永远正确；下游必须通过 `handoff_deviations` 显式声明偏离，review 再判断偏离是否合理。

### LLM 生成模板噪音

Handoff 必须包含当前 story 的具体事实、决策和约束。空泛章节应被视为低质量输出。

### Retry Brief 膨胀

多轮 retry 如果不断追加 fix brief，会让下游 LLM 无法判断哪一份最新。应采用 active brief 覆盖策略：最新 fix brief 是唯一默认输入，旧 brief 只做审计归档。

### 状态推进和文档生成不能混淆

Handoff 是下游执行输入，不是状态推进信号。状态推进仍由 `.done` 和 graph 控制。

## 预期收益

- 下游 code/test/review LLM 不再重复理解上下文。
- 阶段间约束更明确，减少跑偏。
- Review 可以基于 handoff 做精确审查。
- Retry 能拿到具体修复包，而不是重新猜失败原因。
- 用户确认过的设计决策能稳定传递到实现阶段。
- Prompt template 可以更稳定、更薄，减少每个 stage 的硬编码指令。
