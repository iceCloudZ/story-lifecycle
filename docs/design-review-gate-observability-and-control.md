# Review Gate Observability and Control Design

Date: 2026-05-25

## Background

Story Lifecycle Manager 的目标不是简单地启动一个 AI CLI，而是把 AI 工作变成可编排、可恢复、可观察、可审计的多阶段流程。当前主流程是：

```text
plan_stage -> execute_stage -> poll_completion -> review_stage -> router -> advance/retry/wait_confirm/fail
```

stage executor 通过 `.story-done/{story_key}/{stage}.json` 与 orchestrator 握手。done 文件被消费后，系统进入 review gate，由 reviewer 检查阶段产物质量，再由 gate 决定是否进入下一阶段、重试、暂停等待人工确认，或失败。

1064993 暴露了 review gate 这条链路的几个系统性问题：

1. 用户看到 `design.json` 被写入，但 story 没进入 `implement`。
2. 初始排查误以为 done 文件没有写入，后来确认它可能被消费。
3. 发现 `execute_stage_node` 和 `poll_completion_node` 曾经都是 done 文件消费者，导致文件被提前删除后又继续等待同一个文件。
4. 修复单消费者问题后，done 文件确实被 `poll_completion_node` 消费，但 story 仍停在 `design`。
5. 进一步查看 DB 发现 review gate 走到了 `forced_fail` 和 `wait_confirm`：

```json
{
  "event_type": "review",
  "payload": {
    "quality": "forced_fail",
    "retries": 9
  }
}
```

```json
{
  "event_type": "route_decision",
  "payload": {
    "action": "wait_confirm",
    "reason": "pre_routed",
    "last_error": "Review retry limit reached (3 times)",
    "execution_count": 9,
    "review_summary": "达到重试上限 (3 次)",
    "router_mode": "adversarial"
  }
}
```

6. TUI 只显示 story 停在 `paused/design`，没有清晰说明：
   - done 文件已经成功消费；
   - review gate 拦截了推进；
   - review 是否真的调用了 reviewer；
   - reviewer 是 CLI 还是 LLM API；
   - 为什么 retry limit 会被触发；
   - 用户下一步应该重试 stage、重试 review、人工接受风险，还是终止。

这说明当前问题已经不是单个 bug，而是 review gate 缺少正式状态模型、事件模型、可读报告和 TUI 控制语义。

## Problem Statement

当前 review gate 把四类概念混在一起：

```text
stage executor attempt
reviewer run
gate decision
human confirmation
```

这会导致几个直接后果：

1. `execution_count` 被用于 review retry limit，但它表示 stage 执行次数，不等于 review 轮次。
2. `forced_fail` 只记录了 retries 数量，没有记录 reviewer、评审输入、失败原因或可修复 findings。
3. `wait_confirm_node` 只把 story 置为 `paused`，没有把 gate 原因写入 `story.last_error` 或可读报告。
4. TUI 的状态模型没有区分普通暂停、review gate 阻塞、等待人工放行。
5. 用户按 `r/e/s/a` 时无法知道动作语义：
   - `r` 是重新执行 stage，还是重新跑 review？
   - `e` 是看旧 terminal，还是看 review report？
   - `s` 是跳过 stage，还是接受 gate 风险并进入下一阶段？
6. review gate 缺少稳定落点。`event_log` 有部分结构化事件，但没有完整 `GateDecision`；`gate_result` 表存在但当前链路没有充分使用；`.story-context` 下也不一定有可读 gate report。

## Goals

本设计目标是让 review gate 的每一次阻塞都能回答以下问题：

1. 当前 story 为什么没有进入下一阶段？
2. done 文件是否已经消费？
3. 评审是否实际运行？
4. reviewer 是谁？
5. gate 根据什么证据做出决策？
6. 用户下一步可以执行哪些明确动作？
7. 每个动作是否可审计、可恢复？

具体目标：

- 将 executor、reviewer、gate、human action 建模为不同事实。
- 引入结构化 `GateDecision`。
- 将 review retry 计数与 executor attempt 计数拆开。
- 让 `wait_confirm` 写入可读原因。
- 为每次 gate 阻塞生成 markdown report。
- 在 TUI detail panel 中展示 gate 状态、原因、reviewer、report path 和下一步动作。
- 保持 P0 改动小，不重写 LangGraph 主结构。

## Non-Goals

- 不重写整个 orchestrator。
- 不引入完整 Web UI。
- 不改变 `.story-done` 握手机制。
- 不要求所有 stage 都强制 review。
- 不在 P0 引入复杂权限系统。
- 不要求 reviewer 一定是 CLI。reviewer 可以是 LLM API、CLI、规则或人工。

## Current Behavior

### Executor

executor 负责执行 stage，例如：

```json
{
  "tool": "stage_tool",
  "adapter": "claude",
  "provider": "deepseek",
  "model": "sonnet"
}
```

它通常通过 Claude CLI 在 Zellij 中执行，然后写入：

```text
.story-done/{story_key}/{stage}.json
```

### Done Consumption

done 文件应由 `poll_completion_node` 唯一消费：

```text
read done -> parse JSON -> unlink -> update context -> review_stage
```

历史 bug 是 `execute_stage_node` 也曾经消费 done 文件，造成：

```text
execute_stage_node deletes done
-> graph enters poll_completion
-> poll_completion waits for deleted done
```

这个职责边界必须保持：done 文件只有 `poll_completion_node` 是消费者。

### Review

review 可以走普通 `planner.review_stage()`，也可以走 adversarial evaluator loop。当前 minimal profile 默认启用 adversarial：

```yaml
adversarial:
  enabled: true
  code_loop:
    enabled: true
    max_rounds: 3
    reviewer_model: deepseek-chat
```

因此理论 reviewer 是：

```text
kind: llm_api
model: deepseek-chat
base_url: STORY_LLM_BASE_URL or https://api.deepseek.com
```

不是 Claude CLI。

但是 1064993 的实际情况是：`execution_count=9`，超过 `max_rounds=3`，所以 `review_stage_node` 在真正调用 `run_code_review_loop()` 前直接触发 retry fatigue：

```text
quality=forced_fail
action=wait_confirm
```

这意味着本次 gate 并没有产生真实 reviewer findings。

## Proposed Model

### Visible Evaluator-Optimizer Loop

The review gate should not be a hidden LLM call inside the orchestrator. For stages that can materially affect downstream implementation, especially `design`, review should be modeled as a visible evaluator-optimizer loop:

```text
designer CLI -> design done
reviewer CLI -> review result
gate -> pass / revise / wait_confirm / fail
```

When review returns `revise`, the story loops back to the designer with a repair packet:

```text
designer CLI round 1
-> .story-done/{story}/design.json
-> reviewer CLI round 1
-> .story-review/{story}/design-review-1.json
-> gate: revise
-> repair packet
-> designer CLI round 2
```

This loop is intentionally adversarial:

- the designer optimizes for completing the requested stage output;
- the reviewer challenges assumptions, missing scope, weak evidence, risky architecture, and unclear handoff;
- the gate does not invent review opinions itself; it interprets reviewer output and policy.

The loop must be visible and inspectable. If a reviewer decision can block progress, the user must be able to enter the reviewer CLI session, read the review report, and decide whether to retry, accept risk, or stop.

For 0.5.0, the first target loop should be:

```text
design CLI <-> design review CLI
```

The implement/code review loop can reuse the same model later, but design review is the highest-leverage place to start because a weak design poisons all downstream implementation.

### Actor Roles

The loop has three distinct actors:

```text
optimizer / designer
  A CLI agent that produces or revises the stage artifact.

evaluator / reviewer
  A separate CLI agent that critiques the artifact and writes structured review output.

orchestrator / gate
  Deterministic coordinator that launches actors, consumes their result files, applies policy, records evidence, and exposes next actions.
```

The orchestrator may use an LLM for summarization or fallback, but it should not be the default hidden reviewer for blocking decisions.

### ExecutorRun

表示一次 stage 执行尝试。

```json
{
  "story_key": "1064993",
  "stage": "design",
  "executor_run_id": "design-exec-0009",
  "attempt": 9,
  "kind": "cli",
  "adapter": "claude",
  "model": "sonnet",
  "provider": "deepseek",
  "started_at": "...",
  "ended_at": "...",
  "result": "done_written|cli_exit_without_done|failed|cancelled"
}
```

`executor_run_id` 和 `attempt` 不应被 review gate 直接解释为 review 轮次。

### ReviewRun

表示一次 reviewer 评审。

```json
{
  "story_key": "1064993",
  "stage": "design",
  "review_run_id": "design-review-0001",
  "round": 1,
  "kind": "cli",
  "adapter": "claude",
  "model": "sonnet",
  "session": "s-1064993-review-design",
  "input_summary": {
    "spec_path": "docs/2026-05-25-1064993-agreement-config-design.md",
    "stage_output_keys": ["spec_path", "complexity", "summary"]
  },
  "quality": "pass|revise|fail",
  "summary": "...",
  "issues_count": 0,
  "high_count": 0,
  "trajectory_score": 0.9,
  "report_path": ".story-context/1064993/reviews/design-review-0001.md"
}
```

`ReviewRun.round` 才能用于 review retry limit。

Reviewer can still be `llm_api` in fallback or non-interactive profiles, but blocking gates should prefer `kind=cli` so the user can observe and intervene.

### GateDecision

表示 gate 对当前 stage 的正式决策。

```json
{
  "story_key": "1064993",
  "stage": "design",
  "gate_name": "adversarial_review",
  "decision_id": "design-gate-0001",
  "decision": "advance|retry_stage|retry_review|wait_confirm|fail|accept_risk_advance",
  "reason_code": "review_passed|review_blocker|review_retry_limit|review_unavailable|no_progress|manual_accept",
  "human_message": "Review retry limit reached before a fresh reviewer run. Manual decision required.",
  "executor_attempt_count": 9,
  "review_round_count": 0,
  "retry_limit": 3,
  "reviewer": {
    "kind": "cli",
    "adapter": "claude",
    "model": "sonnet",
    "session": "s-1064993-review-design"
  },
  "evidence": {
    "done_consumed": true,
    "review_run_id": null,
    "open_findings": [],
    "report_path": ".story-context/1064993/gates/design-review-gate.md"
  },
  "allowed_actions": ["retry_review", "retry_stage", "accept_risk_advance", "fail_story"]
}
```

This object is the primary source for TUI and API display.

### HumanGateAction

表示用户对 gate 的人工操作。

```json
{
  "story_key": "1064993",
  "stage": "design",
  "decision_id": "design-gate-0001",
  "action": "retry_review|retry_stage|accept_risk_advance|fail_story",
  "actor": "local_user",
  "reason": "Design reviewed manually and accepted for implementation.",
  "created_at": "..."
}
```

人工动作必须可审计。特别是 `accept_risk_advance`，后续 implement 阶段应能看到这是人工接受风险后的推进。

## State Machine

Review gate 相关状态应从普通 `paused` 中拆出来。

```text
STAGE_DONE_MISSING
STAGE_DONE_CONSUMED
REVIEW_PENDING
REVIEW_RUNNING
REVIEW_PASSED
REVIEW_REVISE
REVIEW_FAILED
GATE_ADVANCE
GATE_RETRY_STAGE
GATE_RETRY_REVIEW
GATE_WAIT_CONFIRM
GATE_FAILED
GATE_ACCEPTED_RISK
```

最小状态流：

```text
poll_completion consumes done
-> STAGE_DONE_CONSUMED
-> review_stage
-> ReviewRun
-> GateDecision
-> router
```

当 gate 需要人工确认：

```text
GateDecision(decision=wait_confirm)
-> story.status = paused
-> story.last_error = GateDecision.human_message
-> report written
-> TUI shows WAITING_HUMAN_GATE_DECISION
```

人工接受风险：

```text
HumanGateAction(action=accept_risk_advance)
-> GateDecision(decision=accept_risk_advance)
-> advance_node
-> current_stage = next stage
```

人工重试 review：

```text
HumanGateAction(action=retry_review)
-> review_stage only
-> no stage executor relaunch
```

人工重试 stage：

```text
HumanGateAction(action=retry_stage)
-> execute_stage
-> new ExecutorRun
```

## Counting Strategy

必须拆分计数。

### executor_attempt_count

来源：

```text
execute_stage_node / BaseTool._launch_in_session
```

含义：

```text
当前 stage 被执行器启动过多少次
```

用途：

- 诊断 CLI/Zellij/retry 行为。
- 防止无限重启 executor。
- 不直接用于 review retry limit。

### review_round_count

来源：

```text
review_stage_node / evaluator_loop
```

含义：

```text
当前 stage 的 reviewer 实际运行过多少轮
```

用途：

- adversarial review max_rounds。
- no-progress detection。
- gate retry limit。

### manual_resume_count

来源：

```text
TUI/API human actions
```

含义：

```text
用户手动恢复、接受风险或强制重试的次数
```

用途：

- 审计。
- 防止误触导致重复推进。

### Rule

Review retry fatigue must use `review_round_count`, not `execution_count`.

For 1064993, if no fresh reviewer ran after done consumption, then:

```text
executor_attempt_count = 9
review_round_count = 0
```

This should not produce `forced_fail` due to review retry limit. It should produce a clearer gate decision:

```text
decision = wait_confirm
reason_code = review_not_run_due_to_stale_executor_attempt_count
human_message = Review did not run because old executor attempts exceeded a legacy guard. Manual decision required.
```

or, after implementation cleanup, it should run reviewer round 1 normally.

## Persistence

### event_log

Continue using `event_log` for append-only observability.

Recommended event types:

```text
executor_run_started
executor_run_completed
done_consumed
review_run_started
review_run_completed
gate_decision
human_gate_action
```

### gate_result

The existing `gate_result` table can be used for compact gate status, but its current shape is too small for the full decision. Use it as an index:

```text
gate_name = adversarial_review
result = wait_confirm
detail = path or short JSON
```

The full payload should live in `event_log.payload` and the markdown report.

### story

When gate blocks:

```text
story.status = paused
story.last_error = GateDecision.human_message
```

Do not set only `paused` without a visible reason.

### context_json

Store pointers, not full reports:

```json
{
  "last_gate_decision_id": "design-gate-0001",
  "last_gate_report_path": ".story-context/1064993/gates/design-review-gate.md",
  "review_round_count_design": 1
}
```

### Markdown Report

Every blocking gate decision must write:

```text
.story-context/{story_key}/gates/{stage}-review-gate.md
```

Suggested content:

```md
# Review Gate: design

## Decision

wait_confirm

## Reason

Review retry limit reached before a fresh reviewer run.

## Actors

- Executor: claude CLI, model sonnet
- Reviewer: claude CLI, model sonnet, session s-1064993-review-design
- Gate: adversarial_review

## Counts

- Executor attempts: 9
- Review rounds: 0
- Retry limit: 3

## Evidence

- Done consumed: yes
- Stage output:
  - spec_path: docs/2026-05-25-1064993-agreement-config-design.md
  - complexity: M

## Findings

No concrete reviewer findings were produced in this gate decision.

## Available Actions

- retry_review: run reviewer again without re-executing design
- retry_stage: re-run design executor
- accept_risk_advance: manually accept design and enter implement
- fail_story: mark story failed
```

## TUI Behavior

### Detail Panel

When a story is blocked or paused by a gate, the TUI detail panel should show:

```text
Gate: adversarial_review
Decision: wait_confirm
Reason: Review retry limit reached before a fresh reviewer run
Executor: claude / sonnet
Reviewer: claude CLI / sonnet / s-1064993-review-design
Report: .story-context/1064993/gates/design-review-gate.md

Actions:
  e  enter active CLI session
  r  retry review
  R  retry designer stage
  A  accept risk and advance
  f  fail story
```

### Key Semantics In Gate State

Normal story state and gate state must not share ambiguous key behavior.

Recommended mapping:

| Key | Normal stage state | Gate wait-confirm state |
| --- | --- | --- |
| `e` | enter terminal/session | enter active reviewer session, or show report path if reviewer finished |
| `r` | resume story | retry reviewer CLI only |
| `R` | force restart stage | retry designer CLI with latest repair packet |
| `s` | skip current stage | not available; show notice to use `A` for risk acceptance |
| `A` | no default action | accept risk and advance, requires explicit confirmation |
| `f` | fail story | fail gate/story |
| `a` | abort story | abort story |

If `R` is not available, use a confirmation dialog for retry stage to avoid accidental executor relaunch.

`accept_risk_advance` is a high-risk contract action and must not reuse the normal `s` skip key. It must require either a confirmation dialog with explicit wording or typed confirmation such as `accept`. The action should log `human_gate_action` with the accepted risk reason.

### Status Label

Instead of only:

```text
paused
```

show:

```text
paused: review gate
```

or:

```text
waiting human gate decision
```

### Zellij Layout Strategy

Visible review introduces two CLI actors for the same stage: designer and reviewer. They should share one story-level Zellij session rather than creating unrelated sessions that the TUI cannot explain.

Recommended session naming:

```text
s-{story_key}
```

Recommended panes:

```text
left pane:  designer CLI
right pane: reviewer CLI
bottom pane: optional orchestrator notes / gate report tail
```

The TUI `e` action should attach to the story session, not to a specific pane. Pane selection remains inside Zellij. This avoids inventing separate TUI keys for designer vs reviewer panes and lets the user inspect the whole adversarial loop in one terminal context.

If separate sessions are needed later, use explicit names:

```text
s-{story_key}-designer
s-{story_key}-reviewer
```

but P0 should prefer one session with a stable layout.

## API Behavior

Add endpoints or extend existing story detail API to expose:

```json
{
  "gate": {
    "active": true,
    "gate_name": "adversarial_review",
    "decision": "wait_confirm",
    "reason": "...",
    "report_path": "...",
    "allowed_actions": [...]
  }
}
```

Human action endpoint:

```text
POST /stories/{story_key}/gate-action
```

Body:

```json
{
  "decision_id": "design-gate-0001",
  "action": "accept_risk_advance",
  "reason": "Reviewed manually."
}
```

P0 can implement this inside TUI without public API if needed, but the state model should not assume only TUI exists.

## Router Behavior

The router should consume `GateDecision`, not infer gate state from loosely coupled fields.

Recommended mapping:

```text
GateDecision.advance              -> advance
GateDecision.retry_stage          -> retry
GateDecision.retry_review         -> review_stage
GateDecision.wait_confirm         -> wait_confirm
GateDecision.fail                 -> fail
GateDecision.accept_risk_advance  -> advance
```

If LangGraph topology does not support direct `router -> review_stage`, P0 can implement `retry_review` by resuming from a checkpoint or by setting a flag that skips executor and routes to review. The product semantics should still be explicit.

## Interaction With Adversarial Loop

Current adversarial loop design remains useful, but it should be made visible. The loop should be implemented as first-class CLI executions instead of hidden planner/reviewer API calls when it can block progress.

Required changes:

1. `review_stage_node` launches a reviewer CLI for configured visible review stages.
2. The reviewer CLI writes a structured review result file.
3. `run_code_review_loop` or its replacement emits `review_run_started` and `review_run_completed`.
4. `review_stage_node` must not use `execution_count` as review retry counter.
5. Retry fatigue must generate a full `GateDecision`.
6. No-progress detection must include repeated findings in the gate report.
7. If reviewer is unavailable, gate decision should say `review_unavailable`, not generic `forced_fail`.

### Review Result File

Visible reviewer CLI writes:

```text
.story-review/{story_key}/{stage}-review-{round}.json
```

Example:

```json
{
  "quality": "pass|revise|fail",
  "summary": "Design is mostly sound but misses DB rollback strategy.",
  "issues": [
    {
      "severity": "high",
      "type": "architecture",
      "location": "docs/design.md#database",
      "description": "Migration rollback is not specified.",
      "required_change": "Add rollback and compatibility plan."
    }
  ],
  "suggestions": [],
  "trajectory_score": 0.72
}
```

ReviewRun result states should distinguish normal review output from reviewer process failures:

```text
review_written
cli_exit_without_review
review_parse_error
review_timeout
reviewer_cancelled
```

If the reviewer CLI exits without writing `.story-review`, the gate must not loop forever and must not treat the missing review as pass. It should enter `wait_confirm` with:

```text
reason_code = reviewer_cli_crashed
human_message = Reviewer CLI exited without producing review output.
```

### Repair Packet

When reviewer returns `revise`, orchestrator writes:

```text
.story-context/{story_key}/repair/{stage}-round-{round}.md
```

The next designer CLI round receives this packet before the normal stage prompt. This packet is the explicit bridge in the adversarial loop.

The repair packet must be injected ahead of the normal stage prompt. P0 should generate a combined prompt file for the next designer round:

```text
tmp/story-prompt-{story_key}-{stage}-round-{round}.md
```

with this order:

```text
1. Repair packet
2. Previous review summary and blocking issues
3. Original stage prompt
4. Completion protocol
```

This avoids relying on the CLI to discover context files by itself and prevents the repair packet from being lost in a long workspace context.

### Configuration

P0 can use profile-level configuration:

```yaml
reviewers:
  design:
    mode: cli
    adapter: claude
    model: sonnet
    visible: true
    max_rounds: 3
  implement:
    mode: cli
    adapter: codex
    model: gpt-5-codex
    visible: true
    max_rounds: 3
```

For backward compatibility:

```yaml
reviewers:
  design:
    mode: api
```

can retain the current hidden LLM API behavior, but it should not be the default for blocking review gates in interactive workflows.

## Error Handling

### Reviewer Unavailable

If the configured reviewer cannot start, the LLM API key is missing for an API reviewer, or the reviewer CLI exits before writing review output:

```text
decision = wait_confirm
reason_code = review_unavailable | reviewer_cli_crashed
human_message = Reviewer unavailable; manual decision required.
```

Do not silently pass review unless profile explicitly allows review bypass.

### Invalid Review JSON

If reviewer output is malformed:

```text
decision = wait_confirm
reason_code = review_parse_error
```

Store raw output path if available.

### Gate Report Write Failure

If markdown report cannot be written:

- still write `story.last_error`;
- write full decision to `event_log`;
- TUI should show the DB reason and say report generation failed.

### Stale Checkpoint

If a stale graph thread reaches review or gate after force restart:

- stale epoch guard should stop writes;
- if a stale gate decision is detected, event should include `ignored_stale_epoch=true`;
- do not overwrite active story gate state.

## Migration Plan

### P0: Make Current Gate Visible

Scope:

- Keep current graph topology.
- Add `GateDecision` helper object or dict.
- On any `wait_confirm`, write:
  - `story.last_error`
  - `event_log: gate_decision`
  - `.story-context/{story}/gates/{stage}-review-gate.md`
- TUI detail panel shows active gate summary and report path.
- Fix retry fatigue to use a review-specific counter where possible; if not available, do not pretend a real review happened.
- Make design review a visible CLI actor for the interactive profile.
- Add a design-review result file and consume it before gate decision.

Acceptance:

- 1064993-like case shows why story is paused.
- User can tell whether reviewer was a CLI or an API actor.
- User can see whether review really ran.
- User can enter the reviewer CLI session while review is running.
- Design revise loops back to designer CLI with a repair packet.

### P1: Split Counters

Scope:

- Add review counter storage.
- Track executor attempts separately.
- Update adversarial retry fatigue to use review rounds.

Possible storage:

- `context_json` keys for P1:
  - `executor_attempt_count_design`
  - `review_round_count_design`
- later migrate to dedicated tables if needed.

Acceptance:

- Repeated TUI resume / zellij debug attempts do not exhaust review gate.

### P2: Human Gate Actions

Scope:

- Add explicit actions:
  - retry_review
  - retry_stage
  - accept_risk_advance
  - fail_story
- Record `human_gate_action`.
- Add confirmation dialogs for destructive or risk-accepting actions.

Acceptance:

- User can advance after manually accepting a design.
- Audit trail records who accepted and why.

### P3: Dedicated Gate Tables

If event payloads become hard to query, add tables:

```sql
CREATE TABLE gate_decision (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id INTEGER NOT NULL,
    stage TEXT NOT NULL,
    gate_name TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    human_message TEXT NOT NULL,
    report_path TEXT,
    payload_json TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE review_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id INTEGER NOT NULL,
    stage TEXT NOT NULL,
    round INTEGER NOT NULL,
    reviewer_kind TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    quality TEXT,
    summary TEXT,
    report_path TEXT,
    payload_json TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

P0 should avoid schema churn unless necessary.

## Testing Strategy

### Unit Tests

1. `poll_completion_node` consumes done and records done consumed state.
2. `execute_stage_node` never consumes done.
3. review retry fatigue uses `review_round_count`, not `execution_count`.
4. `wait_confirm_node` writes `story.last_error`.
5. `GateDecision` serializes required fields.
6. gate report is written for `wait_confirm`.

### TUI Tests

1. Story paused by gate shows `paused: review gate`.
2. Detail panel displays reviewer identity.
3. Detail panel displays report path.
4. Gate state maps `r` to retry review, not stage relaunch.
5. Gate state does not map `s` to accept risk.
6. Accept-risk action uses `A` or typed confirmation and requires confirmation.

### Integration Tests

1. Done exists before resume:
   - `execute_stage_node` skips executor.
   - `poll_completion_node` consumes done.
   - review runs.
   - gate decision is visible.
2. Reviewer unavailable:
   - story enters gate wait-confirm.
   - reason is visible.
3. Manual accept:
   - story advances to next stage.
   - `human_gate_action` is logged.
4. Legacy high `execution_count`:
   - does not trigger review retry fatigue unless review rounds are also exhausted.

## Open Questions

The current design takes positions on the most important product choices:

1. `s` must remain skip and must not mean `accept_risk_advance`.
2. Design-stage gates should make manual accept visible because design review contains subjective tradeoffs; implement/test gates should remain stricter.
3. Gate reports should be written to `.story-context`, and `story.context_json` must link the latest report and structured allowed actions for API consumers.
4. `review_round_count` resets only after a new `.story-done/{story}/{stage}.json` is consumed, not after arbitrary manual edits.
5. Reviewer unavailable policy should be configurable, with default `wait_confirm`.

Remaining open questions:

1. Should the accept-risk confirmation require typing `accept`, or is a modal confirmation enough for P0?
2. Should P0 support both one-session multi-pane layout and separate designer/reviewer sessions, or only the one-session layout?
3. Should API review mode be kept in the interactive profile as fallback, or moved to a separate headless profile?

## Recommended Defaults

For the current project:

1. Keep adversarial review enabled, but make it explain itself.
2. Do not use `execution_count` for review fatigue.
3. For design stage, `wait_confirm` should allow `accept_risk_advance` through `A` or typed confirmation, never through `s`.
4. For implement/test stages, blockers should remain stricter.
5. Always write `story.last_error` when entering `wait_confirm`.
6. Always write a gate report for any non-advance decision.
7. Default `review_unavailable_policy` should be `wait_confirm`.

## Summary

1064993 showed that a stage can successfully write and consume done, yet still appear stuck because review gate decisions are not first-class visible state. The fix is not another prompt tweak or another TUI notification. The system needs an explicit review gate model:

```text
ExecutorRun -> DoneConsumed -> ReviewRun -> GateDecision -> HumanGateAction
```

Once this model exists, the TUI can clearly show:

```text
who ran,
who reviewed,
what gate decided,
why it stopped,
what the user can do next.
```

That is the minimum needed for review gates to be trustworthy in a multi-stage AI workflow.
