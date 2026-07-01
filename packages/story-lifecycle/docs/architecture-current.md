# Architecture — Current State (post stage-0..2 + stage-1 partition)

> Snapshot: 2026-07-01, branch `refactor/stages-0-4`. Reflects the ISS-006 → ISS-010 + ISS-008(c) governance pass. This is the **current runtime truth** (supersedes the LangGraph-era design docs, which are kept as historical decision records with deprecation banners).

## Two execution modes (both live)

- **Full-auto FC** — `service/api.py:/plan/stream` → `engine/planner.py:run_orchestrator_agent` (Function-Calling loop, `llm.invoke_with_tools`) writes `_agent_actions` + `_plan_confirmed=False` → pause → frontend confirm → `/plan/confirm` → `engine/graph.py:start_story_async` → `continue_orchestrator_agent` loops actions: launch via `adapters/` (yml) + `terminal/pty.py` manages the CLI process → poll `.done` → `evaluation/gate.py:run_verify_gate` hard gate → advance / retry / fail. The LLM drives its own retries (planner re-inserts a launch action); there is **no Python repair-loop function** — `evaluation/evaluator_loop.py` is just the repair-packet builder.
- **Semi-auto** — `service/api.py:/context/release-prompt` → `context/release_prompt.py` renders a prompt (ContextResolver) → user pastes into a code-agent (Claude/Codex) → `story-context` skill back-fills artifacts. Does **not** go through `engine/planner`.

## orchestrator/ — partitioned (ISS-010), root is minimal

Root holds only `entry.py` + `paths.py` + `__init__.py` (those two are cross-layer shared/infra). Everything else is in a high-cohesion subpackage; dependencies are **one-way (acyclic, verified by stage-4.1 scan)**:

```
orchestrator/
├── engine/        FC core: planner, agent_tools, graph, stage_graph, graph_patch,
│                  router, meta_planner, policy_engine, shadow_router, execution,
│                  profile_loader, prompt_renderer, prompt_sections, stage_library,
│                  demo_tool, notify
├── evaluation/    gate, evaluator_loop, quality, review_feedback, semantic,
│                  test_source, validation
├── service/       api, story_service (renamed from service.py), sync_service,
│                  delivery, prd_generator
├── workspace/     project_scan, project_profile, project_probe, project_registry,
│                  resource_lock, branch_naming, doctor_paths, worktree/
├── observability/ debug_packet, diagnostics, events (renamed from observability.py)
├── learning/      seed_pipeline, seeds  (quality-flywheel seeding)
├── context/       resolver, snapshot, pack, release_prompt, auto_discovery  (③ read-only story parse)
├── nodes/         thin facade (__init__ re-exports engine modules + constants; kept so
│                  existing nodes.xxx call sites keep working)
├── tools/         PENDING ISS-008b (dead in production, live test dependency)
├── entry.py       (root — shared by service.api + observability.debug_packet)
└── paths.py       (root — cross-layer infra, like config.py / json_helpers.py)
```

**Dependency direction:** `learning → engine → evaluation`; `service → {context, engine, evaluation, nodes, observability, workspace}`; `observability → evaluation`; `evaluation → nodes`; `nodes/tools/context → engine`. No reverse edges → **no cycles**.

## Dead code removed (ISS-008 / ISS-008c) — ~2800 lines

`loop_events.py`, `flywheel/` (domain/engine/promotion — superseded dual-flywheel), `working_memory.py`, `blackboard.py`, `budget.py`, `copilot.py`, `decision_chain.py`, + evaluator_loop dead helpers (LoopResult/AdversarialConfig/detect_no_progress/…). Rule: only deleted code with zero production **and** zero test callers; `validation.py` kept (swebench test-exercised), `semantic.py` kept (live ④).

## Cross-package (monorepo dev-flywheel)

`story-lifecycle` ↔ `story-miner` coupling is all graceful now (ISS-007): `cli/list_cmd.py` resolves the miner retrospect script via `STORY_RETROSPECT_SCRIPT` env → `config.retrospect_script` → monorepo-relative fallback; `context_providers` try/except-imports `miner.config`; `knowledge_provider` reads miner JSON via `STORY_MINER_OUT`. Reverse HARD dep (miner scripts `import story_lifecycle.sources.tapd_source`) is offline scripts only.

## Knowledge layer (④) — still aspirational, not a runtime contract (ISS-009 open)

The `knowledge` contract package (`KnowledgeIndex.retrieve()`) is implemented + tested but **not wired** — `context_providers/knowledge_provider.py` still reads raw miner JSON. §3.0 finding: wiring is **partial** — `KnowledgeIndex` models playbook/scenario/failure, but the provider also consumes outcome metrics (`result_axis_phase2`) + structure (`manifest`) the package doesn't model. ISS-009 needs a design decision (partial wiring vs extend the schema) before implementation.

## Infra (⑤) — extracted in ISS-006

`config.py` (get_config/save_config), `json_helpers.py` (robust_json_parse), `story_paths.py`, `llm_client*.py`, `db/`, `benchmarks/`. Dependency direction is clean: cli → business (②③④) → infra (⑤), one-way.
