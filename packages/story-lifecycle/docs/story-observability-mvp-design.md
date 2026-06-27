# Story Observability MVP Design

## 背景

接下来要用新的真实需求跑 Story Lifecycle 全流程。当前系统已经有 `event_log`、`finding`、`learned_pattern`、`.story-context/`、`.story-done/` 和少量错误日志，但这些数据更偏审计和业务结果，不足以在失败时快速回答：

- 当前卡在哪个 graph 节点。
- router 为什么选择 `retry` / `fail` / `advance`。
- AI 实际拿到了哪些 Quality Packet / Checklist 上下文。
- DoD gate 为什么通过或阻塞。
- done file 是否存在、是否解析失败、session 是否还活着。

因此 P0 只补一层轻量可观测性，不做完整 tracing 平台。目标是在跑新需求时快速定位 80% 的流程问题。

## 目标

P0 只做最小可用排障闭环：

1. 记录关键 graph 节点错误。
2. 记录 router 决策证据。
3. 记录 prompt / Quality Packet 注入摘要。
4. 记录 DoD gate 检查结果。
5. 提供一个只读 story debug API，集中查看上述数据。

## 非目标

以下全部列为 P2，不进入 MVP：

- OpenTelemetry / Jaeger / Prometheus。
- 完整 `run_id` / `span_id` / 分布式 tracing 体系。
- `node_start` / `node_end` / `duration_ms`。
- poll loop 每次心跳记录。
- 完整 prompt 归档和 diff。
- TUI debug 面板。
- CLI `story log --debug`。
- source/TAPD sync 全量审计。
- 事件归档、压缩和长期指标看板。

## 与已有事件的关系

继续复用 `event_log`。当前已有质量相关事件包括：

- `story_intake`
- `readiness_check`
- `verification_result`
- `code_review_finding`
- `finding_status_changed`
- `quality_packet_generated`
- 旧的 `router`

P0 新增事件不替代这些事件，而是补齐运行时排障视角：

| 事件 | 作用 |
| --- | --- |
| `node_error` | 记录节点异常和 broad except 降级原因 |
| `route_decision` | 记录完整 router 决策上下文 |
| `prompt_context` | 记录 prompt 注入摘要和 hash |
| `dod_check` | 记录 DoD gate 结果 |

`route_decision` 是旧 `router` 事件的增强版。P0 可以短期双写 `router` 和 `route_decision` 以保持兼容；新 debug API 只读取 `route_decision`。后续 P2 再决定是否停止写旧 `router` 事件。

P0 不引入完整 tracing，但所有新增 observability 事件都必须携带轻量关联字段：

- `execution_count`: 当前 stage 第几次执行。
- `attempt_id`: 推荐格式为 `{stage}:{execution_count}`，例如 `implement:2`。

这两个字段用于在重试场景中关联同一次尝试内的 `prompt_context`、`node_error`、`route_decision` 和 `dod_check`。跨节点 span、跨进程 run id 和耗时统计仍属于 P2。

## P0 事件模型

### 1. `node_error`

用于记录 graph 节点异常，避免错误只落到本地 log 或被 broad except 静默吞掉。

```json
{
  "event_type": "node_error",
  "stage": "implement",
  "payload": {
    "node": "poll_completion_node",
    "error_type": "JSONDecodeError",
    "error": "Failed to parse .story-done/STORY-1/implement.json",
    "attempt_id": "implement:1",
    "execution_count": 1,
    "recoverable": true,
    "action": "set_last_error",
    "file_hint": ".story-done/STORY-1/implement.json"
  }
}
```

P0 不把完整 traceback 写入 DB，避免过大和泄露敏感信息。完整 traceback 仍写入本地日志文件。

### 2. `route_decision`

用于解释 router 为什么选择下一步。

```json
{
  "event_type": "route_decision",
  "stage": "design",
  "payload": {
    "action": "retry",
    "reason": "llm_router",
    "attempt_id": "design:1",
    "last_error": "CC process crashed (session dead)",
    "execution_count": 1,
    "trajectory_score": null,
    "review_summary": null,
    "router_mode": "llm",
    "provider_override": "deepseek",
    "provider_override_reason": "previous provider failed",
    "llm_reasoning": "Retry with another provider because the stage failed before producing done file.",
    "raw_action": "retry"
  }
}
```

所有 router 分支都应该记录：

- happy path `advance`
- `wait_confirm`
- retry fatigue
- low trajectory score
- missing expected outputs
- review-driven retry/fail
- LLM router retry/skip/fail

对于 LLM router 分支，payload 必须从 `state["_router_decision"]` 带出关键字段，至少包括：

- `router_mode`
- `provider_override`
- `provider_override_reason`
- `llm_reasoning`
- `raw_action`

这样 debug 时可以解释 provider 切换和 fallback 依据。

`router_mode` 不能通过 `state.get("_router_decision")` 隐式推断。每个 router 分支必须显式传入：

- `rule`: 本地规则分支，例如 happy path、missing expected outputs、low score。
- `review`: review-driven retry/fail。
- `llm`: LLM router 正常返回。
- `llm_fallback`: LLM router 超时、异常、解析失败后进入降级逻辑。

如果 LLM router 在写入 `_router_decision` 前失败，仍应记录 `router_mode="llm_fallback"` 和 `reason="llm_timeout"` / `llm_parse_failed` / `llm_exception`，避免误判为 rule 模式。

### 3. `prompt_context`

用于确认 AI 实际拿到了哪些质量上下文，而不保存完整 prompt。

```json
{
  "event_type": "prompt_context",
  "stage": "implement",
  "payload": {
    "quality_packet_injected": true,
    "quality_checklist_injected": true,
    "attempt_id": "implement:1",
    "execution_count": 1,
    "open_findings_count": 1,
    "learned_patterns_count": 2,
    "relevance_tags": ["implement", "orchestrator.nodes", "routing"],
    "has_prd": true,
    "has_plan_file": true,
    "prompt_sha256": "ab12...",
    "quality_context_sha256": "cd34..."
  }
}
```

### `prompt_context` 数据流

P0 采用明确方案：**prompt 渲染函数返回 prompt 和 metadata，不在 `execute_stage_node` 里重复调用 quality API。**

建议把当前 `_render_prompt(stage, state)` 调整为内部结构：

```python
@dataclass
class RenderedPrompt:
    text: str
    metadata: dict


def _render_prompt_with_metadata(stage: str, state: StoryState) -> RenderedPrompt:
    ...
```

`execute_stage_node` 使用流程：

```python
rendered = _render_prompt_with_metadata(stage, state)
prompt = rendered.text
metadata = rendered.metadata

if plan_path:
    prompt = f"{plan_content}\n\n---\n\n{prompt}"
    metadata["has_plan_file"] = True

metadata["prompt_sha256"] = sha256(prompt.encode("utf-8")).hexdigest()
log_prompt_context(state, metadata)
```

这样可以保证 `open_findings_count`、`learned_patterns_count`、`relevance_tags` 与实际注入内容一致，也避免重复生成 Quality Packet 造成数据不一致。

`_render_prompt` 如果还被其他代码调用，可以保留为兼容 wrapper：

```python
def _render_prompt(stage: str, state: StoryState) -> str:
    return _render_prompt_with_metadata(stage, state).text
```

`prompt_sha256` 的语义是“最终发送给 AI 的完整 prompt 审计指纹”，不保证可用于比较两次是否使用了相同质量上下文。如果 prompt 包含时间戳、动态路径、随机排序或运行时状态，完整 hash 每次都可能变化。

为了支持上下文比对，metadata 需要额外提供 `quality_context_sha256`：只对稳定的 Quality Packet / Checklist 摘要做 hash。排查“是否注入了相同质量上下文”时优先比较 `quality_context_sha256`，排查“这次实际发送了哪个 prompt”时使用 `prompt_sha256`。

### 4. `dod_check`

用于解释 story/stage 为什么能 advance 或被 gate 阻塞。

```json
{
  "event_type": "dod_check",
  "stage": "implement",
  "payload": {
    "passed": false,
    "attempt_id": "implement:1",
    "execution_count": 1,
    "blocking": ["1 open high finding(s)"],
    "warnings": ["no verification result recorded"],
    "open_high_count": 1,
    "verification_present": false
  }
}
```

`advance_node` 调用 `check_dod()` 后记录 `dod_check`。无论通过还是失败都记录一条事件。`check_dod()` 自身异常记录 `node_error`，不再静默 `pass`。

## Debug API

新增：

```text
GET /api/story/{story_key}/debug
```

返回：

```json
{
  "story": {
    "storyKey": "TAPD-001234",
    "stage": "implement",
    "status": "blocked",
    "lastError": "DoD gate failed: ..."
  },
  "recentEvents": [],
  "routeDecisions": [],
  "nodeErrors": [],
  "promptContexts": [],
  "dodChecks": [],
  "openFindings": [],
  "quality": {
    "dor": {},
    "dod": {}
  },
  "fileHints": {
    "storyContextDir": ".story-context/TAPD-001234",
    "doneDir": ".story-done/TAPD-001234",
    "graphErrorLog": "~/.story-lifecycle/graph_error.log",
    "plannerErrorLog": "~/.story-lifecycle/planner_error.log"
  }
}
```

只读规则：

- Debug endpoint 不调用任何会写 `event_log` 的函数。
- DoR 使用 `check_dor(record=False)`。
- DoD 直接调用当前纯查询 `check_dod(story_key, stage)`。
- Debug endpoint 不记录自身访问事件。

## 集成点

### `router_node`

在每个分支设置 `_next_action` 后调用 `log_route_decision()`。

建议 helper：

```python
def log_route_decision(state, action: str, reason: str, extra: dict | None = None):
    decision = state.get("_router_decision") or {}
    execution_count = state.get("execution_count", 0)
    router_mode = (extra or {}).get("router_mode")
    db.log_event(
        state["story_key"],
        state["current_stage"],
        "route_decision",
        {
            "action": action,
            "reason": reason,
            "attempt_id": f"{state['current_stage']}:{execution_count}",
            "last_error": state.get("last_error"),
            "execution_count": execution_count,
            "trajectory_score": state.get("trajectory_score"),
            "review_summary": state.get("review_summary"),
            "router_mode": router_mode,
            "provider_override": decision.get("provider_override"),
            "provider_override_reason": decision.get("provider_override_reason"),
            "llm_reasoning": decision.get("reasoning"),
            "raw_action": decision.get("action"),
            **(extra or {}),
        },
    )
```

调用方必须传入 `router_mode`，helper 不根据 `_router_decision` 猜测模式。LLM router 调用失败时也要写 `route_decision`，并显式使用 `router_mode="llm_fallback"`。

### `execute_stage_node`

使用 `_render_prompt_with_metadata()` 生成 prompt 和 metadata。组装 plan 文件后计算最终 `prompt_sha256`，再写 `prompt_context`。

必须避免在 `_render_prompt_with_metadata()` 内直接写事件，因为它可能被测试或其他路径调用，容易重复记录。

### `advance_node`

调用 `check_dod()` 后写 `dod_check`：

- passed: true 时写事件，然后继续 advance。
- passed: false 时写事件，设置 `last_error`，回到 router。
- `check_dod()` 抛异常时写 `node_error`，并设置 `last_error`，不静默吞掉。

### `poll_completion_node`

P0 补充两个高价值错误：

- done file 解析失败：写 `node_error`，`node="poll_completion_node"`，`recoverable=true`，`action="set_last_error"`。
- session dead：写 `node_error` 或 `poll_failure`；P0 为减少事件类型，统一写 `node_error`，`error_type="SessionDead"`。

### `plan_stage_node`

planner fallback 时写 `node_error`：

- `node="plan_stage_node"`
- `recoverable=true`
- `action="fallback_to_default_plan"`
- `file_hint="~/.story-lifecycle/planner_error.log"`

### `review_stage_node`

reviewer 失败并跳过 review 时写 `node_error`：

- `node="review_stage_node"`
- `recoverable=true`
- `action="skip_review"`

### Quality Injection

Quality Packet / Checklist 注入失败时写 `node_error`：

- `node="execute_stage_node"`
- `recoverable=true`
- `action="continue_without_quality_context"`

## broad except 覆盖矩阵

P0 不重构所有 broad except，但要明确哪些补事件、哪些后移。

| 位置 | 当前行为 | P0 处理 |
| --- | --- | --- |
| `plan_stage_node`: condenser failed | warning 后继续 | P2，只影响压缩上下文 |
| `plan_stage_node`: planner failed fallback | 写 planner log 后默认计划 | P0 写 `node_error` |
| `review_stage_node`: reviewer failed skipping review | warning 后跳过 | P0 写 `node_error` |
| `_check_pattern_recurrence`: pattern query failed | 静默返回 | P2，非主流程 |
| `execute_stage_node`: tool dispatch / prompt assembly failed | 可能直接抛出 | P0 写 `node_error` 后继续抛出或转 last_error |
| `poll_completion_node`: done file parse failed | 设置 `last_error` | P0 写 `node_error` |
| `poll_completion_node`: session dead | 设置 `last_error` | P0 写 `node_error` |
| `advance_node`: `check_dod` failed | 当前静默 `pass` | P0 写 `node_error`，不静默通过 |
| `advance_node`: source sync failed | warning 后继续 | P2，外部同步问题 |
| design cleanup unlink failed | 静默忽略 | P2，非主流程 |

## Debug API 取数策略

P0 不做复杂分页和长期归档，但不能只用全局最近 50 条事件。长时间、多重试 story 中，早期 `prompt_context` 或第一次 router 决策可能被后续事件挤掉。

Debug endpoint 采用两层取数：

1. `recentEvents`: 最近 50 条相关事件，用于快速看整体时间线。
2. 分类桶独立 limit：
   - `routeDecisions`: 最近 20 条。
   - `nodeErrors`: 最近 20 条。
   - `promptContexts`: 最近 10 条。
   - `dodChecks`: 最近 20 条。
   - `verificationResults`: 最近 5 条。
   - `readinessChecks`: 最近 5 条。

P0 可支持简单 query 参数：

```text
GET /api/story/{story_key}/debug?limit=100
GET /api/story/{story_key}/debug?event_type=prompt_context&limit=20
```

分页、游标和复杂过滤仍放到 P2。

相关事件类型：

```text
route_decision
node_error
prompt_context
dod_check
router
review
execute
complete
fail
retry
skip
verification_result
code_review_finding
finding_status_changed
readiness_check
story_intake
```

P2 再考虑事件压缩、归档和指标化。

## P0 实施步骤

1. 增加只读 debug helpers，例如 `quality_debug.py`。
2. 增加 `log_node_error()`、`log_route_decision()`、`log_prompt_context()`、`log_dod_check()` helper。
3. 增加 `_render_prompt_with_metadata()`，保留 `_render_prompt()` wrapper。
4. 在 `router_node` 接入 `route_decision`。
5. 在 `execute_stage_node` 接入 `prompt_context`。
6. 在 `advance_node` 接入 `dod_check` 和 DoD 异常 `node_error`。
7. 在 `poll_completion_node` 接入 done file parse/session dead `node_error`。
8. 在 planner/reviewer fallback 接入 `node_error`。
9. 所有 P0 observability 事件统一写 `attempt_id` 和 `execution_count`。
10. 新增 `GET /api/story/{story_key}/debug`，实现分类桶独立 limit。

## 测试计划

新增 `tests/test_observability.py`。

测试策略：

- 使用现有 pytest DB fixture，确保每个测试使用独立 SQLite 临时库。
- 使用 fake/demo tool 跑一轮最小 graph，避免真实 AI CLI。
- 通过 `db.get_recent_quality_events()` 或 event helper 断言 `event_log`。

必测用例：

1. `router_node` happy path 写 `route_decision(action=advance, router_mode=rule)`。
2. LLM router 返回 `provider_override` 时，`route_decision` 包含 provider 和 reasoning 字段。
3. LLM router 超时/异常时写 `route_decision(router_mode=llm_fallback)`，不能误记为 rule。
4. Quality Packet 注入后写 `prompt_context`，且包含 `prompt_sha256`、`quality_context_sha256`、counts、tags。
5. 同一 stage 多次 retry 时，`prompt_context`、`route_decision`、`dod_check` 带有可匹配的 `attempt_id`。
6. `advance_node` DoD 通过和失败都写 `dod_check`。
7. `check_dod()` 抛异常时写 `node_error`，且不静默完成 stage。
8. done file JSON 解析失败写 `node_error`，并设置 `last_error`。
9. Debug API 只读：调用前后 `readiness_check`、`dod_check`、`route_decision` 事件数量不变。
10. Debug API 返回已有 `readiness_check`、`verification_result` 和新增 observability 事件，验证新旧事件互补。
11. Debug API 分类桶独立 limit，不因 `recentEvents` 超过 50 条而丢失最近的 `prompt_context`。

## P2 项目

P2 统一后置：

- `run_id` / `stage_run_id` / span correlation。
- `node_start` / `node_end` / `duration_ms`。
- poll heartbeat 和细粒度 done-file diagnostics。
- source sync start/end/error。
- CLI `story log --debug`。
- TUI debug panel。
- OpenTelemetry / metrics backend。
- prompt archive 和 diff。
- 事件归档/压缩。

## 成功标准

用新需求跑全流程时，若 story blocked 或卡住，开发者能在一个 debug 响应里回答：

1. 最近一次 router 选择了什么 action，原因是什么。
2. LLM router 是否切换 provider，为什么切换。
3. 最近是否有 node_error，发生在哪个节点。
4. AI prompt 是否注入了 Quality Packet / Checklist，注入了哪些 tags。
5. DoD gate 是否通过，不通过的 blocking/warnings 是什么。
6. 当前 open findings 是哪些。
7. 应该去哪个目录或日志文件继续排查。

## Review Focus

请重点 review：

1. `prompt_context` metadata 返回方案是否足够清晰。
2. `node_error` P0 覆盖矩阵是否抓住最高频排障点。
3. `route_decision` 是否包含足够的 LLM/provider 决策字段。
4. debug endpoint 只读约束是否明确。
5. 测试计划是否能验证真实 graph 执行会写事件。
6. P2 是否拆得足够干净，没有混入 MVP。
