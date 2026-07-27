# AGENTS.md

This file provides guidance to AI coding assistants (Claude Code / Codex / Kimi Code …) working in this monorepo. **Read this before touching any package.**

## What this repo is

`dev-flywheel` — a Python monorepo where a unified knowledge flywheel connects four packages. One GitHub repo (`story-lifecycle`), one workspace root (`D:/github/story-lifecycle`).

| Package | Path | Role |
|---|---|---|
| `story-lifecycle` | `packages/story-lifecycle` | Core orchestrator: drives AI coding agents through story workflows (design → implement → test), FC-based, Python. |
| `story-miner` | `packages/story-miner` | Producer: normalizes coding-agent transcripts into SQLite, mines behavior/failure/cost knowledge. Uses flat `miner/` layout (not src/). |
| `story-knowledge` | `packages/knowledge` | Contract: unified knowledge schema (scenario/playbook/failure) consumed by both packages above. |
| `testing` | `packages/testing` | Real-AI E2E test harness + asserters + scenarios shared across packages. |

**Flywheel:** `story-miner` mines experience → `knowledge` defines the shared schema → `story-lifecycle` consumes it (via `knowledge/context_providers/`). The seam between packages is soft (try/except imports) so each package can run standalone.

## Setup

```bash
# Create/activate venv at the monorepo root (NOT inside a package)
python -m venv .venv-monorepo-test
source .venv-monorepo-test/Scripts/activate   # Windows Git Bash
# .venv-monorepo-test/bin/activate            # Linux/macOS

# Install dev tools at root
pip install -e ".[dev]"

# Install each package in editable mode (order does not matter, all are leaf-installable)
pip install -e packages/story-lifecycle
pip install -e packages/story-miner
pip install -e packages/knowledge
pip install -e packages/testing
```

## Build & Run

```bash
# Lint (run from a package dir or point at its src/)
ruff check packages/story-lifecycle/src/

# Run the orchestrator (story-lifecycle only)
story serve                  # FastAPI + uvicorn, 127.0.0.1:8180
story setup                  # first-run LLM config wizard
story doctor                 # check system deps

# story-miner ingest
python -m miner.store --since-days 1
python -m miner.story_ingest
python -m miner.link
```

## Tests

Test paths are configured at the monorepo root `pyproject.toml` `[tool.pytest.ini_options]`. Run **from the repo root** (not inside a package):

```bash
# All unit + contract tests (fast, default)
pytest

# Single package
pytest packages/story-lifecycle/tests

# Cross-package contract tests only
pytest tests/contracts tests/integration

# Real-AI E2E (slow/costly, opt-in — skipped by default)
pytest -m real_e2e tests/e2e
```

`testpaths` covers: each package's `tests/` + root `tests/contracts` + `tests/integration` + `tests/e2e`. Root `tests/` is the **cross-package layer** (contracts/integration/e2e) — it does NOT belong to any single package; do not move it into a package.

### Real-story 跑测跟踪（人工盯全程）

跑真实 story 测全流程时,**进度跟踪进** `packages/story-lifecycle/docs/test-runs/`(总表 `README.md` + 每次一份 `RUN-<key>-<date>.md` 详情)。完整操作流程(怎么盯 PTY、卡住判据、怎么记)见 `.claude/skills/run-real-story-test/SKILL.md`——触发词"跑真实story/测全流程/盯一下/看进度"。serve 启停见 `run-story-serve` skill。

**这套机制不是单元测试**(那走 pytest),是**人工端到端走查**:跑一个真实需求 story 穿过编排器,盯 PTY 秒级行为,把发现的 bug(BUGLOG 级)+ 沉淀的 skill 候选记下来。详情模板在 `test-runs/_TEMPLATE.md`。

## Where things live

```
packages/<pkg>/             one package — src/, tests/, frontend/ (story-lifecycle only), docs/
packages/story-lifecycle/src/story_lifecycle/   physical 5-layer: entry/ sourcing/ orchestrator/ knowledge/ infra/
docs/                       monorepo-level docs (MIGRATION/INTEGRATION/ADOPTION + migration/)
tests/                      cross-package contract/integration/e2e
pyproject.toml              workspace root (dev deps + pytest config; packages=[[]] = no root wheel)
```

Each package has its own `pyproject.toml` and `docs/`. **Package-level docs stay in the package** (they describe that package's code); only monorepo-level concerns (migration, integration contracts) live at root `docs/`.

For package internals, read that package's docs first:
- `packages/story-lifecycle/docs/ARCHITECTURE.md` — the source of truth for story-lifecycle's layering
- `packages/story-lifecycle/frontend/AGENTS.md` — frontend UI 规范(design tokens、`.ui-*` 通用原语、SVG 图标规则;所有页面必须遵守)
- `packages/story-miner/README.md` — miner's directory structure, db schema, adapter pattern

## Architecture Review Triggers

Use these as the project rule for deciding when a bugfix should stop being a local patch and become an architecture review. Applies across all packages.

Hard rules:

- If the same functional area has a third related bug, stop patching and write an architecture review or state-machine/protocol design first.
- If a cross-system state needs explanation beyond true/false, model it as an enum/tagged state instead of a boolean.
- TUI, CLI, workflow, and background orchestration changes must define `state x user_action -> action` before handler side effects are implemented.
- Resolver code must only read facts. Decider code must be pure. Handlers are the only layer allowed to update DB, start threads, open terminals, delete sessions, or show UI feedback.
- Every non-executable branch must produce visible user feedback and diagnostic logs.
- Every historical bug fixed in these areas must have a regression test.

Trigger checklist:

```text
1. Do these bugs share the same boundary?
2. Are multiple real states represented by one boolean?
3. Do multiple entry points make similar but inconsistent decisions?
4. Are side effects mixed into state checks?
5. Is a decision table, state machine, or protocol missing?
6. Is the fix spreading across multiple files?
7. Does the user need manual explanation for which action to take next?
```

If three or more answers are yes, pause implementation and design the state model first.

## Domain conventions (story-lifecycle)

These are durable design contracts — not implementation details. Changing them requires reading the linked commits and updating tests + docs together. Each came from a real incident; the contract is the fix.

### Adapter prompt delivery — `SessionSpec` + `start_session`

How an AI CLI (claude/codex/kimi/opencode) receives its seed prompt is the **adapter's** business, not the spawner's. All spawn paths (`continue_orchestrator_agent`, `_spawn_story_agent_pty`, `api_spawn_session`) go through one contract:

- `BaseAdapter.start_session(model, prompt, session_id, ...) -> SessionSpec`
- `SessionSpec` carries `command` + `pty_prompt` + `readiness_marker`
- ClaudeAdapter bakes the prompt into `command` (`claude "query"`), `pty_prompt=""`, `readiness_marker=None`
- OpencodeAdapter bakes the prompt into `command` (`opencode --prompt "..."`, auto-submits once TUI is ready), `pty_prompt=""`, `readiness_marker=None`
- ShellAdapter (kimi/codex) returns bare `command`, `pty_prompt=<seed>`, `readiness_marker=<CLI's ready banner>`
- Spawners do NOT branch on adapter type — they read the spec and execute mechanically

**Anti-pattern**: adding a `prompts_via_pty`/`isinstance(adapter, ClaudeAdapter)` branch in a spawner. That drifts again — happened twice before this contract landed. See commits `a32a00f6`, `c90474c5`.

### Session-id model — `prespecified_session_id` + capture hooks

Whether a session id is known at spawn time or must be captured after is the **adapter's** business, not the spawner's. Three models coexist; the adapter declares which via BaseAdapter hooks:

- `prespecified_session_id: bool` — `True` = spawn knows the sid upfront (claude: `--session-id` with deterministic `compute_session_id(story,stage,adapter)` uuid5). `False` = CLI allocates its own id; must be captured post-spawn.
- `make_sid_capturer(story, stage, cwd, since_ts)` — output-driven capture: returns an `on_output(text)` callback fed to `clean_exit_pty`. Used by CLIs that print the sid on exit (kimi: `To resume this session: kimi -r session_<uuid>`). Returns `None` if not applicable.
- `capture_sid_post_exit(story, stage, cwd, since_ts) -> str | None` — file/system capture: returns the captured sid after `clean_exit_pty`. Used by CLIs that never print the sid but write it to storage (opencode: query `opencode.db` SQLite — `SELECT id FROM session WHERE directory=cwd AND time_created>=since`). Returns `None` if not applicable.

Spawners (api.py / planner.py) read `prespecified_session_id` to decide whether to store a known sid at NEW time, and call both capture hooks at stage-done cleanup when it's `False`. They do NOT branch on `adapter_name == "claude"/"kimi"` — that scatter grew to three CLIs and was converged. See commits `1a5bfbfd` (Phase 0 abstraction), `dd89ba04` (opencode).

Two spawn-path corollaries (2026-07-27, `tapd-1144381896001067713` incident):

- **Interactive spawn path captures via `arm_sid_capture`** (`infra/terminal/sid_capture.py`) — the single strategy executor for all three sid models on PTY spawn paths: prespecified → no-op; output-driven (kimi) → daemon thread drains `pty.add_tap()` into `make_sid_capturer`'s `on_output`; file-scan (opencode) → post-exit watcher calls `capture_sid_post_exit` after PTY death and backfills DB. kimi additionally gets a **live scan** (`capture_sid_live`, polling `~/.kimi-code/sessions/` every 2s) so a running session's sid lands in DB without waiting for exit — the exit-line regex remains as second path (双保险). The api interactive path (`_spawn_story_agent_pty`) arms it at NEW-spawn time (its PTY exits on the user's own `/exit`, so planner's stage-done hooks never fire). `since_ts` must be captured **before** spawn (opencode's `time_created` is the CLI start moment). Resume-spawned sessions skip arming (DB already has the sid).
- **`attach_id` is the WS attach credential, not the DB sid.** For CLI-allocated adapters the DB sid stays empty while the session runs, so `GET /api/story/{key}/sessions` returns `attach_id` (the live PTY's registry id = `compute_session_id`) alongside `session_id`. The frontend must attach via `attach_id` and treat `session_id` as the resume credential only. Resume requires a **captured** sid (or a prespecified sid + marker): never pass the uuid5 fallback to a CLI-allocated adapter's resume flag (`kimi -S <uuid5>` points at a nonexistent session).

| CLI | sid model | capture |
|---|---|---|
| claude | prespecified (uuid5) | none |
| kimi | CLI-allocated (`session_<uuid>`) | exit-line regex (`make_sid_capturer`) + 磁盘扫描双保险(`capture_sid_live` 运行中扫 `~/.kimi-code/sessions/`,`capture_sid_post_exit` 崩溃兜底) |
| opencode | CLI-allocated (`ses_…`) | SQLite query (`capture_sid_post_exit`) |
| codex | CLI-allocated | not captured (no resume support yet) |

**Anti-pattern**: re-adding `if adapter_name == "claude"` / `== "kimi"` in a spawner to special-case sid handling. Put the behavior on the adapter as a hook. See `DESIGN-session-pty-id-model.md` §2.5.

### Per-story workspace — `worktrees_root` + LLM-decided slug

Code agents run in an isolated per-story workspace, not the main monorepo:

- Planning LLM returns `workspace_slug` in `PlanResult` (kebab-case title abbreviation, e.g. `mgm-app-version-limit`)
- Backend `mkdir <worktrees_root>/<slug>/` (default `D:/worktrees` on Windows, `~/worktrees` elsewhere; overridable via `config.yaml` `worktrees_root` or env `STORY_WORKTREES_ROOT`)
- `_build_cli_prompt` writes a `### 工作空间` section: agent's cwd is the workspace, it does `git worktree add` for each project it needs to touch
- Spawn `cwd = ctx.workspace_path`, not the main workspace

**LLM decides the slug, backend builds the dir** (no side effects in the model call — replayable). The agent decides *which projects* to bring in (it's closest to the need). See commit `8ddc3501`.

### Driver lifecycle — dead-PID recovery + passive artifact consumption

Two invariants that must hold, both learned from a stuck-story incident (commit `56583154`):

1. **A dead driver must not lock the story forever.** `claim_story_driver` checks `_driver_pid_alive(token)` before failing CAS — if the holding PID is gone, a new driver may seize. Windows uses `OpenProcess(SYNCHRONIZE)` via ctypes (`os.kill(pid, 0)` returns `WinError 87` regardless of liveness — do not use it). POSIX uses `os.kill(pid, 0)`.
2. **A CLI that self-completes while no driver is watching must still advance state.** `consume_orphan_artifacts(story_key)` scans for stage `artifacts` that have landed (via `check_artifacts_landed`) but aren't yet in `_completed_stages` and claims them. Triggered from `GET /api/story/{key}` — opening the detail page unsticks a story whose CLI finished after an emergency-stop. No-op when a driver is live (its poll loop owns that case) or the story is finished. (`consume_orphan_done` is kept as a backward-compat alias.)

**Hard rule**: the driver assumes "CLI lifecycle ⊆ driver lifecycle". Any path that breaks this (interactive manual run, emergency-stop, crash) must have a reconciliation entry. `consume_orphan_artifacts` is that entry; don't add a second one.

### Story execution entry — 规划在前，执行在后（`_agent_actions` 必须先有）

执行一个 story 的**正确顺序**是先生成规划、再确认启动，不能直接跳到执行：

1. `POST /api/story/{key}/plan/stream` 或 `/plan/regenerate` → `planner.run_orchestrator_agent` 调 LLM 产出 `_agent_actions`（写进 `context_json`），`_plan_confirmed=False`
2. `POST /api/story/{key}/plan/confirm` → 设 `_plan_confirmed=True` + `sm_activate(lifecycle_state="开发")` + `start_story_async`
3. `start_story_async` → `run_story` → `continue_orchestrator_agent` 读 `ctx._agent_actions` 逐个执行

`continue_orchestrator_agent`（`planner.py`）开头直接 `actions = ctx.get("_agent_actions", [])`，**空就 `sm_mark_failed(story_key, "No actions to execute")`**——没有任何 auto-generate plan 的兜底。

- `PUT /api/story/{key}/advance` 的 `active` 分支会直接 `start_story_async`（注释说"single-pass 等 profile 创建即 active，但执行从未触发"），**但不检查 `_agent_actions` 是否存在**。所以 `story create` 后若不先走规划端点就直接 advance，必崩 "No actions to execute"。
- `start_story_async`（`graph.py`）的 docstring 撒谎说 "Otherwise it auto-generates a plan first"——**代码里没有这个逻辑**，是过时描述。
- failed 的 story 复位重跑：`POST /plan/regenerate` 是 `failed → active` 的唯一合法通道（清旧 `_agent_actions` + `sm_activate` + 重跑 planner）。`/restore` 只清 `deleted_at`、`/advance` 对 failed 是 no-op、`/abort` 再标一次 failed，都不能用于重跑。

**Anti-pattern**：`story create` 后直接 `PUT /advance` 想跑，或相信 `start_story_async` docstring 的 "auto-generate"。任何 profile（含 single-pass）都必须先走 `/plan/stream` 或 `/plan/regenerate`。真实事件（2026-07-27）：single-pass story 直接 advance → "No actions to execute" failed。

### 接手中途需求 — handoff 模式（`seed_context`）

接手一个外部已开始的需求（story 没建、部分成果物已在 workspace）：

- 前端 `IntakeStartModal` 顶部 `.ui-chip` 模式切换选「接手中途需求」→ 默认切 `single-pass` profile（单阶段包干，agent 审阅已有 + 补全缺口）
- 「接手说明」文字（做到哪了、还差什么）→ `/start` body 的 `seed_context` → `context_json.seed_context`
- 已有成果物（`spec.md`/代码/测试报告）由**用户自己放进 workspace 目录**，不做文件上传。`check_artifacts_landed` 会自然看见它们
- handoff 模式 PRD 可选：用户没填 `content` 时，后端用 `seed_context` 兜底当 PRD 正文（`api.py` `/start` handler 的 `effective_content = req.content or req.seed_context`），否则 `_prepare_intake_prd_content` 会因 `content_required` 挡住

`seed_context` 注入**两个 LLM 调度点**（这是关键——之前规划 LLM 根本不读 `context_json`）：

1. **规划 LLM**（`run_orchestrator_agent`）：读 `ctx.get("seed_context")` → 拼进 `_build_agent_user_message` 的「接手说明」段。让规划 LLM 知道这是接手需求，决定 `workspace_slug`/`task_actions` 时有依据
2. **执行 prompt**（`_build_cli_prompt`）：`### 已有工作(接手)` section（紧跟 `### PRD` 之后）。agent 跑起来时直接看见接手说明

单阶段包干语义：agent 审阅已有工作 + 补全缺口，三件 artifact（`story/spec.md`, `git`, `story/test-report.md`）齐全即完成。**不是 orphan-claim 跳过**（那是 minimal 多阶段的机制——spec 在跳 design、git 有改动跳 build）。

**顺带修的潜伏 bug**（2026-07-27）：`run_orchestrator_agent` 原代码 `content = story.get("content","")`（`planner.py`）读的是**不存在的 DB 列**（`content` 不在 `VALID_COLUMNS`）→ 永远空串 → 规划 LLM 从来看不到任何 intake material，只凭 title 一个字面量做规划。改成从 `context_json.prd_path` 读 PRD 文件前 3000 字（`_read_prd_snippet` helper，best-effort 失败返空不阻塞规划）。

**Anti-pattern**：在 `/create`（`CreateStoryRequest`）加 `seed_context`。`/create` 的 `content` 字段已经是死的（handler 不转发，见 `api.py` create_story）——在死字段旁加新字段风险高。`/start` 才是 intake material 的规范入口（已有 `content` → PRD 文件 + `context_json.prd_path` 的完整链路）。

### Artifact-driven stage completion — done.json 砍掉 + 成果物落地是完成信号

Stage completion no longer relies on the code agent self-reporting via `done.json` (un-trusted — it lies and omits, real incident: design ran 25min without producing done). The completion signal is now **artifacts landing** (DESIGN-artifact-driven-stage-completion v3):

- Every stage MUST declare ≥1 file-typed `artifacts` in its profile (`artifacts: [path|glob|"git"]`), enforced at profile load by `_validate_artifacts` (`ProfileValidationError` if missing). This is the schema contract (§1.3).
- `check_artifacts_landed(artifacts, workspace, evidence_candidates=...)` is the pure completion checker: file exists+non-empty / glob match / `git status --porcelain` non-empty. `evidence_candidates` is a robust fallback for code agents that write to the story-evidence dir or use alias filenames (e.g. `design.md` vs `spec.md`).
- Code agents land artifacts via `story tool declare <doc_type> <path>` (atomic write + `story_doc` versioning + done.json compat view for miner + `artifact_declared` event). Agents that don't call declare but write the file directly are still detected (§7.6 fallback).
- Planner poll loop: `done_path.exists()` → `check_artifacts_landed`. PTY path writes `anchors.jsonl` for miner binding (symmetric to headless).
- **Anti-pattern**: re-adding a `done_path.exists()` / done-file completion check, or making code agents write done.json. See DESIGN-artifact-driven-stage-completion §6.

### LLM 判定层 — 边界纯判定 + 卡住诊断(STEP 2,DESIGN §4.1-4.3)

On top of the artifact loop, two LLM dispatch points (DESIGN v3 §4):

1. **调度点① 边界纯判定 (`boundary_judge.judge_boundary`)** — artifacts landed → pure judgment function (approve/reject/escalate), **non-agentic** (no read_file/query_db tools; context pre-injected via `assemble_judge_context`). `unified_gate` merged in. **`confirm=true` is an explicit invariant**: approve does NOT auto-advance — it still goes through the human confirm-gate; LLM false-approves are caught by human (§评审 A). Reject budget (`check_reject_budget`): ≤3 rejects per stage + each reject reason must differ from the prior, else force-escalate (防打回循环,§评审 A2).
2. **调度点② 卡住诊断 (`stuck_diagnose`)** — supervisor rule-detected stuck (STEP 1 `detect_stuck`) → summary-first pure judgment (`diagnose_stuck_summary`, 5 卡因) → rule-triggered agentic exception (`diagnose_stuck_agentic`, only `read_file` + ≤5 calls; triggers: 2nd stuck in same stage / loop pattern). Decisions: restart-with-seed / escalate / wait. **No typed correction** (评审 C — restart-with-seed replaces it).

All decisions logged to `orchestrator_decision` table (无状态编排前提 + 审计载体,§4.6/§4.9). Decider/Handler layering: judgment modules are pure Deciders (only `log_decision` side-effect); planner is the Handler executing advance/retry/pause.

- **Anti-pattern**: giving boundary judge read_file/query_db tools (边界 agentic 买不到能力,评审 B);typed correction / PTY injection to fix a stuck agent (时序脆弱 + 单次带歪防不住,评审 C);auto-advancing on LLM approve without the confirm-gate.

### `task_actions` drives stage semantics — not stage name, not prompt keywords

Stage constraints (what the agent may/may not do) come from the **structured `task_actions` list**, never from keyword-matching the assembled prompt text:

- `_build_exec_constraint(action_keys)` branches on `task_actions` content:
  - only `write_design_doc` (and no code/tests) → **no code edits** (design is investigation only)
  - contains `run_tests` → lightweight tests allowed (covers verify: `[run_tests, accept_review, write_test_report]` has no `write_code` but does write test code)
  - `write_code` without `run_tests` → write code, no tests
- All branches forbid heavy builds (mvn/gradle/yarn install).

**Anti-pattern**: judging stage semantics by grepping the prompt for "写代码"/"Edit"/"Write". Keyword matching misclassifies negations, synonyms, and API names. Use the structured field. See commits `bcabcc43`, `88b02033` — the second was a bug in the first found via the offline analysis export.

### Offline prompt analysis — no real-time prompt judge LLM

Prompt quality is **not** judged by an LLM at spawn time. Real-time judges waste tokens, misjudge structured conditions, and false-positive-block normal stories. Instead:

- `GET /api/analysis/prompts?status=&stage=&profile=&since=&limit=` exports `(prompt + outcome + events + llm_calls)` tuples per (story, stage), one row per assembled-prompt-and-its-result
- An external AI (or human) analyzes correlations offline (which prompt patterns correlate with failures / retries / long durations) and feeds findings back into template changes
- This module is `orchestrator/observability/prompt_export.py`

**Outcome gates stay LLM-judged** (`unified_gate.py` — judges *results*, not prompts): result quality is fuzzy, prompt template correctness is structural. Don't conflate them.

## Conventions

- **Commit when done（改完就提交）**: 每完成一轮改动就立即 `git commit`，不要攒着。这个仓库常有多个 agent 会话并行工作，未提交的改动随时可能被另一个会话的 `git checkout -- .` 之类操作冲掉（真实发生过）。提交时注意：只暂存本次任务相关的文件，别把其他会话进行中的改动（如他人未提交的 `api.py`）卷进来；`git push` 仍需用户明确要求才执行。
- **Chinese content**: story-lifecycle's stage templates and prompts are in Chinese — maintain this when editing.
- **No ORM**: DB access uses raw SQL (`db/models.py`), zero ORM.
- **Editable installs**: packages are always editable-installed from `packages/`; never build wheels for local dev.
- **Do not commit runtime artifacts**: `ws/`, `*.db`, `dist/`, `.venv*/`, `.story*/`, `.claude/` (zcode workspace) are gitignored — leave them ignored.
