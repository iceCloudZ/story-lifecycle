# Story Lifecycle 代码冻结与 v0.6 Reliability Loop 收敛计划

> 生成日期：2026-05-27 | 基于代码事实的深度对账

## 1. 背景

Story Lifecycle 项目当前处于 v0.5.37，正从 Story Lifecycle 演进为 StoryOS / Code Agent 操作层。`docs/` 下有 60+ 设计文档，`roadmap-v0.5-to-v1.0.md` 规划到 v1.0.0 的五版本路线。但设计文档和代码实现之间存在显著错位——部分能力远超 roadmap 声称的"已完成"，部分 roadmap 声称已完成的能力实际上只是提示层面的软约束、无系统级代码。

**核心矛盾**：项目需要先让最小闭环稳定（"可安装、可启动、可跑通、可卡住、可排查"），再谈 StoryOS 扩展。

## 2. 为什么需要冻结

1. **设计文档膨胀**：60+ 文档覆盖到 v1.0，大量标为 `idea_*` 的文档无实现计划，混淆当前能力边界
2. **实现超前于 roadmap**：P0 Story Source、P0+P1 Quality Flywheel、P2 Policy Engine、P2 Copilot 等均已实现，但 roadmap 声称这些属于 v0.6-v0.8
3. **提示层面软约束伪装成系统能力**：`affected_repos`、分支门、Git 约束仅存在于 prompt 文本中，无系统级验证
4. **测试覆盖不均**：431 个测试全部通过，但 setup/doctor/TUI/workspace-lock 缺少直接测试

## 3. 冻结原则

1. **不新增功能**，只修 bug
2. **不新增设计文档**，只归档和分类现有文档
3. **v0.6 只做可靠性收敛**：CLI 稳定、协议稳定、诊断可用、最小回归测试
4. **明确推迟**所有 StoryOS 扩展（Project Intelligence、Workspace Onboarding、双飞轮等）

## 4. 当前代码事实清单

### 4.1 版本与包

| 项目 | 值 |
|------|-----|
| 版本 | 0.5.37 (`pyproject.toml:7`) |
| 入口 | `story = story_lifecycle.cli.main:cli` (`pyproject.toml:53`) |
| 包 | `src/story_lifecycle` + `profiles/**` + `prompts/**` (`pyproject.toml:61-62`) |
| Python | >=3.10 |
| 依赖 | fastapi, uvicorn, click, rich, pyyaml, langgraph, langgraph-checkpoint-sqlite, httpx, plyer, filelock |

### 4.2 CLI 命令全景

**已实现命令**（`src/story_lifecycle/cli/main.py`）：

| 命令 | 状态 | 来源文件 |
|------|------|----------|
| `story` (默认→TUI) | ✅ | `main.py:104-164` |
| `story create <KEY>` | ✅ | `main.py:167-248` |
| `story setup` | ✅ | `main.py:251-255`, `setup.py` |
| `story serve` | ✅ | `main.py:258-264` |
| `story demo` | ✅ | `main.py:266-272`, `demo.py` |
| `story upgrade` | ✅ | `main.py:306-333` |
| `story doctor` / `story doctor paths` | ✅ | `main.py:351-364`, `doctor.py` |
| `story diagnostics <KEY>` / `--global` | ✅ | `main.py:421-422`, `diagnostics.py` |
| `story swebench prepare/solve/export/eval/summarize/run` | ✅ | `swebench.py` |
| `story review-feedback import/list/decide` | ✅ | `review_feedback.py` |
| `story approvals list/decide` | ✅ | `review_feedback.py:207-340` |
| `story findings` | ✅ | `review_feedback.py:346` |
| `story seed-quality analyze/apply/preview-packet` | ✅ | `seed_quality.py` |

**未实现命令**（README 或设计文档提及但无代码）：
- `story project inspect/onboard/confirm/refresh/probe` — 不存在
- `story swebench analyze` — 不存在

**API Key 绕过逻辑**（`main.py:122-129`）：`setup`、`serve`、`doctor`、`demo`、`upgrade`、`swebench`、`diagnostics` 七个命令绕过 `is_configured()` 检查，其余命令在未配置时会弹出 setup wizard。

### 4.3 Orchestrator 核心流程

**StateGraph 节点**（`graph.py:214-275`）：
```
START → plan_stage → execute_stage → poll_completion → review_stage → router
                                                                          │
                            ┌─────────────────────────────────────────────┤
                            │          │          │          │            │
                         advance    retry    skip_stage  fail_stage  wait_confirm
                            │          │          │          │            │
                      (next/END)  plan_stage  advance     END      plan_stage
```

**Done 文件协议**（实际路径）：
- 写入：`.story/done/{story_key}/{stage}.json`（`paths.py:30`）
- 快照：`.story/context/{story_key}/done/{stage}.json`（`paths.py:50`）
- 格式错误：`.story/context/{story_key}/done/{stage}.malformed`（`paths.py:55`）
- 消费：解析成功后 `done_file.unlink()`（`nodes.py:1312`）——通过删除标记已消费
- 解析：`robust_json_parse()`（`nodes.py:168-194`），三策略：直接解析 → 括号提取 → markdown fence

**Router 模式**（`nodes.py:1392-1558`）：
1. 预路由覆盖（adversarial 设置的 `_pre_routed_action`）
2. 重试疲劳 → fail
3. 低轨迹分数（<0.3）→ fail
4. 快乐路径（无错误）→ advance 或 wait_confirm
5. 缺少预期输出 → fail
6. 审查驱动（有 review_summary）→ retry 或 fail
7. 执行次数上限 → wait_confirm
8. **LLM 回退**：以上都不匹配时调用 `router.py` 的 LLM 路由

**无头模式合成 done**（`tools/base.py:213-261`）：当 `_run_headless()` 子进程返回 0 时，从 stdout 提取 JSON 或生成 `{"output": "...", "synthetic": true}`。

### 4.4 数据库表

| 表 | 关键字段 |
|----|----------|
| `story` | id, story_key, title, workspace, profile, current_stage, status, complexity, context_json, execution_count, last_error, parent_key, subtask_index, sub_type, source_type, source_id |
| `stage_log` | id, story_id, stage, action, detail, created_at |
| `gate_result` | id, story_id, stage, gate_name, result, detail, created_at |
| `event_log` | id, story_key, stage, event_type, payload, created_at |
| `llm_trace` | id, story_key, stage, operation, model, prompt_tokens, completion_tokens, total_tokens, duration_ms, success, error |
| `finding` | id, story_key, stage, source, severity, category, location, description, recommendation, root_cause, status, evidence |
| `learned_pattern` | id, pattern, applies_to, rule, source_findings, confidence, status |

### 4.5 已实现的核心能力（超出 roadmap 声称范围）

| 能力 | Roadmap 定位 | 实际状态 | 关键文件 |
|------|-------------|----------|----------|
| StorySource ABC + TapdSource + ManualSource | v0.8 (P0) | ✅ 已实现 | `sources/base.py`, `tapd_source.py` |
| Quality Flywheel P0+P1 | "已完成" | ✅ 已实现 | `quality.py`, `seed_quality.py` |
| Policy Engine (DecisionEnvelope + P3) | v0.6 (P0) | ✅ 已实现 | `policy_engine.py` |
| Copilot + SuggestedAction (P1+P2) | v0.6 以后 | ✅ 已实现 | `copilot.py`, `tui.py` |
| Debug Packet + Diagnostics Bundle | v0.6 | ✅ 已实现 | `debug_packet.py:170`, `diagnostics.py:22` |
| Board 右侧诊断面板 | v0.6 | ✅ 已实现 | `tui.py:1123, 1286-1434` |
| E2E Scenario Runner (5 场景) | v1.0 方向 | ✅ 已实现 | `tests/e2e/` |
| TAPD HTML→Markdown (基础) | v0.8 | ✅ 已实现 | `prd_providers.py:99-108` |
| 子 Story P0+P1 | v0.5.8 已完成 | ✅ 已实现 | `models.py`, `service.py` |
| SWE-bench Pipeline (prepare→solve→export→eval→summarize→run) | v0.5.8 已完成 | ✅ 已实现 | `swebench.py`, `cli/swebench.py` |

## 5. 设计文档状态表

| 文档 | 类型 | 状态 | 关键实现文件 | 说明 |
|------|------|------|-------------|------|
| `design-smart-orchestrator.md` | design | **implemented** | `nodes.py`, `planner.py`, `router.py` | Plan→Execute→Review→Router 核心流程 |
| `design-sub-story.md` | design | **implemented** | `models.py`, `service.py`, `graph.py` | 子 Story 拆分和依赖管理 |
| `design-story-source-integration.md` | design | **implemented** | `sources/base.py`, `tapd_source.py` | StorySource ABC + TAPD adapter (P0 done, P1 partial) |
| `story-quality-flywheel-design.md` | design | **implemented** | `quality.py`, `seed_quality.py` | Finding 生命周期 + Quality Packet + Learned Pattern |
| `story-observability-mvp-design.md` | design | **implemented** | `observability.py`, `api.py:307` | Event log + Debug API |
| `design-llm-semantic-extraction.md` | design | **implemented** | `semantic.py` | LLM bug context + pattern matching |
| `design-review-gate-observability-and-control.md` | design | **implemented** | `gate.py`, `nodes.py:565-945` | GateDecision + review round tracking |
| `superpowers/specs/2026-05-24-evaluator-optimizer-loop-design.md` | spec | **implemented** | `evaluator_loop.py`, `loop_events.py` | 对抗循环 plan↔review + code↔review |
| `design-terminal-entry-lifecycle.md` | design | **implemented** | `entry.py`, `tui.py` | 终端会话生命周期 + 入口状态机 |
| `design-tui-entry-state-machine.md` | design | **implemented** | `tui.py`, `entry.py` | TUI 入口状态机 + resolve_stage_state + decide_action |
| `design-foreground-zellij-execution.md` | design | **implemented** | `tui.py:2573-2644`, `entry.py` | Zellij 前台 attach 流程 |
| `design-board-diagnostics-panel.md` | design | **implemented** | `tui.py:1123, 1286-1434` | 右侧诊断面板 + [p][P][o][y] 快捷键 |
| `design-headless-zellij-feedback-abstraction.md` | design | **implemented** | `validation.py`, `tools/base.py` | 无头/Zellij 抽象 + 合成 done |
| `design-swebench-runner.md` | design | **implemented** | `benchmarks/swebench.py`, `cli/swebench.py` | SWE-bench pipeline |
| `engineering-architecture-review-triggers.md` | design | **partial** | `quality.py` (pattern matching) | 触发判定代码未实现 (`architecture_triggers.py` 不存在) |
| `idea-architecture-review-gate.md` | idea | **partial** | `gate.py` | 基础 gate 已实现，架构审查 gate 未实现 |
| `idea-stage-handoff-package.md` | idea | **not_implemented** | — | 阶段交接包协议未实现 |
| `design-swebench-gradient-data-flywheel.md` | design | **not_implemented** | — | analyze / gradient / preference dataset 均未实现 |
| `idea-swebench-data-flywheel.md` | idea | **not_implemented** | — | 同上 |
| `design-three-layer-validation.md` | design | **partial** | `tests/e2e/` (5 scenarios) | 5/20+ E2E scenarios done; SWE-bench Pro/Multi/Feature/Context/ProjDev 均未实现 |
| `problem-workspace-git-constraint.md` | problem | **partial** | 提示文本 (not system-level) | 仅提示层面约束，无系统级 git/branch/dirty gate |
| `design-workspace-onboarding-project-profile.md` | design | **not_implemented** | — | 所有 project.py / project_profile.py / project_scan.py / project_probe.py 均不存在 |
| `idea-project-intelligence-pipeline.md` | idea | **not_implemented** | — | Pipeline 未实现 |
| `idea-storyos-project-intelligence-control-plane.md` | idea | **not_implemented** | — | Control Plane 层未实现 |
| `idea-dual-flywheel-domain-and-engine.md` | idea | **not_implemented** | — | 双飞轮治理未实现 |
| `idea-orchestrator-agent.md` | idea | **partial** | P0 Policy Engine + P1 E2E done; P1.5-P7 not done | Orchestrator Agent 设计 |
| `idea-ttyd-server-side-web-terminal.md` | idea | **not_implemented** | — | v2 范围 |
| `idea-board-copilot-diagnostics-panel.md` | idea | **implemented** | `tui.py`, `copilot.py` | 已实现到 TUI |
| `roadmap-v0.5-to-v1.0.md` | roadmap | **needs_update** | — | 多个"待补"项实际已实现，多个"已完成"项描述不准确 |
| `superpowers/specs/2026-05-21-story-lifecycle-v2-design.md` | spec | **idea_backlog** | — | v2 服务端部署 |
| `superpowers/specs/2026-05-23-headless-e2e-test-tool-design.md` | spec | **implemented** | `tests/e2e/` | Headless E2E 测试工具 |
| `superpowers/specs/2026-05-23-quality-flywheel-seed-pipeline-design.md` | spec | **implemented** | `seed_quality.py` | Quality Flywheel Seed Pipeline |
| `e2e-test.md` | design | **partial** | `tests/e2e/` (5 scenarios) | 扩展 scenarios 未实现 |

## 6. 已实现设计归档建议

以下设计文档对应代码已完整实现，建议在文档头部标注 `status: implemented`：

- `design-smart-orchestrator.md`
- `design-sub-story.md`
- `design-story-source-integration.md`（标注 P0 已实现，P1 部分实现）
- `story-quality-flywheel-design.md`
- `story-observability-mvp-design.md`
- `design-llm-semantic-extraction.md`
- `design-review-gate-observability-and-control.md`
- `design-terminal-entry-lifecycle.md`
- `design-tui-entry-state-machine.md`
- `design-foreground-zellij-execution.md`
- `design-board-diagnostics-panel.md`
- `design-headless-zellij-feedback-abstraction.md`
- `design-swebench-runner.md`

## 7. 未实现设计 Backlog 建议

以下设计文档无对应代码实现，建议移到 `docs/backlog/` 或标注 `status: planned (v0.8+)`：

- `design-workspace-onboarding-project-profile.md` → v0.8+
- `idea-project-intelligence-pipeline.md` → v0.8+
- `idea-storyos-project-intelligence-control-plane.md` → v0.8+
- `idea-dual-flywheel-domain-and-engine.md` → v0.9+
- `idea-ttyd-server-side-web-terminal.md` → v2
- `design-swebench-gradient-data-flywheel.md` → v0.7+
- `idea-swebench-data-flywheel.md` → v0.7+
- `idea-stage-handoff-package.md` → v0.7+

## 8. 最小可靠闭环差距分析

### 8.1 可安装

**已具备**：
- `pip install story-lifecycle` 可用（`pyproject.toml` 完整）
- `pip install -e .` editable install
- `hatchling` 构建，`packages` + `artifacts` 声明完整（`pyproject.toml:60-62`）
- `console_scripts` 注册 `story` 命令（`pyproject.toml:53`）

**缺什么**：
- Windows PATH / `story.exe` 风险：pip 安装后在 Windows 上生成 `story.exe`，若 PATH 污染可能导致版本冲突。`upgrade` 命令在 Windows 上写 bat 脚本以规避 exe 锁定（`main.py:280-301`），但这只是 workaround
- 无 CI/CD pipeline（`.github/workflows/` 不存在或未验证）
- 无版本升级迁移逻辑（DB schema 通过 ALTER TABLE IF NOT EXISTS 做增量迁移，但无版本号追踪）

**风险**：Windows 上 pip upgrade 时 exe 锁定导致安装失败；无 CI 无法验证跨平台可安装性

**v0.6 必须做**：
- 验证 `pip install` 在 Windows/Linux/macOS 三平台可用
- `story upgrade` 在 Windows 上的 exe 替换逻辑需要端到端验证

### 8.2 可启动

**已具备**：
- `story setup`：交互式 LLM 配置向导（`setup.py`）
- `story doctor`：系统依赖检查（`doctor.py`）——检测 zellij, git, python, pip
- `story serve`：启动 FastAPI 服务器（port 8180）
- `story`（默认）：首次运行检查 → setup（如需）→ 启动 TUI
- 7 个命令绕过 API key 检查（`main.py:122-129`）

**缺什么**：
- `story serve` 在 `is_configured()` 返回 False 时仍然拒绝启动（`main.py:402-409`），与 bypass 列表矛盾——serve 在 bypass 列表中但内部自己又检查了一次
- setup/doctor 仅检验命令注册（CLI test），未测试实际执行逻辑
- 无 `story --version` 命令

**风险**：`story serve` 的配置检查逻辑有两层（bypass + 内部），容易混淆

**v0.6 必须做**：
- 统一 serve 的配置检查逻辑（要么信任 bypass，要么移除 bypass）
- 增加 `story --version`

### 8.3 可跑通

**已具备**：
- 最小 story 生命周期完整：create → plan → execute → poll → review → router → advance/retry/fail
- Done 文件协议完整：写入路径、解析（含 markdown 容错）、快照、消费（删除）、格式错误处理
- 无头模式合成 done（`tools/base.py:213-261`）
- profiles（`minimal.yaml`, `swebench.yaml`）和 prompts 完整
- 5 个 E2E scenarios 全部通过

**缺什么**：
- `affected_repos` 仅存在于 prompt 文本中，系统级无验证（见第 8.4 节 workspace 约束分析）
- 无 `story swebench analyze` 命令
- 对抗循环 CLI 化（mode: cli）仅在 `review_design` 阶段使用 codex CLI（CHANGELOG v0.5.36），其余仍为 API 模式

**风险**：AI agent 不遵守 prompt 中的 git 约束时（如修改非 affected_repos 文件），系统无感知

**v0.6 必须做**：
- `affected_repos` 从设计阶段 done JSON 提取并注入 context_json
- `implement` 阶段启动前验证 `affected_repos` 存在

### 8.4 可卡住

**已具备**：
- `wait_confirm` → 状态 `"paused"`（`nodes.py:1763-1831`）
- `fail_node` → 状态 `"blocked"`（`nodes.py:1745-1757`）
- `waiting_subtasks` → 父 story 等待子 story
- 工作区锁：per-workspace `threading.Lock`（`graph.py:53-74`），支持 epoch 校验防陈旧线程误释放
- TUI 看门狗每 3 秒检测（`tui.py:1990-2098`）：done 文件到达、CLI 无 done 退出、子 story 依赖解除、父 story 恢复
- 卡住原因在 Story Card 和诊断面板均可见（`tui.py:278-299`, `debug_packet.py:66-162`）
- 9 种 stuck_reason 检测（`debug_packet.py:66-162`）：missing_config / story_blocked / waiting_subtasks / gate_blocked / done_malformed / stage_timeout / cli_exited_without_done / done_waiting / loop_exhausted

**缺什么**：
- `threading.Lock` 是进程内锁，不能防跨进程并发写同一 workspace
- watch dog 未按设计 doc 拆分为多个独立 watcher
- 无 `filelock`（已作为依赖安装但 workspace lock 未使用它）
- `session_dead` 检测依赖 zellij `list-sessions`，在 session 名冲突时可能误判

**风险**：两个 `story serve` 进程同时写同一 workspace 会绕过锁；Windows 上 zellij session 管理可能有 edge case

**v0.6 必须做**：
- 将 workspace lock 升级为 `filelock` 或至少加 DB-level advisory lock
- 补充 `session_dead` 的确定性检测（不仅依赖 zellij list-sessions）

### 8.5 可排查

**已具备**：
- `event_log` 表 + 5 种事件类型（route_decision, node_error, prompt_context, dod_check, gate_decision）
- `stage_log` + `gate_result` + `llm_trace` 表
- `/api/story/{key}/debug` API endpoint（`api.py:307`）返回 `build_debug_response()`
- `build_debug_packet()`（`debug_packet.py:170`）：15 个 top-level 字段的稳定诊断结构
- `create_story_diagnostics_bundle()`（`diagnostics.py:22`）：生成含 summary.md + debug_packet.json + events + terminal output + git info 的 ZIP
- `create_global_diagnostics_bundle()`（`diagnostics.py:143`）
- 敏感数据脱敏（`debug_packet.py:341-393`）：14 个正则模式 + 递归字典 key 匹配
- TUI 右侧诊断面板（`tui.py:1286-1434`）：stage activity + stuck reason + session info + loop status + recent events + copilot
- TUI 快捷键 `[o]` 切换诊断面板、`[p]` 打包 story 诊断、`[P]` 打包全局诊断、`[y]` 问 copilot
- Story card 直接显示卡住原因提示
- CLI `story diagnostics <KEY>` / `--global` 命令

**缺什么**：
- `build_debug_packet()` 中 `terminal_output.line_count` 和 `.truncated` 硬编码为 0 和 False（`debug_packet.py:316-317`）
- 终端最近输出仅在诊断 bundle 中可用（需要 zellij `dump-screen`），不在 debug packet 或 debug API 中
- 无 `graph_error.log` / `planner_error.log` 的自动轮转

**风险**：无 zellij 时终端输出完全不可用；debug packet 报告中 terminal 信息为占位值

**v0.6 必须做**：
- 修复 `terminal_output` 字段的硬编码占位值
- 无 zellij 时提供替代的终端输出捕获方案（如 subprocess stdout 重定向到文件）

## 9. v0.6 Reliability Loop 范围

v0.6 从宏大的 Control Plane Foundation 收敛为 **Reliability Loop**。只包含以下 8 项：

1. **CLI 稳定**：所有命令可执行、`--help` 输出正确、`story --version` 可用
2. **setup / doctor 稳定**：增加 setup/doctor 的测试覆盖，验证实际执行逻辑
3. **最小 story 跑通**：E2E scenarios 全部通过，done 协议在所有阶段行为一致
4. **Done 协议稳定**：路径、schema 文档、格式错误处理、合成 done 的 behavior 规范化
5. **Workspace 基础校验**：`affected_repos` 从提示约束升级为系统级校验（design done → context_json → implement 启动前验证）
6. **Diagnostics / Debug Packet / Bundle**：修复 terminal_output 占位值、确保 bundle 在无 zellij 下也能生成
7. **Board 右侧诊断面板**：已完成，需回归验证
8. **最小回归测试**：确保 `pytest` 431 个测试全部通过，补充 setup/doctor/workspace-lock 测试

## 10. v0.6 非目标（明确推迟）

| 推迟项 | 原计划版本 | 推迟理由 |
|--------|-----------|----------|
| StoryOS branding 重构 | v0.6+ | 非功能需求 |
| Project Intelligence | v0.8 | 设计完整但无实现计划 |
| Workspace Onboarding | v0.8 | `project_profile.py` 等文件均不存在 |
| Project Intelligence Probe | v0.8 | `project_probe.py` 不存在 |
| Test Source 抽象 | v0.8 | 无代码 |
| 双飞轮治理 (Domain + Engine) | v0.9 | 无代码 |
| Meta-Planner + Plan-stage Decomposition | v0.9 | 无代码 |
| Dynamic Stage Graph + Graph Patch | v1.0 | 当前 10 节点固定图覆盖所有场景 |
| Policy Engine 完整版 (L0-L5) | v1.0 | 骨架已实现，够了 |
| Copilot 主动建议 | — | 已实现，但非 v0.6 重点 |
| SWE-bench analyze / gradient / preference | v0.7 | 基础 pipeline 已够用 |
| 多模型并行对比 | v0.8 | 设计仅文档 |
| TAPD HTML→Markdown 高质量转换 | v0.8 | 当前基础实现够用 |
| 架构审查门禁 | v0.6 | 设计完整但无 `architecture_triggers.py` |
| 阶段交接包 | v0.6 | 设计完整但无代码 |
| Complexity Classifier | v0.6 | 无代码 |
| Working Memory + Budget Ledger | v0.7 | 无代码 |

## 11. v0.6 验收标准

```text
可安装：pip install story-lifecycle 在三平台成功
可启动：story setup / story doctor / story serve 均可独立启动，无需预配置
可跑通：story demo 成功（0 依赖），5 个 E2E scenarios 全部通过
可卡住：paused / blocked / waiting_subtasks 三种卡住状态均能在 TUI 看到原因
可排查：story diagnostics <KEY> 生成有效 bundle，TUI [o] 面板可用
```

## 12. 推荐执行顺序

| 优先级 | 任务 | 预计工作量 |
|--------|------|-----------|
| P0 | 修复 `terminal_output` 硬编码占位值（`debug_packet.py:316-317`） | 小 |
| P0 | 补充 `story --version` 命令 | 小 |
| P0 | `affected_repos` 从 design done JSON 提取到 context_json，implement 启动前验证 | 中 |
| P0 | Workspace lock 升级：`threading.Lock` → `filelock.FileLock` | 中 |
| P0 | 补充 setup/doctor/workspace-lock 测试 | 中 |
| P1 | 统一 `story serve` 配置检查逻辑 | 小 |
| P1 | Done 协议 schema 文档化（最小 JSON schema 或字段说明） | 小 |
| P1 | 无 zellij 时的终端输出替代方案（stdout 重定向到文件） | 中 |
| P1 | E2E scenarios 扩展到 10+（补充 done malformed / timeout / gate blocked / CLI exit without done） | 中 |
| P2 | CLI help 文本与 README 对齐 | 小 |
| P2 | roadmap 文档更新：标注实际已完成项 | 中 |
| P2 | 设计文档状态标注 | 中 |

## 13. 风险与注意事项

1. **`threading.Lock` 不能跨进程**：如果用户同时启动两个 `story serve` 进程指向同一 workspace，workspace lock 会失效。升级到 `filelock` 需处理锁文件清理和跨平台路径。

2. **Windows Zellij 依赖**：4 个 Windows-only 测试依赖 zellij behavior。若 zellij 在 Windows 上的行为变化，可能导致 silent regression。

3. **测试全部通过但覆盖盲区大**：431 tests pass / 0 fail 看起来很好，但 setup、doctor、TUI app 集成、workspace-lock 内部逻辑均无直接测试。

4. **设计文档与代码的双向不一致**：roadmap 声称"待补"的能力已实现（Policy Engine、Copilot、Diagnostics Panel），而声称"已完成"的能力实际上只是提示约束（affected_repos、分支门）。

5. **`affected_repos` 是系统级约束的基础**：Workspace onboarding、Project Profile、Resource Lock 都依赖系统知道"这个 story 涉及哪些 repo"。当前全依赖 AI 自主遵守 prompt 指令。

6. **Done 文件无 schema 验证**：任何合法 JSON 都会被接受为 done 输出，可能导致下游节点拿到非预期数据。

## 14. 后续 Roadmap 调整建议

1. **v0.6 聚焦可靠性**：按本文档第 12 节执行顺序，不新增功能。
2. **v0.7 合并原 v0.6+v0.7 剩余项**：`affected_repos` 系统级约束 + Working Memory + Budget Ledger + SWE-bench analyze（无 gradient/preference）
3. **v0.8 推迟到 v0.7 之后评估**：Project Intelligence / Workspace Onboarding 在 v0.7 稳定后再启动
4. **v0.9+v1.0 保持远景**：双飞轮、Stage Graph、Guarded Apply 等保持为远景，不做近期承诺
5. **文档整理**：将 `idea_*` 文档移到 `docs/ideas/`，将已实现的设计文档标注 status，将 obsolete 文档归档

---

*本文档基于 2026-05-27 代码冻结点的深度扫描生成，包含对 60+ 源文件和 60+ 设计文档的对账。所有"已实现"判断均有具体文件路径和行号支撑。*
