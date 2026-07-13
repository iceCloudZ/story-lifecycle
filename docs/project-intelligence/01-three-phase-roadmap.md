# Project Intelligence Three-Phase Roadmap

## Purpose

This roadmap turns the existing Project Intelligence ideas into a practical local-first implementation plan.

The first three phases stay inside `story-lifecycle`. Remote `ys-agent` integration is a later platform phase and should not block the local data flywheel.

## End-to-End Shape

```text
Existing assets
  PRD / spec / plan / done / context / finding / pattern / test / code diff
    -> Phase 1: Artifact Registry
    -> Phase 2: Local Knowledge Flywheel
    -> Phase 3: Local Advanced Flywheel
    -> Future: ys-agent remote governance
```

## Phase 1: Asset Registry

### Goal

Make local project assets discoverable, traceable, and reusable.

This phase does not generate a full knowledge pack. It only answers:

```text
Which assets exist?
Which story do they belong to?
What role do they play?
Are they raw, extracted, proposed, verified, stale, or deprecated?
```

### Command

```bash
story project index-assets
```

### Inputs

```text
prd/
docs/superpowers/specs/
docs/superpowers/plans/
.story/context/
.story/done/
SQLite DB:
  story
  stage_log
  event_log
  finding
  learned_pattern
```

### Outputs

```text
.story/artifacts/
  registry.json
  by-story/
    <story_key>.json
```

### Minimal Rules

- Match PRD by `context_json.prd_path`, story key, or filename.
- Match spec/plan by frontmatter, story key, TAPD id, or filename.
- Match done/context by `.story/context/<story_key>/` and `.story/done/<story_key>/`.
- Match findings and learned patterns from DB.
- Do not infer business meaning yet; only register assets and evidence links.

### Acceptance Criteria

- `story project index-assets` runs without modifying business code.
- Registry includes PRD, specs, plans, done/context files, findings, and learned patterns when they exist.
- Each asset has `type`, `path` or `id`, `status`, `role`, and optional `story_key`.
- Missing or ambiguous matches are recorded as warnings, not silently ignored.

### Why This Is First

The project already has many useful assets. Without a registry, every later prompt or tool must rediscover them from scratch. Registry creates the stable input layer for all later phases.

## Phase 2: Local Knowledge Flywheel

### Goal

Turn registered assets into `.story/knowledge`, then use that knowledge during story execution.

Scenario-level knowledge is a sub-workflow of this phase. `init-knowledge` creates the overview layer; user-confirmed business scenarios and deep scenario scans are defined in `07-scenario-knowledge-workflow-design.md`.

### Commands

```bash
story project init-knowledge
story project sync-knowledge
story project scenarios review
story project scenario scan <scenario-id>
story context build <story_key> --stage <stage>
```

### Flow

```text
Artifact Registry
  -> Project Knowledge Pack
  -> Search Catalog
  -> Product Context Graph
  -> Knowledge Context Packet
  -> Planner / Executor
  -> Finding / Verification / Pattern
  -> sync-knowledge update suggestion
```

### Inputs

- `.story/artifacts/registry.json`
- source tree and Git metadata
- PRD/spec/plan assets
- done/context assets
- findings and learned patterns
- verification events

### Outputs

```text
.story/knowledge/
  product.yaml
  manifest.yaml
  search-catalog.md
  scenarios/
  indexes/
  graph/
  playbooks/
  reviews/
  declarations/
  events/
```

Context output:

```text
.story/context/<story_key>/knowledge-context/<stage>.md
.story/context/<story_key>/knowledge-context/<stage>.json
```

### Required Behaviors

- `init-knowledge` must read Artifact Registry before scanning code.
- `sync-knowledge` must detect stale PRD/spec/plan/done/finding/pattern/code sources.
- Context Builder must prefer current story PRD/spec/plan before global project knowledge.
- Verified findings and active learned patterns can be injected as strong context.
- Proposed or stale content must be clearly marked.

### Acceptance Criteria

- A story stage can receive a compact knowledge context packet.
- The packet includes source refs.
- Current story PRD/spec/plan are prioritized.
- A verified finding can appear in bug-risk or regression context.
- A verification result can strengthen a test-case or finding status.
- No complete historical artifact is dumped into the prompt.

## Phase 3: Local Advanced Flywheel

### Goal

Analyze local story runs and improve future execution without waiting for `ys-agent`.

This phase brings in ideas from the earlier SWE-bench gradient data flywheel, but applies them locally to real stories as well as benchmark runs.

### Commands

```bash
story project analyze-run <story_key|run_id>
story project promote-patterns
story project eval-context <story_key>
```

### Capabilities

#### 1. Post-Mortem Analyzer

Analyze story/run execution:

- stage duration
- retries
- route decisions
- done output quality
- failed or missing verification
- repeated findings
- context packet usefulness

Outputs:

```text
.story/analysis/<story_key>/postmortem.md
.story/analysis/<story_key>/trace-samples.jsonl
```

#### 2. Trace Samples

Convert execution into structured samples:

```json
{
  "story_key": "STORY-12345",
  "stage": "implement",
  "decision_point": "router.route_decision",
  "selected_context": ["scenario:order.withdraw"],
  "action": {"type": "retry"},
  "outcome": {"status": "completed"},
  "evidence": [".story/context/STORY-12345/knowledge-context/implement.md"]
}
```

#### 3. Pattern Promotion

Promote evidence-backed lessons:

```text
verified finding -> proposed learned pattern
repeated bug risk -> regression checklist item
repeated context miss -> search-catalog update
stable troubleshooting path -> playbook update
```

#### 4. Context Quality Evaluation

Evaluate whether context helped:

- Did Planner reference the context packet?
- Did Executor open relevant files?
- Did the chosen context reduce repeated findings?
- Did test coverage include historical risks?
- Were important nodes missing?

### Acceptance Criteria

- A completed story can produce a postmortem report.
- A trace sample can be written for at least one stage.
- A verified finding can become a proposed learned pattern.
- A context miss can become a search-catalog or graph update suggestion.
- No proposed pattern becomes active without review.

## Future Phase: Remote Governance and Company Skill Platform

Remote `ys-agent` integration comes after the local flywheel is stable.

Future commands may include:

```bash
story project export-knowledge-pack
story project export-events
```

`ys-agent` should handle:

- Knowledge Pack registry
- remote review and publish
- company-level Skill
- permission and audit
- multi-project governance

Remote source rules:

- Published packs must bind to Git repo + commit.
- Local paths are not canonical remote sources.
- Local `.story/knowledge` can be draft/export material, not final authority.

## Relationship to Existing Designs

| Earlier Design | How It Is Used |
| --- | --- |
| Quality Flywheel | Already mostly implemented; reused for findings, verification, learned patterns, and quality packets. |
| Seed Quality Pipeline | Used as the Distill step for registered story artifacts. |
| Project Intelligence Bootstrap | Phase 2 implementation foundation. |
| SWE-bench Gradient Data Flywheel | Phase 3 reference for postmortem, trace samples, and gradient signals. |
| Dual Flywheel | Governance principle: domain project knowledge and engine improvement must remain separated. |

## Recommended Implementation Order

1. Implement `story project index-assets`.
2. Generate registry for the current `story-lifecycle` repo itself.
3. Feed registry into `seed-quality` manifest generation.
4. Feed registry into `init-knowledge`.
5. Add Context Builder using registry + `.story/knowledge`.
6. Add `analyze-run` only after at least several real story executions have registry and context packets.

## Success Criteria

Phase 1 succeeds when assets are discoverable.

Phase 2 succeeds when assets improve story planning and execution.

Phase 3 succeeds when execution results produce better future context, tests, and patterns.

The larger system succeeds when each story leaves behind reusable evidence for the next one.
