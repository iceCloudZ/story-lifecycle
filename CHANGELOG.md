# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.5.54] - 2026-06-02

### Fixed
- 修复 zellij 启动语法错误：`zellij -s name -- claude` 无效，改为 `start cmd /k "zellij -s name"` 后通过 PowerShell SendKeys 注入 claude 命令和 prompt

## [0.5.53] - 2026-06-02

### Fixed
- Zellij 自动创建新 session 并注入 prompt（不再要求在已有 session 内运行）
- Windows 弹窗自动粘贴 prompt 到 claude 窗口（PowerShell SendKeys Ctrl+V + 回车）
- 有 zellij 时直接使用，不再检查 `$ZELLIJ` 环境变量

## [0.5.52] - 2026-06-02

### Fixed
- 交互模式下不再误跑校验（AI 还没工作就报"6 个问题"并显示"初始化完成"）
- Windows 弹窗启动修复：`start` 命令需要 `shell=True`
- zellij `run` 只在已有 session 内使用（检查 `$ZELLIJ` 环境变量），否则走 Windows 弹窗分支

## [0.5.51] - 2026-06-02

### Changed
- Bootstrap prompt 改为 9 步交互式流程：项目概况 → 并行扫描规划 → 数据库 → 前端 → 测试 → CI/CD → 逐域扫描 → 汇总 → 并行写入+健康评估
- 识别数据库、前端、测试、CI/CD 四个独立维度可并行扫描
- 写入知识包产物和健康评估并行执行，评估结果落地到 `reviews/health-assessment.md`
- AI 每步主动提问确认，不再一次性埋头执行

### Added
- 健康评估报告：测试覆盖缺口、代码坏味道（重复/硬编码/废弃代码/依赖风险）、架构建议、红黄绿优先级排序
- 自动检测可用 AI CLI（claude/codex），多 CLI 时展示列表供选择
- `init-knowledge` 默认交互模式（zellij/新终端），`--headless` 保留给 CI

## [0.5.50] - 2026-06-01

### Changed
- `story project init-knowledge` 自动检测环境中可用的 AI CLI（claude/codex），不再硬编码 claude
- 检测到多个 CLI 时显示列表，默认使用第一个

## [0.5.49] - 2026-06-01

### Changed
- `story project init-knowledge` 默认改为交互模式：启动 zellij 会话/新终端窗口运行 Claude CLI，注入 prompt 让 AI 实时交互式生成知识包
- 保留 `--headless` 标志用于 CI 场景的非交互执行

## [0.5.48] - 2026-06-01

### Added
- 项目智能初始化（Project Intelligence Bootstrap）：`story project init-knowledge` 一键生成 `.story/knowledge/` 知识包，通过 CLI headless 调用 AI 扫描代码库生成 manifest、product、search-catalog、graph、scenarios、indexes
- `story project sync-knowledge` 检测知识包是否过期（基于 Git commit 对比）
- 知识包产物校验器：自动检查 manifest/product/search-catalog/graph 文件存在性和格式
- 结构化搜索工具 `search_knowledge()`：支持按类型/关键词/limit 搜索知识包文件，避免 LLM 直接拼接 shell
- Bootstrap prompt 模板：指导 AI 按 scan profile（java-spring-microservice/frontend-react-umi/python-service）扫描并生成知识包
- 创建 story 时自动检测知识包是否存在，缺失时给出 `story project init-knowledge` 提示
- 知识包模板文件：manifest.yaml、product.yaml、search-catalog.md、graph-schema.json、scenario.md、index.md

## [0.5.47] - 2026-05-29

### Changed
- 重构 entry.py 状态机：砍掉 StageEntryState（13 状态枚举）和 _ACTION_TABLE（26 条二维决策表），改为 decide_enter_action / decide_resume_action 优先级决策链
- TUI 动作菜单只看 story status 驱动，graph/session 状态下沉到具体动作执行时检查（参考 Temporal/GitHub Actions 设计模式）

### Removed
- 移除 StageEntryState 枚举、resolve_stage_state()、decide_action()、_ACTION_TABLE

## [0.5.46] - 2026-05-29

### Changed
- TUI 按键从 27 个精简到 7 个（↑↓ Enter n o ? q），参考 lazydocker 设计模式
- Enter 键改为弹出上下文动作菜单，根据 story 状态动态显示可用操作（进终端/开始/接受风险/跳过/终止/删除/详情）
- 底栏提示精简为 6 个核心按键

### Removed
- 移除直接快捷键 e/d/s/f/r/R/F5/x/N/a/A/c/D/S/i/y/1/2/3/p/P，功能全部整合到 Enter 动作菜单
- 移除 doctor/setup/package-diagnostics TUI 快捷键，改用 CLI 命令

## [0.5.45] - 2026-05-29

### Fixed
- 右侧面板用 Textual Tabs/ContentSwitcher 崩溃：改为 `o` 键循环切换（隐藏 → 诊断 → Copilot → 隐藏），单面板渲染，不再依赖 Textual 复杂控件
- Copilot 查询完成后自动切到 Copilot 视图，按 `y` 也自动切换

## [0.5.44] - 2026-05-29

### Fixed
- 按 `A` accept risk 后 story 被误 abort：graph 恢复后未跳过 gate 导致 plan→review→wait_confirm 死循环，现通过 `gate_override` 标记直接跳到 advance
- `failed`/`aborted` 状态的 story 从 TUI 完全消失：`list_completed_stories` 现在包含所有终态，底部区域用 ✓/✗/⊘ 图标区分

## [0.5.43] - 2026-05-29

### Fixed
- 按 `r` 启动后立刻按 `e` 报错"没有运行中的 session"：新增 `STARTING` 状态区分 graph 已启动但 session 尚未创建的中间态，给出友好提示
- 按 `r` 启动时增加 toast 通知"session 创建中"，让用户知道操作已生效

## [0.5.42] - 2026-05-29

### Fixed
- Claude adapter 不再传 `--model` 参数，避免 planner LLM 返回的模型名（如 `deepseek-chat`）导致 Claude Code 报 400 错误

## [0.5.41] - 2026-05-29

### Added
- TUI 右侧面板拆分为「诊断」/「Copilot」双 Tab，Copilot 回答独立展示不再被诊断信息遮挡
- Copilot 查询完成后自动切换到 Copilot Tab，回答即时可见
- 新增 `_switch_to_copilot_tab()` 自动跳转和 `_build_copilot_lines()` 独立渲染

### Fixed
- Copilot 建议渲染在 diagnostics 面板最底部，被 stuck reason / evaluator loop / 最近事件等挤到视口外完全看不到
- `_add_loop_status` 在同一个面板中被重复调用两次，进一步膨胀内容
- 切换 story 时不再无条件 reset Copilot 状态，改为仅在选中 story 实际变化时 reset

## [0.5.40] - 2026-05-28

### Fixed
- E2E 测试无限循环：`planner` 和 `load_profile` 的 mock.patch 目标未适配 `nodes/` 子包拆分，mock 未生效导致真实 planner 调用失败 → `wait_confirm → plan_stage` 死循环
- `test_demo`、`test_review_gate`、`test_evaluator_loop`、`test_smart_orchestrator` 中同类 patch 路径问题一并修复

## [0.5.39] - 2026-05-28

### Fixed
- CI 在 macOS/Windows 上失败：`terminal/` 缺少 `__init__.py`，导致 `from ...terminal import ttyd` 导入失败
- `nodes/__init__.py` 缺少 `ttyd`、`notify`、`planner`、`router` 等重导出
- `nodes/__init__.py` 缺少 `interrupt`、`GraphInterrupt` 重导出
- 测试 `mock.patch` 目标未适配 `nodes/` 子包拆分（`load_profile`、`planner` 引用路径）
- monkeypatching 未适配 `nodes/` 子包结构

### Changed
- 重构：提取 `CopilotState` dataclass，从分散的 `_copilot_*` 属性集中管理

### Removed
- 删除废弃的 `CopilotDialog` 类（已被 inline input 替代）

## [0.5.38] - 2026-05-27

### Changed
- `orchestrator/nodes.py` (2115 行) 拆分为 `orchestrator/nodes/` 子包：`state.py`、`profile_loader.py`、`json_helpers.py`、`stage_resolver.py`、`routing.py`、`subtask_delegate.py`、`knowledge.py`、`prompt_renderer.py`、`graph_nodes.py` 十个文件，`__init__.py` 重导出保持向后兼容
- `cli/tui.py` (2644 行) 拆分为 `cli/tui/` 包，为后续 widget 拆分做准备

### Added
- `orchestrator/decision_chain.py`：Router 8 级决策优先级显式文档 + Router/Policy/Gate 三者权责说明
- `docs/architecture-for-java-developers.md`：Java 开发者视角的架构导航指南

## [0.5.37] - 2026-05-27

### Added
- 阶段条显示对抗循环标记 `⟳`：`design⟳` / `implement⟳`
- `review_design` 阶段使用 `codex` CLI

## [0.5.36] - 2026-05-27

### Added
- Profile v3：review 变为可见阶段，阶段条显示 `design → review_design → implement → review`
- `review_design` 阶段：设计审查，使用 CLI 执行（Claude Code + haiku），含独立 prompt
- `review` 阶段走 CLI 执行，不再只是内部 LLM API 调用
- stage 支持挂载 skill：profile 中配 `skill: "/xxx"`，prompt 自动注入 Skill 工具调用指令

### Fixed
- `_build_plan_executor_prompt` 丢弃 skill 指令：改为从 metadata 提取并注入到最终 prompt
- skill 指令格式从斜杠命令改为 "使用 Skill 工具调用"，适配非交互模式

### Changed
- 诊断面板按 stage 显示不同活动描述，对抗循环状态可见
- Session 退出不再写 `last_error` 或 block story，保持 active 等待重新进入

## [0.5.35] - 2026-05-27

### Changed
- 诊断面板按 stage 显示不同活动描述（design/implement/review/test 各有对应文案）
- 对抗循环状态在诊断面板可见：显示循环类型、轮次、决策
- Session/终端退出不再写 `last_error` 或 block story，保持 active 等待重新进入
- 措辞优化：去掉 `crash`/`dead`/`blocked` 等负面用语

## [0.5.34] - 2026-05-27

### Added
- 规划面板显示 LLM 实时活动：正在生成计划 → 评估第N轮 → 计划完成，不再只显示"正在规划中..."

## [0.5.33] - 2026-05-27

### Fixed
- Copilot 输入框在无 Story 时提交导致 `IndexError` 崩溃

## [0.5.32] - 2026-05-27

### Added
- Config 自动备份保护：每次 CLI 启动备份 `config.yaml` → `config.yaml.bak`，丢失时自动恢复

## [0.5.31] - 2026-05-27

### Added
- P3 Policy Engine：`orchestrator/policy_engine.py` 实现 DecisionEnvelope + AutonomyLevel（shadow/confirm/apply/forbidden）+ 拒绝追踪（3 次连续拒绝后降级 forbidden）
- Copilot 操作结果写入 `copilot_action_confirmed` / `copilot_action_rejected` / `copilot_action_applied` 事件
- TUI 诊断面板展示 Policy 决策：`[自动]` / `[需确认]` / `[禁止]` 标签 + policy 理由行 + 拒绝次数提示
- forbidden 级别操作拒绝执行并 notify 提示

## [0.5.30] - 2026-05-27

### Changed
- Copilot LLM 调用移除 `max_tokens` 限制，避免长回复被截断

## [0.5.28] - 2026-05-27

### Fixed
- Copilot 输入框发送无响应：`run_worker` 传参错误导致 `WorkerError`，改为 lambda 延迟调用
- `story upgrade` bat 文件找不到：确保目录存在 + `start` 替代 `cmd /c`

## [0.5.27] - 2026-05-27

### Changed
- Copilot 弹窗改为诊断面板下方内嵌输入框：按 `y` 聚焦输入，Enter 发送，结果直接渲染在面板中

## [0.5.26] - 2026-05-27

### Fixed
- Story card 直接显示卡住原因提示（如 `review gate — 按 r 重试审查`），无需打开诊断面板
- Copilot `call_from_thread` 在主线程执行时抛 `RuntimeError`，改为检查线程后再调度

## [0.5.25] - 2026-05-27

### Added
- P2 SuggestedAction TUI 交互层：诊断面板渲染 Copilot 建议操作（含风险着色），数字键 1-3 触发执行，workflow_state / local_config 操作弹确认框，所有确认/拒绝写入 `copilot_action_confirmed` / `copilot_action_rejected` 事件

## [0.5.24] - 2026-05-27

### Changed
- P2 SuggestedAction：Copilot 新增 8 种结构化操作建议（read_only / local_config / workflow_state），含风险等级和确认机制
- `story upgrade` bat 脚本写入已知路径 `~/.story-lifecycle/upgrade.bat`，修复 tempfile 路径找不到的问题

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
