# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
