# Story Lifecycle AI Engineering Gap Roadmap

## 背景

当前 `story-lifecycle` 已经具备基础编排能力：

- story 多阶段流转：design / implement / test / review。
- headless e2e：用 fake runner 快速回归生命周期。
- quality flywheel：记录 finding、verification、learned pattern，并注入 Quality Packet。
- seed pipeline：从少量真实 story 中提炼初始质量经验。
- observability：记录 route decision、node error、prompt context、DoD check，提供 debug API。

这些能力已经覆盖“AI 工程流程”的骨架，但和当前人工实践相比，仍有若干缺口。人工实践中的真实流程是：

```text
需求 / Story
  -> 设计文档
  -> AI review
  -> 人工判断吸收
  -> AI 编码
  -> Codex code review
  -> 测试验证
  -> finding / pattern 沉淀
  -> 下一个 story 复用
```

本文件记录 8 个尚未完全产品化的能力点，并将它们拆成 3 条递进主线。目标不是一次做完，而是先把当前人工高质量流程里最费人的决策点产品化，再逐步增强恢复、交接和多 AI 调度。

## Roadmap Phases

### Phase 1: Review Feedback Intake Loop

目标：先把“AI review -> LLM 结构化提取 -> 人工判断 -> finding 沉淀”做成闭环。

覆盖能力：

- Review Feedback Intake
- Human Approval Queue
- End-To-End User Experience 中和 review/finding/approval 相关的命令

第一阶段只解决一个主线：

```text
review markdown/json
  -> LLM extract candidate findings
  -> dedupe / merge similar findings
  -> human approve/reject/defer
  -> write accepted finding
  -> optional generate fix task
```

规则解析只作为兜底和校验，不作为主路径。Review 解析是语义理解任务，Phase 1 明确使用 LLM 结构化提取，避免把 markdown review 降级为脆弱的 regex ETL。

不做 pattern 生成，不做 Automatic Learning From Execution，不做自动决策，不做多 reviewer 仲裁，不做大型 UI。

### Phase 2: Debug, Pattern, And Recovery Loop

目标：在 Phase 1 的 accepted / verified findings 基础上，推进 pattern 生成和自动学习；同时把“系统知道哪里坏了”推进到“系统能告诉人下一步怎么恢复”。

覆盖能力：

- Automatic Learning From Execution
- learned pattern proposal / approval / activation
- Failure Recovery Loop
- Story Debug CLI
- observability API 的 CLI 化
- DoR/DoD blocked、done file、planner/reviewer fallback 等高频失败的恢复建议

第二阶段包含两条主线：

```text
verified findings
  -> LLM propose learned patterns
  -> human approve / activate
  -> next story Quality Packet
```

```text
story stuck / failed / blocked
  -> story debug
  -> failure classification
  -> recommended next action
  -> human confirm retry / fix / defer
```

不做自动 activate pattern，不做全自动 retry policy；pattern 激活和恢复动作都必须保留人工确认入口。

### Phase 3: Artifact, Role, And Scheduling Contracts

目标：让多阶段、多 AI、多 artifact 的协作边界稳定下来。

覆盖能力：

- Artifact Contract And Handoff Packet
- AI Role Orchestration
- Multi-AI Scheduling Strategy
- End-To-End User Experience 中的 dashboard/final report

第三阶段主线：

```text
intake/design/review/implement/verify artifacts
  -> standardized handoff packet
  -> role contract per stage
  -> explicit AI strategy config
  -> later evolve to learned scheduling
```

这一阶段才稳定抽象 role 和 multi-AI strategy，避免在缺少真实使用数据时过早设计复杂调度。

## Capability Catalog

## 1. AI Role Orchestration

### 缺口

系统当前有 stage、tool、planner、reviewer，但没有把不同 AI 的职责显式建模。人工流程里已经存在多个角色：

- Designer：写设计文档。
- Design Reviewer：评审设计。
- Coder：实现代码。
- Code Reviewer：找 bug、风险、测试缺口。
- Verifier：跑测试并解释失败。
- Human Owner：决定吸收、驳回、降级或延期。

当前系统更像 `stage -> tool.execute()`，缺少 role contract。

### MVP

新增轻量 `agent_role` 概念，先不做复杂调度：

```yaml
roles:
  designer:
    allowed_actions: [write_design]
    output: design_doc
  coder:
    allowed_actions: [edit_code, run_tests]
    output: code_diff
  reviewer:
    allowed_actions: [review_only]
    output: review_findings
  verifier:
    allowed_actions: [run_verification]
    output: verification_result
```

每个 stage 绑定一个 role，并在 prompt/task file 中声明该角色的职责和禁止动作。

### P2

- 多 AI 自动选型。
- 双 reviewer。
- 角色质量评分。
- 成本、速度、成功率驱动的调度策略。

## 2. Review Feedback Intake

### 缺口

当前 AI review 意见主要靠人工复制、判断和吸收。系统虽然有 finding，但还缺一个正式入口把外部 review 转成结构化生命周期对象。

### MVP

新增 review feedback intake。主路径使用 LLM 将 review markdown/json 提取为结构化 candidate findings；规则 parser 只做 JSON 输入、显式列表等简单格式的兜底：

```text
review markdown / json
  -> LLM extract candidate findings JSON
  -> validate schema
  -> dedupe / merge similar findings
  -> human decision: accept / reject / downgrade / defer
  -> write finding
  -> generate fix task
```

建议 CLI：

```bash
story review-feedback import STORY-123 review.md
story review-feedback list STORY-123
story review-feedback decide finding-xxx --accept
story review-feedback decide finding-yyy --reject --reason "overclaimed"
```

### P2

- 多 reviewer 结果合并。
- 冲突检测。
- 自动识别 overclaim。
- 自动生成 follow-up implementation plan。

## 3. Human Approval Queue

### 缺口

Quality Flywheel 和 Seed Pipeline 都依赖人工确认，但目前确认动作分散在 JSON、API、CLI 中。缺少一个集中工作台承载 Human Owner 的核心决策。

### MVP

提供最小 approval queue：

```text
pending findings
pending review decisions
```

每个 item 显示：

- source story。
- evidence。
- severity / confidence。
- applies_to。
- current status。
- recommended action。

支持动作：

```text
approve
reject
edit
downgrade
defer
mark verified
```

### P2

- pending learned patterns / seed proposals 进入统一队列。
- activate pattern。
- TUI/Browser 工作台。
- 批量审批。
- 审批规则模板。
- 审批历史和责任人。

## 4. Artifact Contract And Handoff Packet

### 缺口

当前已有 `.story-done/{stage}.json` 和 `.story-context/`，但不同阶段之间的交接还不够强。人工流程中，交接物通常包括设计、review、实现、测试、验证、debug 结论。

### MVP

定义每个 stage 的标准 handoff packet：

```text
.story-context/{story_key}/
  intake.json
  design.md
  design_review.md
  implementation_plan.md
  code_review.md
  verification_result.json
  debug_report.json
  quality_packet.md
```

每个 stage 声明：

- required inputs。
- expected outputs。
- verification checks。
- next-stage handoff summary。

### P2

- artifact schema versioning。
- artifact diff。
- artifact completeness gate。
- 自动生成 story final report。

## 5. Failure Recovery Loop

### 缺口

Observability 能帮助定位失败，但定位后怎么恢复仍然主要靠人。系统需要从“能看见错误”进化到“能推荐下一步”。

### MVP

在 debug response 基础上生成 recovery recommendation：

```json
{
  "failure_type": "done_file_parse_error",
  "likely_cause": "AI wrote markdown-wrapped JSON",
  "recommended_action": "retry_with_stricter_done_file_prompt",
  "safe_to_retry": true
}
```

先覆盖高频失败：

- done file 缺失。
- done file JSON 解析失败。
- session dead。
- missing expected outputs。
- DoD blocked。
- planner/reviewer fallback。

### P2

- 自动 retry policy。
- 自动 split subtask。
- 自动 ask human。
- 防止同类错误无限重试。

## 6. Multi-AI Scheduling Strategy

### 缺口

当前 adapter/tool 能调用 AI，但缺少“哪个 AI 适合哪个任务”的策略。人工上已经在做：让一个 AI 编码，让另一个 AI review，让 Codex 做最终判断。

### MVP

引入简单 strategy 配置：

```yaml
ai_strategy:
  design:
    primary: codex
    reviewers: [claude]
  implement:
    primary: claude
    reviewers: [codex]
  review:
    primary: codex
  verify:
    primary: local
```

先只做显式配置，不做自动学习。

### P2

- 基于历史成功率选择 AI。
- 成本和耗时统计。
- 同任务多模型投票。
- reviewer 冲突仲裁。

## 7. Automatic Learning From Execution

### 缺口

Seed Pipeline 解决了“初始燃料”，但真正的飞轮应该从每次 story 执行中自然学习：

```text
review finding
  -> fix diff
  -> verification result
  -> recurrence check
  -> proposed learned pattern
  -> human approval
  -> active pattern
```

当前这些步骤还没有完全串起来。

### P2 MVP

在 story 完成时生成 learning candidates：

- 本次 high/medium findings。
- 被修复并验证的 finding。
- 重复出现的 category/module。
- 新增或修改的测试。
- 最终 verification_result。

主路径复用 Seed Pipeline 的 LLM Seed Analyst 能力：输入 verified finding、修复 diff、验证结果和 story context，由 LLM 提议 proposed learned patterns，并输出 confidence、applies_to、evidence。系统只做 schema 校验、去重和入队，不自动 active。

### P2 Enhancements

- 跨 story 聚类。
- pattern 置信度自动更新。
- stale pattern 检测。
- learned pattern 复发后自动降级。

## 8. End-To-End User Experience

### 缺口

能力已经分散存在于 CLI、API、文档和测试中，但缺少一个“操作主线”。真实使用者需要知道当前 story 到底卡在哪、下一步该做什么。

### MVP

提供几个高频命令：

```bash
story status STORY-123
story debug STORY-123
story findings STORY-123
story approvals
story verify STORY-123 --cmd "pytest"
story review-feedback import STORY-123 review.md
```

`story status` 应集中展示：

- current stage / status。
- last route decision。
- latest node error。
- open findings。
- pending approvals。
- last verification result。
- recommended next action。

### P2

- TUI dashboard。
- browser UI。
- notification。
- story final report。

## Phase 1 Plan: Review Feedback Intake Loop

### Scope

Phase 1 只做 review feedback 到 finding 的闭环，不同时推进 pattern 生成、Automatic Learning、debug、artifact、role 或 scheduling。

包含：

1. Review Feedback Intake
2. Human Approval Queue
3. `story findings` / `story approvals` / `story review-feedback` 等 CLI 主线
4. Reviewer prompt 最小角色约束：reviewer 只读不改，避免把错误职责混入 finding 数据

不包含：

- 自动替人接受 finding。
- pattern 生成、pattern activate 和 Quality Packet 新增注入逻辑。
- Automatic Learning From Execution。
- 自动修代码。
- 多 reviewer 合并。
- Web dashboard。
- 多 AI 调度。

### User Flow

```text
story review-feedback import STORY-123 review.md
  -> LLM 提取 candidate findings，并合并同类项

story approvals
  -> Human Owner 查看待处理 finding / review decision

story approvals decide finding-xxx --accept
  -> finding 写入质量飞轮
```

### Phase 1 Deliverables

| Deliverable | Description |
| --- | --- |
| Review import extractor | 使用 LLM 支持 markdown/json review 输入，输出结构化 candidate findings |
| Finding dedupe/merge | 在进入 queue 前合并同一 story 内语义重复的 findings |
| Approval queue | 统一展示 pending findings / review decisions，降低重复噪声 |
| Decision commands | accept / reject / downgrade / defer / mark verified |
| Reviewer role guardrail | Phase 1 prompt 中明确 reviewer 只读不改 |

### Phase 1 Success Criteria

1. 能导入一份 AI review，并由 LLM 生成待确认 candidate findings。
2. Human Owner 能在一个队列里 approve/reject/defer finding。
3. 同一 story 的重复 findings 会在进入队列前合并或聚合展示。
4. 被 accept 的 finding 会进入 quality flywheel。
5. 被 mark verified 的 finding 会记录验证证据和状态变更事件。
6. Phase 1 不生成 learned pattern，也不改变 Quality Packet 的 pattern 注入行为。

## Phase 2A Plan: Pattern And Automatic Learning Loop

### Scope

Phase 2A 在 Phase 1 的 accepted / verified findings 基础上，再推进 pattern 生成和自动学习闭环。

包含：

1. 从 verified findings、修复 diff、验证结果和 story context 生成 learning candidates。
2. 复用 Seed Pipeline 的 LLM Seed Analyst 提议 proposed learned patterns。
3. proposed patterns 进入人工审批队列。
4. Human Owner approve / activate pattern。
5. active patterns 被后续 story 的 Quality Packet 注入。

不包含：

- 自动 activate pattern。
- 自动修代码。
- 跨 story 聚类和 pattern 置信度自动更新。

### User Flow

```text
story completed with verified findings
  -> LLM proposes learned patterns
  -> Human Owner reviews proposed patterns
  -> approved / active pattern
  -> next story Quality Packet includes relevant active patterns
```

### Success Criteria

1. story 完成后能基于 verified finding、修复 diff 和验证结果生成 proposed learned patterns。
2. proposed pattern 默认不是 active，必须人工确认。
3. Human Owner 能 approve / activate pattern。
4. 下一个类似 story 的 Quality Packet 能看到相关 active pattern。

## Phase 2B Plan: Debug And Recovery Loop

### Scope

Phase 2B 把已有 observability API 产品化成 CLI，并为高频失败提供恢复建议。

包含：

1. `story status STORY-123`
2. `story debug STORY-123`
3. recovery recommendation
4. optional human-confirmed retry/fix/defer action

不包含：

- 全自动 retry。
- 自动拆 subtask。
- 自动改代码。
- 长期 tracing 平台。

### User Flow

```text
story status STORY-123
  -> 当前 stage/status、open finding、last error、pending approval

story debug STORY-123
  -> failure_type / likely_cause / recommended_action / safe_to_retry

story debug STORY-123 --apply retry
  -> 仅在人确认后执行恢复动作
```

### Phase 2B Success Criteria

1. 对 done file 缺失、JSON 解析失败、missing expected outputs、DoD blocked 等高频失败能给出分类。
2. 每个分类至少有一个 recommended action。
3. CLI 输出能直接说明“下一步该做什么”。
4. 恢复动作默认不自动执行，必须人工确认。
5. 无法分类的失败必须推荐 ask human，不允许强行建议 retry。

## Phase 3 Plan: Artifact, Role, And Scheduling Contracts

### Scope

Phase 3 稳定 artifact 交接和角色职责，再引入显式 AI strategy。

包含：

1. 标准 handoff packet
2. stage required inputs / expected outputs / verification checks
3. agent_role prompt contract
4. explicit `ai_strategy` 配置

不包含：

- 自动学习调度策略。
- 多模型投票。
- reviewer 冲突仲裁自动化。

### User Flow

```text
story design
  -> design.md + design_review.md + handoff summary

story implement
  -> 读取标准 handoff packet
  -> 按 coder role 执行

story review
  -> 按 reviewer role 只做 review，不改代码

profile.yaml
  -> 明确每个 stage 使用哪个 role / adapter / reviewer
```

### Phase 3 Success Criteria

1. 每个 stage 都能声明 role、required inputs、expected outputs。
2. 下游 stage 能只读 handoff packet 理解上游结论。
3. reviewer role 的 prompt 明确禁止改代码。
4. profile 能显式配置 design/implement/review/verify 的 AI strategy。

## Overall Success Criteria

三阶段全部完成后，系统应达到：

1. review 意见能结构化进入 finding/pattern 生命周期。
2. Human Owner 的关键判断集中在 approval queue。
3. story 卡住时，CLI 能给出可解释恢复建议。
4. AI 之间交接依赖标准 artifact，而不是隐式上下文。
5. 多 AI 使用策略可配置，并能为未来自动学习调度积累数据。

## Non-Goals

第一阶段不做：

- 自动替人做最终决策。
- pattern 生成和 Automatic Learning From Execution。
- 全自动多 AI 调度。
- 大型 Web 平台。
- 生产监控接入。
- embedding/RAG。
- 完整 tracing。

核心原则是：**先把当前人工高质量流程中的关键决策点产品化，再逐步自动化。**
