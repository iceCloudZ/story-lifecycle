# Architecture

> 本文档描述 story-lifecycle 的当前架构（codemap + 不变量），随架构治理同步更新。
> **最后更新**：2026-07-01（文档去腐化首版，对应 ISS-006 后状态）
> 历史设计决策见 [`archive/`](archive/)（ADR，正文冻结）。

---

## Monorepo 概览（dev-flywheel）

`D:/github/story-lifecycle` 是 monorepo，4 个包协作：

| 包 | 角色 | 关键事实 |
|---|---|---|
| `story-lifecycle` | 编排引擎（本文档主体） | 消费知识，执行 story |
| `story-miner` | 生产者 | transcript → SQLite → 行为分析 → 知识产物 |
| `knowledge` | 契约（**当前未接线**） | 定义统一 schema，但 lifecycle 绕过它直接读 JSON |
| `testing` | E2E 设施 | HARD 依赖 lifecycle（by design） |

**跨包依赖方向**：
- `testing ──HARD──▶ story-lifecycle`
- `story-lifecycle ──SOFT(try/except)──▶ story-miner`（miner 没装则优雅降级）
- `story-miner library ◇◇ story-lifecycle library`（零相互 import，干净）
- `story-miner scripts ──反向HARD──▶ story-lifecycle`（3 个 TAPD bug 富化脚本，待解耦 ISS-007）

---

## story-lifecycle 包内分层（5 层）

源码在 `src/story_lifecycle/`：

### ① 入口层（最薄壳）
- `cli/` — Click 命令（main/setup/list_cmd/plan_cmd...）
- `web/` — Vue3 Board 静态资源
- `profiles/` `prompts/` — 配置文件

### ② 源头/创建
- `sources/` — 数据源（TAPD/GitHub/手动），`__init__.py:19` get_source
- `planner/` — 项目级规划（GitHub 链路，≠ orchestrator/planner.py）

### ③ 编排引擎（核心）— `orchestrator/`
**FC 全自动模式**（主链路）：
- `planner.py:170` `run_orchestrator_agent` — FC 规划（LLM invoke_with_tools）
- `planner.py:426` `continue_orchestrator_agent` — 执行 action list + gate 验证循环
- `gate.py:190` `run_verify_gate` — 硬闸（round_count > max_retries 强制 fail）

**半自动模式**（用户日常用）：
- `context/release_prompt.py` — 模板渲染，用户拷贝提示词粘 CLI，不走 planner 编排

**两套并存，不同代码路径。**

orchestrator/ 子目录（部分待归位，见架构治理阶段 1）：
- `context/` — 只读 story 解析 + snapshot/pack（**≠** 顶层 `context_providers/`）
- `nodes/` — profile_loader + prompt_renderer（LangGraph 残留已清，仅剩 prompt 工具）
- `worktree/` — 工作区 git 操作
- 根级散落：api.py / service.py / quality.py / evaluator_loop.py / graph*.py / router.py ...（71 文件）

**⚠ 死代码（待清，ISS-008）**：`flywheel/` `tools/` `loop_events.py` —— 零调用，待删。
注：`semantic.py` **不是**死代码（quality.py + seed_pipeline.py 用，归④知识层）。

### ④ 知识消费层（横切）
- `context_providers/__init__.py:71` `get_transcript_context` — **SOFT 缝 1**：try/except import miner，没装返回 None
- `context_providers/__init__.py:92` `get_knowledge_context` — **SOFT 缝 2**：读 story-miner 产物 JSON
- `context_providers/knowledge_provider.py` — **绕过 knowledge 包**，直接读裸 JSON
- `adapters/` — claude/codex/kimi/shell，写 `anchors.jsonl` 供 miner 回读
- `knowledge/` — lifecycle 内的 `.story/knowledge` 读写（**≠** packages/knowledge 契约包）

### ⑤ 基础设施（包根叶子模块，零内部 import）
- `config.py` `json_helpers.py` — ISS-006 新迁入（config IO + 容错 JSON 解析）
- `llm_client.py` `llm_client_kimi_cli.py` — LLM 调用
- `schemas.py` `story_paths.py` — schema + 路径常量
- `db/` — SQLite 持久化（story 汇合点）
- `terminal/` — CLI 进程管理（pty.py）
- `adapters/` 配置见 `~/.story-lifecycle/adapters.yaml`

---

## 跨包知识飞轮

```
story-miner(生产) ──artifact JSON──▶ story-lifecycle(消费,2 SOFT缝)
     ▲                                   │
     │ anchors.jsonl (lifecycle 写)       │
     └───────────────────────────────────┘
```

- **飞轮靠 2 个 SOFT 缝运转，不靠 knowledge 包**（knowledge 包 aspirational，未接线）
- 要让 ④ 真正成契约：`KnowledgeContextProvider` 须改用 `KnowledgeIndex.retrieve()`（ISS-009）

---

## 不变量（写代码时不能破坏）

1. **`context_providers` 的 miner 依赖必须 try/except** — lifecycle 单独可跑（miner 是可选增强）
2. **gate 是硬闸** — `round_count > max_retries` 代码强制 fail，不可绕过
3. **adapters ↔ miner 通过 anchors.jsonl 文件契约通信**，非 import
4. **infra 模块（⑤）零内部 import** — config.py/json_helpers.py 只 import stdlib+yaml，无循环风险
5. **knowledge 包当前未接线** — 任何"知识契约"说法是设计意图非运行时

---

##演进史（一句话各阶段）

- LangGraph 状态机 → Function Calling（cb6f9cd, 2026-06-13，big-bang 重写）
- Zellij 终端 → python 自管 CLI 进程（terminal/pty.py，zellij 已删）
- 三角色显式编排（plan_stage/review_stage）→ FC 内化审查（已删 ISS-001/003）
- 配置/工具散落入口层 → 抽 infra（ISS-006，config.py/json_helpers.py）

详见 `archive/` 的 ADR + `决策日志.md`。
