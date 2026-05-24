# Evaluator-Optimizer 对抗循环设计

## 概述

Story Lifecycle 应支持针对规划阶段和实现审查阶段的对抗式 evaluator-optimizer 循环。

设计保持现有图结构不变：

```text
plan_stage -> execute_stage -> poll_completion -> review_stage -> router
```

循环在现有阶段边界内部实现，而不是添加大量新的 LangGraph 节点。规划循环是 `plan_stage_node` 内部的真循环（in-node loop）。P0 代码循环不是 `review_stage_node` 内部的 `while` 循环，而是跨节点的迭代重试（cross-node iterative retry）：`review_stage_node` 只负责审查和生成 repair packet，实际修复通过现有 router retry 路径重新进入 plan/execute 完成。

长期运行时策略：

- reviewer 每轮保持全新状态，上下文隔离
- implementer 尽量持久化，避免重复加载上下文
- 持久化会话失败时，降级为短生命周期的 repair packet

## 目标

- 在代码执行前捕获规划缺陷。
- 当用户测试基础设施不可用或不可靠时，提供代码质量兜底。
- 通过为 reviewer 提供全新上下文来保持其独立性。
- 在持久化会话可用时减少 implementer 的上下文浪费。
- 记录轮次级事件，以便衡量 token/时间浪费和收敛情况。
- 复用现有的 `finding`、`learned_pattern`、`quality.py`、`event_log`、`trajectory_score` 和 `.story-knowledge/` 基础设施。

## 非目标

- 不替代测试。确定性检查在可用时仍有价值。
- P0 不添加持久化 implementer 会话。
- P0 不重写 LangGraph 拓扑结构。
- P0 不向 `finding.status` 添加新的 finding 生命周期状态。
- 不要求每个项目都有可运行的测试命令。

## 架构

新增两个聚焦模块：

```text
src/story_lifecycle/orchestrator/evaluator_loop.py
src/story_lifecycle/orchestrator/loop_events.py
```

`evaluator_loop.py` 负责循环行为：

```text
run_plan_loop(state, stage_config) -> LoopResult
run_code_review_loop(state, stage_config, stage_output) -> LoopResult
build_repair_packet(state, findings, verification) -> str
detect_no_progress(previous_rounds, current_round) -> bool
```

`loop_events.py` 负责事件写入：

```text
log_loop_started(...)
log_loop_round(...)
log_loop_completed(...)
log_loop_fallback(...)
```

`nodes.py` 仍然是图的集成入口：

```text
plan_stage_node:
  if adversarial.plan_loop.enabled:
      result = run_plan_loop(...)
  else:
      plan = planner.plan_stage(...)

review_stage_node:
  if adversarial.code_loop.enabled:
      result = run_code_review_loop(...)
  else:
      review = planner.review_stage(...)
```

## Profile 配置

在 profile 中添加 `adversarial` 配置块：

```yaml
adversarial:
  enabled: true

  plan_loop:
    enabled: true
    stages: [design, implement]
    max_rounds: 3
    reviewer_model: deepseek-chat
    pass_condition: no_open_blocker_or_major

  code_loop:
    enabled: true
    mode: short_lived
    max_rounds: 3
    reviewer_model: deepseek-chat
    pass_condition: no_open_blocker
    fallback: repair_packet

  observability:
    log_round_events: true
    estimate_prompt_segments: true
```

P0 仅支持：

```yaml
code_loop:
  mode: short_lived
```

P1 可能添加：

```yaml
code_loop:
  mode: persistent
```

Reviewer model 解析顺序：

1. 优先使用 `adversarial.plan_loop.reviewer_model` 或 `adversarial.code_loop.reviewer_model`。
2. 未配置时使用当前 orchestrator LLM 配置（`STORY_LLM_MODEL` / setup config）。
3. P0 允许 reviewer 与 planner/executor 使用相同模型；如果 profile 显式配置不同 reviewer model，则优先使用不同模型降低同源偏差。

## P0 流程

### Plan Loop（规划循环）

`plan_stage_node` 调用 `run_plan_loop()`。

```text
planner 起草方案
  -> 全新 plan reviewer 评估
  -> 发现阻塞性问题时 planner 修订
  -> 在 pass、max_rounds、fail 或 no_progress 时停止
```

最终通过的方案写入现有的 `plan_{stage}.md` 文件，并按现有方式存储到 state 中。

### Code Loop（代码循环）

P0 代码循环是审查驱动的跨节点迭代重试，使用现有的 retry 路径。`review_stage_node` 不得在节点内部等待 implementer 反复修复；它每次只运行一轮全新 reviewer，记录结果，然后把控制权交还给 router。

```text
execute_stage_node
  -> 现有 implementer 执行
  -> poll_completion_node 读取 .story-done
  -> review_stage_node 运行全新 reviewer
  -> pass: advance
  -> revise: 记录 findings，构建 repair_packet，设置 last_error
  -> router retry
  -> 下一次 plan/execute 尝试接收 repair_packet 和未关闭的 findings
```

这仍然是代码循环，但实现修复通过现有 graph retry 机制完成，而非节点内的持久化会话。

因此 P0 中两个 loop 的语义不同：

- Plan Loop：in-node loop，planner 和 reviewer 在 `plan_stage_node` 内多轮收敛。
- Code Loop：cross-node iterative retry，reviewer 在 `review_stage_node` 内单轮审查，修复依赖 router retry 后重新执行阶段。

## P1 持久化 Implementer 流程

P1 引入持久化 implementer 会话。Reviewer 保持全新。

```text
启动 implementer 会话
  -> implementer 写入 implementation_ready 信号
  -> orchestrator 启动全新 reviewer
  -> reviewer 返回 pass 或 findings
  -> pass: orchestrator 写入 .story-done 并释放会话
  -> revise: orchestrator 将 findings 注入同一 implementer 会话
  -> implementer 修复后再次写入 implementation_ready
```

持久化模式使用独立的信号目录，因此 `.story-done` 保持其现有含义：阶段完成。

```text
.story-signal/{story_key}/{stage}/implementation_ready.json
.story-signal/{story_key}/{stage}/review_feedback.json
.story-signal/{story_key}/{stage}/continue_requested.json
```

如果持久化会话不可用，orchestrator 构建 repair packet 并降级到 P0 短生命周期 retry。

## Reviewer 上下文隔离

Reviewer 不得继承 implementer 的对话历史。

Reviewer 输入限制为：

- 原始 story 上下文
- 当前阶段的 plan
- 当前阶段的输出
- diff 摘要和相关文件片段
- 验证结果
- 历史 findings 和 implementer 回复
- Quality Packet 和已学习模式（learned patterns）

这保持了对抗独立性，降低了 reviewer 在无证据情况下接受 implementer 辩解的可能性。

## Repair Packet（修复包）

Repair packet 是短生命周期 retry 和持久化会话降级的恢复协议。

写入路径：

```text
.story-context/{story_key}/repair_{stage}_round{n}.md
```

内容：

- story 摘要
- 当前阶段的 plan
- 当前阶段的输出摘要
- 变更文件和 diff 摘要；不得包含 full diff
- 按严重程度分组的未关闭 findings
- Reviewer 要求的修改
- 已接受的风险（如有）
- 验证状态及不可用的原因
- 必须保留的决策
- 避免无关重构的指令

下一次 planner/executor 的 prompt 将此包与现有 Quality Packet 一起注入。

Repair packet 必须有 token 预算，但 P0 不应把正常重试压到过小上下文。默认预算分三档：

```text
target_budget: 4000 tokens
hard_budget: 20000 tokens
emergency_compact_budget: 6000 tokens
```

预算含义：

- `target_budget`：常规目标。1-3 个 finding、少量文件变更应尽量压在此范围内；P0 观测期可放宽，不因超过目标而裁剪。
- `hard_budget`：默认硬上限。P0 先设为 20000 tokens，优先保证修复上下文完整，运行一段时间后再根据 waste metrics 优化 token 消耗。
- `emergency_compact_budget`：只有在模型上下文不足、prompt 拼装失败、或 fallback 需要极限压缩时使用。

超过 `hard_budget` 时按以下优先级裁剪：

1. 保留阻塞和高级别 findings、required changes、验证失败或不可用原因。
2. 保留当前阶段 plan 摘要、必须保留的决策和已接受风险。
3. 将 diff 摘要降级为变更文件路径列表和极短意图说明。
4. 丢弃具体代码片段、长 diff、重复背景和低级别建议。

Repair packet 只包含 summary + references，不包含 full diff。需要查看完整 diff 时，prompt 应引用文件路径、commit/base 信息或事件中的 diff metadata。

## 事件 Schema

P0 必须在构建持久化会话之前添加轮次级事件。这些事件是判断 P1 复杂度是否值得的度量基础。

### evaluator_loop_started

```json
{
  "loop_id": "implement:20260524-abc123",
  "loop_type": "plan|code",
  "stage": "implement",
  "mode": "short_lived|persistent",
  "max_rounds": 3,
  "optimizer_model": "claude-sonnet",
  "reviewer_model": "deepseek-chat",
  "attempt_id": "implement:1"
}
```

### evaluator_loop_round

```json
{
  "loop_id": "implement:20260524-abc123",
  "round_id": 1,
  "loop_type": "code",
  "mode": "short_lived",
  "decision": "pass|revise|fail|wait_confirm",
  "score": 0.78,
  "findings": {
    "open_before": [],
    "new": ["F-001", "F-002"],
    "resolved": [],
    "repeated": []
  },
  "verification": {
    "status": "passed|failed|unavailable|not_run",
    "commands": []
  },
  "prompt_tokens": {
    "total": 0,
    "context": 0,
    "feedback": 0,
    "repeated_context": 0,
    "estimated": true
  },
  "timing_ms": {
    "round_total": 0,
    "agent_startup": null
  },
  "diff": {
    "base": "HEAD",
    "files_changed": 0,
    "insertions": 0,
    "deletions": 0,
    "sha256": ""
  },
  "no_progress": false
}
```

### evaluator_loop_completed

```json
{
  "loop_id": "implement:20260524-abc123",
  "loop_type": "code",
  "decision": "pass|fail|wait_confirm",
  "rounds": 2,
  "reason": "all_blockers_resolved|max_rounds|no_progress|verification_failed",
  "remaining_findings": []
}
```

### evaluator_loop_fallback

```json
{
  "loop_id": "implement:20260524-abc123",
  "from_mode": "persistent",
  "to_mode": "short_lived",
  "reason": "session_dead|timeout|context_overflow|agent_confused",
  "repair_packet_path": ".story-context/STORY/repair_implement_round2.md"
}
```

## Finding 状态对齐

P0 不向 `finding.status` 添加 `disputed`、`accepted_risk` 或 `stale`。

现有 status 作为系统状态继续用于 DoD、审批队列和 Quality Packet 查询：

```text
open
accepted
verified
rejected/deferred（如果 CLI 路径已使用）
```

循环专用的状态放在事件载荷中：

```json
{
  "finding_id": "F-001",
  "round_status": "new|repeated|resolved|disputed|accepted_risk"
}
```

这样可以避免破坏 `get_open_findings()`、DoD 门禁、审批队列和 Quality Packet 注入。

## 验证阶梯（Verification Ladder）

代码审查应记录可用的最强验证级别：

```text
L0 diff 检查
L1 语法 / 编译 / import 检查
L2 lint / 格式检查（如可用）
L3 定向冒烟命令（如可发现）
L4 项目测试命令（如可靠）
L5 人工确认
```

验证状态值：

```text
passed
failed
unavailable
not_run
```

`verification_unavailable` 本身不会导致阶段失败，但 reviewer 应将其视为更高的残余风险。

`verification_failed` 必须产生 `revise` 或 `fail`，除非通过人工确认显式覆盖。

当最强验证级别只有 L0/L1，或验证状态为 `unavailable` / `not_run` 时，reviewer prompt 必须约束 finding 置信度：

- 纯推理得到的 High finding 默认降级为 Medium，除非它直接对应可定位的安全、数据破坏、语法或接口契约证据。
- 无法降级但证据不足的 finding 必须标记低信度，并在 required changes 中说明需要人工或更高验证级别确认。
- 低验证级别不得仅凭风格偏好或猜测阻塞 DoD。

## 浪费指标（Waste Metrics）

当前数据不足以精确计算上下文浪费。P0 轮次事件应收集足够数据以估算：

```text
context_reuse_waste = repeated_context_tokens / total_prompt_tokens
feedback_density = actionable_feedback_tokens / repair_prompt_tokens
fix_efficiency = resolved_findings / open_findings_before_round
repeat_finding_rate = repeated_findings / findings_in_current_round
agent_startup_overhead = time_to_first_useful_action / total_round_time
post_pass_rework_rate = later_findings_linked_to_passed_stage / passed_stage_count
```

当任一阈值被反复触发时，从短生命周期模式升级到持久化 implementer 模式：

```text
context_reuse_waste > 60%
feedback_density < 10%
repeat_finding_rate > 30%
avg code loop rounds >= 2.2
agent_startup_overhead > 40%
```

## 失败处理

- Reviewer JSON 解析失败：记录带解析失败的 `evaluator_loop_round`，降级为 markdown 审查或 `wait_confirm`；不从无效 JSON 创建结构化 findings。
- 达到 `max_rounds` 仍有阻塞项：不通过；路由到 `wait_confirm`。
- 检测到无进展（no progress）：若当前轮 Major/Blocker finding 与上一轮在 category + location 上语义重复，且 implementer 未改变对应区域或解释无法成立，则判定无进展并路由到 `wait_confirm`，附带重复的 findings。
- 若当前轮发现的是全新 Major/Blocker finding，而非重复项，视为审查继续剥离风险；不要简单判定 no progress。若已接近 `max_rounds` 或全新 finding 持续出现，应路由到 `wait_confirm` 让人工决定继续审查、接受风险或调整范围。
- 验证不可用：不自动失败；暴露残余风险。
- 验证失败：revise 或 fail。
- 持久化会话死亡：记录降级，构建 repair packet，继续短生命周期 retry。
- Repair packet 生成失败：直接 fail，而非用空上下文 retry。

## 测试策略

P0 测试应聚焦于纯函数和 graph 安全的集成：

- `loop_events` 写入稳定的事件载荷。
- plan loop 在 pass、max_rounds 和 no_progress 时停止。
- code review loop 将 reviewer findings 转换为 `finding` 记录和事件。
- repair packet 包含未关闭的 findings、验证状态和 plan 上下文。
- repair packet 超过 `hard_budget` 时按优先级裁剪，且不包含 full diff；`emergency_compact_budget` 路径保留最小可修复信息。
- no_progress 只对 category + location 语义重复的 Major/Blocker finding 生效；全新高级别 finding 触发 `wait_confirm` 路径覆盖。
- L0/L1 或验证不可用时，纯推理 High finding 会降级或标记低信度。
- 现有 DoD 门禁仍然阻止未关闭的高级别 findings。
- 无效的 reviewer JSON 不创建 findings。
- profile 配置默认值在 adversarial loop 禁用时保持当前行为。

P1 测试应添加会话和信号覆盖：

- implementation_ready 信号触发全新 reviewer。
- revise 反馈注入到同一 implementer 会话。
- 会话死亡降级到 repair packet。
- `.story-done` 仅在 pass 后写入。

## 上线计划

1. 添加 P0 配置，默认禁用。
2. 添加 loop event 辅助函数和测试。
3. 在配置开关后添加 plan loop。
4. 在配置开关后添加 code review loop。
5. 在 retry 时添加 repair packet 注入。
6. 在真实 story 上观察浪费指标。
7. 根据度量数据判断持久化 implementer P1 是否值得。

## 已敲定决策

- Repair packet 绝不包含 full diff；只包含 summary、必要引用、文件路径和事件 diff metadata。
- Plan Loop P0 仅对 design 和 implement 阶段开启；其他阶段默认不开启，除非 profile 显式覆盖。
- 高级别 open findings 在 P0 默认阻塞 DoD，沿用现有 `quality.block_on_open_high_findings: true` 行为；需要放行时通过人工处理 finding 状态或后续 profile 策略扩展完成，不在 P0 增加 warning-only 模式。
- Reviewer model 按 profile 显式配置优先，其次回落到当前 orchestrator LLM 配置；P0 不强制要求 evaluator 与 optimizer 使用不同模型。
- P1 持久化 implementer 使用信号文件协议：implementer 写入 `implementation_ready.json` 后等待 `continue_requested.json` / `review_feedback.json`，不依赖 CLI 显式暂停能力。
