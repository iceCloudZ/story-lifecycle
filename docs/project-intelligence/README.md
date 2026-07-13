# Project Intelligence Docs

This directory is the current entry point for Project Intelligence design work.

Project Intelligence turns local engineering assets into reusable context for Story Lifecycle:

```text
PRD / spec / plan / done / context / finding / pattern / test / code diff
  -> Artifact Registry
  -> Project Knowledge Pack
  -> Knowledge Context Packet
  -> Story Execution
  -> Finding / Verification / Pattern
  -> Next Knowledge Update
```

## Current Reading Order

1. [01-three-phase-roadmap.md](01-three-phase-roadmap.md)
   - The implementation roadmap.
   - Defines Phase 1 Asset Registry, Phase 2 Local Knowledge Flywheel, Phase 3 Local Advanced Flywheel.

2. [02-data-flywheel-design.md](02-data-flywheel-design.md)
   - Detailed data flywheel design.
   - Explains how PRD/spec/plan/done/context/finding/pattern assets feed Project Intelligence.

3. [03-bootstrap-design.md](03-bootstrap-design.md)
   - Local bootstrap design.
   - Defines `.story/knowledge`, prompt/CLI-first generation, context packets, graph-as-navigation.

4. [04-bootstrap-roadmap-legacy.md](04-bootstrap-roadmap-legacy.md)
   - Earlier roadmap note.
   - Kept as background; the current roadmap is `01-three-phase-roadmap.md`.

5. [07-scenario-knowledge-workflow-design.md](07-scenario-knowledge-workflow-design.md)
   - Scenario Knowledge Layer design.
   - Explains how business scenarios become computable project context for Context Builder, Planner, Reviewer, test assistants, and regression selection.

6. [08-init-knowledge-interaction-design.md](08-init-knowledge-interaction-design.md)
   - Interactive `init-knowledge` design.
   - Borrows CodeGraph-style scan summary and scope confirmation, while keeping `.story/knowledge` as the file-first knowledge body.

7. [09-simple-command-layer-design.md](09-simple-command-layer-design.md)
   - Simple command layer design.
   - Defines short user-facing commands such as `story init`, `story ask`, `story scan`, and maps them to the existing Project Intelligence internals.

## Design Position

Project Intelligence is local-first:

```text
story-lifecycle
  local asset registry
  local knowledge pack
  local context packet
  local data flywheel
```

Remote `ys-agent` integration is intentionally outside the first three phases. It should consume stable exported assets later, but it should not block local learning.

## Three Phases

```text
Phase 1: Asset Registry
  Make existing local assets discoverable and traceable.

Phase 2: Local Knowledge Flywheel
  Turn assets into `.story/knowledge` and inject context into story execution.

Phase 3: Local Advanced Flywheel
  Analyze runs, generate trace samples, promote patterns, evaluate context quality.
```

Future phase:

```text
Phase 4: Remote Governance and Company Skill Platform
  Sync stable knowledge packs and events to ys-agent.
```

## Existing Documents

| Document | Status | How to Use |
| --- | --- | --- |
| [../story-quality-flywheel-design.md](../story-quality-flywheel-design.md) | implemented reference | Existing quality flywheel: findings, verification, learned patterns, quality packet. |
| [../docs/superpowers/specs/2026-05-23-quality-flywheel-seed-pipeline-design.md](../superpowers/specs/2026-05-23-quality-flywheel-seed-pipeline-design.md) | implemented spec | Seed-quality pipeline design. |
| [../idea-swebench-data-flywheel.md](../idea-swebench-data-flywheel.md) | background | Earlier SWE-bench data flywheel idea. |
| [../design-swebench-gradient-data-flywheel.md](../design-swebench-gradient-data-flywheel.md) | future advanced flywheel | Reference for Phase 3 run analysis and trace samples. |
| [../idea-dual-flywheel-domain-and-engine.md](../idea-dual-flywheel-domain-and-engine.md) | architecture background | Explains separation between domain flywheel and engine flywheel. |
| [05-pipeline-idea.md](05-pipeline-idea.md) | background | Earlier project intelligence pipeline note. |
| [06-control-plane-idea.md](06-control-plane-idea.md) | remote future | Useful when planning `ys-agent` integration. |

## What Not To Do Yet

- Do not start with vector DB, GraphRAG, or graph database.
- Do not build a heavy AST scanner before CLI/prompt-first bootstrap is validated.
- Do not move local-only raw project data into global engine rules.
- Do not make `ys-agent` a blocker for local learning.
- Do not inject complete historical artifacts into prompts; always use compact context packets.

## Immediate Next Step

Implement scenario declaration and review workflow:

```text
story project scenarios review
```

It should generate:

```text
.story/knowledge/declarations/business-scenarios.yaml
```

This gives `scenario scan <scenario-id>`, `sync-knowledge`, and Context Builder one stable source of user-confirmed business scenario boundaries.

## Templates

Project knowledge templates live under:

```text
docs/project-intelligence/templates/project-knowledge/
```

They define the local file protocol for manifests, search catalogs, scenario docs, graph JSON, manual declarations, bootstrap prompts, and context builder prompts.
