# Evaluator-Optimizer Loop Design

## Summary

Story Lifecycle should support adversarial evaluator-optimizer loops for both planning and implementation review.

The design keeps the existing graph shape:

```text
plan_stage -> execute_stage -> poll_completion -> review_stage -> router
```

The loop is implemented inside the existing stage boundaries instead of adding many new LangGraph nodes. Plan loop runs inside `plan_stage_node`. Code review loop runs inside `review_stage_node` and uses the existing router retry path for implementation fixes in P0.

The long-term runtime strategy is:

- reviewer stays fresh each round, with isolated context
- implementer is persistent when possible, to avoid repeated context loading
- if persistent session fails, fallback to a short-lived repair packet

## Goals

- Catch plan defects before code execution.
- Provide a code quality safety net when user test infrastructure is unavailable or unreliable.
- Preserve reviewer independence by giving it fresh context.
- Reduce implementer context waste when persistent sessions become available.
- Record round-level events so token/time waste and convergence can be measured.
- Reuse existing `finding`, `learned_pattern`, `quality.py`, `event_log`, `trajectory_score`, and `.story-knowledge/` infrastructure.

## Non-Goals

- Do not replace tests. Deterministic checks remain valuable when available.
- Do not add persistent implementer sessions in P0.
- Do not rewrite the LangGraph topology in P0.
- Do not add new finding lifecycle states to `finding.status` in P0.
- Do not require every project to have a runnable test command.

## Architecture

Add two focused modules:

```text
src/story_lifecycle/orchestrator/evaluator_loop.py
src/story_lifecycle/orchestrator/loop_events.py
```

`evaluator_loop.py` owns loop behavior:

```text
run_plan_loop(state, stage_config) -> LoopResult
run_code_review_loop(state, stage_config, stage_output) -> LoopResult
build_repair_packet(state, findings, verification) -> str
detect_no_progress(previous_rounds, current_round) -> bool
```

`loop_events.py` owns event writing:

```text
log_loop_started(...)
log_loop_round(...)
log_loop_completed(...)
log_loop_fallback(...)
```

`nodes.py` remains the graph integration point:

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

## Profile Configuration

Add an `adversarial` block to profiles:

```yaml
adversarial:
  enabled: true

  plan_loop:
    enabled: true
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

P0 supports only:

```yaml
code_loop:
  mode: short_lived
```

P1 may add:

```yaml
code_loop:
  mode: persistent
```

## P0 Flow

### Plan Loop

`plan_stage_node` calls `run_plan_loop()`.

```text
planner draft
  -> fresh plan reviewer evaluates
  -> planner revises when findings block
  -> stop on pass, max_rounds, fail, or no_progress
```

The final accepted plan is written to the existing `plan_{stage}.md` file and stored in state as today.

### Code Loop

P0 code loop is review-driven and uses the existing retry path.

```text
execute_stage_node
  -> existing implementer execution
  -> poll_completion_node reads .story-done
  -> review_stage_node runs fresh reviewer
  -> pass: advance
  -> revise: record findings, build repair_packet, set last_error
  -> router retry
  -> next plan/execute attempt receives repair_packet and open findings
```

This is still a code loop, but implementation repair is performed through the existing graph retry mechanism rather than an in-node persistent session.

## P1 Persistent Implementer Flow

P1 introduces persistent implementer sessions. Reviewer remains fresh.

```text
start implementer session
  -> implementer writes implementation_ready signal
  -> orchestrator starts fresh reviewer
  -> reviewer returns pass or findings
  -> pass: orchestrator writes .story-done and releases session
  -> revise: orchestrator injects findings into same implementer session
  -> implementer repairs and writes implementation_ready again
```

Persistent mode uses a separate signal directory so `.story-done` keeps its existing meaning: stage completion.

```text
.story-signal/{story_key}/{stage}/implementation_ready.json
.story-signal/{story_key}/{stage}/review_feedback.json
.story-signal/{story_key}/{stage}/continue_requested.json
```

If persistent session is unavailable, the orchestrator builds a repair packet and falls back to P0 short-lived retry.

## Reviewer Context Isolation

Reviewer must not inherit implementer chat history.

Reviewer input is constrained to:

- original story context
- current stage plan
- current stage output
- diff summary and relevant file excerpts
- verification results
- previous findings and implementer responses
- quality packet and learned patterns

This preserves adversarial independence and reduces the chance that reviewer accepts implementer rationalization without evidence.

## Repair Packet

Repair packet is the recovery protocol for short-lived retries and persistent-session fallback.

It should be written under:

```text
.story-context/{story_key}/repair_{stage}_round{n}.md
```

Content:

- story summary
- current stage plan
- current stage output summary
- changed files and diff summary
- open findings grouped by severity
- reviewer required changes
- accepted risks, if any
- verification status and unavailable reasons
- decisions that must be preserved
- instructions to avoid unrelated refactors

The next planner/executor prompt injects this packet together with the existing Quality Packet.

## Event Schema

P0 must add round-level events before persistent sessions are built. These events are the measurement basis for deciding whether P1 is worth the complexity.

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

## Finding State Alignment

Do not add `disputed`, `accepted_risk`, or `stale` to `finding.status` in P0.

Existing status remains the system state used by DoD, approval queues, and Quality Packet queries:

```text
open
accepted
verified
rejected/deferred if already used by CLI paths
```

Loop-specific state belongs in event payloads:

```json
{
  "finding_id": "F-001",
  "round_status": "new|repeated|resolved|disputed|accepted_risk"
}
```

This avoids breaking `get_open_findings()`, DoD gates, approval queues, and Quality Packet injection.

## Verification Ladder

Code review should record the strongest available verification level:

```text
L0 diff inspection
L1 syntax / compile / import check
L2 lint / format check if available
L3 targeted smoke command if discoverable
L4 project test command if reliable
L5 human confirmation
```

Verification status values:

```text
passed
failed
unavailable
not_run
```

`verification_unavailable` does not fail the stage by itself, but reviewer should treat it as higher residual risk.

`verification_failed` must produce `revise` or `fail` unless explicitly overridden by human confirmation.

## Waste Metrics

Current data is not sufficient to compute context waste precisely. P0 round events should collect enough data to estimate:

```text
context_reuse_waste = repeated_context_tokens / total_prompt_tokens
feedback_density = actionable_feedback_tokens / repair_prompt_tokens
fix_efficiency = resolved_findings / open_findings_before_round
repeat_finding_rate = repeated_findings / findings_in_current_round
agent_startup_overhead = time_to_first_useful_action / total_round_time
post_pass_rework_rate = later_findings_linked_to_passed_stage / passed_stage_count
```

Upgrade from short-lived mode to persistent implementer mode when any threshold is repeatedly hit:

```text
context_reuse_waste > 60%
feedback_density < 10%
repeat_finding_rate > 30%
avg code loop rounds >= 2.2
agent_startup_overhead > 40%
```

## Failure Handling

- Reviewer JSON parse fails: record `evaluator_loop_round` with parse failure, fallback to markdown review or `wait_confirm`; do not create structured findings from invalid JSON.
- `max_rounds` reached with blockers: do not pass; route to `wait_confirm`.
- No progress detected: route to `wait_confirm` with repeated findings.
- Verification unavailable: do not fail automatically; expose residual risk.
- Verification failed: revise or fail.
- Persistent session dead: log fallback, build repair packet, continue short-lived retry.
- Repair packet generation fails: fail rather than retry with empty context.

## Testing Strategy

P0 tests should focus on pure functions and graph-safe integration:

- `loop_events` writes stable event payloads.
- plan loop stops on pass, max rounds, and no progress.
- code review loop converts reviewer findings into `finding` records and events.
- repair packet includes open findings, verification status, and plan context.
- existing DoD gate still blocks open high findings.
- invalid reviewer JSON does not create findings.
- profile config defaults preserve current behavior when adversarial loop is disabled.

P1 tests should add session and signal coverage:

- implementation_ready signal triggers fresh reviewer.
- revise feedback is injected into the same implementer session.
- session death falls back to repair packet.
- `.story-done` is written only after pass.

## Rollout

1. Add P0 config disabled by default.
2. Add loop event helpers and tests.
3. Add plan loop behind config.
4. Add code review loop behind config.
5. Add repair packet injection on retry.
6. Observe waste metrics on real stories.
7. Decide whether persistent implementer P1 is justified by measured waste.

## Open Questions

- Should plan loop be enabled for every stage or only design/implement?
- Which reviewer model should be default when planner and executor use the same model?
- Should high findings always block, or should profile config allow warning-only mode?
- Should repair packet include full diff or only diff summary plus file references?
- When persistent mode arrives, should the implementer session be paused explicitly or instructed to wait for a signal file?
