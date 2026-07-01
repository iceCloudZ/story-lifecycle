> ⚠️ **历史快照（归档于 2026-07）**：描述的架构可能已被后续演进取代。当前架构见 [../../ARCHITECTURE.md](../../ARCHITECTURE.md)。本文件保留作决策记录（ADR），正文未修改。

---

# 质量飞轮设计 Brief：Story 全流程闭环

## 背景

Story Lifecycle Manager 当前已经能把一个 story 编排为多阶段工作流：

```text
design -> implement -> test
```

现有 brief 主要关注 `plan -> code -> test -> review` 的短链路，并提出把 Reviewer 的 `trajectory_score` 反馈给 Planner。这个方向有价值，但不足以让质量飞轮真正转起来。

最近一次 Cross-AI Code Review 暴露了更典型的问题：

- 测试只断言最终状态，漏掉错误路径。
- Scenario DSL 的 `expect` 没有被 runner 消费，测试数据和断言脱节。
- 同一 stage 多次执行时静默复用最后一次 payload，掩盖异常重试。
- sub-story 测试 patch 掉真实 interrupt，未覆盖生产边界。
- lint/test 信号没有统一记录为 story 质量证据。

这些问题说明：质量飞轮的核心不是“分数反馈”，而是 **结构化问题能否被追踪、接受、修复、验证、沉淀为下次 story 的约束**。

## 参考框架

本设计借鉴三类外部框架，但不照搬：

- DORA：关注端到端交付表现，包括 lead time、deployment frequency、change failure rate、recovery time。参考：https://dora.dev/guides/dora-metrics/
- Agile story workflow / backlog refinement：story 从 intake、澄清、拆分、开发、验证到反馈是连续流动的，不是一次性 prompt。参考：https://www.atlassian.com/agile/project-management/workflow
- SPACE developer productivity：不能只看 activity，需要综合 performance、communication、flow 等维度。参考：https://space-framework.com/

## 目标

建立覆盖 story 全流程的数据飞轮，让每个 story 的质量事实能反馈到后续 story：

```text
Intake -> Refinement -> Planning -> Execution -> Review -> Verification -> Learning -> Next Story
```

P0/P1 聚焦本地和开发闭环；发布/生产反馈列为 P2。

## 非目标

- 不在 P0/P1 实现生产监控、incident、rollback、用户反馈接入。
- 不新增复杂数据仓库；但 P0 需要新增轻量 `finding` 当前状态表，`event_log` 继续作为审计轨迹。
- 不让质量飞轮阻塞所有流程；只对 high severity、verification failed 等明确高风险情况提供 gate。
- 不把所有历史原文塞进 prompt；只注入压缩后的 Quality Packet。

## 当前问题

### 1. 分数不可执行

`trajectory_score=0.7` 只能说明“质量一般”，但不能告诉 Planner 下一步该怎么做。真正可执行的是：

- 哪个文件/行为错了。
- 根因是什么。
- 应该补什么测试。
- 是否已经验证。
- 这个模式是否反复出现。

### 2. Review findings 没有生命周期

当前 review event 更像一次性记录。缺少：

```text
open -> accepted -> fixed -> verified -> learned
```

没有生命周期，就无法区分“已发现但未修”、“已修但未验证”、“已验证并沉淀为规则”。

### 3. Verification 不是一等数据

测试和 lint 现在只存在于终端输出或人工总结里，没有稳定进入 story 数据流。Planner 也不知道：

- 这次是否跑过 `pytest`。
- 是否跑过 `ruff check src tests`。
- 是否新增 regression test。
- 测试覆盖的是 happy path 还是错误路径。

### 4. Story intake/readiness 缺少质量判断

story 创建时通常只记录 key/title/context，但没有结构化判断：

- PRD 是否存在。
- 是否有验收标准。
- 是否有影响模块。
- 是否存在 scope ambiguity。
- 是否需要拆子任务。

这会导致 Planner 在信息不足时直接规划实现。

### 5. 历史问题没有反哺下一次 story

例如最近 learned pattern：

```text
修改 graph routing 时，不能只断言最终 status；必须断言路径行为，例如 event_counts、last_error、retry 次数。
```

这类规则应该在后续类似 story 的 Planner/Executor prompt 中出现。

## 设计原则

1. **Finding first, score second**
   分数用于趋势，finding 用于行动。

2. **Evidence over assertion**
   “已修复”必须绑定 verification evidence。

3. **Compact feedback**
   Planner 只消费 Quality Packet，不消费完整历史日志。

4. **Story-wide lifecycle**
   覆盖 intake/refinement/planning/execution/review/verification/learning，不只覆盖 coding 阶段。

5. **Progressive rollout**
   P0 先把本地开发闭环跑起来，P1 做相似 story 检索和 prompt 注入，P2 再接发布/生产反馈。

## 核心模型

### 1. Finding

Finding 是质量飞轮的核心数据。

```json
{
  "id": "finding-20260523-001",
  "story_key": "TAPD-001234",
  "stage": "implement",
  "source": "code_review",
  "severity": "high",
  "category": "routing",
  "location": "src/story_lifecycle/orchestrator/nodes.py:747",
  "description": "advance_node missing expected outputs 后没有回到 router",
  "root_cause": "route_after_advance 只判断 completed，未处理 last_error",
  "recommendation": "last_error 存在时 route 到 router，并补错误路径 E2E",
  "status": "open",
  "created_at": "2026-05-23T12:00:00+08:00"
}
```

状态流转：

```text
open -> accepted -> fixed -> verified -> learned
               -> ignored

fixed    -> open    # 验证失败，修复无效
verified -> open    # 后续回归或同类问题复发
learned  -> open    # learned pattern 被证明不充分或误导
ignored  -> open    # 后续重新确认该问题有效
```

状态含义：

- `open`：review 发现但未处理。
- `accepted`：确认是有效问题，准备修复。
- `fixed`：代码或文档已修改。
- `verified`：通过测试/lint/review 证据验证。
- `learned`：已沉淀为可复用规则。
- `ignored`：明确不处理，需要理由。

状态回退必须保留审计证据。例如 `fixed -> open` 需要绑定失败的 `verification_result`，`verified -> open` 需要绑定复发位置或新的 finding。这样 DoD gate 能区分“未处理问题”和“修复失败问题”。

### 2. Verification Evidence

Verification 是修复闭环的证据。

```json
{
  "event_type": "verification_result",
  "payload": {
    "story_key": "TAPD-001234",
    "stage": "review",
    "commands": [
      {
        "cmd": "pytest",
        "status": "passed",
        "summary": "72 passed in 3.07s"
      },
      {
        "cmd": "ruff check src tests",
        "status": "passed",
        "summary": "All checks passed"
      }
    ],
    "regression_tests_added": true,
    "covered_findings": ["finding-20260523-001"],
    "commit": "709c4ca"
  }
}
```

### 3. Learned Pattern

当 finding verified 后，可以沉淀为 learned pattern。

```json
{
  "event_type": "learned_pattern",
  "payload": {
    "pattern": "Graph routing changes require path-level assertions",
    "applies_to": ["orchestrator.graph", "orchestrator.nodes", "langgraph"],
    "rule": "不要只断言最终 status；必须断言 event_counts、last_error 或 next_action",
    "source_findings": ["finding-20260523-001"],
    "confidence": "high"
  }
}
```

Learned Pattern 不能由 LLM 自动直接进入 active 知识库，必须经过人工确认，避免过度泛化或污染后续 prompt。

状态流转：

```text
proposed -> approved -> active -> deprecated
         -> rejected
```

防毒化约束：

- `applies_to` 必须尽量窄，例如 `["orchestrator.graph", "langgraph-routing"]`，不能写成 `["all-code"]`。
- 规则必须是可执行 checklist，不能是泛泛建议。
- 如果 pattern 会影响 blocking gate，必须人工 approval。
- Quality Packet 只注入 `active` learned patterns。

### 4. Quality Packet

Quality Packet 是 Planner/Executor 消费的压缩输入。

示例：

```text
Quality Packet for TAPD-001234

Readiness:
- PRD present: yes
- Acceptance criteria: missing
- Risk tags: graph-routing, test-quality

Open Findings:
- none

Recent Learned Patterns:
- Graph routing changes require path-level assertions:
  Do not assert final status only. Assert event_counts, last_error, retry count, or route result.

Verification Baseline:
- Last run: pytest passed, ruff passed
- Regression tests added for previous high finding: yes

Stage Checklist:
- If modifying graph edges, add at least one error-path E2E.
- If adding scenario fields, runner must consume them in assertions.
```

Quality Packet 不应该超过一个 prompt 小节，默认限制：

- open high/medium findings 最多 5 条。
- learned patterns 最多 5 条。
- verification summary 最多 5 条命令。
- 相似 story 最多 3 个。

## 事件设计

优先复用 `event_log`。新增事件类型：

P0 新增轻量 `finding` 表保存当前状态，`event_log` 保存审计轨迹。原则是：

```text
finding table = 当前状态，用于查询、DoD gate、TUI/API 展示
event_log     = 状态变化和证据，用于审计、回放、质量摘要
```

建议表结构：

```sql
CREATE TABLE finding (
  id TEXT PRIMARY KEY,
  story_key TEXT NOT NULL,
  stage TEXT,
  source TEXT NOT NULL,
  severity TEXT NOT NULL,
  category TEXT NOT NULL,
  location TEXT,
  description TEXT NOT NULL,
  recommendation TEXT,
  status TEXT NOT NULL,
  root_cause TEXT,
  verification_event_id INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX idx_finding_story_status ON finding(story_key, status);
CREATE INDEX idx_finding_severity_status ON finding(severity, status);
```

`event_log` 新增事件类型：

```text
story_intake
readiness_check
code_review_finding
finding_status_changed
verification_result
learned_pattern
quality_packet_generated
```

### story_intake

story 创建或从 TAPD/Jira 导入时记录。

```json
{
  "source": "tapd",
  "source_id": "123456",
  "has_prd": true,
  "has_acceptance_criteria": false,
  "risk_tags": ["unclear_acceptance", "cross_module"],
  "suggested_start_stage": "design"
}
```

### readiness_check

进入 `plan_stage` 前执行轻量检查。

```json
{
  "ready": false,
  "missing": ["acceptance_criteria"],
  "warnings": ["affected_modules not declared"],
  "action": "continue_with_warning"
}
```

### code_review_finding

来自 Cross-AI Code Review 或深度 reviewer。

```json
{
  "findings": [
    {
      "id": "finding-20260523-001",
      "severity": "high",
      "category": "routing",
      "location": "nodes.py:747",
      "description": "advance error path routes back to plan_stage",
      "recommendation": "route last_error to router"
    }
  ],
  "scores": {
    "correctness": 0.7,
    "test_quality": 0.6,
    "maintainability": 0.8
  }
}
```

### finding_status_changed

记录 finding 生命周期。

```json
{
  "finding_id": "finding-20260523-001",
  "from": "fixed",
  "to": "verified",
  "reason": "pytest and ruff passed",
  "verification_event_id": 42
}
```

## Flow 设计

### P0：本地闭环

P0 目标：让 review finding 能闭环到 verification 和 learned pattern。

流程：

```text
code_review output
  -> parse findings
  -> write code_review_finding event
  -> developer/AI accepts finding
  -> fix + regression test
  -> run verification commands
  -> write verification_result event
  -> mark finding verified
  -> generate learned_pattern
```

P0 不需要相似 story 检索，也不需要生产反馈。

### P1：Planner/Executor 注入

P1 目标：让历史质量数据影响后续 story。

流程：

```text
plan_stage_node
  -> _load_quality_packet(story_key, stage)
  -> inject into planner.plan_stage prompt
  -> write quality_packet_generated event

execute_stage_node
  -> prepend Quality Checklist into task file
  -> Executor sees concrete acceptance and risk rules
```

注入策略：

- Planner 获取完整 Quality Packet。
- Executor 只获取 Stage Checklist 和 relevant learned patterns。
- Reviewer 获取 open findings 和 DoD checklist，用于检查是否复发。

### P2：发布/生产反馈

P2 目标：接入 release/deploy/incident/user feedback。

P2 事件：

```text
deploy_result
prod_signal
incident
rollback
user_feedback
postmortem
```

示例：

```json
{
  "event_type": "prod_feedback",
  "payload": {
    "deployment_id": "release-2026-05-23",
    "change_failed": true,
    "incident_id": "INC-123",
    "root_cause": "missing migration validation",
    "linked_story": "TAPD-001234"
  }
}
```

P2 明确不进入本轮实现。

## Definition of Ready / Done

### Definition of Ready

用于 story 进入 planning 前的轻量检查。

```yaml
definition_of_ready:
  required:
    - title
    - source
  recommended:
    - prd_path
    - acceptance_criteria
    - affected_modules
    - risk_tags
```

策略：

- 缺 required：阻塞或要求补充。
- 缺 recommended：允许继续，但写 `readiness_check` warning，并进入 Quality Packet。

### Definition of Done

用于 story 完成前的质量检查。

```yaml
definition_of_done:
  required:
    - no_open_high_findings
    - verification_result_present
    - expected_outputs_present
  recommended:
    - regression_tests_added_for_bugfix
    - lint_passed
    - learned_patterns_generated
```

策略：

- high finding 未 verified：默认阻塞完成。
- verification 缺失：允许人工 override，但写 warning。
- regression test 缺失：bugfix 类 story 默认 warning，核心模块默认阻塞。

## Prompt 注入设计

### Planner Prompt

新增小节：

```text
## Quality Packet
{quality_packet}

请基于上述质量历史调整本阶段计划：
1. 优先处理 open high/medium findings。
2. 对 Recent Learned Patterns 中与当前 stage 相关的规则，写入任务书。
3. 如果 readiness_check 有 missing recommended fields，先补齐上下文或降低计划置信度。
```

### Executor Task File

新增小节：

```text
## Quality Checklist
- 修改 graph routing 时，必须新增错误路径 E2E。
- 测试不能只断言最终 status，需要断言路径证据。
- 完成后运行：pytest；ruff check src tests。
```

### Reviewer Prompt

Add a Review Focus section:

```text
## Review Focus
- Check whether open findings were fixed.
- Check whether active learned patterns were followed.
- Check whether verification evidence exists and covers this change.
```

## API / Function Design

### quality.py

New module:

```text
src/story_lifecycle/orchestrator/quality.py
```

Suggested functions:

```python
def record_finding(story_key: str, stage: str, finding: dict) -> str:
    """Insert into finding table, write code_review_finding event, return finding_id."""

def update_finding_status(
    story_key: str,
    finding_id: str,
    status: str,
    reason: str = "",
    evidence: dict | None = None,
) -> None:
    """Update finding.status and append finding_status_changed event."""

def record_verification(
    story_key: str,
    stage: str,
    commands: list[dict],
    covered_findings: list[str] | None = None,
    commit: str | None = None,
) -> None:
    """Write verification_result event."""

def get_open_findings(story_key: str, min_severity: str = "medium") -> list[dict]:
    """Query finding table for current open findings."""

def build_quality_packet(story_key: str, stage: str, max_items: int = 5) -> str:
    """Load relevant findings, learned patterns, verification summary and format prompt text."""

def build_quality_checklist(story_key: str, stage: str) -> str:
    """Return compact checklist for Executor task file."""
```

### DB Queries

P0 adds a lightweight `finding` table for current state. `event_log` remains the audit trail. DoD gates, TUI/API views, and Quality Packet generation query `finding` first instead of replaying events to compute current status.

Required helpers:

```python
def get_events_by_type(story_key: str, event_type: str) -> list[dict]:
    ...

def get_recent_quality_events(
    story_key: str,
    event_types: list[str],
    limit: int = 50,
) -> list[dict]:
    ...

def get_open_findings(story_key: str, min_severity: str = "medium") -> list[dict]:
    ...

def get_finding(finding_id: str) -> dict | None:
    ...
```

`event_log` still records `finding_status_changed`, `verification_result`, and related audit events.

## Profile Config

Optional config:

```yaml
quality:
  enabled: true
  readiness_check: true
  inject_quality_packet: true
  inject_executor_checklist: true
  block_on_open_high_findings: false
  verification_commands:
    - pytest
    - ruff check src tests

stages:
  design:
    quality:
      require_readiness: false
  implement:
    quality:
      require_regression_for_bugfix: true
  test:
    quality:
      require_verification_result: true
```

Config principles:

- Record by default; do not hard-block by default.
- High-finding gates can move gradually from warning to blocking.
- Different profiles can tune strictness by team maturity.

## Integration With Existing Nodes

### create story / source sync

Record:

- `story_intake`
- source metadata
- initial risk tags

### plan_stage_node

Before planning:

- Run/read readiness check.
- Build Quality Packet.
- Inject Quality Packet into planner prompt.
- Record `quality_packet_generated`.

### execute_stage_node

When building the task file:

- Inject Quality Checklist.
- Inject relevant active learned patterns.

### review_stage_node

During review:

- Check whether open findings recur.
- Emit new findings.
- Update score based on findings.

### advance_node

Before completing a stage:

- Check DoD gate.
- Open high findings can block or warn.
- Missing verification can warn.

## P0 Scope

P0 is the minimum local loop:

1. Add `finding` current-state table and DB helpers.
2. Add basic `quality.py` functions.
3. Write `code_review_finding`, `finding_status_changed`, and `verification_result` events.
4. Implement `build_quality_packet()`.
5. Implement `build_quality_checklist()`.
6. Inject Quality Packet into Planner prompt.
7. Inject Quality Checklist into Executor task file.
8. Tests cover:
   - finding lifecycle.
   - `fixed -> open` when verification fails.
   - `verified -> open` when recurrence is detected.
   - verification_result recording.
   - quality packet formatting.
   - prompt/task file quality sections.

## P1 Scope

P1 reuses quality memory:

1. learned pattern `proposed -> approved -> active` lifecycle.
2. Similar-history lookup by story/source/module/risk_tag/category/touched_paths.
3. Quality Packet injects only strongly related structured records, not merely recent records.
4. Reviewer checks whether active learned patterns recur.
5. DoR/DoD gates become configurable.
6. TUI/API show open findings, verification state, and learned pattern approval state.
7. Semantic embedding lookup is deferred to P1.5 or P2 pre-work; P1 stays deterministic and testable.

## P2 Scope

P2 connects release and production feedback:

1. deploy result event.
2. incident/rollback/user feedback event.
3. DORA-like story summary.
4. production feedback into Quality Packet.
5. pre-release quality gate.

P2 is not part of the current implementation target.

## Risks And Handling

### Prompt Bloat

- Limit Quality Packet item count and length.
- Include only current-stage relevant content.
- Keep long findings as summaries with file references.
- Use structured relevance filtering first to reduce noise.

### finding Query And Audit Complexity

- P0 queries current state from `finding`.
- `event_log` stores audit trail and verification evidence.
- If cross-story trend aggregation becomes needed, add summary helpers instead of replaying all events online.

### Blocking Too Early

- Default to warning-first.
- Only open high findings and verification failures can become optional blocking gates.
- Manual override is allowed but must record a reason.

### Unstable Review Output

- Validate finding schema strictly.
- If parsing fails, downgrade to markdown review and do not create structured findings.
- Structured findings require severity/category/location/description.

### Learned Pattern Poisoning

- learned patterns must be human-approved before becoming active.
- `applies_to` must be narrow.
- rejected/deprecated patterns do not enter Quality Packet.

## Success Metrics

P0 success:

- Every code review finding is recorded in `finding` and audit events.
- Fixes can be linked to verification_result.
- Verification failure can trigger `fixed -> open`.
- Planner prompt can include compact Quality Packet.
- Executor task file can include Quality Checklist.

P1 success:

- Similar future stories can see strongly related active learned patterns.
- Reviewer can check whether learned patterns recur.
- TUI/API can show open high findings, verification state, and learned pattern approval state.

P2 success:

- story can link deploy/incident/user feedback.
- production failure can generate proposed learned patterns and enter future planning after approval.

## 启动资产盘点（2026-05-23）

### 现有 PRD（可作为 intake 测试数据）

| 文件 | 说明 |
|------|------|
| `hc-all/prd/1065520.md` | Story 1065520，已生成 PRD |
| `hc-all/prd/1065534.md` | Story 1065534，已生成 PRD |
| `hc-all/prd/1064811【HC】新客7天免息活动.md` | 23.8K 完整 PRD |
| `hc-all/prd/1064006【HC】反欺诈贷前策略流.md` | 反欺诈需求 |
| `hc-all/prd/2026-04-22-STORY-1064348-用户列表新增联系人筛选.md` | 联系人筛选 |
| `hc-all/prd/2026-04-22-STORY-1064685-逾期利息收取方式调整.md` | 逾期利息调整 |

### 设计文档（可提取 learned patterns）

`hc-all/docs/superpowers/specs/` 下共 44 份设计文档，覆盖：

- **业务功能**：还款方式、授信、反欺诈、免息活动、账号合并、额度校验等
- **基础设施**：监控告警、MQ 重试、API 自动化、journey 测试、生产监控
- **story-lifecycle 系统**：v2 设计、orchestration、board、langgraph 集成

每份设计文档背后是一次完整的 intake → design → implement → review 循环，可提取为第一批 proposed learned patterns。

### TAPD 待办（可验证 intake 通道）

- **10 条 open bug**：填资/KTP/额度分层/UI/国际化等问题，可作为 bug-fix 子故事来源
- **10 条 story**：含授信提现展示拒绝原因等，PRD 指向钉钉文档链接
- workspace_id: 44381896, owner: 赵子豪

### 已有 Story 执行记录

- `hc-all/.story-context/1065520/plan_design.md` — 已跑过一轮 story lifecycle
- `hc-all/.story-context/1065534/plan_design.md` — 同上

### 跨 AI Code Review 发现的典型问题（可直接作为 seed findings）

- 测试只断言最终状态，漏掉错误路径
- Scenario DSL 的 `expect` 没被 runner 消费，测试数据和断言脱节
- 同一 stage 多次执行时静默复用最后一次 payload，掩盖异常重试
- sub-story 测试 patch 掉真实 interrupt，未覆盖生产边界
- lint/test 信号没有统一记录为 story 质量证据

### 启动策略

1. **P0 第一步**：建立 finding 表 + quality.py 基础函数
2. **Seed findings**：将上述 5 个典型问题手动录入为第一批 findings，验证生命周期
3. **Intake 测试**：用 TAPD 10 条 open story/bug 验证 story_intake → readiness_check
4. **Verification baseline**：复用现有 pytest + ruff 体系
5. **Learned patterns seed**：从 44 份设计文档提取 Top 5 反复出现的问题模式

## Review Focus

Please review:

1. Whether finding lifecycle fallback paths cover verification failure and recurrence.
2. Whether the P0 `finding` table schema supports DoD gates, TUI/API views, and Quality Packet generation.
3. Whether structured relevance filtering is enough for Quality Packet, or semantic search should move earlier.
4. Whether warning-first DoR/DoD gates are appropriate.
5. Whether P0/P1/P2 layering is clear and production feedback should remain P2.
6. Whether learned pattern approval, poisoning controls, and `applies_to` scoping are sufficient.
