# Architecture

> story-lifecycle 当前架构（codemap + 不变量），随架构治理同步更新。
> **最后更新**：2026-07-01（架构治理阶段 0-4 完成后状态，ISS-006~010 + 文档去腐化）
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

---

## story-lifecycle 包内分层（5 层）

源码在 `src/story_lifecycle/`：

### ① 入口层（最薄壳）
- `cli/` — Click 命令（main/setup/list_cmd/plan_cmd...）
- `web/` — Vue3 Board 静态资源
- `profiles/` `prompts/` — 配置文件

### ② 源头/创建
- `sources/` — 数据源（TAPD/GitHub/手动），`__init__.py:19` get_source
- `planner/` — 项目级规划（GitHub 链路，**≠** `orchestrator/engine/planner.py`）

### ③ 编排引擎（核心）— `orchestrator/`（已分层归位，ISS-010）

根级只剩 `entry.py`（TUI 入口决策）+ `paths.py`（路径注册表单一源）+ `__init__.py`。其余在高内聚子包，**依赖单向无环**（stage-4.1 验证）：

```
orchestrator/
├── engine/        FC 核心: planner, agent_tools, graph, stage_graph, graph_patch,
│                  router, meta_planner, policy_engine, shadow_router, execution,
│                  profile_loader, prompt_renderer, prompt_sections, stage_library,
│                  demo_tool, notify
├── evaluation/    gate, evaluator_loop, quality, review_feedback, semantic,
│                  test_source, validation
├── service/       api, story_service, sync_service, delivery, prd_generator
├── workspace/     project_scan, project_profile, project_probe, project_registry,
│                  resource_lock, branch_naming, doctor_paths, worktree/
├── observability/ debug_packet, diagnostics, events
├── learning/      seed_pipeline, seeds（quality-flywheel seeding）
├── context/       resolver, snapshot, pack, release_prompt, auto_discovery（③只读 story 解析）
├── nodes/         thin facade（__init__ re-export engine 模块 + 常量，保 nodes.xxx 调用兼容）
├── entry.py       （根级 — service.api + observability.debug_packet 共用）
└── paths.py       （根级 — 跨层 infra，同 config.py / json_helpers.py）
```

**依赖方向**：`learning → engine → evaluation`；`service → {context, engine, evaluation, nodes, observability, workspace}`；`observability → evaluation`；`evaluation → nodes`；`nodes/context → engine`。无反向边 → **无循环**。

### ④ 知识消费层（横切）— 已接线（ISS-009）
- `context_providers/__init__.py` `get_transcript_context` — **SOFT 缝 1**：try/except import miner，没装返回 None
- `context_providers/knowledge_provider.py` `get_context()` — 通过 `knowledge` 契约包的 `KnowledgeIndex.retrieve()` 获取 playbook/scenario/failure 知识（**已接线，ISS-009 9a**），叠加原有的裸 JSON 读取（result_axis_phase2/bug_story_graph 等结果指标，knowledge 包不建模这些，两者共存）
- `adapters/` — claude/codex/kimi/shell，写 `anchors.jsonl` 供 miner 回读
- `knowledge/`（lifecycle 内）— `.story/knowledge` 读写（**≠** packages/knowledge 契约包）

### ⑤ 基础设施（包根叶子模块，零内部 import）
- `config.py` `json_helpers.py` — ISS-006 迁入（config IO + 容错 JSON 解析）
- `llm_client.py` `llm_client_kimi_cli.py` `schemas.py` `story_paths.py`
- `db/` — SQLite 持久化（story 汇合点）
- `terminal/` — CLI 进程管理（pty.py）
- `benchmarks/` — SWE-bench 评测

**依赖方向干净**：cli → 业务（②③④）→ infra（⑤），单向。

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
