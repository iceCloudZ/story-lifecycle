# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.5.23] - 2026-05-27

### Added
- P1 Ask Copilot：诊断面板中按 `y` 打开对话，输入问题后调 LLM 分析 Debug Packet，返回结构化建议（不自动执行）

### Fixed
- 诊断面板渲染异常导致 `r` 键无响应：`_copilot_*` 属性未初始化 + `_render()` 中异常未捕获
- `story` import 时 `PackageNotFoundError` 崩溃：版本号查找改为 try/except fallback
- Board 启动前检查 LLM 配置，缺失时主动引导 setup（而非静默进入显示 `missing_config`）
- 启动时静默清理 `~` 前缀损坏 pip 安装片，消除 `WARNING: Ignoring invalid distribution` 警告

## [0.5.22] - 2026-05-27

### Fixed
- TUI 启动时 `on_resize` 先于 `on_mount` 触发导致 `AttributeError: '_show_diagnostics'` 闪退
- `story upgrade` Windows bat 脚本闪退：改用 `start /min` 最小化、`findstr` 替代 `find`、ascii 编码、初始延时

## [0.5.21] - 2026-05-27

### Added
- Board 右侧常驻诊断面板：展示 Story 卡住原因、会话状态、最近事件和诊断动作
- `story diagnostics STORY_KEY` 命令：生成 Story 级诊断包（debug_packet、summary、events、stage_logs、gate_results、配置、环境、done 快照、terminal 输出、git 状态）
- `story diagnostics --global` 命令：生成全局系统诊断包（环境、配置、CLI 帮助输出、包元数据、错误日志尾部）
- Debug Packet 模块：`build_debug_packet()` 统一诊断数据 schema，供 TUI 和 CLI 共享
- 确定性 Stuck Reason 规则引擎：10 种阻塞信号检测（missing_config、story_blocked、gate_blocked、done_malformed、done_waiting、cli_exited_without_done、stage_timeout、loop_exhausted 等）
- 脱敏工具 `redact_text()` / `redact_mapping()`：自动遮盖 API key、token、password 等敏感信息
- `get_stage_logs()` / `get_gate_results()` DB 查询助手，JOIN story 表按 story_key 查询
- 窄屏自适应：终端宽度 < 120 列自动隐藏诊断面板
- TUI 快捷键 `o` 切换诊断面板、`p` 打包 Story 诊断、`P` 打包全局诊断
- StoryOS 定位理念文档
- Board Copilot 诊断设计文档

### Changed
- `.gitignore` 新增 `.worktrees/` 排除项

### Fixed
- `stage_timeout` 卡住原因分支死代码：与 `done_waiting` 条件相同导致永远不可达，已通过 stage_log 时间戳计算实际耗时修复

## [0.5.20] - 2026-05-27

### Changed
- 测试隔离改进：monkeypatch load_profile 强制使用 package 内置 profile，防 xdist 污染
- xdist 并行暂回退，待测试隔离彻底解决后再启用

## [0.5.19] - 2026-05-27

### Changed
- CI 测试并行化：单元测试 xdist 4 worker，e2e 串行
- 测试隔离改为 monkeypatch load_profile 强制使用 package 内置 profile

## [0.5.18] - 2026-05-27

### Changed
- CI 单元测试 xdist 并行，e2e 测试串行跑

## [0.5.17] - 2026-05-27

### Changed
- CI 测试并行化：pytest-xdist 4 worker + loadfile 调度
- conftest 自动隔离 cwd，防止测试间 profile 加载污染

## [0.5.16] - 2026-05-27

### Changed
- CI 流水线优化：lint/test/build-check 并行、pip 缓存、pytest-xdist 4 worker 并行测试
- Release 流程 TestPyPI 与 PyPI 并行发布

## [0.5.15] - 2026-05-27

### Fixed
- `story upgrade` Windows 升级改为 bat 脚本等待进程退出后再执行 pip，解决 exe 文件锁导致安装失败
- 抽取 `_run_upgrade()` 函数，平台特殊逻辑与业务逻辑分离

## [0.5.14] - 2026-05-27

### Fixed
- `story upgrade` 在 Windows 上因 story.exe 被锁定而失败，改为后台子进程升级后自动退出
- 修复 `is_workspace_locked` mock 缺少 `exclude_story` 参数导致 CI 失败

## [0.5.13] - 2026-05-27

### Fixed
- `execution_count` 超过 `max_retries` 时暂停等人工决定，不再无限 retry 或直接 fail（影响 `plan_stage` 入口和 `router_node` 两条路径）

## [0.5.12] - 2026-05-27

### Fixed
- `story upgrade` 升级前自动清理 site-packages 中 `~` 开头的损坏安装残片，避免 pip 升级失败

## [0.5.11] - 2026-05-27

### Fixed
- TUI 按 `r` 恢复时，同一 story 持有的 workspace lock 不再误报"被其他 story 占用"
- `get_compiled_graph()` 缓存 compiled graph 实例，避免重复创建导致 LangGraph executor shutdown 崩溃

## [0.5.10] - 2026-05-27

### Fixed
- `story setup` 完成提示改为 `story`（启动 board），而非 `story serve`（v2 功能）

## [0.5.9] - 2026-05-27

### Fixed
- 修复 `story doctor` 作为子命令组无法直接运行的问题
- `story doctor` 输出提示只需安装一个 AI CLI 工具即可运行，减少首次安装困惑

## [0.5.8] - 2026-05-27

### Added
- 注册 `story setup` 子命令，安装后可直接进入 LLM 配置向导
- 注册 `story serve` 子命令，保留 `story --serve` 的兼容入口
- 增加 CLI 命令注册回归测试，覆盖 `setup` / `serve` / `doctor`
- 新增多层验证体系设计文档：`docs/design-three-layer-validation.md`

### Changed
- 更新 Orchestrator Agent idea 文档，补充 Policy Engine、Resource Locks、异步 Blackboard、Shadow Mode 与上下文分片落地细节
- 更新 v0.5 到 v1.0 路线图，将 Orchestrator Agent 各阶段设计文件映射到版本计划

### Fixed
- 修复 pip 安装后 `story setup` 找不到命令的问题
- 修复用户手动加入 PATH 后 `story setup` / `story serve` 被误判为用法不对的问题
- `story setup` / `story serve` 不再被启动前 API key 检查拦截

## [0.5.7] - 2026-05-27

### Fixed
- 任务书增加完成信号（done 文件路径 + JSON 格式示例）
- 任务书增加事实/假设边界约束：未找到的表名/字段名标记为假设
- 任务书 headless 兼容：用 open_questions 替代"与产品经理确认"
- 任务书配置语义清晰：执行工具/执行模型，移除编排层内部字段
- 移除任务书中的 skill 指令（skill 通过 tool_args 传递，非所有 AI CLI 支持）

## [0.5.6] - 2026-05-26

### Added
- `story upgrade` 命令 — 一键升级到最新版本
- Orchestrator Agent idea 文档（`docs/idea-orchestrator-agent.md`）

### Changed
- profiles/ 和 prompts/ 打包进 wheel，`pip install` 后不再报 profile not found
- `load_profile()` / `_render_prompt()` 改用 `importlib.resources` 加载内置资源
- 无 LLM API key 时自动引导 setup 向导（排除 doctor/demo/upgrade 命令）
- `story demo` 输出精简：按 stage 分组，只显示 plan/execute/review 关键步骤

### Fixed
- `story demo` planner mock 不覆盖 evaluator_loop 内部 import，导致调真实 LLM
- demo header 硬编码 "design → implement → test"，实际是 review

## [0.5.4] - 2026-05-26

### Changed
- 默认 DeepSeek 模型更新为 deepseek-v4-pro / deepseek-v4-flash
- `story doctor` 新增 textual 依赖检查

## [0.5.3] - 2026-05-26

### Fixed
- retag 修复发布流程

## [0.5.2] - 2026-05-26

### Changed
- **编排 LLM 必填化** — 移除所有 rule-based 回退路径，LLM 不再是可选配置
- `story serve` 无 API key 时直接报错退出，不再静默启动
- `planner.is_available()` / `router.llm_is_available()` 已移除
- 移除 static_fallback 计划生成、正则 fallback（Bug 上下文提取、模式复发检测、审查解析）
- Planner 运行时异常不再降级为静态计划，改为 block story 进入 `wait_confirm`

### Removed
- `_planner_policy()` — profile 中的 `planner.required` / `planner.static_fallback` 配置项
- `_fallback_result()` / `_unavailable_result()` — semantic.py 回退结果构造函数
- `_regex_extract_bug_context()` / `_SECTION_PATTERNS` — Bug 上下文正则提取
- `_keyword_match_pattern()` — 模式复发关键词匹配
- `_marker_summarize_review()` — 审查摘要标记解析
- `_parse_bullet_review()` — 审查 bullet-list 解析
- `log_loop_fallback()` — 对抗循环回退日志
- `_match_pattern()` — nodes.py 关键词匹配

### Fixed
- 43 服务器部署验证：swebench prepare → export 全链路通过，LLM 必填行为正确

## [0.5.1] - 2026-05-26

### Changed
- 移除 tmux 回退，Zellij 为唯一复用器

## [0.5.0] - 2026-05-26

### Added
- SWE-bench Runner：prepare / solve / export / eval / summarize / run 全命令
- 前台 Zellij 执行模式
- TUI 12 状态入口决策机
- Review gate 可观测性：GateDecision、review_round_count、gate report
- 对抗循环：run_plan_loop / run_code_review_loop / detect_no_progress
- E2E runner 和场景 DSL
- LLM 语义提取层：Bug 上下文、模式复发检测、审查摘要、恢复建议
- Headless 执行模式（claude -p）
- Tool Registry：stage_tool / skill_tool
- trajectory_score 路由
- Condenser（上下文压缩）

## [0.4.0] - 2026-05-23

### Added
- `story demo` command — zero-dependency simulated lifecycle (design→implement→test) in 0.2s
- ShellAdapter — config-driven adapter for any AI CLI tool via `~/.story-lifecycle/adapters.yaml`
- CodexAdapter — built-in adapter for OpenAI Codex CLI (`codex`)
- Quality flywheel: finding table, lifecycle queries, quality packet injection into prompts
- Story Source Integration: TAPD polling, inbox, PRD generation, bug-parent resolution

### Fixed
- Finding ID collision when multiple findings created in same second (uuid4 instead of timestamp)
- StoryState field propagation (`_next_action`, `_pending_sub_keys`, `_router_decision`) in LangGraph
- Graph termination: advance→END when completed, plan→END when waiting_subtasks
- Sub-story infinite loop on delegation

## [0.3.0] - 2026-05-22

### Added
- Smart Orchestrator: LLM routing with rule-based fallback and provider rotation
- Headless E2E test framework (FakeStageTool + Scenario DSL + YAML scenarios)
- Sub-story splitting and delegation with dependency tracking
- Review loop with retry fatigue and trajectory score
- Cross-AI Code Review: structured findings with severity/location/description

### Fixed
- `fail_node` crash when `last_error` is None
- E2E test MagicMock return type rejected by LangGraph

## [0.2.0] - 2026-05-21

### Added
- LangGraph StateGraph orchestration engine
- Textual TUI board with keybindings for story management
- Profile system (YAML stage sequences, minimal profile)
- Handshake protocol (`.story-done/{stage}.json`)
- CLI: `story serve`, `story doctor`, `story setup`

## [0.1.0] - 2026-05-20

### Added
- Initial project scaffold
- SQLite DB with zero-ORM raw SQL
- Claude Code adapter
