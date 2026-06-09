
# StoryOS v1.0 Full Push

## 总览

项目当前 23,084 行 Python 代码，532/535 测试通过。目标：删除 TUI（2,643 行），Web UI 从原型推进到完整状态可视化平台，引擎层补完 v0.6-v1.0 全部未实现功能，最后做全量回归测试。

---

## 设计文档索引

| Task | 关联设计文档 |
|------|-------------|
| 1.1-1.4 TUI 删除 | `superpowers/specs/2026-05-21-story-board-tui-design.md`, `superpowers/plans/2026-05-21-story-board-tui.md` |
| 1.2 _tui_app 清理 | `design-tui-entry-state-machine.md`, `superpowers/plans/2026-05-25-tui-entry-state-machine.md` |
| 2.1 graph_nodes 去重 | `design-smart-orchestrator.md`, `design-orchestration-refactor.md` |
| 2.3 修复失败测试 | `story-quality-flywheel-design.md` |
| 3.1 Timeline API | `story-observability-mvp-design.md` |
| 3.2 Gate History API | `design-review-gate-observability-and-control.md` |
| 3.3 Loop Trace API | `superpowers/specs/2026-05-24-evaluator-optimizer-loop-design.md`, `superpowers/plans/2026-05-24-evaluator-optimizer-loop.md` |
| 3.4 Findings API | `story-quality-flywheel-design.md` |
| 3.5 Dependency Graph API | `design-sub-story.md`, `superpowers/plans/2026-05-23-sub-story-p0.md` |
| 4.1-4.6 Web UI | `design-web-board.md`, `design-board-diagnostics-panel.md`, `idea-board-copilot-diagnostics-panel.md` |
| 5.1 Workspace Lock | `problem-workspace-git-constraint.md`, `code-freeze-and-reliability-loop-plan.md` |
| 5.2 Working Memory | `idea-orchestrator-agent.md` (Working Memory 章节) |
| 5.3 Budget Ledger | `idea-orchestrator-agent.md` (Budget Ledger 章节) |
| 5.4 SWE-bench Analyze | `design-swebench-gradient-data-flywheel.md`, `idea-swebench-data-flywheel.md` |
| 5.5 Project Profile | `design-workspace-onboarding-project-profile.md`, `project-intelligence/06-control-plane-idea.md` |
| 5.6 Workspace Onboarding | `design-workspace-onboarding-project-profile.md` |
| 5.7 Test Source | `design-three-layer-validation.md` |
| 5.8 Resource Lock | `idea-orchestrator-agent.md` (Resource Locks 章节) |
| 6.1 Strategic Router | `idea-orchestrator-agent.md` (Strategic Router 章节), `design-orchestration-llm-mandatory.md` |
| 6.2 Runtime Blackboard | `idea-orchestrator-agent.md` (Runtime Blackboard 章节) |
| 6.3 Meta-Planner | `idea-orchestrator-agent.md` (Meta-Planner 章节) |
| 6.4 双飞轮治理 | `idea-dual-flywheel-domain-and-engine.md` |
| 6.5 Stage Graph + Graph Patch | `idea-orchestrator-agent.md` (Stage Library / Stage Graph / Graph Patch 章节) |
| 6.6 Guarded Apply | `idea-orchestrator-agent.md` (Guarded Apply 章节) |
| 7.2 E2E 场景扩展 | `e2e-test.md`, `design-three-layer-validation.md` |
| 7.5 CI 配置 | `v0.6-reliability-loop-tasks.md` |

---

## Phase 1: TUI 删除 + CLI 重构（2 天）

### Task 1.1: 删除 TUI 代码

> 设计文档：`superpowers/specs/2026-05-21-story-board-tui-design.md`, `superpowers/plans/2026-05-21-story-board-tui.md`

- 删除 `src/story_lifecycle/cli/tui/` 整个目录（2,643 行）
- 从 `pyproject.toml` 移除 `textual` 依赖和 `[tui]` optional-dependencies
- 从 `doctor.py` 移除 textual 检测逻辑

### Task 1.2: 清理 _tui_app 耦合

> 设计文档：`design-tui-entry-state-machine.md`, `superpowers/plans/2026-05-25-tui-entry-state-machine.md`

TUI 通过 `_tui_app` 全局变量控制 headless/TUI 模式分支，删除后需要统一为 headless-only：

- `orchestrator/graph.py`: 删除 `_tui_app`、`set_tui_app()`、`take_plan_done()`、`take_plan_activity()`、`take_terminal_opened()`、`take_terminal_request()`、`emit_plan_done()`、`emit_plan_activity()`、`emit_terminal_opened()`、`emit_terminal_request()` — 这些都是 TUI 专用的进程内状态总线
- `orchestrator/tools/base.py:_launch_in_session()`: 移除 `if _tui_app is None` 分支，统一走 headless（subprocess）路径
- `orchestrator/nodes/graph_nodes.py:_poll_done_file()`: 移除 TUI 模式的 `interrupt()` 分支，统一走 headless 轮询
- `orchestrator/nodes/graph_nodes.py:_do_wait_confirm()`: 移除 `interrupt()` 调用，改为写 DB 状态后返回

### Task 1.3: CLI 入口重构

`story` 默认命令改为启动 Web Board（当前 `story --web` 的行为）：

- `cli/main.py`: 删除 `_run_board()` 和 TUI import，`cli()` 默认走 `_run_web_board()`
- `--serve` 保留为无浏览器启动
- `--web` 标志不再需要（默认就是 web）
- 保留的 CLI 子命令：`setup`, `doctor`, `create`, `demo`, `upgrade`, `swebench`, `diagnostics`, `project`, `plan`, `review-feedback`, `approvals`, `findings`, `seed-quality`

### Task 1.4: 清理 TUI 相关测试

- `tests/test_terminal_multiplexer.py`: 删除 TUI 相关测试（test_tui_debug_log_writes_to_story_home, test_tui_defers_attach_until_after_textual_exits_on_windows）
- `tests/test_entry_decisions.py`: 保留 entry.py 决策逻辑测试，移除 TUI 模式特定分支

---

## Phase 2: 引擎层去重 + 死代码清理（1 天）

### Task 2.1: graph_nodes.py 去重

> 设计文档：`design-smart-orchestrator.md`, `design-orchestration-refactor.md`

提取三个辅助函数：

- `_write_plan_task_file(state, plan, stage, workspace, cfg)` — 合并对抗循环路径和 fallback 路径的重复代码（L151-L219 与 L259-L307）
- `_apply_gate_decision(state, gd, stage)` — 合并 review_stage_node 中 4 处 GateDecision 应用（L606-L632, L636-L664, L679-L704, L707-L735）
- `_sync_story_source(state, key, stage, ctx)` — 合并 advance_node 中 2 处 source sync 代码（L1248-L1276 与 L1281-L1314）

### Task 2.2: 引擎层死代码清理

- `orchestrator/notify.py`: 删除 `send()` — 无引用
- `orchestrator/graph.py`: 删除 `_set_workspace_owner()` — 无引用
- `schemas.py`: 删除 `FeedbackExtractionResult` — 无引用
- `orchestrator/quality.py`: 删除 `deprecate_pattern()`, `record_verification()` — 无引用
- `orchestrator/observability.py`: 删除 `log_gate_decision()` — 无引用
- `orchestrator/semantic.py`: 删除 `recommend_recovery()` — 无引用
- `orchestrator/service.py`: 删除 `pause_story()`, `resume_parent()` — 无引用

### Task 2.3: 修复 2 个失败测试

> 设计文档：`story-quality-flywheel-design.md`

- `tests/test_quality_flywheel.py::test_learned_pattern_workflow`
- `tests/test_quality_flywheel.py::test_build_quality_packet_relevance_filtering`

---

## Phase 3: API 层增强（3 天）

为 Web UI 补齐状态演进所需的结构化 API：

### Task 3.1: Story Timeline API

> 设计文档：`story-observability-mvp-design.md`

`GET /api/story/{key}/timeline` — 返回完整阶段时间线：

```python
# 从 event_log + stage_log 聚合
{
  "story_key": "FEAT-001",
  "stages": [
    {
      "stage": "design",
      "status": "completed",
      "started_at": "...",
      "completed_at": "...",
      "duration_ms": 120000,
      "plan_summary": "...",
      "review_summary": "...",
      "gate_decisions": [...],
      "loop_rounds": 2,
      "trajectory_score": 0.7,
      "events": [...]  # 关键事件摘要
    },
    ...
  ]
}
```

### Task 3.2: Gate History API

> 设计文档：`design-review-gate-observability-and-control.md`

`GET /api/story/{key}/gate-history` — 返回 Gate 决策链：

```python
{
  "decisions": [
    {
      "decision_id": "implement-gate-abc123",
      "stage": "implement",
      "decision": "wait_confirm",
      "reason_code": "review_retry_limit",
      "human_message": "...",
      "evidence": {...},
      "allowed_actions": [...],
      "created_at": "..."
    }
  ]
}
```

### Task 3.3: Loop Trace API

> 设计文档：`superpowers/specs/2026-05-24-evaluator-optimizer-loop-design.md`, `superpowers/plans/2026-05-24-evaluator-optimizer-loop.md`

`GET /api/story/{key}/loop-trace` — 返回对抗循环轨迹：

```python
{
  "plan_loop": {
    "rounds": [
      {"round": 1, "optimizer_output": "...", "reviewer_verdict": "revise", "score": 0.5},
      {"round": 2, "optimizer_output": "...", "reviewer_verdict": "pass", "score": 0.8}
    ]
  },
  "code_loop": { ... }
}
```

### Task 3.4: Findings API

> 设计文档：`story-quality-flywheel-design.md`

`GET /api/story/{key}/findings` — 返回 Quality findings，支持状态过滤：

```python
?status=open&min_severity=high
```

### Task 3.5: Dependency Graph API

> 设计文档：`design-sub-story.md`, `superpowers/plans/2026-05-23-sub-story-p0.md`

`GET /api/story/{key}/dependency-graph` — 返回子 Story DAG：

```python
{
  "nodes": [{"key": "STORY-100", "status": "completed", "stage": "implement"}, ...],
  "edges": [{"from": "STORY-100-api", "to": "STORY-100-auth"}]
}
```

### Task 3.6: Per-Story WebSocket

`WS /ws/story/{key}` — 单 Story 实时事件流，替代全局 `/ws/stories` 的粗粒度推送。

### Task 3.7: Patterns API

`GET /api/patterns` — 返回 learned patterns 列表。
`POST /api/patterns/{id}/approve` — 批准 pattern。
`POST /api/patterns/{id}/reject` — 拒绝 pattern。

---

## Phase 4: Web UI 演进（5 天）

> 设计文档：`design-web-board.md`, `design-board-diagnostics-panel.md`, `idea-board-copilot-diagnostics-panel.md`, `superpowers/plans/2026-05-27-diagnostics-panel.md`

### Task 4.1: 项目基建升级

- 引入 React Router 做页面导航
- 引入状态管理（zustand 或 React Context）
- 引入 UI 组件库（shadcn/ui 或 Ant Design）加速开发
- 引入数据请求层（tanstack/react-query 或 swr）

### Task 4.2: Story Dashboard 页面

替代 TUI 的 Story 列表：

- Story 卡片：key、标题、阶段条（design→implement→test 进度条）、状态 badge、轨迹评分
- 实时更新：WebSocket 推送 + 自动刷新
- 操作按钮：根据状态动态显示（继续/跳过/终止/删除）
- 新建 Story 弹窗

### Task 4.3: Story Detail 页面（状态演进核心）

- 阶段时间线组件：水平时间线，每阶段显示耗时、plan 摘要、review 结论
- Gate 决策展开面板：decision_id、reason_code、evidence、可用动作
- 对抗循环面板：每轮 optimizer/reviewer 输入输出对比，diff 高亮
- Finding 列表：severity 排序、状态过滤、展开详情
- 子 Story DAG 图：依赖拓扑可视化（可用 dagre/d3）
- 操作按钮栏：根据当前状态和 gate 允许动作动态生成

### Task 4.4: 终端面板增强

当前 xterm.js 终端面板已有，增强：

- 自动连接：Story active 时自动 spawn PTY + 连接 WebSocket
- 断线重连
- 输出搜索

### Task 4.5: Diagnostics 页面

- 全局诊断：系统环境、配置、错误日志
- Story 诊断：debug_packet 可视化、stuck reason 高亮、最近事件流
- 一键打包下载

### Task 4.6: Quality Dashboard 页面

- Patterns 列表：proposed/active/rejected，批准/拒绝操作
- Finding 统计：按 category/severity 聚合
- Quality Packet 预览：下一个 Story 会注入什么质量上下文

---

## Phase 5: 引擎层 v0.6-v0.8 功能补完（7 天）

### Task 5.1: Workspace Lock 升级

> 设计文档：`problem-workspace-git-constraint.md`, `code-freeze-and-reliability-loop-plan.md` (8.4 可卡住)

- `threading.Lock` → `filelock.FileLock`（依赖已安装）
- 锁文件路径：`~/.story-lifecycle/workspace-locks/{workspace_hash}.lock`
- 支持 `exclude_story` 参数
- 跨进程安全

### Task 5.2: Working Memory（v0.7）

> 设计文档：`idea-orchestrator-agent.md` (Working Memory + Budget Ledger 章节)

新增 `orchestrator/working_memory.py`：

```python
@dataclass
class WorkingMemory:
    confirmed_facts: list[str]      # 已确认的事实
    open_risks: list[str]           # 未关闭风险
    discarded_paths: list[str]      # 已放弃的方案
    latest_findings: list[dict]     # 最近 findings
    budget_status: dict             # 预算状态
```

- 持久化：`.story/context/{story_key}/working_memory.json`
- plan_stage 开始时读取，review_stage 结束时结构化更新
- Planner prompt 注入 working memory 上下文

### Task 5.3: Budget Ledger（v0.7）

> 设计文档：`idea-orchestrator-agent.md` (Budget Ledger 章节)

新增 `orchestrator/budget.py`：

```python
@dataclass
class BudgetLedger:
    max_minutes: int = 30
    used_minutes: float = 0
    max_llm_calls: int = 50
    used_llm_calls: int = 0
    max_retries: int = 3
    used_retries: int = 0
    max_human_interrupts: int = 2
    used_human_interrupts: int = 0
```

- 每个 DecisionEnvelope 声明 budget_delta
- Policy Engine 校验预算
- 超预算时 hard kill
- TUI/Web 展示剩余预算和 burn rate

### Task 5.4: SWE-bench Analyze（v0.7）

> 设计文档：`design-swebench-gradient-data-flywheel.md`, `idea-swebench-data-flywheel.md`, `design-swebench-runner.md`

新增 `benchmarks/attribution.py` 和 `story swebench analyze` 命令：

- 单实例后分析：定位失败节点
- 归因报告：结构化输出失败原因分类
- 反事实候选：基于梯度信号生成改进方案

### Task 5.5: Project Profile Seed（v0.8）

> 设计文档：`design-workspace-onboarding-project-profile.md`, `project-intelligence/06-control-plane-idea.md`

新增 `orchestrator/project_profile.py`：

- Repo scanner：语言、包管理器、入口文件、测试目录、CI 文件
- Profile 文件：`.story/project/profile.json`
- 启动/测试候选推断
- CLI: `story project inspect` 输出项目画像

### Task 5.6: Workspace Onboarding（v0.8）

> 设计文档：`design-workspace-onboarding-project-profile.md`

- Onboarding 检测：检查 `.story/project/profile.json` 是否存在
- Deterministic scan：生成 observed facts
- 用户确认：observed facts 经用户 accept/edit/ignore 后成为 confirmed facts
- CLI: `story project onboard`、`story project confirm`、`story project refresh`

### Task 5.7: Test Source 抽象（v0.8）

> 设计文档：`design-three-layer-validation.md`

新增 `orchestrator/test_source.py`：

```python
class TestSource(ABC):
    def discover_tests(self, workspace: str) -> list[TestCandidate]: ...
    def run_tests(self, workspace: str, candidates: list[TestCandidate]) -> TestResult: ...
```

- Repo test discovery：自动发现 pytest/maven/npm 候选测试命令
- Test Plan：根据 Story 影响范围选择最小验证命令
- PRD checklist：从 acceptance criteria 生成验证 checklist

### Task 5.8: Resource Lock Dry-run（v0.8）

> 设计文档：`idea-orchestrator-agent.md` (Resource Locks + 并行策略 章节)

新增 `orchestrator/resource_lock.py`：

- 四类锁：file_glob、domain_area、db_table、api_prefix
- Decomposition Plan 扩展：task 增加 resource_locks 字段
- Dry-run 调度器：模拟并行执行，输出冲突报告

---

## Phase 6: 引擎层 v0.9-v1.0 功能补完（7 天）

### Task 6.1: Strategic Router Shadow Mode（v0.9）

> 设计文档：`idea-orchestrator-agent.md` (Strategic Router 章节), `design-orchestration-llm-mandatory.md`

- 异常触发判定：review revise/fail、retry 无进展、trajectory 低分
- Shadow DecisionEnvelope：记录 proposed vs actual decision
- 反事实评估字段：human_label、later_outcome、counterfactual_note
- Web UI 标注入口

### Task 6.2: Runtime Blackboard（v0.9）

> 设计文档：`idea-orchestrator-agent.md` (Runtime Blackboard 章节)

新增 `orchestrator/blackboard.py`：

- EventBus 事实流：所有事件结构化写入
- Blackboard 聚合器：异步消费 event_log，生成 provider_health / failure_signatures / workspace_pressure snapshot
- TTL + 滑动窗口
- Router 消费：planner/router 读取 blackboard 作为低优先级证据

### Task 6.3: Meta-Planner（v0.9）

> 设计文档：`idea-orchestrator-agent.md` (Meta-Planner + Plan-stage Decomposition 章节)

扩展 `orchestrator/planner.py`：

- StrategyEnvelope 生成：Story START 输出 mode、budget、thresholds、fallback_plan
- Scope & Decomposition Gate：plan_stage 判断 S/M/L/Epic
- Task Packet 生成：per-task context sharding

### Task 6.4: 双飞轮治理（v0.9）

> 设计文档：`idea-dual-flywheel-domain-and-engine.md`

新增 `orchestrator/flywheel/` 子包：

- `domain.py`: Domain Asset / Outcome / Trace Maturity
- `engine.py`: Engine Trace / Strategy / Eval Evidence
- `promotion.py`: 共享晋升队列 proposed → sandbox_validated → active
- 冲突仲裁：safety > domain production > engine execution

### Task 6.5: Stage Graph + Graph Patch（v1.0）

> 设计文档：`idea-orchestrator-agent.md` (Stage Library + Stage Graph + Graph Patch 章节)

新增 `orchestrator/stage_library.py` + `orchestrator/stage_graph.py` + `orchestrator/graph_patch.py`：

- Stage Library：所有合法原子 stage 定义
- Stage Graph：stage 间允许的边（非固定线性序列）
- Graph Patch Registry：insert_stage、repeat_stage、skip_stage、split_sub_story、switch_model、pause_for_human
- Policy 校验：patch 必须通过 policy check 才能执行

### Task 6.6: Guarded Apply（v1.0）

> 设计文档：`idea-orchestrator-agent.md` (Guarded Apply 章节)

扩展 `orchestrator/policy_engine.py`：

- Autonomy L0-L5 执行等级
- L0-L2：所有 apply 类动作转 shadow_only 或 needs_confirm
- L3：低风险 patch 自动执行，高风险问人
- L4：预算内允许调模型、调 retry、插入低风险 stage
- L5：仅 SWE-bench / 显式授权 profile
- 全链路审计：每次 autonomy decision 写入 trace

---

## Phase 7: 全量回归测试（3 天）

### Task 7.1: 修复所有失败测试

确保 `pytest` 0 failed

### Task 7.2: E2E 场景扩展

> 设计文档：`e2e-test.md`, `design-three-layer-validation.md`, `superpowers/specs/2026-05-23-headless-e2e-test-tool-design.md`

从 5 个场景扩展到 15+：

- done malformed / timeout / gate blocked / CLI exit without done
- Working Memory 跨 stage 传播
- Budget 超限 hard kill
- 对抗循环 no_progress / max_rounds
- 子 Story 并行完成 + 依赖解除
- Resource Lock 冲突检测

### Task 7.3: API 集成测试

为每个新 API endpoint 补充测试：

- Timeline / Gate History / Loop Trace / Findings / Dependency Graph / Patterns
- WebSocket 实时推送
- 边界条件：不存在的 story、空 timeline、无 findings

### Task 7.4: CLI 回归测试

- `story setup` / `story doctor` / `story create` / `story demo` / `story serve`
- `story project onboard` / `story project inspect`
- `story swebench prepare/solve/export/eval/summarize`
- `story diagnostics` / `story review-feedback`

### Task 7.5: CI 配置

> 设计文档：`v0.6-reliability-loop-tasks.md`, `roadmap-and-priorities.md` (P3)

- GitHub Actions：lint + test + build 三平台（Windows/Linux/macOS）
- Release 流程：PyPI 发布 + changelog

---

## 执行顺序与依赖

```
Phase 1 (TUI删除)     → 无前置，立即开始
Phase 2 (引擎去重)    → 依赖 Phase 1 完成
Phase 3 (API增强)     → 依赖 Phase 1 完成（_tui_app 清理后）
Phase 4 (Web UI)      → 依赖 Phase 3（需要 API 就绪）
Phase 5 (v0.6-v0.8)  → 依赖 Phase 2（引擎层干净后），可与 Phase 3/4 并行
Phase 6 (v0.9-v1.0)  → 依赖 Phase 5
Phase 7 (回归测试)    → 依赖所有 Phase 完成
```

Phase 1+2 同步推进。Phase 3 和 Phase 5 可并行。Phase 4 在 Phase 3 API 就绪后启动。Phase 6 在 Phase 5 基础上推进。Phase 7 最后。

## 预估总工时

| Phase | 工时 |
|-------|------|
| 1. TUI 删除 + CLI 重构 | 2 天 |
| 2. 引擎去重 + 死代码 | 1 天 |
| 3. API 层增强 | 3 天 |
| 4. Web UI 演进 | 5 天 |
| 5. v0.6-v0.8 功能 | 7 天 |
| 6. v0.9-v1.0 功能 | 7 天 |
| 7. 回归测试 | 3 天 |
| **总计** | **~28 天** |
