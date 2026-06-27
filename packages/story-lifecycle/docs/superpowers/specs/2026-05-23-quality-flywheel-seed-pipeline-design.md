# Quality Flywheel Seed Pipeline MVP Design

## Background

Quality Flywheel 已经具备本地闭环和 Prompt 注入能力：`finding` 可以记录当前质量问题，`learned_pattern` 可以承载沉淀规则，Quality Packet 可以把相关经验注入后续 story。

当前缺口不是“批量导入所有历史资产”，而是：飞轮还没有经过真实 story 验证，缺少一条可信的种子数据生成路径。如果第一版直接扫描 TAPD、PRD、ADR、Git、生产问题和测试报告，风险很高：

- 数据量大，调试困难。
- 自动生成的 finding/pattern 容易噪声过多。
- learned pattern 可能过度泛化，污染后续 Prompt。
- 很难判断 Quality Packet 是否真的帮助了新 story。

因此本设计将 MVP 收敛为：**手动选择 2-3 个真实 story，通过 LLM 辅助理解历史上下文，生成候选 findings 和 learned patterns，经人工确认后进入 Quality Flywheel，再用新 story 验证注入效果。**

## Design Goal

P0 目标不是做全量数据管线，而是跑通一条小而完整的闭环：

```text
Select 2-3 stories
  -> collect story artifacts
  -> LLM semantic analysis
  -> propose findings / learned patterns
  -> human review and approval
  -> write to story.db
  -> build Quality Packet
  -> run a new story with injected context
  -> observe whether the context changes planning/review behavior
```

成功标准：

- 能从 2-3 个真实 story 中提炼出少量高质量候选记录。
- LLM 输出包含证据、置信度、适用范围和建议动作，而不是只做关键词分类。
- 所有 learned patterns 默认进入 `proposed`，不能自动 `active`。
- 人工确认后，Quality Packet 能在后续 story 中注入相关上下文。
- 出现错误时，可以定位是上下文采集、LLM 理解、人工确认、写入、还是注入阶段的问题。

## Non-Goals

以下内容不进入 P0：

- 全量扫描 `D:/hc-all` 下所有历史文档。
- 自动拉取全部 TAPD story/bug。
- Git history 全量模式挖掘。
- release、slow SQL、incident、生产监控接入。
- embedding/RAG 语义检索。
- 定时同步或 webhook 增量同步。
- 自动 approve/activate learned pattern。

这些能力作为 P2 或后续增强项处理。

## MVP Input

P0 手动选择 2-3 个 story，建议覆盖三种类型：

| 类型 | 目的 | 示例材料 |
| --- | --- | --- |
| 普通需求 story | 验证 PRD/计划/实现经验是否能沉淀 | PRD、plan_design、review 记录 |
| bugfix story | 验证 finding 生命周期和验证证据 | bug 复盘、修复说明、测试结果 |
| 跨模块 story | 验证 applies_to 和 Quality Packet 相关性 | 多服务改动、接口/MQ/数据流说明 |

每个 story 的输入由人工提供一个 manifest 文件，避免 P0 自动猜测数据来源。

示例：

```yaml
story_key: STORY-1065520
title: 新客 7 天免息活动
type: requirement
source_root: D:/hc-all
artifacts:
  - path: prd/1065520.md
    type: prd
  - path: .story-context/1065520/plan_design.md
    type: plan
  - path: story-board/docs/stories/STORY-1065520/story.json
    type: story_record
known_outcomes:
  - "活动配置涉及 hc-marketing 和 hc-order"
  - "需要检查还款计划和优惠金额一致性"
```

## LLM Role

LLM 不是可选增强，而是 P0 的核心语义层。规则和文件扫描只负责收集材料，LLM 负责理解和决策建议。

LLM 输入：

- story 基本信息。
- 人工选择的 artifacts 摘要。
- 已知结果或复盘结论。
- 当前 Quality Flywheel schema。
- 输出格式约束。

LLM 输出必须是结构化 JSON：

```json
{
  "story_key": "STORY-1065520",
  "summary": "该 story 涉及新客免息活动配置、订单优惠金额和还款计划一致性。",
  "risk_tags": ["promotion", "repayment-plan", "cross-service"],
  "proposed_findings": [
    {
      "severity": "medium",
      "category": "field-propagation",
      "location": "prd/1065520.md",
      "description": "优惠金额字段需要在订单、还款计划和活动结算链路中保持一致。",
      "root_cause": "跨服务字段传递和计算口径容易不一致。",
      "recommendation": "实现时增加端到端校验，覆盖订单创建、还款计划生成和活动金额核对。",
      "evidence": ["prd/1065520.md#优惠规则", ".story-context/1065520/plan_design.md#数据流"],
      "confidence": "medium"
    }
  ],
  "proposed_patterns": [
    {
      "pattern": "促销金额跨服务链路必须做一致性校验",
      "applies_to": ["promotion", "repayment-plan", "hc-order", "hc-marketing"],
      "rule": "涉及优惠、减免、免息等金额类需求时，必须检查订单金额、还款计划、活动结算三处口径，并补充至少一个链路级测试。",
      "source_findings": [],
      "confidence": "medium",
      "evidence": ["prd/1065520.md", ".story-context/1065520/plan_design.md"]
    }
  ],
  "review_questions": [
    "该规则是否只适用于免息活动，还是所有促销金额需求？",
    "是否已有链路级测试可以作为验证证据？"
  ]
}
```

LLM 不能直接写入 active pattern。它只能生成候选项和 review questions。

## Human Review

人工确认是防止知识库毒化的硬门禁。

Review queue 展示每条候选记录：

- 原始 story。
- 候选 finding/pattern。
- evidence links。
- confidence。
- applies_to。
- LLM 提出的 review questions。

人工动作：

```text
approve finding       -> write finding, status=open or verified
approve pattern       -> write learned_pattern, status=proposed
edit and approve      -> save edited content
reject                -> record rejection reason, do not inject
```

规则：

- `learned_pattern` 只能写入 `proposed`。
- 是否 `approved -> active` 使用现有 pattern approval 流程。
- `applies_to` 必须窄，不允许 `all`、`backend` 这类过宽标签单独作为作用域。
- 每条 pattern 至少要有一个 evidence。
- LLM confidence 不能替代人工判断。

## Data Flow

```text
story manifest
  -> artifact loader
  -> context summarizer
  -> LLM seed analyst
  -> proposal JSON
  -> schema validator
  -> review queue
  -> human approval
  -> quality.record_finding / quality.propose_learned_pattern
  -> Quality Packet
```

### Artifact Loader

只读取 manifest 中声明的文件。P0 不递归扫描目录。

职责：

- 校验文件存在。
- 读取文本内容。
- 对大文件做长度限制和摘要。
- 保留 path/type，供 evidence 回链。

### Context Summarizer

将 artifact 压缩成 LLM 可消费的上下文：

- PRD：目标、验收标准、业务规则、影响模块。
- plan：实现路径、风险点、测试计划。
- story record：阶段、事件、验证结果。
- bug 复盘：现象、根因、修复、验证。

### LLM Seed Analyst

LLM 根据上下文生成候选结果。Prompt 必须强调：

- 只基于 evidence 输出。
- 不确定时降低 confidence。
- 不要生成泛化过度的 pattern。
- 不要把一次性业务结论伪装成通用规则。
- pattern rule 必须可执行、可检查。

### Schema Validator

写入 review queue 前做严格校验：

- severity 必须是 `high|medium|low`。
- category 必须在允许集合内，未知则为 `unknown` 并要求人工处理。
- evidence 不能为空。
- pattern 的 `applies_to` 至少 2 个标签，且不能全是宽泛标签。
- rule 长度有限制，避免 prompt 膨胀。

### Review Queue

P0 可以先用文件作为 review queue，不必做 UI：

```text
.story/quality-seed/proposals/STORY-1065520.json
.story/quality-seed/reviewed/STORY-1065520.json
```

CLI 提供最小操作：

```bash
story seed-quality analyze manifests/1065520.yaml --dry-run
story seed-quality apply .story/quality-seed/reviewed/STORY-1065520.json
```

`analyze` 只生成 proposal，不写数据库。`apply` 只写 reviewed 文件中被人工标记为 approved 的条目。

## Database Write Strategy

Finding 写入策略：

- 与当前 story 强相关且代表未闭环风险：`status=open`。
- 已在历史 story 中验证修复的问题：`status=verified`，并附带 verification evidence。
- 不确定的问题不写入 finding，只保留 proposal。

Pattern 写入策略：

- 只调用 `quality.propose_learned_pattern(...)`。
- 默认 `status=proposed`。
- 不调用 approve/activate。
- source_findings 可以为空，但 evidence 必须写入 metadata。

不使用 `BATCH-SEED` 作为默认容器 story。P0 所有记录必须关联真实 story_key。后续全量导入如果需要 batch 容器，必须单独设计隔离规则。

## Quality Packet Verification

P0 必须验证 seed 数据真的能被后续 story 使用。

选择一个新的测试 story，执行：

1. build Quality Packet。
2. 检查是否注入相关 learned patterns。
3. 检查是否没有注入无关 story 的噪声。
4. 运行 story lifecycle 到至少 design/implement 阶段。
5. 观察 Planner/Executor 输出是否引用了注入规则。
6. 记录一次 `quality_packet_generated` 或 `prompt_context` 事件，便于排障。

验收标准：

- Quality Packet 中最多出现 3 条 seed patterns。
- 每条 pattern 都能解释为什么与当前 story 相关。
- Planner 计划中能看到对应风险或检查项。
- 如果没有相关记录，Quality Packet 应保持为空或只显示 baseline，不应硬塞历史经验。

## CLI Scope

P0 CLI：

```bash
story seed-quality analyze <manifest.yaml> --dry-run
story seed-quality apply <reviewed-proposal.json>
story seed-quality preview-packet <story-key> --stage design
```

命令语义：

- `analyze`：读取 manifest，调用 LLM，生成 proposal 文件。
- `apply`：校验 reviewed proposal，只写 approved 条目。
- `preview-packet`：查看某个 story 当前会注入哪些 seed 数据。

P0 不提供：

- `--source-root D:/hc-all` 全量扫描。
- `--max-items 100` 批量导入。
- TAPD 自动分页拉取。
- Git analyzer。

## P0 Implementation Steps

1. 定义 manifest schema。
2. 实现 artifact loader，只读取 manifest 声明文件。
3. 实现 context summarizer。
4. 实现 LLM seed analyst，输出严格 JSON。
5. 实现 proposal schema validator。
6. 实现文件型 review queue。
7. 实现 `apply reviewed proposal`，写入 finding/pattern。
8. 实现 `preview-packet`。
9. 用 2-3 个真实 story 跑通闭环。

## Tests

P0 测试重点：

- manifest 缺文件时失败信息清晰。
- artifact loader 不会递归扫描未声明目录。
- LLM 输出缺 evidence 时被 validator 拒绝。
- pattern `applies_to=["backend"]` 这类过宽作用域被拒绝。
- `analyze --dry-run` 不写数据库。
- `apply` 只写 approved 条目。
- learned pattern 写入后状态仍是 `proposed`。
- `preview-packet` 只展示相关 pattern。

## P2 Enhancements

以下能力留到 P2：

- TAPD 全量 story/bug 拉取。
- PRD、ADR、Superpowers specs 全量扫描。
- Git history fix pattern 挖掘。
- journey test report 自动转 finding。
- release/slow SQL/incident/production signal 接入。
- batch seed 容器 story。
- 去重和跨来源 entity resolution。
- embedding/RAG 语义检索。
- 定时同步和 webhook。
- Review queue UI。

P2 的前提是 P0 已证明：少量人工选择 story 经过 LLM 提炼后，确实能改善后续 story 的计划、执行或 review 行为。

## Risks And Mitigations

### LLM Over-Generalization

风险：LLM 将一次性业务经验总结成过宽规则。

处理：

- 强制 evidence。
- 强制窄 `applies_to`。
- 默认 proposed。
- 人工审核后才能 active。

### Noisy Seed Data

风险：候选 findings/patterns 太多，Quality Packet 变成噪声。

处理：

- P0 每个 story 最多生成 5 条候选 findings、3 条候选 patterns。
- Quality Packet 每次最多注入 3 条 seed patterns。
- 不相关记录不注入。

### Hidden Automation

风险：脚本悄悄写入数据库，难以回滚。

处理：

- `analyze` 默认 dry-run。
- 只有 `apply reviewed-proposal.json` 会写库。
- 写库前输出 summary。
- 每条写入记录保留 source story 和 evidence。

### Missing Semantic Understanding

风险：管线退化成关键词 ETL。

处理：

- LLM Seed Analyst 是 P0 必选组件。
- 规则分类只能作为辅助信号。
- 最终 proposal 必须解释“为什么这是 finding/pattern”。

## Review Focus

请重点评审：

1. P0 是否足够小，能否用 2-3 个 story 快速跑通。
2. LLM 在语义理解和候选决策中的职责是否清晰。
3. 人工确认是否足以防止 learned pattern 毒化。
4. 不使用 `BATCH-SEED`，全部关联真实 story_key 是否合理。
5. `analyze -> review -> apply -> preview-packet` 是否能支撑端到端验证。
6. P2 边界是否清楚，是否避免了全量数据工程过早进入 MVP。
