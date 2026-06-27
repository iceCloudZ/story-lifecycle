# LLM Semantic Extraction And Matching Design

## 背景

当前 `story-lifecycle` 已经引入 LLM 做 planner、reviewer、router 和 seed analyst，但项目中仍有一些自然语言语义判断由正则、关键词或简单 tag overlap 承担。

正则和规则适合处理确定性结构，例如 JSON fence、文件路径、HTML 标签清洗、状态机策略和 schema validation。但以下任务不是结构解析，而是语义理解：

- 从 TAPD Bug 正文中识别复现步骤、预期结果、实际结果、环境、日志。
- 判断 review issue 是否复发了某个 learned pattern。
- 判断哪些 active learned patterns 真正适合注入当前 story 的 Quality Packet。
- 从 review markdown 中保留对后续学习有价值的证据和摘要。
- 在 story debug 时判断失败类型、可能原因和恢复动作。

本文设计一层可复用的 LLM 语义服务，把这些语义任务从脆弱的 regex / keyword 逻辑中拆出来，同时保留规则作为 schema 校验、快速路径和兜底。

## 目标

1. 明确哪些地方应该使用 LLM，哪些地方继续使用规则。
2. 提供统一的 LLM 结构化输出调用入口，避免每个模块重复拼 prompt、解析 JSON、处理 fallback。
3. 优先改造对 story 输入质量和质量飞轮准确性影响最大的路径。
4. 保持系统可降级：没有 LLM key 时功能不崩，只退回到现有规则行为并明确标记 `confidence=low` 或 `mode=rule_fallback`。

## 非目标

- 不把所有 regex 替换成 LLM。
- 不改变 LangGraph 主流程和现有 stage 状态机。
- 不做自动审批、自动激活 pattern 或自动执行恢复动作。
- 不引入大型 UI。
- 不要求一次完成所有 P0/P1/P2，可按优先级分批实现。

## 判断原则

### 应该使用 LLM

满足以下任一条件时，主路径应该使用 LLM：

- 输入是自然语言，格式不稳定。
- 需要识别同义表达、隐含含义、上下文关系。
- 输出会影响 finding、pattern、Quality Packet、retry/recovery 等后续决策。
- 纯规则误判会污染质量飞轮或让 story 走错路径。

### 应该保留规则

以下场景继续使用规则：

- JSON / markdown code fence / bracket counting 解析。
- 本地文件路径、URL、图片 markdown 链接提取。
- HTML 到 markdown 的轻量清洗。
- schema validation、枚举值校验、数量限制、置信度默认值。
- 状态机策略，例如 retry 上限、happy path advance、low score fail。
- DB 查询、status gate、DoR/DoD hard block。

## 总体架构

新增一个轻量语义模块：

```text
src/story_lifecycle/orchestrator/semantic.py
```

职责：

- 封装 LLM availability 检查。
- 统一 OpenAI-compatible chat completion 调用。
- 提供 JSON schema 风格的结构化输出约束。
- 解析 LLM 返回 JSON。
- 失败时返回可审计的 fallback 结果，而不是抛出到业务主流程。

建议接口：

```python
class SemanticResult(TypedDict):
    ok: bool
    mode: Literal["llm", "rule_fallback", "unavailable", "error"]
    confidence: Literal["high", "medium", "low"]
    data: dict
    warnings: list[str]


def extract_bug_context(markdown: str, title: str = "") -> SemanticResult: ...


def match_pattern_recurrence(issue: dict, patterns: list[dict]) -> SemanticResult: ...


def rerank_relevant_patterns(
    story_context: dict,
    candidate_patterns: list[dict],
    limit: int = 5,
) -> SemanticResult: ...


def summarize_review_for_learning(review_markdown: str) -> SemanticResult: ...


def recommend_recovery(debug_packet: dict) -> SemanticResult: ...
```

业务模块只依赖这些函数，不直接拼 LLM prompt。

## 改造一：Bug 正文结构化提取

### 当前问题

文件：

- `src/story_lifecycle/sources/bug_providers.py`

当前 `TapdBodyBugProvider` 用 `_extract_section()` 和标题关键词提取：

- 复现步骤
- 预期结果
- 实际结果
- 环境
- 日志

TAPD Bug 描述来自人、模板、复制粘贴、富文本和评论，字段标题不稳定，甚至可能没有标题。用正则会漏掉大量真实输入。

### 设计

`TapdBodyBugProvider.fetch_content()` 流程调整为：

```text
TAPD html
  -> _html_to_markdown 结构清洗
  -> semantic.extract_bug_context(markdown, title)
  -> validate BugContext fields
  -> fallback to existing regex extractor if LLM unavailable/error
```

LLM 输出：

```json
{
  "description": "简短问题概述",
  "steps_to_reproduce": "复现步骤，保留编号或要点",
  "expected_behavior": "预期结果",
  "actual_behavior": "实际结果",
  "environment": "环境、版本、设备、账号、租户等",
  "logs": "错误日志、堆栈、接口返回、关键报错",
  "missing_fields": ["expected_behavior"],
  "confidence": "high|medium|low"
}
```

图片链接仍使用 `_extract_images()` 正则提取，因为 markdown 图片链接是结构数据。

### 降级

- LLM 不可用：保留现有 `_extract_section()`。
- LLM 返回非法 JSON：记录 warning，保留 regex fallback。
- LLM 抽取为空但 markdown 非空：`raw_markdown` 必须保留，避免信息丢失。

## 改造二：Learned Pattern 复发检测

### 当前问题

文件：

- `src/story_lifecycle/orchestrator/nodes.py`

当前 `_check_pattern_recurrence()` 调用 `_match_pattern()`，通过 pattern name/rule 分词后做 substring 命中，命中 2 个关键词就认为复发。

这对中文和同义表达非常脆弱：

- “缺少回滚方案”和“没有降级路径”可能语义相同但关键词不同。
- “接口未校验输入”和“校验测试缺失”可能共享词但不是同一 pattern。

### 设计

保留 `_check_pattern_recurrence()` 的调用点，但内部改为：

```text
review issues
  -> active patterns limit 20
  -> cheap pre-filter by category/tags/keyword if available
  -> semantic.match_pattern_recurrence(issue, candidate_patterns)
  -> log pattern_recurrence with confidence and reasoning
```

LLM 输出：

```json
{
  "matches": [
    {
      "pattern_id": "pattern-xxx",
      "matched": true,
      "confidence": "high",
      "reasoning": "issue 描述的问题与 pattern 的规则都指向缺少回滚方案",
      "evidence": ["issue.description", "pattern.rule"]
    }
  ]
}
```

只记录 `confidence in ["high", "medium"]` 的匹配。`low` 不作为 recurrence，只写 debug event 或忽略。

### 降级

- LLM 不可用时使用现有 `_match_pattern()`，但 event payload 增加 `mode: "rule_fallback"` 和 `confidence: "low"`。
- 不允许 fallback 结果自动降级或 deprecated pattern，只能作为提示。

## 改造三：Quality Packet Pattern 相关性筛选

### 当前问题

文件：

- `src/story_lifecycle/db/models.py`
- `src/story_lifecycle/orchestrator/quality.py`
- `src/story_lifecycle/orchestrator/nodes.py`

当前 `find_relevant_patterns(tags)` 只按 `applies_to` tag overlap 排序。active pattern 少时可用；pattern 多后，容易把标签相同但语义无关的规则注入 prompt，增加噪声。

### 设计

保留 DB 层 `find_relevant_patterns(tags)` 作为 cheap pre-filter，不在 DB 层调用 LLM。

在 Quality Packet 构建层增加 rerank：

```text
relevance_tags
  -> db.find_relevant_patterns(tags, limit=20)
  -> semantic.rerank_relevant_patterns(story_context, candidates, limit=5)
  -> inject top patterns
```

LLM 输入应包含：

- story key/title/type
- current stage
- PRD/design/review summary 的短上下文
- candidate patterns 的 `pattern/rule/applies_to/confidence`

LLM 输出：

```json
{
  "selected": [
    {
      "pattern_id": "pattern-xxx",
      "relevance": "high",
      "reasoning": "当前 story 涉及 schema migration，该 pattern 要求回滚方案"
    }
  ],
  "rejected": [
    {
      "pattern_id": "pattern-yyy",
      "reasoning": "标签相关但当前 story 不涉及外部依赖"
    }
  ]
}
```

### 降级

- LLM 不可用时继续使用 tag overlap 的前 5 条。
- Quality Packet 中标记 `Pattern selection mode: rule_fallback`，方便 review 判断噪声来源。

## 改造四：Review 摘要压缩

### 当前问题

文件：

- `src/story_lifecycle/orchestrator/seed_pipeline.py`

当前 `_summarize_review()` 通过 marker 行提取 review 内容。它不是最终 finding/pattern 生成逻辑，但会影响 seed analyst 的上下文质量。

### 设计

优先顺序：

1. 如果 review 是结构化 JSON，直接保留 `quality/issues/suggestions/trajectory_score/reasoning`。
2. 如果 review 是 markdown，调用 `semantic.summarize_review_for_learning()`。
3. 如果 LLM 不可用，使用现有 marker summary fallback。

LLM 输出：

```json
{
  "quality": "pass|revise|fail|unknown",
  "key_issues": [
    {
      "severity": "high|medium|low",
      "description": "问题描述",
      "evidence": "原文证据或文件位置",
      "recommendation": "建议"
    }
  ],
  "useful_for_learning": true,
  "summary": "适合喂给 seed analyst 的压缩摘要"
}
```

## 改造五：Debug Recovery Recommendation

### 当前状态

文件：

- `src/story_lifecycle/orchestrator/observability.py`

`build_debug_response()` 已经能聚合 story、recent events、route decisions、node errors、DoR/DoD、verification results 和 open findings，但还没有形成恢复建议。

### 设计

新增 `semantic.recommend_recovery(debug_packet)`，供后续 `story debug --recommend` 或 `story debug --apply retry` 使用。

流程：

```text
build_debug_response(story_key)
  -> semantic.recommend_recovery(debug_packet)
  -> print recommendation
  -> human confirm before apply
```

LLM 输出：

```json
{
  "failure_type": "done_file_parse_error|missing_expected_outputs|dor_blocked|dod_blocked|tool_crash|review_retry_exhausted|unknown",
  "likely_cause": "一句话说明",
  "recommended_action": "retry|retry_with_prompt|fix_input|ask_human|defer|fail",
  "safe_to_retry": true,
  "confidence": "high|medium|low",
  "evidence": [
    "route_decision: missing_expected_outputs",
    "node_error: JSONDecodeError"
  ],
  "human_message": "给操作者看的中文说明"
}
```

要求：

- `failure_type=unknown` 时，默认 `recommended_action=ask_human`，不能强行建议 retry。
- `safe_to_retry=false` 时，CLI 不允许 `--apply retry` 直接执行。
- LLM recommendation 只提供建议，不直接改变 story 状态。

## Prompt 与输出约束

所有 semantic 函数都遵循同一原则：

- temperature 固定为 `0` 或 `0.1`。
- prompt 明确要求只输出 JSON。
- 返回值必须经过本地 schema validation。
- 枚举字段非法时降级为安全默认值。
- 字符串字段做长度截断，避免污染 DB 或 prompt。
- 每次 fallback 都要带 warning，关键路径写 event log。

建议复用现有 LLM 配置：

- `STORY_LLM_API_KEY`
- `STORY_LLM_BASE_URL`
- `STORY_LLM_MODEL`

不要新增第二套 LLM 配置。

## 事件与可观测性

新增或扩展 event payload：

### `semantic_extraction`

用于 BugContext / review summary：

```json
{
  "task": "bug_context|review_summary",
  "mode": "llm|rule_fallback|unavailable|error",
  "confidence": "high|medium|low",
  "warnings": []
}
```

### `pattern_recurrence`

扩展现有 event：

```json
{
  "mode": "llm|rule_fallback",
  "recurrences": [
    {
      "pattern_id": "pattern-xxx",
      "pattern": "缺少回滚方案",
      "confidence": "high",
      "reasoning": "review issue 与 learned pattern 都指向缺少回滚或降级路径",
      "issue": {}
    }
  ],
  "count": 1
}
```

### `recovery_recommendation`

```json
{
  "failure_type": "done_file_parse_error",
  "recommended_action": "retry_with_prompt",
  "safe_to_retry": true,
  "confidence": "medium",
  "mode": "llm"
}
```

## 实施优先级

### P0：输入质量与质量飞轮准确性

1. 新增 `orchestrator/semantic.py` 基础设施。
2. Bug 正文结构化提取改为 LLM 主路径、regex fallback。
3. Pattern recurrence 改为 LLM 语义匹配、keyword fallback。

### P1：降低 Quality Packet 噪声

1. Quality Packet pattern selection 增加 LLM rerank。
2. Seed Pipeline review summary 增加 LLM compact summary。

### P2：恢复建议

1. 基于 `build_debug_response()` 增加 `recommend_recovery()`。
2. CLI 增加只读展示。
3. 后续再支持人工确认后的 `--apply retry`。

## 测试策略

### 单元测试

- LLM unavailable 时，每个 semantic 调用都返回 fallback，不抛异常。
- 非法 JSON 返回 `mode=error` 或 fallback。
- BugContext LLM 输出字段缺失时能补默认值。
- Pattern recurrence 只接受 medium/high confidence。
- `failure_type=unknown` 时 action 必须是 `ask_human`。

### Fake LLM 测试

用 monkeypatch 替换 semantic LLM client，覆盖：

- 正常 JSON。
- markdown fenced JSON。
- 非法 JSON。
- 空结果。
- 超长字段。

### 回归测试

- 现有无 LLM key 的 e2e 不应失败。
- `story create` 从 TAPD bug 导入时仍能保存基础 PRD / BugContext。
- Quality Packet 在无 LLM key 时仍使用原 tag overlap。
- router 现有 unhappy path LLM 行为不变。

## 验收标准

1. 无 LLM key 时，项目行为与当前版本兼容，最多增加 fallback warning。
2. TAPD Bug 正文即使没有标准标题，也能通过 LLM 抽取出主要复现信息。
3. Pattern recurrence event 包含 `mode/confidence/reasoning`。
4. Quality Packet pattern selection 支持 tag pre-filter + LLM rerank。
5. Review markdown summary 不再只依赖 marker 行。
6. Debug recommendation 对 unknown failure 默认 ask human。
7. 所有 LLM 输出都经过本地 schema validation。

## 风险与控制

### LLM 过度推断

控制：

- prompt 要求只基于输入证据。
- 输出必须包含 evidence。
- confidence 低的结果不进入关键决策。

### LLM 不稳定导致流程失败

控制：

- semantic 层吞掉 LLM 调用异常，返回 fallback。
- 业务流程不直接依赖裸 LLM response。

### Prompt 噪声增加

控制：

- Quality Packet rerank 限制最终注入数量。
- 保留 tag pre-filter，避免把所有 pattern 交给 LLM。

### 成本增加

控制：

- P0 只在 bug import 和 review recurrence 时调用。
- Pattern rerank 只处理候选 top 20。
- Debug recommendation 只在用户主动 `story debug` 时调用。

## 给实现 AI 的注意事项

- 不要删除现有 regex fallback。
- 不要在 DB 层调用 LLM。
- 不要让 LLM 结果绕过 schema validation。
- 不要把 recovery recommendation 做成自动执行。
- 不要改动 router 的确定性规则分支。
- 优先保持现有 public CLI/API 兼容。
