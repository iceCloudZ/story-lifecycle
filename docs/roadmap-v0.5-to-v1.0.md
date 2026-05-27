# v0.5.0 → v1.0.0 版本路线图

> 基于 `docs/` 全部设计文档与当前代码实现的真实差距。当前版本 v0.5.8。
>
> Orchestrator Agent 设计（`docs/idea-orchestrator-agent.md`）的 P0–P7 已拆入各版本。
>
> Story Lifecycle 正在演进为 **StoryOS**：面向真实软件项目的 Code Agent 操作层。v1.0 前不改包名、CLI 和 DB 命名；StoryOS 作为产品愿景，`Agentic SDLC Control Plane` 作为品类定位，`Project Intelligence Layer` 作为核心模块。
>
> **服务端部署（ttyd / 后台守护 / 多租户等）属于 v2，不纳入此路线图。**

---

## 已完成（v0.5.8 已包含，无需重复规划）

以下内容已实现，**不出现在后续版本中**：

| 模块 | 关键文件 |
|------|----------|
| Headless / Zellij 共享抽象层 | `validation.py`、`artifacts.py`、`paths.py` |
| 可观测性（log_node_error / Debug API / TUI 门禁） | `observability.py`、`api.py` |
| 子故事 P0（DB + API + Service + workspace mutex） | `models.py`、`api.py`、`service.py` |
| SWE-bench Runner（clone cache / worktree / patch noise / eval harness） | `benchmarks/swebench.py`、`cli/swebench.py` |
| 路径收敛（`.story-done/` → `.story/done/` / prompt 更新 / doctor paths） | `paths.py`、`doctor_paths.py`、`prompts/` |
| `story demo` / `--dry-run` | `cli/demo.py`、`cli/main.py` |
| 质量飞轮 P0+P1（finding 生命周期 / checklist / packet / learned pattern） | `quality.py` |
| Review 门禁（GateDecision / review_round_count / gate report） | `gate.py` |
| Planner / Reviewer（plan_stage / review_stage / compress_context） | `planner.py` |
| 对抗循环（run_plan_loop / run_code_review_loop / detect_no_progress） | `evaluator_loop.py`、`loop_events.py` |
| LLM 语义提取（bug context / pattern matching / rerank / recovery） | `semantic.py` |
| StorySource 抽象（ManualSource / TapdSource / DB source_type+source_id） | `sources/base.py`、`tapd_source.py`、`models.py` |
| TUI 收件箱 `[i]` + 状态回写 TAPD | `tui.py`、`tapd_source.py` |
| Tool Registry（stage_tool / skill_tool） | `tools/stage_tool.py`、`tools/skill_tool.py` |
| trajectory_score 路由 | `planner.py` |
| 任务书模板（done 格式 / 事实假设边界 / headless 兼容） | `nodes.py` |
| profiles + prompts 打包进 wheel（importlib.resources） | `nodes.py`、`pyproject.toml` |
| 启动配置检查 + `story setup` / `story serve` 命令入口 | `cli/main.py`、`cli/setup.py` |
| `story upgrade` 命令 | `cli/main.py` |

参考设计文档：

- `docs/design-headless-zellij-feedback-abstraction.md`
- `docs/design-swebench-runner.md`
- `docs/swebench-headless-debug-journey.md`
- `docs/story-observability-mvp-design.md`
- `docs/design-sub-story.md`
- `docs/story-quality-flywheel-design.md`
- `docs/design-review-gate-observability-and-control.md`
- `docs/design-smart-orchestrator.md`
- `docs/design-llm-semantic-extraction.md`
- `docs/design-story-source-integration.md`
- `docs/design-terminal-entry-lifecycle.md`
- `docs/design-foreground-zellij-execution.md`
- `docs/idea-orchestrator-agent.md`
- `docs/idea-storyos-project-intelligence-control-plane.md`

---

## StoryOS 收敛主线

现有 roadmap 不推翻，StoryOS 是它的上位叙事：

| 版本 | StoryOS 视角 | 核心结果 |
|---|---|---|
| v0.6.0 | Control Plane Foundation | 诊断、DecisionEnvelope、Policy 骨架、复杂度分流 |
| v0.7.0 | Evidence & Memory Layer | SWE-bench 梯度、Working Memory、Budget Ledger |
| v0.8.0 | Project Intelligence Input Layer | Story Source、Test Source、Project Profile seed、Agent Probe、Resource Lock |
| v0.9.0 | Project-Aware Orchestration | Meta-Planner、Strategic Router、Blackboard、双飞轮治理 |
| v1.0.0 | StoryOS Baseline | 可诊断、可编排、可验证、可治理的 agent 操作层 |

最小闭环：

```text
Story Source
  -> Project Intelligence
  -> Agent Runtime
  -> Test Source / Review Gate
  -> Diagnostics
  -> Flywheel
```

---

## 总览

```
v0.6.0            v0.7.0            v0.8.0            v0.9.0            v1.0.0
    │                 │                 │                 │                 │
    │ Control Plane   │ Evidence &      │ Project Intel   │ Project-aware   │ StoryOS         │
    │ Foundation      │ Memory Layer    │ Input Layer     │ Orchestration   │ Baseline        │
    │ Diagnostics /   │ 梯度归因 /      │ Story Source /  │ 双飞轮治理 /    │ CI/CD /         │
    │ DecisionEnvelope│ 模式提取 /      │ Test Source /   │ Meta-Planner /  │ 文档 /          │
    │ Policy Engine / │ 偏好数据集 /    │ Project Profile │ Strategic Router│ Stage Graph /   │
    │ Complexity分类 │ Working Memory  │ Resource Lock   │ Blackboard      │ Guarded Apply   │
    │ 质量闭环        │ Budget Ledger   │ 开放生态        │ 边界仲裁         │                 │
```

---

## v0.6.0 — Control Plane Foundation

**目标**：三条主线并行——① 补齐 review 环节最后两块拼图（架构审查 + 阶段交接），② 建立编排决策闭环（DecisionEnvelope + Policy Engine + Complexity Classifier），③ 建立 StoryOS 的诊断地基（Debug Packet + `story diagnostics` + Board 右侧常驻诊断面板）。

> 对应 Orchestrator Agent P0 + P1；对应 StoryOS 的 Control Plane Foundation。

当前代码对比：

- 已有：`quality.py`、`gate.py`、`review_feedback.py`、`seed_pipeline.py`、`semantic.py`，能支撑 finding、learned pattern、quality packet 和 review 语义摘要。
- 已有：`router.py` 做 retry/advance/skip 路由、`planner.py` 输出 plan dict、`nodes.py` 做 stage 状态推进。但决策输出非结构化，无 policy 校验，无复杂度分流。
- 已有：`observability.py::build_debug_response()`、`event_log`、`stage_log`、`gate_result`、`.story/context` 和 `.story/done`，但无稳定 Debug Packet schema、无脱敏诊断包、无右侧常驻诊断面板。
- 待补：`architecture_triggers.py`、stage handoff 协议、DecisionEnvelope 类型定义、Policy Engine 骨架、Complexity Classifier、`debug_packet.py`、`diagnostics.py`、`story diagnostics` CLI、Board Diagnostics Panel。

参考设计文档：

- `docs/idea-architecture-review-gate.md`
- `docs/engineering-architecture-review-triggers.md`
- `docs/idea-stage-handoff-package.md`
- `docs/story-lifecycle-ai-engineering-gap-roadmap.md`
- `docs/idea-orchestrator-agent.md` §P0 DecisionEnvelope + Policy Engine、§P1 Complexity Classifier
- `docs/design-board-diagnostics-panel.md`
- `docs/idea-storyos-project-intelligence-control-plane.md`

### Board Diagnostics + Debug Packet

| 模块 | 内容 | 关键文件 |
|------|------|----------|
| Debug Packet | 稳定 story 诊断 schema：story、done_state、session_state、terminal_output、stuck_reason、recent_events | 新增 `orchestrator/debug_packet.py` |
| Stuck Reason | `cli_exited_without_done`、`done_malformed`、`stage_timeout`、`loop_exhausted` 等确定性规则 | `orchestrator/debug_packet.py` |
| Diagnostic Bundle | 脱敏 zip：summary.md、debug_packet.json、events、stage_logs、gate_results、terminal/recent_output.txt | 新增 `orchestrator/diagnostics.py` |
| CLI | `story diagnostics STORY_KEY`、`story diagnostics --global`、`--no-zip` | 新增 `cli/diagnostics.py`，注册到 `cli/main.py` |
| Board 右侧常驻面板 | 当前 Story 摘要、卡住原因、最近事件、诊断动作 | `cli/tui.py` |
| 诊断快捷键 | `[p]` 当前 Story 诊断、`[P]` 全局诊断 | `cli/tui.py` |
| 脱敏 | API key/token/password/authorization/.env/完整 diff 默认不打包 | `orchestrator/diagnostics.py` |

### 架构审查门禁

| 模块 | 内容 | 关键文件 |
|------|------|----------|
| Signal 收集 | Bug 分类 → 模式识别 | `orchestrator/quality.py`（已有 pattern 匹配） |
| 触发判定 | 多 Bug 指向同一抽象层失败 | 新增 `orchestrator/architecture_triggers.py` |
| Packet 生成 | 结构化的架构审查请求 | `orchestrator/quality.py`（扩展现有 packet） |
| TUI 展示 | 架构告警入口 | `cli/tui.py` |

### 阶段交接包

| 模块 | 内容 | 关键文件 |
|------|------|----------|
| Handoff 协议 | YAML frontmatter + markdown 结构定义 | `orchestrator/nodes.py`（advance 节点触发生成） |
| LLM 自动生成 | 阶段完成时自动生成交接文档 | `orchestrator/planner.py`（调用 LLM 生成） |
| Review 校验 | 交接质量审查 | `orchestrator/gate.py`（扩展 gate 检查） |
| 注入到下级 prompt | Executor 拿到上一阶段的 handoff 作为上下文 | `orchestrator/nodes.py` `_render_prompt()` |

### P0: DecisionEnvelope + Policy Engine 骨架

统一 planner / router / reviewer 的决策输出结构，建立 `LLM proposes, Policy disposes` 闭环。

| 模块 | 内容 | 关键文件 |
|------|------|----------|
| DecisionEnvelope 类型 | decision_id、confidence、reason、budget_delta、requires_human、policy_result | 新增 `orchestrator/envelope.py` |
| StrategyEnvelope 类型 | strategy_id、mode、initial_route、budget、thresholds、human_interrupt_policy | 新增 `orchestrator/envelope.py` |
| Router 输出改造 | `router.py` 返回 DecisionEnvelope 而非裸 action string | `orchestrator/router.py` |
| Planner 输出改造 | `planner.py` plan dict 嵌入 DecisionEnvelope 字段 | `orchestrator/planner.py` |
| Policy Engine 骨架 | allow / reject / needs_confirm / shadow_only 四态裁决 | 新增 `orchestrator/policy.py` |
| Policy 最小规则 | destructive→needs_confirm、production risk→needs_confirm、超预算→reject、低 confidence→needs_confirm | `orchestrator/policy.py` |
| Graph 节点集成 | plan_stage / router_node 调用 policy_check 后再执行 | `orchestrator/nodes.py`、`orchestrator/graph.py` |
| 审计日志 | 每次 policy decision 写入 event_log | `orchestrator/observability.py` |

### P1: Complexity Classifier + Simple Execution Path

START 阶段做 cheap intake，trivial 任务走简化路径。

| 模块 | 内容 | 关键文件 |
|------|------|----------|
| Complexity Classifier | 规则/小模型判断 trivial/S/M/L/Epic | 新增 `orchestrator/complexity.py` |
| START cheap intake | 只读标题、来源元数据、PRD 摘要、关键词 | `orchestrator/entry.py`（扩展 intake 逻辑） |
| Simple Execution Path | trivial 任务跳过完整 Meta-Planner，可选轻量 review | `orchestrator/graph.py`（条件分支） |
| 熔断机制 | 修改文件数超阈值、触及高风险路径、retry 超限 → return_to_plan | `orchestrator/nodes.py`（circuit breaker） |
| Profile 集成 | minimal profile 增加 `simple_path` 配置段 | `profiles/minimal.yaml` |

---

## v0.7.0 — Engine 数据飞轮 + 工作记忆

**目标**：两条主线——① SWE-bench 数据飞轮（梯度归因、模式提取、偏好数据集），② 每个 story 建立 Working Memory 和 Budget Ledger，让跨 stage 上下文不再丢失。

> 对应 Orchestrator Agent P2。

当前代码对比：

- 已有：`story swebench prepare/solve/export/eval/summarize/run`，以及 clone cache、worktree、patch extraction、official harness 调用和 summary 输出。
- 已有：`planner.py` 有 trajectory_score、`evaluator_loop.py` 有 retry count、`observability.py` 有 event log。但无 story 级持续记忆、无预算追踪。
- 待补：`story swebench analyze`、failure attribution、counterfactual candidate、preference dataset、pattern extraction、A/B 效果追踪、Working Memory 持久化、Budget Ledger。

参考设计文档：

- `docs/idea-swebench-data-flywheel.md`
- `docs/design-swebench-gradient-data-flywheel.md`
- `docs/design-swebench-runner.md`
- `docs/design-headless-zellij-feedback-abstraction.md`
- `docs/idea-orchestrator-agent.md` §Working Memory、§Budget Ledger

### 梯度归因 + 反事实候选

| 模块 | 内容 | 关键文件 |
|------|------|----------|
| `story swebench analyze` | 单实例后分析，定位失败节点 | `benchmarks/swebench.py`（新增 analyze 子命令） |
| 归因报告 | 结构化输出失败原因分类 | 新增 `benchmarks/attribution.py` |
| 反事实候选 | 基于梯度信号生成改进方案 | `benchmarks/attribution.py` |
| 候选排序 | 预估改进收益排序 | `benchmarks/attribution.py` |

### 模式提取管道

| 模块 | 内容 | 关键文件 |
|------|------|----------|
| 约束提取 | 从失败实例提取通用约束 | `orchestrator/semantic.py`（扩展 pattern 提取） |
| 提示注入 | 约束注入到同类实例的 prompt | `orchestrator/nodes.py` `_render_prompt()` |
| 效果追踪 | A/B 对比，验证注入是否提升 pass@1 | 新增 `benchmarks/ab_tracker.py` |

### 偏好数据集生成

| 模块 | 内容 | 关键文件 |
|------|------|----------|
| 轨迹对比 | 好轨迹 vs 坏轨迹配对 | 新增 `benchmarks/preference.py` |
| 数据集导出 | 标准格式，可复用于 router 训练 | `benchmarks/preference.py` |
| 回归套件 | 固定实例集，每次改动必跑 | `benchmarks/swebench.py`（扩展 run 子命令） |

### 对抗审查 CLI 化

| 模块 | 内容 | 关键文件 |
|------|------|----------|
| `mode: cli` 执行路径 | 调用 adapter 启动独立 CLI session 做审查 | `orchestrator/evaluator_loop.py` |
| Plan Review CLI | Reviewer 用 Claude Code 独立 session 审计划 | `orchestrator/evaluator_loop.py`（新增 review mode） |
| Code Review CLI | Reviewer 用 Codex CLI 独立 session 审代码 | `orchestrator/evaluator_loop.py`（新增 review mode） |
| 混合策略 | 默认 `mode: api`（快），高复杂度/安全敏感 story 自动 `mode: cli` | `orchestrator/evaluator_loop.py`（策略选择） |

> 设计依据：`docs/design-review-gate-observability-and-control.md` §reviewers 配置。

### P2: Working Memory + Budget Ledger

| 模块 | 内容 | 关键文件 |
|------|------|----------|
| Working Memory 结构 | confirmed_facts、open_risks、discarded_paths、latest_findings、budget_status | 新增 `orchestrator/working_memory.py` |
| 持久化 | `.story/context/{story_key}/working_memory.json` | `orchestrator/working_memory.py` |
| Stage 读取/更新 | 每个 stage 开始读 memory，结束时结构化更新 | `orchestrator/nodes.py`（plan_stage / review_stage） |
| Planner 消费 memory | planner prompt 注入 working memory 上下文 | `orchestrator/planner.py` |
| Budget Ledger 结构 | max/used：minutes、llm_calls、expensive_model_calls、retries、human_interrupts | 新增 `orchestrator/budget.py` |
| 预算检查 | 每个 DecisionEnvelope 声明 budget_delta，Policy Engine 校验 | `orchestrator/policy.py`（扩展规则） |
| 预算报告 | TUI 展示剩余预算、burn rate | `cli/tui.py` |
| Evaluator 集成 | retry 时更新 budget，超预算时 hard kill | `orchestrator/evaluator_loop.py` |

---

## v0.8.0 — Project Intelligence Input Layer

**目标**：五条主线——① Story Source 产品化和 PRD 输入增强，② Test Source 抽象与项目测试发现，③ Project Profile seed（让系统开始熟悉项目），④ 受控 Project Intelligence Probe（调用 code agent 做只读项目探查），⑤ 资源锁 dry-run（为并行调度做安全准备）。

> 对应 Orchestrator Agent P1.5；对应 StoryOS 的 Project Intelligence Input Layer。

当前代码对比：

- 已有：`StorySource`、`ManualSource`、`TapdSource`、`PrdProvider`、`TapdBodyPrdProvider`、`LocalFilePrdProvider`、`ShellAdapter`、`story demo`、doctor 中 Qoder/Gemini 检测。
- 已有：`service.py` 子故事创建、`graph.py` story 状态推进。但无资源锁机制，子故事并行依赖 workspace mutex。
- 待补：TAPD HTML→Markdown 高质量转换、AI PRD 增强、Story Source 配置产品化、Test Source 抽象、repo test discovery、Project Profile seed、Project Intelligence Probe、多模型并行对比、Resource Lock dry-run。

参考设计文档：

- `docs/design-story-source-integration.md`
- `docs/idea-project-intelligence-pipeline.md`
- `docs/superpowers/specs/2026-05-23-story-source-p0.md`
- `docs/superpowers/plans/2026-05-23-story-source-p1.md`
- `docs/superpowers/specs/2026-05-21-story-lifecycle-v2-design.md`
- `docs/idea-orchestrator-agent.md` §Resource Locks、§并行策略
- `docs/idea-storyos-project-intelligence-control-plane.md`
- `docs/design-workspace-onboarding-project-profile.md`

### Story Source 产品化 + PRD 输入增强

| 模块 | 内容 | 关键文件 |
|------|------|----------|
| PRD 提取质量 | TAPD HTML→markdown 替换为 markdownify/html2text | `sources/tapd_source.py`（替换提取逻辑） |
| AI 增强 PRD | 拉取时可选 LLM 优化 PRD 内容 | `sources/tapd_source.py`（新增 enhance 步骤） |
| 本地文件 PRD | 已有 LocalFilePrdProvider，补配置化、错误提示和 TUI 可见性 | `sources/base.py`、`cli/tui.py` |
| Story Source 统一输出 | source、source_id、type、title、body、acceptance_criteria、comments、priority、business_area | `sources/base.py` |
| TUI 可见性 | Story 来源、原始链接、验收标准、最近评论可见 | `cli/tui.py` |

### Test Source 抽象

没有 Test Source，系统只能判断“任务写完了”；有 Test Source，才能判断“任务做对了”。

| 模块 | 内容 | 关键文件 |
|------|------|----------|
| TestSource 接口 | 统一本地测试、CI、PRD checklist、benchmark 验证源 | 新增 `testsources/base.py` 或 `orchestrator/test_source.py` |
| Repo test discovery | 自动发现 pytest/maven/npm/gradle 等候选测试命令 | 新增 `orchestrator/project_profile.py` |
| Test Plan | 根据 Story 影响范围选择最小验证命令 | 新增 `orchestrator/test_plan.py` |
| PRD checklist | 从 acceptance criteria 生成人工/半自动验证 checklist | `orchestrator/test_plan.py` |
| TUI 展示 | 当前 Story 的建议测试和验证状态 | `cli/tui.py` |

### Project Profile Seed

Project Profile 是 Project Intelligence Layer 的第一版项目画像，P0.8 只做事实收集，不做复杂学习。

| 模块 | 内容 | 关键文件 |
|------|------|----------|
| Repo scanner | 语言、包管理器、入口文件、测试目录、CI 文件、服务目录 | 新增 `orchestrator/project_profile.py` |
| Profile 文件 | `.story/project/profile.json` 或 `.story/context/project_profile.json` | 新增 `orchestrator/project_profile.py` |
| 启动/测试候选 | 从 README、package/maven/pytest/CI 推断候选命令 | `orchestrator/project_profile.py` |
| Evidence | 每个候选命令记录来源证据，避免 LLM 幻觉 | `orchestrator/project_profile.py` |
| CLI | `story project inspect` 输出项目画像 | 新增 `cli/project.py` |

### Workspace Onboarding

首次在某个目录运行 `story` 时，建立该目录的 Project Profile。

| 模块 | 内容 | 关键文件 |
|------|------|----------|
| Onboarding 检测 | 检查 `.story/project/profile.json` 是否存在，不存在则进入初始化 | `orchestrator/project_profile.py` |
| Deterministic scan | 生成 observed facts：workspace_type、repo inventory、test candidates、CI、doc assets、release signals | `orchestrator/project_scan.py` |
| 用户确认 | observed facts 经用户 accept/edit/ignore 后成为 confirmed facts | `cli/project.py` |
| Project Profile | 写入 `.story/project/profile.json` | `orchestrator/project_profile.py` |
| Story Start Refresh | 每个 Story 开始前轻量检测 repo/test/profile drift | `orchestrator/project_profile.py` |
| CLI | `story project onboard`、`story project confirm`、`story project refresh` | `cli/project.py` |

### Project Intelligence Probe

StoryOS 可以受控调用 code agent 理解项目上下文，但必须是只读、有边界、有 schema、有 evidence 的探查任务。

| 模块 | 内容 | 关键文件 |
|------|------|----------|
| Probe 任务书 | 明确探查问题、只读约束、输出 schema、禁止 destructive 命令 | 新增 `orchestrator/project_probe.py` |
| Agent 调用 | 复用 adapter 调 Claude Code/Codex/Qoder 做只读项目探查 | `orchestrator/project_probe.py`、`adapters/` |
| 输出 schema | `facts` / `hypotheses` / `open_questions`，每条 fact 必须带 evidence | `orchestrator/project_probe.py` |
| 校验 | 路径存在、命令非 destructive、JSON schema 合法、confidence 合法 | `orchestrator/project_probe.py` |
| 落盘 | 写入 Project Profile 或 Evidence Store，供 planner/router 后续消费 | `orchestrator/project_profile.py` |
| CLI | `story project probe --question ...` 或 `story project inspect --agent` | `cli/project.py` |

### 多模型并行对比

| 模块 | 内容 | 关键文件 |
|------|------|----------|
| 并行执行 | 同一 story 多模型同时跑 | `orchestrator/service.py`（新增 parallel execution） |
| 结果对比 | 质量评分、diff 对比 | 新增 `orchestrator/comparison.py` |
| TUI 展示 | 对比面板 | `cli/tui.py` |

### 开放生态

| 模块 | 内容 | 关键文件 |
|------|------|----------|
| ShellAdapter 文档 | 已有 `adapters.yaml` 配置驱动实现，补文档和示例 | `adapters/` |
| Adapter 测试基类 | 已有 adapter 单测，补新适配器复用模板 | `tests/` |
| 分层引导 | Quick Start → 进阶 → 自定义 | `README.md` |
| Qoder / Gemini CLI 适配器 | doctor 已检测，补上适配器实现 | `adapters/`（新增 adapter 文件） |

### P1.5: Resource Lock Dry-run

在真正开启并行调度前，后台模拟 resource_locks 争用，校准锁粒度。

| 模块 | 内容 | 关键文件 |
|------|------|----------|
| Resource Lock 定义 | file_glob、domain_area、db_table、api_prefix 四类锁 | 新增 `orchestrator/resource_lock.py` |
| Decomposition Plan 扩展 | task 增加 resource_locks 字段 | `orchestrator/planner.py`（plan 输出扩展） |
| Dry-run 调度器 | 模拟并行执行，输出冲突报告 | `orchestrator/resource_lock.py`（dry_run 方法） |
| 冲突报告 | “如果并行会冲突”的结构化输出 | `orchestrator/resource_lock.py` |
| TUI 可见 | dry-run 报告在 TUI 展示 | `cli/tui.py` |
| Service 集成 | create_and_start_story 后台触发 dry-run | `orchestrator/service.py` |

---

## v0.9.0 — 双飞轮治理层 + 智能路由

**目标**：三条主线汇合——① 双飞轮治理（domain + engine 统一治理），② Strategic Router shadow mode + Runtime Blackboard（跨 story 实时信号），③ Meta-Planner + Plan-stage Decomposition（Story 级策略和拆分）。

> 对应 Orchestrator Agent P3 + P4 + P5。

当前代码对比：

- 已有：`semantic.py`、`evaluator_loop.py`、`planner.py`、`tools/`、`trajectory_score` 和 review recovery 的基础能力。
- 已有：`observability.py` 有 event log、`router.py` 有 retry/advance 路由、`nodes.py` 有 plan_stage。但 router 只处理异常、无 cross-story 信号、planner 无全局策略。
- 待补：Project Intelligence collector、Strategic Router shadow、Runtime Blackboard、Meta-Planner StrategyEnvelope、Scope & Decomposition Gate、Task Packet 生成。

参考设计文档：

- `docs/idea-dual-flywheel-domain-and-engine.md`
- `docs/idea-project-intelligence-pipeline.md`
- `docs/design-swebench-gradient-data-flywheel.md`
- `docs/story-quality-flywheel-design.md`
- `docs/idea-plan-review-adversarial-loop.md`
- `docs/superpowers/specs/2026-05-24-evaluator-optimizer-loop-design.md`
- `docs/idea-orchestrator-agent.md` §Strategic Router、§Runtime Blackboard、§Meta-Planner、§Plan-stage Decomposition

### 项目智能管道

| 模块 | 内容 | 关键文件 |
|------|------|----------|
| `RepoScannerCollector` | 代码库静态信号收集 | 新增 `orchestrator/collectors/repo_scanner.py` |
| `TapdCollector` | 需求/Bug 动态信号收集 | `sources/tapd_source.py`（扩展为 collector） |
| Project Intelligence Packet | 注入 Planner prompt | `orchestrator/planner.py`（消费 intelligence packet） |
| 运行时信号 | 慢查询、错误日志检测 | `orchestrator/observability.py`（扩展 runtime signal） |

### 双飞轮治理

| 模块 | 内容 | 关键文件 |
|------|------|----------|
| Domain 治理 | Domain Asset / Outcome / Trace Maturity | 新增 `orchestrator/flywheel/domain.py` |
| Engine 治理 | Engine Trace / Strategy / Eval Evidence | 新增 `orchestrator/flywheel/engine.py` |
| 共享晋升队列 | `proposed → sandbox_validated → active` | 新增 `orchestrator/flywheel/promotion.py` |
| 冲突仲裁 | `safety > domain production > engine execution > domain pattern > engine pattern` | `orchestrator/policy.py`（扩展仲裁规则） |
| 边界控制 | 原始业务数据不进 engine，原始 engine trace 不直接改 domain | `orchestrator/policy.py`（边界校验） |

### 高级对抗循环收尾

| 模块 | 内容 | 关键文件 |
|------|------|----------|
| 结构化 findings 收敛 | 替代分数阈值判停 | `orchestrator/evaluator_loop.py` |
| Verification ladder | L0-L5 验证等级体系 | `orchestrator/gate.py`（扩展验证等级） |
| Debug recovery | LLM 驱动的恢复建议接入治理层 | `orchestrator/semantic.py` `recommend_recovery()` |

### P3: Strategic Router Shadow Mode

只在异常点生成 Strategic Router 建议，不执行，只记录 old_decision vs proposed_decision。

| 模块 | 内容 | 关键文件 |
|------|------|----------|
| 异常触发判定 | review revise/fail、retry 无进展、trajectory 低分、provider 降级 | `orchestrator/router.py`（新增 shadow trigger） |
| Shadow DecisionEnvelope | 记录 proposed vs actual decision | `orchestrator/router.py`（shadow output） |
| 反事实评估字段 | human_label、later_outcome、counterfactual_note | `orchestrator/envelope.py`（扩展字段） |
| TUI 标注入口 | 非阻塞 human counterfactual label | `cli/tui.py` |
| Shadow 统计 | proposed decision 与后续 outcome 关系统计 | 新增 `orchestrator/shadow_stats.py` |

### P4: Runtime Blackboard

从 event_log 聚合 provider/model/stage 健康度，异步更新，主流程只读 snapshot。

| 模块 | 内容 | 关键文件 |
|------|------|----------|
| EventBus 事实流 | 所有 story/stage/LLM/tool 事件结构化写入 | `orchestrator/observability.py`（扩展 event_log） |
| Blackboard 聚合器 | 异步消费 event_log，生成 provider_health / failure_signatures / workspace_pressure snapshot | 新增 `orchestrator/blackboard.py` |
| TTL + 滑动窗口 | snapshot 带 updated_at、staleness_ms、ttl_seconds | `orchestrator/blackboard.py` |
| Router 消费 | planner/router 读取 blackboard 作为低优先级证据 | `orchestrator/router.py`、`orchestrator/planner.py` |
| TUI 新鲜度展示 | 显示 snapshot staleness | `cli/tui.py` |
| 降级容错 | blackboard 不可用时 router 降级为不使用该信号 | `orchestrator/router.py` |

### P5: Meta-Planner + Plan-stage Decomposition

Story START 生成 StrategyEnvelope，Plan 阶段执行 Scope & Decomposition Gate。

| 模块 | 内容 | 关键文件 |
|------|------|----------|
| StrategyEnvelope 生成 | Story START 输出 mode、budget、thresholds、fallback_plan | `orchestrator/planner.py`（新增 Meta-Planner 函数） |
| Scope & Decomposition Gate | plan_stage 判断 S/M/L/Epic，L/Epic 输出拆分建议 | `orchestrator/planner.py`（decomposition 分支） |
| Decomposition Plan | `.story/context/{key}/plan/decomposition.json` | `orchestrator/planner.py`（输出 decomposition） |
| Task Packet 生成 | per-task context sharding → `.story/context/{key}/plan/tasks/{id}.md` | 新增 `orchestrator/task_packet.py` |
| 子故事自动创建 | decomposition 确认后 auto-create sub-stories | `orchestrator/service.py`（create_sub_stories） |
| Plan-stage 集成 | plan_stage 输出 StrategyEnvelope 或 Decomposition Plan | `orchestrator/nodes.py`（plan_stage 扩展） |
| Autonomy Level | L0-L5 自主等级控制，profile 或 CLI 指定 | `orchestrator/policy.py`（autonomy 约束） |
| 人机协作协议 | 结构化提问（question + recommendation + options） | `orchestrator/nodes.py`（wait_confirm 扩展） |

---

## v1.0.0 — StoryOS Baseline

**目标**：达到可公开发布的质量标准 + 完成动态编排能力（Stage Graph、Graph Patch、Guarded Apply），形成 StoryOS baseline：可诊断、可编排、可验证、可治理的 Code Agent 操作层。

> 对应 Orchestrator Agent P6 + P7。

当前代码对比：

- 已有：单元测试、部分 e2e scenario、SWE-bench runner、doctor、demo、Windows/Zellij 修复经验。
- 已有：`graph.py` 固定 10 节点 StateGraph、`policy.py`（v0.6 引入）基础 policy check、`envelope.py`（v0.6 引入）DecisionEnvelope。
- 待补：Stage Library 定义、Stage Graph 动态边、Graph Patch Registry、sandbox validation、autonomy-driven guarded apply、全平台 CI、文档、稳定性。

参考设计文档：

- `docs/e2e-test.md`
- `docs/roadmap-and-priorities.md`
- `docs/story-observability-mvp-design.md`
- `docs/design-terminal-entry-lifecycle.md`
- `docs/design-swebench-runner.md`
- `docs/idea-orchestrator-agent.md` §Stage Library、§Stage Graph、§Graph Patch、§Guarded Apply
- `docs/idea-storyos-project-intelligence-control-plane.md`

### CI / CD

| 模块 | 内容 | 关键文件 |
|------|------|----------|
| GitHub Actions | 全平台 CI（Windows / Linux / macOS） | `.github/workflows/` |
| 自动化测试 | lint + unit + e2e 完整流水线 | `.github/workflows/` |
| 发布流程 | PyPI 发布 + changelog 自动生成 | `.github/workflows/`、`pyproject.toml` |

### 文档

| 模块 | 内容 | 关键文件 |
|------|------|----------|
| README Quick Start | 5 分钟上手 | `README.md` |
| 完整 API 文档 | OpenAPI / Swagger | `orchestrator/api.py`（扩展 schema） |
| 架构文档 | 设计决策记录 | `docs/` |
| 贡献指南 | CONTRIBUTING.md | `CONTRIBUTING.md` |

### 稳定性

| 模块 | 内容 | 关键文件 |
|------|------|----------|
| 回归套件 | SWE-bench + 自定义用例 | `benchmarks/`、`tests/` |
| 错误恢复 | 所有已知异常路径有恢复逻辑 | `orchestrator/nodes.py` |
| 向后兼容 | 配置文件 / DB schema 版本化迁移 | `db/models.py` |
| Windows CI | WSL + Git Bash 双通道修复 | `.github/workflows/` |

### P6: Stage Graph + Graph Patch Registry + Sandbox Validation

| 模块 | 内容 | 关键文件 |
|------|------|----------|
| Stage Library | 所有合法原子 stage 定义（输入/输出/tools/model/budget/auto_insert） | 新增 `orchestrator/stage_library.py` |
| Stage Graph | stage 间允许的边（非固定线性序列） | 新增 `orchestrator/stage_graph.py` |
| Graph Patch Registry | insert_stage、repeat_stage、skip_stage、split_sub_story、switch_model、pause_for_human | 新增 `orchestrator/graph_patch.py` |
| Patch schema | 每个 patch 声明 precondition、budget_delta、risk_level、rollback | `orchestrator/graph_patch.py` |
| Policy 校验 | patch 必须通过 policy check 才能执行 | `orchestrator/policy.py`（扩展 patch 规则） |
| Graph 动态修改 | 根据 approved patch 动态修改 StateGraph 边 | `orchestrator/graph.py`（动态 rebuild） |
| Shadow mode | 低自治等级下 patch 先记录不执行，积累反事实数据 | `orchestrator/graph_patch.py`（shadow logic） |
| Sandbox validation | SWE-bench / 显式授权环境真实跑分支，比较 pass rate | 新增 `orchestrator/sandbox.py` |

### P7: Guarded Apply

根据 autonomy level 启用自动应用 graph patch。

| 模块 | 内容 | 关键文件 |
|------|------|----------|
| Autonomy L0-L2 | 所有 apply 类动作转 shadow_only 或 needs_confirm | `orchestrator/policy.py`（autonomy 执行） |
| Autonomy L3 | 低风险 patch 自动执行（skip_stage、repeat_stage），高风险问人 | `orchestrator/policy.py`（风险分级） |
| Autonomy L4 | 预算内允许调模型、调 retry、插入低风险 stage | `orchestrator/policy.py`（budget autonomy） |
| Autonomy L5 | 仅 SWE-bench / 显式授权 profile，允许更大范围 graph patch | `orchestrator/policy.py`（full autonomy） |
| Human override | 用户可随时覆盖 agent 决策 | `cli/tui.py`（override 入口） |
| 全链路审计 | 每次 autonomy decision 写入 trace | `orchestrator/observability.py` |

### 高级功能收尾

| 模块 | 内容 | 关键文件 |
|------|------|----------|
| 分支搜索 | MCTS 多路径探索 | 新增 `orchestrator/search.py` |
| 批量子故事 | 一键拆解大需求 | `orchestrator/service.py`（batch create） |
| 嵌套子故事 | 子故事再拆子故事 | `orchestrator/service.py`（recursive split） |

### 最终审查

| 模块 | 内容 | 关键文件 |
|------|------|----------|
| 安全审查 | 全量代码安全审计 | 全仓库 |
| 性能基准 | 大规模 story 并发测试 | `benchmarks/` |
| 升级指南 | v0.x → v1.0.0 迁移文档 | `docs/` |

---

## v2 — 服务端部署（后续）

参考设计文档：

- `docs/idea-ttyd-server-side-web-terminal.md`
- `docs/superpowers/specs/2026-05-21-story-lifecycle-v2-design.md`

| 模块 | 内容 |
|------|------|
| ttyd Web 终端重连 | 执行模型统一、生命周期管理、安全认证 |
| 后台守护 | systemd / Windows Service |
| 多租户 | 用户隔离、资源配额 |
| Webhook 模式 | 外部事件触发 story |
| Jira / GitHub Issues | 其他平台适配器 |

---

## 版本依赖关系

```
v0.6.0 ──→ v0.7.0 ──→ v0.8.0 ──→ v0.9.0 ──→ v1.0.0
  │           │           │           │           │
  │           │           │           │           └── StoryOS baseline + 动态编排
  │           │           │           └── 项目感知编排需要 P0-P2 和输入层就位
  │           │           └── Project Intelligence Input Layer + 资源锁预演，可与 v0.7 并行
  │           └── Evidence & Memory Layer，需要 P0 诊断和决策基础
  └── Control Plane Foundation：诊断 + DecisionEnvelope + Policy Engine + Complexity Classifier
```

v0.7.0 和 v0.8.0 可并行推进，二者在 v0.9.0 汇合。

---

## StoryOS 能力映射

| StoryOS 能力 | 首次落地版本 | 说明 |
|---|---|---|
| Diagnostics / Debug Packet | v0.6.0 | 操作层可观测地基 |
| DecisionEnvelope / Policy | v0.6.0 | 控制平面决策协议 |
| Evidence & Memory | v0.7.0 | trace、budget、working memory |
| Story Source | v0.8.0 | TAPD/PRD/本地需求输入产品化 |
| Test Source | v0.8.0 | 项目验证入口和 test plan |
| Project Profile | v0.8.0 | Project Intelligence seed |
| Workspace Onboarding | v0.8.0 | 首次接管目录，生成并确认 Project Profile |
| Project Intelligence Probe | v0.8.0 | 受控调用 code agent 做只读项目上下文探查 |
| Project-aware Router / Planner | v0.9.0 | 消费项目画像、blackboard、双飞轮信号 |
| StoryOS Baseline | v1.0.0 | 可诊断、可编排、可验证、可治理 |

---

## Orchestrator Agent P0–P7 版本映射

| 优先级 | 内容 | 目标版本 |
|--------|------|----------|
| P0 | DecisionEnvelope + Policy Engine 骨架 | v0.6.0 |
| P1 | Complexity Classifier + Simple Execution Path | v0.6.0 |
| P2 | Working Memory + Budget Ledger | v0.7.0 |
| P1.5 | Resource Lock Dry-run | v0.8.0 |
| P3 | Strategic Router Shadow Mode | v0.9.0 |
| P4 | Runtime Blackboard | v0.9.0 |
| P5 | Meta-Planner + Plan-stage Decomposition | v0.9.0 |
| P6 | Stage Graph + Graph Patch + Sandbox Validation | v1.0.0 |
| P7 | Guarded Apply（autonomy L0-L5） | v1.0.0 |
