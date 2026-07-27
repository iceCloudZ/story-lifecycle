# Architecture

> story-lifecycle 当前架构（codemap + 不变量），随架构治理同步更新。
> **最后更新**：2026-07-02（ISS-012 物理分层完成：5 层从逻辑分组升级为物理目录 entry/sourcing/orchestrator/knowledge/infra）
> 历史设计决策见 [`archive/`](archive/)（ADR，正文冻结）。

---

## Monorepo 概览（dev-flywheel）

`D:/github/story-lifecycle` 是 monorepo，4 个包协作：

| 包 | 角色 | 关键事实 |
|---|---|---|
| `story-lifecycle` | 编排引擎（本文档主体） | 消费知识，执行 story |
| `story-miner` | 生产者 | transcript → SQLite → 行为分析 → 知识产物 |
| `knowledge` | 契约包 | 统一 schema，**已接线**（ISS-009，optional dep） |
| `testing` | E2E 设施 | HARD 依赖 lifecycle（by design） |

**跨包依赖方向**：
- `testing ──HARD──▶ story-lifecycle`
- `story-lifecycle ──SOFT(try/except)──▶ story-miner`（miner 没装则优雅降级）
- `story-miner library ◇◇ story-lifecycle library`（零相互 import，干净）
- `story-miner scripts ──反向HARD──▶ story-lifecycle`（3 个 TAPD bug 富化脚本，离线脚本非 library）

---

## 两种执行模式（并存）

- **全自动 FC** — `service/api.py:/plan/stream` → `engine/planner.py:run_orchestrator_agent`（Function-Calling 循环，`llm.invoke_with_tools`）写 `_agent_actions` + `_plan_confirmed=False` → 暂停 → 前端 confirm → `/plan/confirm` → `engine/graph.py:start_story_async` → `continue_orchestrator_agent` 循环执行 actions：launch via `adapters/`（yml 配置）+ `terminal/pty.py` 管 CLI 进程 → 轮询 `.done` → `evaluation/gate.py:run_verify_gate` 硬闸 → advance / retry / fail。**LLM 驱动自己的重试**（planner 重新插入 launch action），**无 Python repair-loop 函数**——`evaluation/evaluator_loop.py` 只是 repair-packet 构造器。
- **半自动** — `service/api.py:/context/release-prompt` → `context/release_prompt.py` 渲染提示词（ContextResolver）→ 用户拷贝给 code-agent（Claude/Codex）→ `story-context` skill 回填产物。**不走 `engine/planner`**。

### FC 模式的实时监控 — `events.jsonl` 是首选

全自动 FC 跑起来后，**观察 CLI 实时行为的最佳手段是 tail PTY 日志文件**，不是轮询 DB：

- **`<spawn_cwd>/.story/runs/<story_key>/pty_<stage>/events.jsonl`** — 结构化、剥 ANSI、秒级落盘。每行 `{ts, dir, type, text}`：
  - `dir=output` = CLI 产出（claude/kimi 的思考、工具调用、输出）
  - `dir=injection` = 编排器注入（区分谁说的）
  - `dir=system` = stuck 检测等系统事件（`type=stuck_detected` / `stuck_restart`）
  - `tail -f` 即可秒级盯 CLI 行为，比 DB 审计快得多
- **`<spawn_cwd>/.story/runs/<story_key>/pty_<stage>/raw.log`** — 字节保真（含 ANSI 颜色/光标），调试终端渲染问题用
- `spawn_cwd` = `ctx.workspace_path`（规划 LLM 决定的 `D:/worktrees/<slug>/`），退回 story 主 `workspace`。`PtyLogger`（`infra/terminal/pty_logger.py`）的 workspace 参数收的是这个 cwd，不是 `story.workspace` 字段——两者可能不同（见下 workspace 字段注意事项）
- 仅 planner 自主 spawn 路径创建 PtyLogger；交互式手动 spawn（`/sessions/spawn`）**不落这两层日志**

DB `event_log` 是**分钟级审计载体**（`supervisor_decision` / `awaiting_confirm` / `gate_decision` / `boundary_*` / `completed`），不自动从 PTY 提取，是各模块显式 `db.log_event` 落的。监控实时进度用 events.jsonl，事后审计/复盘用 event_log。

### adapter 自动 recovery — 崩了/卡住自动换 adapter

`continue_orchestrator_agent` 的 poll 循环里有两层 recovery，都**不改代码、只切 adapter 重跑**：

1. **stuck 检测 → restart（同 adapter）**：`detect_stuck`（`supervisor.py`，纯规则：300s 无输出 / 启动 60s 无输出 / 末 5 条连 error）命中 → `diagnose_stuck_summary`（LLM 摘要判定）→ 决策 `wait`/`restart`/`escalate`。`restart` = 杀当前 PTY + 插入 retry action（同 adapter）+ 带 seed 重起，无打字纠偏。
2. **stage 失败 → rescue（换 adapter）**：poll 循环抛异常或 PTY 退出未落地 → `run_story` 的 `except` 捕获 → `decide_recovery` / `rescue_story` 决策 `retry_new_adapter`（如 claude→kimi），落 `recovery_action` 事件。这是上层（`graph.py`）的兜底，attempt 计数（默认 1/3）。

关键：**recovery 不持久化当前 adapter 的进度**——切 adapter 后新 CLI 从头跑（kimi 不会继承 claude 的上下文）。所以 claude 已 declare 的产物（如 spec.md）会保留，但 kimi 得自己重新探索代码。

**反模式**：把 recovery 当成"正常的多 adapter 协作"。它是故障兜底，正常路径不该频繁触发。频繁触发说明 adapter 选型或 prompt 有问题。


---

## story-lifecycle 包内分层（5 层 · 物理目录）

> **ISS-012（2026-07）**：5 层从"逻辑分组"升级为**物理目录**——依赖方向从目录树即可读出。
> 源码在 `src/story_lifecycle/`，根级只剩 `__init__.py` + `__main__.py`，业务全在 5 个层目录：

```
src/story_lifecycle/
├── entry/          ① 入口层（最薄壳）
│   ├── cli/        Click 命令（main/setup/list_cmd/plan_cmd...）
│   ├── web/        Vue3 Board 静态资源
│   └── profiles/   流程定义 yaml（story create --profile 选，入口交互性质）
├── sourcing/       ② 源头层
│   ├── sources/    数据源（TAPD/GitHub/手动），__init__.py get_source
│   ├── planner/    项目级规划（GitHub 链路，≠ orchestrator/engine/planner.py）
│   └── integrations/  上游适配（gitlab 等）
├── orchestrator/   ③ 编排引擎（核心，ISS-010 已内部分层，物理分层未动其内部）
├── knowledge/      ④ 知识层
│   ├── context_providers/  故事上下文注入（SOFT 缝接 miner）
│   ├── adapters/           AI CLI 适配（claude/codex/shell）
│   └── knowledge_store/    lifecycle 内 .story/knowledge 读写（原顶层 knowledge/，ISS-012 改名避与层目录撞名）
└── infra/          ⑤ 基础设施层
    ├── config.py json_helpers.py schemas.py story_paths.py paths.py llm_client.py llm_client_kimi_cli.py
    ├── prompts/     stage 提示词模板（9 个 .md，跨层共享数据资源）
    ├── db/          SQLite 持久化（story 汇合点）
    ├── terminal/    CLI 进程管理（pty.py）
    └── benchmarks/  SWE-bench 评测
```

### ① 入口层 `entry/`（最薄壳）
- `cli/` — Click 命令（main/setup/list_cmd/plan_cmd...）
- `web/` — Vue3 Board 静态资源
- `profiles/` — 流程定义 yaml（用户 `story create --profile` 时选，入口交互性质）

### ② 源头层 `sourcing/`
- `sources/` — 数据源（TAPD/GitHub/手动），`__init__.py:19` get_source
- `planner/` — 项目级规划（GitHub 链路，**≠** `orchestrator/engine/planner.py`）
- `integrations/` — 上游适配（gitlab 等）

### ③ 编排引擎（核心）— `orchestrator/`（已分层归位，ISS-010，物理分层未动其内部）

根级只剩 `entry.py`（TUI 入口决策）+ `__init__.py`（`paths.py` 已下沉 `infra/`，见 ISS-013）。其余在高内聚子包，**依赖单向无环**（stage-4.1 验证）：

```
orchestrator/
├── engine/        FC 核心: planner, graph, router, execution, supervisor,
│                  profile_loader, prompt_renderer, prompt_sections, notify,
│                  recovery, consult_runner, consult_orchestrator, awaiting_detector,
│                  scheduler, task_actions, claude_stream, artifact_check, artifact_declare
├── evaluation/    unified_gate, boundary_judge, stuck_diagnose, reject_budget,
│                  quality, review_feedback, semantic, validation
├── context/       judge_context, resolver, snapshot, pack, auto_discovery（成果物驱动 +
│                  ③只读 story 解析）
├── service/       api, story_service, sync_service, delivery, prd_generator
├── workspace/     project_scan, project_profile, project_probe, project_registry,
│                  resource_lock, branch_naming, doctor_paths, worktree/
├── observability/ debug_packet, diagnostics, events, prompt_export
├── learning/      reflection, seed_pipeline, seeds（playbook 持久化 + quality-flywheel seeding）
├── nodes/         thin facade（__init__ re-export engine 模块 + 常量，保 nodes.xxx 调用兼容）
└── entry.py       （根级 — CLI exit state 检测，service.api + observability.debug_packet 用；
                    paths.py 已下沉 infra/，见 ISS-013）
```

**依赖方向**：`learning → engine → evaluation`；`service → {context, engine, evaluation, nodes, observability, workspace}`；`observability → evaluation`；`evaluation → nodes`；`nodes/context → engine`。无反向边 → **无循环**。

### ④ 知识层 `knowledge/`（横切）— 已接线（ISS-009）
- `context_providers/__init__.py` `get_transcript_context` — **SOFT 缝 1**：try/except import miner，没装返回 None
- `context_providers/knowledge_provider.py` `get_context()` — 通过 `knowledge` 契约包的 `KnowledgeIndex.retrieve()` 获取 playbook/scenario/failure 知识（**已接线，ISS-009 9a**），叠加原有的裸 JSON 读取（result_axis_phase2/bug_story_graph 等结果指标，knowledge 包不建模这些，两者共存）
- `adapters/` — claude/codex/shell，写 `anchors.jsonl` 供 miner 回读
- `knowledge_store/`（原顶层 `knowledge/`，ISS-012 改名）— `.story/knowledge` 读写（**≠** packages/knowledge 契约包）

### ⑤ 基础设施层 `infra/`
- `config.py` `json_helpers.py` — config IO + 容错 JSON 解析
- `llm_client.py` `llm_client_kimi_cli.py` `schemas.py` `story_paths.py`
- `prompts/` — stage 提示词模板（design/build/verify/review...9个.md，被③engine 拼 prompt + ④knowledge/bootstrap 读，跨层共享数据资源，非入口配置）
- `db/` — SQLite 持久化（story 汇合点）
- `terminal/` — CLI 进程管理（pty.py）
- `benchmarks/` — SWE-bench 评测

**层间依赖方向**（ISS-012 P6.2 AST 扫描实测）：
- 干净方向：`entry → {sourcing, orchestrator, knowledge, infra}`；`sourcing → {infra, orchestrator}`；`orchestrator → {infra, knowledge, sourcing}`；`knowledge → infra`。
- **曾存的 infra 上探倒置**：`infra/benchmarks/artifacts.py → orchestrator.paths` 已由 **ISS-013 修复**（`paths.py` 下沉 `infra/`，纯路径注册表本属 infra）；`infra/db/models.py → sourcing.integrations` 已由 **ISS-014** 修复（workspace-diff 三函数 `_local_git_diff` / `get_story_workspace_diff` / `_try_gitlab_diff` 提到 `sourcing/workspace_diff.py`，gitlab 由 lazy 上探变为同级 import，models.py 回归纯持久化、零 sourcing 引用）。

---

## 跨包知识飞轮

```
story-miner(生产) ──artifact JSON──▶ story-lifecycle(消费,2 SOFT缝)
     ▲                                   │
     │ anchors.jsonl (lifecycle 写)       │
     └───────────────────────────────────┘
```

- 飞轮靠 2 个 SOFT 缝 + knowledge 契约包运转（ISS-009 后 knowledge 已接线）
- `story-knowledge` 是 optional dep（`pip install story-lifecycle[knowledge]`），没装则 `KnowledgeIndex` 段优雅跳过

---

## 不变量（写代码时不能破坏）

1. **`context_providers` 的 miner 依赖必须 try/except** — lifecycle 单独可跑（miner 是可选增强）
2. **gate 是硬闸** — `round_count > max_retries` 代码强制 fail，不可绕过
3. **adapters ↔ miner 通过 anchors.jsonl 文件契约通信**，非 import
4. **infra 模块（⑤）零内部 import** — config.py/json_helpers.py 只 import stdlib+yaml，无循环风险
5. **knowledge 契约包是 optional** — `KnowledgeIndex.retrieve()` 必须 try/except ImportError，lifecycle 不装 story-knowledge 也要跑

---

## 死代码已清（ISS-008/008b/008c，~2800 行）

`loop_events.py`、`flywheel/`（dual-flywheel 未接线设计）、`working_memory.py`、`blackboard.py`、`budget.py`、`copilot.py`、`decision_chain.py`、`tools/`、evaluator_loop 死 helper（LoopResult/AdversarialConfig/detect_no_progress/…）全删。规则：只删生产+测试均零调用的。保留：`validation.py`（swebench 测试用）、`semantic.py`（活的④组件，quality+seed_pipeline 用）。

---

## 演进史（一句话各阶段）

- LangGraph 状态机 → Function Calling（cb6f9cd, 2026-06-13，big-bang 重写）
- Zellij 终端 → python 自管 CLI 进程（terminal/pty.py，zellij 已删）
- 三角色显式编排（plan_stage/review_stage）→ FC 内化审查（已删 ISS-001/003）
- 配置/工具散落入口层 → 抽 infra（ISS-006，config.py/json_helpers.py）
- orchestrator 根级散落 → 分层归位（ISS-010，engine/evaluation/service/...）
- knowledge 包 aspirational → runtime 契约接线（ISS-009）

详见 `archive/` 的 ADR + `决策日志.md`。
