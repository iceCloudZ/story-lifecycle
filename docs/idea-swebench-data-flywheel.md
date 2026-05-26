# SWE-bench 数据飞轮：从 Benchmark 到持续进化

> **状态**: Idea 阶段
> **日期**: 2026-05-26
> **背景**: story-lifecycle 端到端 SWE-bench pipeline 已跑通（real-3 run: 1/1 resolved），event_log 完整采集 4 阶段 × 4 事件共 16 条 trace

---

## 1. 背景

story-lifecycle 的 SWE-bench 集成已完成核心闭环：

```
prepare → solve (design→implement→test→finalize) → export → eval → score
```

每个 instance 在执行过程中产生完整的 event_log trace：
- `prompt_context` — 渲染后的 prompt 和 stage 参数
- `execute` — adapter、model、timeout 等执行元数据
- `route_decision` — router 的 advance/retry/fail 决策及理由
- `dod_check` — completion 检查结果

这些 trace 数据目前只用于调试，但它们是数据飞轮的原材料。核心问题是：**如何让每次 run 的结果（成功或失败）自动转化为下一次 run 的改进？**

## 2. 参考文章与关键洞察

### 2.1 Live-SWE-agent: On-the-fly Tool Creation (arxiv 2511.13646)

**核心思路**: Agent 在解决 SWE-bench 问题的过程中，遇到困难时动态创建新工具，新工具在后续步骤中可直接调用。

**关键洞察**:
- Step-reflection prompt: 每步之后让 LLM 反思"是否需要新工具"
- 工具创建本身也是 LLM 调用，产出的工具代码经过验证后才注册
- 在 SWE-bench Lite 上达到 44% resolve rate

**对我们的启发**: 不需要动态创建工具，但可以动态注入 **pattern**。每次 run 后提取的 pattern 就是"预创建的工具"——把成功经验固化成可复用的知识块。

### 2.2 Augment Code: Learning Flywheel

**核心思路**: Execute → Coach → Distill → Improve 四阶段循环。

```
Execute (执行任务，收集 trace)
   ↓
Coach (人工或 LLM 标注 trace 中的好/坏决策)
   ↓
Distill (提炼为可复用 pattern)
   ↓
Improve (下次执行时注入 pattern)
```

**关键洞察**:
- Trace 是一等公民——不只要代码，还要决策过程
- Coach 阶段是瓶颈：全人工标注成本太高，需要 LLM 辅助 + 人工抽检
- Distill 的输出不是代码，是 **规则 + 示例** 的结构化知识

**对我们的启发**: 我们的 event_log 已经天然是 trace。缺的是 Coach 和 Distill 阶段。可以先用 LLM 做 Coach（分析 trace 给出评价），人工抽检校正。

### 2.3 SWE-Search: MCTS for Multi-Path Exploration

**核心思路**: 用 Monte Carlo Tree Search 在 SWE-bench 问题空间做多路径探索，每个 node 是一个代码状态，edge 是编辑操作。

**关键洞察**:
- 单路径执行的方差很大——同样的 prompt，随机性可能导致完全不同的结果
- MCTS 通过多次模拟找到高概率成功路径
- 比单路径提升 23% resolve rate

**对我们的启发**: 当前 pipeline 是单路径的。P3 阶段可以引入 A/B 对比——同一 instance 用不同配置跑，对比结果，找到最优配置。

### 2.4 Martin Fowler: Feedback Flywheel

**核心思路**: 快速反馈循环比完美反馈更重要。关键不是一次给对的反馈，而是让反馈的间隔尽可能短。

**关键洞察**:
- 飞轮转速比单次反馈质量更重要
- 自动化是加速飞轮的关键
- 度量要简单：resolve rate 就是终极度量

### 2.5 Anthropic: Evaluation-Driven Development

**核心思路**: 用 eval 作为开发的北极大星。先写 eval，再写代码。

**关键洞察**:
- Eval 不是事后验证，是设计工具
- 小 eval 集比大 eval 集更有用（快速迭代）
- SWE-bench 本身就是最好的 eval——它是真实 bug 修复场景

---

## 3. 改进方向

### P0: Post-mortem Analyzer（事后分析器）

**目标**: Run 结束后自动分析 event_log，生成结构化报告。

**为什么是 P0**: 数据已经在 event_log 里了，只需要读取和分析。这是飞轮的"Execute → Coach"的第一步，没有分析就没有后续的一切。

**具体产出**:
- 每个 instance 的事后报告：
  - 各阶段耗时、token 估算、是否超时
  - Router 决策链：哪些 advance/retry/fail，理由是什么
  - 失败分类：checkout_failure / empty_patch / execution_failure / resolve_failed
- Run 级聚合：
  - 成功率、平均轮次、平均耗时
  - 失败模式分布
- 自动标注"值得学习的 case"：
  - 成功且 round ≤ 2 → 候选正向 pattern
  - 失败且 round ≥ 3 → 候选负向 pattern

**实现方式**:
```
story swebench analyze --run-id xxx
→ 读取 manifest + event_log + .story-done
→ LLM 分析（可选，先用规则引擎）
→ 输出 reports/{run_id}/analysis.json
```

**度量**: 分析覆盖率 = 被分析的 instance 数 / 总 instance 数

---

### P1: Pattern Extraction & Injection（模式提取与注入）

**目标**: 从成功的 case 中提取 pattern，注入到后续 run 的 prompt 中。

**为什么是 P1**: 依赖 P0 的分析结果。提取 pattern 是飞轮的"Distill"阶段，注入是"Improve"阶段。

**数据流**:

```
event_log (P0 分析)
   ↓
Pattern Extractor (LLM 或规则)
   ↓
learned_pattern 表 (已有 schema，未使用)
   ↓
quality_packet 注入 (已有机制，未使用)
   ↓
下次 run 的 prompt 增强
```

**Pattern 类型**:
1. **正向 pattern** (resolved cases):
   - 有效的调试策略（如"先看 test case 再读源码"）
   - 特定 repo 的结构知识（如"Django 的 QuerySet 在 django/db/models/query.py"）
   - 成功的 patch 模式（如"单文件小改动成功率高于跨文件重构"）

2. **负向 pattern** (failed cases):
   - 常见陷阱（如"不要修改 test 文件来通过测试"）
   - 无效策略（如"反复重试同样方法不改变策略"）

**注入机制**: 已有的 `quality_packet` 在 `prompt_context` 阶段注入到 system prompt。需要：
- 匹配逻辑：根据 repo / error_type / stage 选择相关 pattern
- 去重：避免注入过多 pattern 导致 prompt 过长
- 时效性：pattern 有生命周期，长期不被验证的自动降权

**度量**: 注入 pattern 后的 resolve rate 对比基线

---

### P2: Review Stage + LLM Router（质量审查与智能路由）

**目标**: 当前 pipeline 只有"执行→检查是否完成"的简单循环。增加 review stage 做"完成质量判断"，LLM Router 做"下一步最优决策"。

**为什么是 P2**: 需要前两个 P 积累足够数据来训练/校准 review 和 routing 的判断标准。

**Review Stage 增强**:
- 当前的 `poll_completion_node` 只检查 `.story-done` 文件是否存在
- 增强：LLM review 完成 patch 的质量
  - patch 是否解决了 problem_statement 的核心问题
  - patch 是否引入了不必要的变更
  - patch 大小是否合理
- Review 结果作为 router 的输入

**LLM Router 增强**:
- 当前 router 基于简单规则（有 error → retry/fail）
- 增强：基于 review 结果 + 历史数据做决策
  - "patch 看起来不对但可以修复" → retry with feedback
  - "patch 看起来正确" → advance
  - "反复尝试同一策略" → switch strategy
  - "超出预算" → fail with good error message

**度量**: Router 决策准确率 = LLM router 决策与最终结果一致的比例

---

### P3: A/B Comparison Framework（对比实验框架）

**目标**: 系统化地对比不同配置的效果，找到最优 pipeline 配置。

**为什么是 P3**: 需要稳定的 pipeline (P0-P1) 和质量度量 (P2) 才能做有意义的对比。

**对比维度**:

| 维度 | 选项 A | 选项 B |
|------|--------|--------|
| Stage 数量 | 4-stage (design→impl→test→finalize) | 2-stage (impl→finalize) |
| Review | 有 review stage | 无 review |
| Pattern 注入 | 有 quality_packet | 无 |
| Model | sonnet | opus / haiku |
| Budget | smoke (1 round) | standard (3 rounds) |
| Prompt | 详细 prompt | 简洁 prompt |

**实现方式**:
```
story swebench run --config configs/ab-test-a.yaml
story swebench run --config configs/ab-test-b.yaml
story swebench compare --run-ids a,b
→ 对比 resolve rate / avg rounds / avg time / cost
```

**度量**: 各配置的 resolve rate + cost efficiency（resolve per dollar）

---

## 4. 飞轮全景

```
                    ┌─────────────┐
                    │  SWE-bench  │
                    │  Instances  │
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │  Pipeline   │  ← P3: 不同配置对比
                    │ (design→    │
                    │  impl→test→ │  ← P2: Review + Router
                    │  finalize)  │
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │  Event Log  │  ← 每阶段 4 类事件
                    │  Traces     │
                    └──────┬──────┘
                           │
              ┌────────────▼────────────┐
              │    P0: Post-mortem      │
              │    Analyzer             │
              └────────────┬────────────┘
                           │
              ┌────────────▼────────────┐
              │  P1: Pattern Extractor  │
              │  (learned_pattern 表)   │
              └────────────┬────────────┘
                           │
              ┌────────────▼────────────┐
              │  Quality Packet 注入    │
              │  (prompt 增强)          │
              └────────────┬────────────┘
                           │
                    ┌──────▼──────┐
                    │  下一次 Run  │  ← resolve rate ↑
                    └─────────────┘
```

**飞轮转速的度量**:
- 从 run 完成 → pattern 注入 的端到端时间（目标: < 1 小时自动化）
- 每轮 run 的 resolve rate 变化趋势
- Pattern 命中率：注入的 pattern 被实际使用到的比例

---

## 5. 现有基础设施

以下组件已经构建完成，为飞轮提供了基础：

| 组件 | 状态 | 位置 |
|------|------|------|
| event_log trace 采集 | ✅ 已完成 | `db.log_event()` — 4 类事件/阶段 |
| learned_pattern 表 | ✅ schema 存在 | `db/models.py` |
| quality_packet 注入 | ✅ 机制存在 | `nodes.py` prompt_context |
| SWE-bench CLI | ✅ prepare/solve/export/eval | `cli/swebench.py` |
| Headless 执行 | ✅ claude -p | `tools/base.py` _run_headless |
| 总结报告 | ✅ summarize command | `cli/swebench.py` summarize_cmd |

**缺失的环节** (即 P0-P3 要做的事):
- ❌ Post-mortem 分析逻辑
- ❌ Pattern 提取逻辑
- ❌ learned_pattern 的 CRUD API
- ❌ quality_packet 的动态选择与注入
- ❌ Review stage 的 LLM 调用
- ❌ A/B 对比框架

---

## 6. 路线图

```
Phase 1 (1-2 周): P0 — Post-mortem Analyzer
  ├── 实现 analyze 子命令
  ├── 规则引擎分析（失败分类、耗时统计）
  ├── 可选 LLM 分析（case 评价）
  └── 输出结构化 JSON 报告

Phase 2 (2-3 周): P1 — Pattern Extraction
  ├── 实现 pattern_extractor 模块
  ├── 写入 learned_pattern 表
  ├── 实现 quality_packet 选择逻辑
  └── 注入到 prompt_context

Phase 3 (3-4 周): P2 — Review + Router
  ├── 增强 poll_completion 为 review + completion
  ├── LLM review 调用
  ├── Router 接收 review 信号
  └── Router 历史数据校准

Phase 4 (4-6 周): P3 — A/B Framework
  ├── 配置化 pipeline 参数
  ├── compare 子命令
  ├── 自动化多配置运行
  └── 结果可视化
```

每完成一个 Phase，都回到 SWE-bench 跑一轮完整 eval，度量 resolve rate 的变化。飞轮一旦转起来，每轮 run 都比上一轮更强。
