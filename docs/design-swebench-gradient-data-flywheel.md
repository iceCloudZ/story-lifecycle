# SWE-bench Gradient Data Flywheel 设计

## 背景

`docs/idea-swebench-data-flywheel.md` 提出一个关键方向：SWE-bench pipeline 不应只输出分数，而应把每次 run 的 trace 转化为下一次 run 的改进材料。

当前系统已经具备飞轮原材料：

- SWE-bench pipeline：`prepare -> solve -> export -> eval`
- 多阶段执行：`design -> implement -> test -> finalize`
- event_log trace：`prompt_context`、`execute`、`route_decision`、`dod_check`
- learned_pattern 表和 quality_packet 注入机制
- headless 运行、finalize patch gate、empty patch failure 标记

原 idea 的 P0-P3 路线是：

```text
P0 Post-mortem Analyzer
P1 Pattern Extraction & Injection
P2 Review Stage + LLM Router
P3 A/B Comparison Framework
```

这份设计在原 idea 基础上进一步升级：SWE-bench 的真正价值不是最终 score，而是 trace 中隐含的 gradient。Score 只能告诉系统“好不好”，trace 能告诉系统“哪里改会更好”。

## 核心洞察：SWE-bench 的价值是 Gradient

传统 benchmark 给出的是标量：

```text
resolved = 0 or 1
resolve_rate = resolved / total
```

这个标量只能判断整体效果，不能直接指导优化。

Story Lifecycle 的 event_log trace 是高维向量：

```text
stage
prompt_context
adapter/model/provider
router decision
review finding
retry count
patch size
failure type
final score
```

当系统同时拥有 trace 和 SWE-bench outcome，就可以近似回答：

```text
哪个决策节点对最终 resolved 贡献最大？
哪个 prompt segment 与失败高度相关？
哪个 route_decision 更可能导致 empty_patch？
哪个 repair packet 形式更可能让第二轮修复成功？
```

P0 Post-mortem Analyzer 不只是事故报告生成器，而是 Gradient 计算器。它的输出必须面向未来的优化系统，而不只是给人阅读。

## 目标

1. 将 SWE-bench run 产物转化为结构化 trace samples。
2. 用 post-mortem 分析定位对成功率影响最大的决策节点。
3. 从失败和成功中提取可执行改进：文本 pattern、执行约束、micro-tool、router preference data。
4. 让 pattern 具备生命周期、上下文范围和验证机制，避免 pattern poisoning。
5. 为未来 outcome-driven DPO、小型 router/classifier、MCTS-style branch search 留出数据接口。

## 非目标

1. P0 不训练模型。
2. P0 不自动生成可执行 micro-tool。
3. P0 不引入真实 MCTS 分叉执行。
4. P0 不依赖人工 Coach 才能产出基础分析。
5. P0 不把所有 pattern 直接注入 prompt；高风险 pattern 必须先进入候选和验证流程。

## 架构总览

```text
SWE-bench Run
  -> Event Trace Capture
  -> Post-mortem Analyzer
  -> Trace Sample Store
  -> Counterfactual Analyzer
  -> Pattern / Constraint / Tool Candidate Extractor
  -> Validation & Half-life Manager
  -> Injection / Constraint / Tool Registry
  -> Next Run
```

飞轮不是单一“分析报告”链路，而是多种改进资产的生产链路：

```text
trace -> report                 # 给人看
trace -> pattern                # 给 prompt/quality packet 用
trace -> constraint             # 给执行层权限/边界用
trace -> micro-tool candidate   # 给工具注册流程用
trace -> preference sample      # 给 router/classifier/DPO 用
trace -> branch policy          # 给未来 MCTS/search 用
```

## P0：Post-mortem Analyzer 作为 Gradient 计算器

### 输入

P0 读取：

- run manifest
- event_log
- stage done 快照
- predictions.jsonl
- eval result
- git diff / model_patch
- failure metadata：`checkout_failure`、`empty_patch`、`execution_failure`、`resolve_failed`

### 输出

P0 必须输出两类文件：

```text
.story/runs/{run_id}/analysis/report.md
.story/runs/{run_id}/analysis/trace_samples.jsonl
```

`report.md` 给人看，`trace_samples.jsonl` 给未来机器学习和自动优化使用。

### Trace Sample Schema

每条 sample 应该尽量稳定，避免只是一段自然语言。

```json
{
  "schema_version": 1,
  "run_id": "real-3",
  "instance_id": "django__django-12345",
  "repo": "django/django",
  "base_commit": "abc123",
  "stage": "implement",
  "decision_point": "router.route_decision",
  "state_summary": {
    "current_stage": "implement",
    "retry_count": 1,
    "last_error": "missing_expected_outputs",
    "patch_stats": {
      "files_changed": 2,
      "diff_size_bytes": 4300
    }
  },
  "action": {
    "type": "retry",
    "provider": "deepseek",
    "repair_packet_shape": "summary_only"
  },
  "outcome": {
    "resolved": false,
    "failure_type": "empty_patch",
    "stage_terminal_status": "blocked"
  },
  "credit_assignment": {
    "suspected_failure_node": "finalize.patch_gate",
    "confidence": 0.74,
    "evidence": ["finalize completed without model_patch", "git diff empty"]
  },
  "counterfactuals": [],
  "tags": ["swebench", "django", "router", "empty_patch"]
}
```

### Gradient Signals

P0 不需要真的做数学求导，但需要产出类似 gradient 的结构化信号：

- `suspected_failure_node`
- `decision_sensitivity`
- `retry_effectiveness`
- `prompt_segment_risk`
- `patch_gate_effect`
- `review_feedback_usefulness`

这些信号用于排序“下一步最值得优化哪里”。

## 反事实推理

规则分类能快速覆盖常见失败，但长尾问题需要反事实推理。

Post-mortem Analyzer 应在规则分析后，针对关键失败 case 生成 bounded counterfactuals：

```text
如果 design 阶段先定位文件 B，而不是文件 A，是否更可能成功？
如果 repair packet 包含具体错误行号，第二轮是否更可能修复？
如果 router 在 retry 前要求重新读取 failing test，是否能避免空 patch？
```

### Counterfactual Schema

```json
{
  "counterfactual_id": "cf-001",
  "decision_point": "design.target_file_selection",
  "actual_action": "focused_on_file_a",
  "alternative_action": "inspect_file_b_from_failing_test",
  "expected_effect": "increase_resolve_probability",
  "confidence": 0.62,
  "evidence": [
    "failing test imports file_b",
    "final patch touched unrelated file_a"
  ],
  "recommended_asset": {
    "type": "pattern",
    "title": "Start from failing test imports before selecting target file"
  }
}
```

### Guardrails

反事实输出不能直接进入 active pattern。它只能生成候选资产：

- proposed pattern
- proposed constraint
- proposed micro-tool
- router preference sample

这些候选必须经过验证或人工审核。

## Pattern 本体升级

Pattern 不应只是一段文本。它可以有三种形态。

### 1. Text Pattern

适合低风险经验：

```text
处理 Django ORM bug 时，先从 failing test 的 import 和 assertion 反推目标模块。
```

注入位置：

- quality_packet
- executor checklist
- repair packet

### 2. Constraint Pattern

适合高确定性负向经验。

例子：

```text
不要修改 SWE-bench 提供的测试文件来通过测试。
```

比起在 prompt 里“建议不要修改测试”，更有效的是转成执行层约束：

```text
disallow_edit:
  - "tests/**"
  - "**/test_*.py"
```

注意：SWE-bench 有时需要新增或调整本地辅助测试，不能一刀切。constraint 必须有 scope：

```json
{
  "type": "constraint",
  "scope": {
    "benchmark": "swebench",
    "stage": "implement",
    "repo": "django/django"
  },
  "rule": {
    "disallow_edit": ["tests/**"],
    "allow_override": false
  },
  "confidence": 0.9
}
```

### 3. Micro-tool Candidate

当多个成功 trace 反复出现同一段脚本化动作，应提取为 micro-tool 候选。

例子：

```text
analyze_django_migration
```

来源：

- 多个 Django migration case 都手写脚本分析 `makemigrations --dry-run`
- 成功 trace 显示该步骤提升定位速度

P0/P1 只产生候选，不自动注册执行工具。Micro-tool 必须经过：

1. 静态检查
2. sandbox smoke run
3. 人工批准或高置信自动批准
4. registry 激活

## Outcome-Driven Preference Dataset

在 SWE-bench 场景下，Eval Harness 是客观 Coach。系统不必完全依赖 LLM 判断“哪个决策好”。

对于同一个 instance，如果有多个 run：

```text
Trace A -> resolved
Trace B -> failed
```

可以在关键决策点构造偏好样本：

```json
{
  "preference_type": "router_decision",
  "instance_id": "django__django-12345",
  "context": {
    "stage": "implement",
    "last_error": "review_found_issue",
    "retry_count": 1
  },
  "chosen": {
    "trace_id": "A",
    "decision": "retry_with_specific_repair_packet"
  },
  "rejected": {
    "trace_id": "B",
    "decision": "advance"
  },
  "label_source": "swebench_eval",
  "outcome_delta": 1
}
```

这类数据未来可用于：

- 小型 router classifier
- reranker
- DPO-style preference training
- rule mining

P0 不训练模型，但必须从第一天就保存这种结构化样本，避免以后只能从自然语言报告里反挖数据。

## 从 A/B 到 Story 内 MCTS

P3 原本是 run-level A/B：

```text
profile A vs profile B
model A vs model B
4-stage vs 2-stage
```

长期可以下沉到 story 内部的 decision-level branch search：

```text
router uncertainty high
  -> branch A: continue patch
  -> branch B: rollback to design and re-localize root cause
  -> branch C: generate focused repair packet
```

哪个分支先通过本地测试或 patch gate，就采纳哪个分支，终止其他分支。

P0/P1 需要提前留下数据接口：

- branch_id
- parent_trace_id
- decision_point
- action
- outcome
- cost

这样未来引入轻量 MCTS 不需要重写 trace schema。

## Pattern Poisoning 与半衰期

数据飞轮最大的风险是 pattern poisoning。

例子：

- Django 3.x 的 repo 结构 pattern 在 Django 5.x 失效。
- 某个 patch strategy 在一个 instance 成功，但在类似 instance 中导致错误定位。
- 过长 quality_packet 造成上下文污染，agent 忽略真正重要约束。

### Pattern Metadata

每个 pattern 必须带上下文元数据：

```json
{
  "pattern_id": "p-001",
  "kind": "text|constraint|micro_tool_candidate",
  "repo": "django/django",
  "base_commit_range": ["abc123", "def456"],
  "framework_version": "5.0",
  "applies_to": ["orm", "queryset"],
  "created_from": ["trace-a", "trace-b"],
  "success_count": 3,
  "failure_count": 1,
  "last_verified_at": "2026-05-26T00:00:00Z",
  "half_life_days": 30,
  "status": "proposed|active|deprecated|rejected"
}
```

### Half-life Policy

Pattern score 随时间衰减：

```text
effective_score = base_score * decay(age_days, half_life_days) * context_similarity
```

降权条件：

- 长期未被命中
- 命中后 resolve rate 下降
- 当前 repo commit 与 pattern 来源 commit 距离过远
- smoke validation 失败

高风险 constraint / micro-tool candidate 必须先在 smoke set 上验证。如果 resolve rate 下降，立即回滚或降级为 proposed。

## P0 设计

### CLI

```text
story swebench analyze --run-id real-3 --workspace-root <root>
```

输出：

```text
<workspace-root>/.story/runs/{run_id}/analysis/report.md
<workspace-root>/.story/runs/{run_id}/analysis/analysis.json
<workspace-root>/.story/runs/{run_id}/analysis/trace_samples.jsonl
<workspace-root>/.story/runs/{run_id}/analysis/preference_samples.jsonl
<workspace-root>/.story/runs/{run_id}/analysis/candidates.jsonl
```

### Analysis JSON

```json
{
  "run_id": "real-3",
  "summary": {
    "total": 10,
    "resolved": 4,
    "resolve_rate": 0.4,
    "empty_patch": 2,
    "execution_failure": 1
  },
  "gradient": {
    "top_failure_nodes": [
      {"node": "finalize.patch_gate", "count": 2},
      {"node": "design.localization", "count": 1}
    ],
    "top_optimization_targets": [
      {"target": "repair_packet_specificity", "expected_gain": "medium"}
    ]
  }
}
```

### Candidate Asset JSONL

```json
{
  "candidate_id": "cand-001",
  "type": "pattern",
  "status": "proposed",
  "title": "Start from failing test imports",
  "source_trace_ids": ["trace-001"],
  "risk": "low",
  "requires_validation": true
}
```

```json
{
  "candidate_id": "cand-002",
  "type": "constraint",
  "status": "proposed",
  "title": "Disallow editing benchmark tests",
  "scope": {"benchmark": "swebench", "stage": "implement"},
  "risk": "high",
  "requires_validation": true
}
```

## P1 设计

P1 消费 P0 candidates。

### Text Pattern Flow

```text
candidates.jsonl
-> review/approval
-> learned_pattern table
-> context-aware retrieval
-> quality_packet injection
```

### Constraint Flow

```text
constraint candidate
-> smoke validation
-> constraint registry
-> adapter/tool launch policy
```

### Micro-tool Flow

```text
micro-tool candidate
-> generated tool spec
-> sandbox validation
-> registry activation
-> prompt/tool availability injection
```

P1 不应该把所有 candidates 都自动激活。默认进入 `proposed`，只有低风险 text pattern 可以走半自动批准。

## P2 设计

P2 不只做 LLM Coach，而是优先使用 outcome-driven preference data。

### Router Dataset

P2 从 `preference_samples.jsonl` 中学习：

- 当前 stage + context + failure signal 下，retry/advance/fail 哪个更可能成功
- repair packet 类型与二轮成功率关系
- provider/model 切换与成功率关系

### Router Runtime

短期：

```text
rule router + historical priors + LLM fallback
```

长期：

```text
small classifier / reranker -> router decision
```

LLM 仍可用于解释和长尾推理，但不应作为唯一 Coach。

## P3 设计

P3 分两层。

### Run-level A/B

对比：

- profile
- model
- budget
- review gate
- pattern injection on/off
- constraint injection on/off

### Decision-level Branch Search

在高不确定节点分叉：

- branch A：继续当前 patch
- branch B：回到 design 重定位
- branch C：生成更具体 repair packet

每个 branch 写入独立 trace：

```json
{
  "branch_id": "b1",
  "parent_branch_id": "root",
  "decision_point": "router.after_review",
  "action": "rollback_to_design",
  "cost": {"seconds": 180, "tokens": 12000},
  "outcome": {"local_tests_passed": true, "resolved": true}
}
```

## 数据表/存储建议

P0 可以先落文件，不急着扩 DB schema：

```text
.story/runs/{run_id}/analysis/*.jsonl
```

P1 再把已批准资产写入 DB：

- `learned_pattern`
- `pattern_evidence`
- `constraint_rule`
- `micro_tool_candidate`
- `router_preference_sample`

如果暂不加表，`router_preference_sample` 可先保留 JSONL。

## 风险与约束

1. 不让 LLM analysis 直接改运行策略。
2. 不让单个成功 case 自动产生 active constraint。
3. 不注入过多 pattern，避免 prompt 污染。
4. Pattern 必须带 repo、commit、版本、stage、evidence。
5. Constraint 和 micro-tool 必须 smoke validation。
6. Preference sample 必须标注 label source，区分 SWE-bench eval、LLM judgment、人类 judgment。

## 落地路线

### P0.1 Trace Sample Export

1. `story swebench analyze`
2. 读取 manifest/event_log/eval result。
3. 输出 `trace_samples.jsonl`。
4. 输出基础 `report.md`。

### P0.2 Gradient Attribution

1. 规则识别 failure node。
2. 统计 top failure nodes。
3. 输出 `analysis.json.gradient`。

### P0.3 Counterfactual Candidates

1. 对失败 case 生成 bounded counterfactuals。
2. 只写 candidates，不自动激活。
3. 输出 `candidates.jsonl`。

### P1 Pattern/Constraint Intake

1. 将 low-risk text pattern 写入 learned_pattern proposed。
2. 将 high-risk constraint 写入 constraint candidate。
3. 增加 half-life metadata。

### P2 Preference Dataset

1. 对同 instance 多 run 进行 trace alignment。
2. 生成 preference_samples.jsonl。
3. 用于 router offline evaluation。

### P3 Branch Search

1. 在 router uncertainty 高时允许 fork。
2. branch 共享 workspace snapshot 或 git worktree。
3. 以 local test / patch gate / eval outcome 决定采纳。

## 决策

1. P0 analyzer 输出必须机器可消费，不能只有 Markdown 报告。
2. SWE-bench outcome 是最高优先级 label source。
3. Counterfactual 只能生成候选，不能直接激活策略。
4. Pattern 分三类：text、constraint、micro-tool candidate。
5. Constraint 比 prompt pattern 更强，但风险也更高，必须验证。
6. Pattern 必须有 half-life 和 context-aware retrieval。
7. P0 先文件存储，P1 再扩 DB。

## 推荐方案

先做 P0.1 + P0.2。

原因：

- 直接复用现有 event_log 和 SWE-bench run 产物。
- 不需要新执行路径。
- 不需要训练模型。
- 能立即把每次 run 变成结构化 trace samples。
- 为后续 counterfactual、pattern、DPO、MCTS 留下统一数据接口。

P0 的验收标准不是“报告写得漂亮”，而是：

```text
每个 SWE-bench instance 都能产出一条可被未来训练/分析直接消费的 trace sample。
```

