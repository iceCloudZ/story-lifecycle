> ⚠️ **历史快照（归档于 2026-07）**：描述的架构可能已被后续演进取代。当前架构见 [../../ARCHITECTURE.md](../../ARCHITECTURE.md)。本文件保留作决策记录（ADR），正文未修改。

---

# 双层对抗循环思路（待设计）

## 原始想法

每个阶段引入双层对抗循环，让 reviewer 和 implementer 交替打分直到收敛。

### plan 循环（纯 LLM，秒级）

```
planner 出方案 → reviewer 审查打分
  ↓ 分数不够
planner 修改方案 → reviewer 再打分
  ↓ 分数 ≥ 阈值
进入 execute
```

### code 循环（涉及代码改动，分钟级）

```
code LLM 实现 → reviewer 审查打分
  ↓ 分数不够
code LLM 改代码 → reviewer 再打分
  ↓ 分数 ≥ 阈值
通过，进入下一阶段
```

### 核心想法

- 改计划比改代码成本低，plan 阶段 catch 问题更划算
- 对抗式循环比单次审查质量高，双方互相制衡
- 收敛条件：reviewer 打分 ≥ 阈值（如 0.8）或返回 pass
- 安全阀：max_rounds（如 3 轮）防止死循环

### 状态

待设计，多方 AI 协作参与设计后再定方案。

---

## 行业实践与参考

业界称这类模式为 **Adversarial Code Review** 或 **Multi-Agent Loop**，已有成熟实践和理论。

### 强参考：可作为设计依据

| 来源 | 可借鉴点 |
|------|----------|
| [Anthropic: Evaluator-Optimizer](https://platform.claude.com/cookbook/patterns-agents-evaluator-optimizer) | 一个 LLM 生成，另一个 LLM 评估并反馈，循环直到满足条件。双层对抗循环本质上就是 plan/code 两个 evaluator-optimizer 子循环。 |
| [ASDLC.io — Adversarial Code Review](https://asdlc.io/patterns/adversarial-code-review/) | 独立 Critic Agent 审查 Builder Agent 的产出；reviewer 应有 fresh context，只看 spec/diff/result，不继承 implementer 的解释。 |
| [Anthropic: Multi-Agent Research System](https://www.anthropic.com/engineering/multi-agent-research-system) | 多 agent 能提高复杂任务质量，但 token 成本和编排复杂度显著增加；不应默认无限扩张 agent 数。 |
| [OpenAI: SWE-bench Verified](https://openai.com/index/introducing-swe-bench-verified/) | 真实软件工程验证存在需求不清、环境不稳定、测试质量不一致等问题，说明单靠测试不足以覆盖质量判断。 |
| [arXiv: Are "Solved Issues" in SWE-bench Really Solved Correctly?](https://arxiv.org/abs/2503.15223) | 即使 benchmark 显示通过，patch 也可能没有真正正确解决问题；需要测试之外的语义审查。 |
| [arXiv: Self-Refine](https://arxiv.org/abs/2303.17651) | 反馈-修改循环能提升 LLM 输出质量，但反馈质量和收敛条件必须结构化。 |
| [arXiv: Reflexion](https://arxiv.org/abs/2303.11366) | 语言化反馈可帮助 agent 从失败中改进；适合作为 retry/review 记录的一部分。 |

### 实践观察：可作为启发，不作为核心论证

| 来源 | 做法 |
|------|------|
| [Reddit: Adversarial Collaboration](https://www.reddit.com/r/LocalLLaMA/comments/1navnzc/adversarial_collaboration_between_ai_coding_tools/) | 实验证明对抗式交互显著提升代码质量 |
| [Facebook Claude 社区](https://www.facebook.com/groups/claudeaicommunity/posts/1260159912817840/) | 同时用两个对抗 reviewer，不合并代码直到两方达成一致 |
| [Dan Harper (LinkedIn)](https://www.linkedin.com/posts/danharper_making-a-plan-before-you-code-with-ai-is-activity-7435066548609826816-7PbW) | review 不止于 planning，延伸到实现阶段 |
| [Addy Osmani: My LLM Coding Workflow 2026](https://medium.com/@addyosmani/my-llm-coding-workflow-going-into-2026-52fe1681325e) | spec → plan → implement → review 循环 |
| [Augment Code: Harness Engineering](https://www.augmentcode.com/guides/harness-engineering-ai-coding-agents) | 约束、反馈循环、质量门禁的设计原则 |
| [The Code Agent Orchestra — Addy Osmani](https://addyosmani.com/blog/code-agent-orchestra/) | 多 agent 编排核心模式 |
| [arXiv: Autonomous Research via Adversarial Multi-Agent](https://arxiv.org/html/2605.03042v1) | 学术论文，对抗式多 agent 协作 |
| [Adversarial Planning for Spec Driven Development](https://dev.to/marcosomma/adversarial-planning-for-spec-driven-development-4c3n) | Sentry 的 AI code review 系统 |

### 核心好处

1. **Critic 比自省更有效** — LLM 审查自己的代码容易"放过"，独立 agent 更严格
2. **Plan 阶段纠错成本最低** — 改方案比改代码便宜 10 倍以上
3. **收敛性好** — 实践表明 2-3 轮对抗就能达到高质量
4. **可度量** — 每轮记录 findings、风险和验证状态，质量趋势可视化

---

## 设计修正意见

### 0. 模式命名：Evaluator-Optimizer

双层对抗循环可以定义为两个 evaluator-optimizer 子循环：

- **plan loop**：planner 是 optimizer，plan reviewer 是 evaluator
- **code loop**：implementer 是 optimizer，code reviewer 是 evaluator

这样比“互相打分”更准确：optimizer 负责修改产物，evaluator 负责给出可执行反馈和通过/拒绝决策。

### 1. 收敛条件：结构化 findings 清单，不是分数阈值

`score >= 0.8` 不应作为通过条件。LLM 分数容易虚高、漂移，不同 reviewer/模型之间不可比较。

**可靠收敛条件**：结构化问题清单为空或只剩可接受项（residual_risks），分数仅作辅助信号。

```json
{
  "score": 0.78,
  "findings": [
    {
      "id": "F-001",
      "severity": "blocker|major|minor",
      "status": "open|fixed|accepted_risk|stale|disputed",
      "claim": "...",
      "evidence": "...",
      "required_change": "...",
      "implementer_response": "..."
    }
  ],
  "residual_risks": []
}
```

通过条件：

- 没有 `open blocker`
- 没有未解释的 `open major`
- `accepted_risk` 必须有明确理由
- 分数只用于趋势观察，不作为唯一 gate

### 2. 无进展检测

max_rounds=3 是必要的，但还不够。还要检测：
- reviewer 连续指出同类问题
- planner/code LLM 只是改写文字但没有实质修复

这种情况应转 `needs_human` 或 `fail`，不要继续循环。

### 3. 超过 max_rounds 不硬 pass

如果达到 max_rounds 仍有未解决 findings，不要硬 pass，应进入 `needs_human` / `wait_confirm`，并把剩余 findings、已尝试修复、无法验证的原因写清楚。

### 4. 和现有架构的贴合

不需要推翻现有状态机。这个能力自然落在 stage 内部，作为 `execute_stage` 前后的子循环：
- 现有节点：plan_stage → execute_stage → poll_completion → review_stage → router
- 新增：plan_stage 内部加 adversarial 子循环，review_stage 内部加 code review 子循环

reviewer 应使用独立上下文，输入固定为：

- story 原始需求
- 当前 stage 的 spec/plan
- 本轮产物：plan 文本或代码 diff
- 相关文件摘要
- 可用验证结果：passed / failed / unavailable
- 上轮 findings 与 implementer response

reviewer 不应继承 implementer 的完整对话历史，避免被解释性上下文带偏。

### 5. MVP 范围：先做 plan adversarial loop

**理由**：plan 循环纯 LLM 交互（秒级、低成本），能快速验证对抗循环是否真的提升质量。

MVP 定义：
- 每个 stage 执行前先生成 plan
- reviewer 结构化审查，最多 3 轮
- 只有 findings 清单为空（decision=pass）才进入真实执行
- 所有轮次写入 stage_log，方便观察质量提升效果

### 6. Code loop 定位：测试不可用时的质量兜底

**不要定位为"测试替代品"或"分数驱动循环"**。

Code loop 的核心产品理由：不能假设用户项目有可运行、可信、快速的测试基建。所以 code loop 是"测试不可靠环境下的审查兜底"。

MVP 支持：diff review + structured findings + max 3 rounds + verification unavailable 记录。

建议引入分层验证梯子：

```text
L0 diff inspection
L1 syntax / compile / import check
L2 lint / format check if available
L3 targeted smoke command if discoverable
L4 project test command if reliable
L5 human confirmation
```

验证失败要区分两类：

- `verification_failed`：检查成功运行，发现真实问题
- `verification_unavailable`：命令不存在、依赖缺失、环境不可用、测试基建无法启动

`verification_unavailable` 不直接导致失败，但 reviewer 应提高语义审查严格度，并在最终结果里暴露残余风险。

### 7. 规格反哺

如果 code reviewer 无法判断实现是否正确，通常说明 spec/plan 不够清晰。此时不应让 implementer 继续猜测，而应回流到 plan loop 或进入 `wait_confirm`。

典型触发条件：

- reviewer 无法判断某个行为是否符合需求
- plan 中没有覆盖关键边界条件
- implementer 和 reviewer 对验收标准理解不一致
- 代码修改需要产品/用户决策

### 8. 成本控制

多 agent 会显著增加 token 和时间成本。默认策略应保守：

- MVP 使用单 reviewer，最多 3 轮
- 只有高风险 stage 或大 diff 才启用第二 reviewer
- 大 diff 先做 context filtering，再送审
- 超过 token/时间预算转 `wait_confirm`
- reviewer 只读取必要上下文，不默认读取整个仓库

### 9. 观测指标

每轮循环都应写入 `stage_log`，用于判断这个机制是否真的提升质量。

建议记录：

- plan/code loop 轮数
- findings 数量、严重度和状态变化
- max_rounds 命中率
- `verification_failed` / `verification_unavailable` 比例
- reviewer pass 后的后续返工率
- 平均 token 成本和耗时
- 进入 `wait_confirm` 的原因分布

### 10. 与现有 story 数据的复用

当前系统已有以下基础设施，evaluator-optimizer 可直接复用：

- **finding 表**：已有的 severity/status/evidence 结构可直接承载 findings
- **learned_pattern 表**：reviewer 发现的 recurring issues 可自动沉淀为 patterns
- **quality.py**：`build_quality_packet()` 已支持注入 findings 到后续 prompt
- **trajectory_score**：已有路径评分，可复用为趋势信号
- **.story-knowledge/**：reviewer 产出的知识可写入知识库，跨 story 复用

P0 优先复用现有表，通过 `event_log` 承载 loop round、reviewer/optimizer 模型、diff 摘要、finding 继承关系等轮次信息。只有当查询、模型对比和收敛分析需求变强时，再考虑新增专门的 loop round 表。

### 11. 与 LLM 模型对比的关联

同一个 task 让不同模型跑 evaluator-optimizer 循环，可以对比：

- 哪个 model 做 reviewer 时 findings 更精准（误报率/漏报率）
- 哪个 model 做 optimizer 时修复更彻底（一轮通过率）
- 哪个组合收敛最快（总轮数）

这为后续 LLM 模型对比能力提供了天然的评测场景。

### 12. 待讨论的开放问题

- **reviewer 模型选择**：用同一个 LLM 还是不同 LLM 做 evaluator？可考虑用不同模型降低同源偏差，但会增加成本和不可比性
- **findings 去重**：多轮 review 可能重复提出同类问题，需要 findings id + 去重逻辑
- **implementer 拒绝权**：optimizer 能否 dispute 一个 finding？格式中已有 `disputed` 状态，但争议解决流程未定义
- **context window 管理**：多轮循环后 context 膨胀，可能需要每轮压缩历史
- **finding 状态机对齐**：现有系统已有 `open/accepted/verified` 等状态；code loop 设计中的 `disputed/accepted_risk/stale` 需要明确是新增状态，还是通过 reason/resolution 字段表达，避免破坏 DoD gate、approval queue 和 quality packet 查询
- **loop round 记录方式**：P0 是否只写 `event_log`，还是新增 loop round 表？如果未来要做模型对比和收敛分析，round 级数据需要稳定 schema
