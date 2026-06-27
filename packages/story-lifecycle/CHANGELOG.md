# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.12.0] - 2026-06-27

### Added
- **Monorepo 迁移完成（M1–M6）** — `story-lifecycle` 与 `agent-transcript-miner` 合并到 `packages/story-lifecycle` / `packages/story-miner`，新增共享 `packages/knowledge` 统一知识层（统一 schema、INDEX、失败知识合并）。
- **定时扫描兜底（I1）** — `miner/store.py` 支持 `--since-days` 增量入库；新增 `packages/story-miner/scripts/refresh.sh`，每日增量 + 每周全量并重算 playbook/failure。
- **Anchor-first story↔session 绑定（I2）** — `story-lifecycle` 在 `inject_prompt` 时写 `anchors.jsonl`，`miner/link.py` 优先读锚点精确绑定，去掉旧 uniq-window 宽窗兜底；hc-all 工作区 story-sign 会话绑定率达到 80.4%。
- **Transcript 上下文注入（I3）** — `story-lifecycle` 默认启用 `miner.story_context_provider`，`design/build/verify` prompt 自动注入 `{transcript_context}`。
- **Done 复盘钩子（I4）** — `story done <key>` 使用 `sys.executable` 稳定调用 `retrospect.py --story <key>`，自动生成 story 级合并复盘。
- 新增契约测试：`tests/contracts/test_anchors_contract.py`、`test_provider_contract.py`、`test_done_retrospect_contract.py`、`test_store_link_schema_contract.py`。
- 新增 miner 测试：`test_link_anchors.py`、`test_knowledge_outputs.py`。

### Changed
- 刷新项目级文档：`docs/MIGRATION.md`、`docs/INTEGRATION.md`、`docs/ADOPTION.md`、`packages/story-miner/docs/ROADMAP.md`、顶层 `README.md`。
- `packages/story-miner` 脚本改为 config 驱动，不再硬编码 hc-all 路径。

### Fixed
- `miner/link.py` 旧宽窗兜底导致 `1064837` 误绑 84 个 session，现降到 5 个。
- `story done` 复盘调用由硬编码 `"python"` 改为 `sys.executable`，避免 venv/PATH 错乱。

## [0.11.6] - 2026-06-27

### Added
- **Design prompt 共享状态影响分析** — design 阶段任务书强制触及五类符号调用 `codegraph_impact` 做共享状态影响分析，防止 AI 写出局部正确、全局崩坏的跨功能数据流污染 bug
- **`worktree_state` 字段** — `SetBranchRequest` 新增该字段，agent 自建分支可直接标记 `available`，免走 worktree handler
- **前端 Bugs 模块** — 新增 `BugsTab`/`BugsPage`，Dashboard/StoryDetailPage/ContextTab/StorySidebar 配套改进
- **Kimi CLI client** — LLM 层新增 `kimi_cli` 适配，`llm_client` 同步扩展
- **编排 / 前端 / profiles / prompts 协同开发链路** — 贯通多端协同开发流程

### Changed
- 新增 `progress.yaml` 看板元数据（github-ops Phase 2）

### Fixed
- **声明 `python-multipart` 依赖** — `/api/intake/preview` 改用 `Form`/`File` 上传后依赖 `python-multipart`，此前缺失导致全新环境（CI 全矩阵）测试收集阶段即报错 `Form data requires python-multipart`，现补进 `dependencies`
- **`test_intake_preview` 请求格式** — `/api/intake/preview` 端点改为表单上传（支持截图附件）后，测试请求由 `json=` 修正为 `data=` 对齐 `Form` 签名，恢复 200 响应

## [0.11.5] - 2026-06-17

### Added
- **改 bug 闭环** — `pack(bug)` 自动含关联需求的全套 context（spec/plan/分支/DDL/Nacos）；进 story 详情自动同步关联 bug（节流 5min）；ContextTab 复制 pack 时可指定 skill 提示词；bug 详情「标记已修复」一键收尾（TAPD + 本地状态）；pack 完整度检查标红缺失项（spec/branch/bugfix-report）。端到端验证 bug 1009779 ↔ 需求 1065460。
- **POST /api/story/{key}/sync-related-bugs** — 从 TAPD `get-related-bugs` 拉关联 bug，设 `parent_key`（bug↔需求关联从 story 侧拿，不依赖 bug.story_id null 字段）。
- **POST /api/story/{bug_key}/resolve** — bug 标记已修复，更新 TAPD bug 状态 + 本地 status，返回 bugfix-report 证据是否就位。
- **pack 增强** — `parent_key` 解析（拼关联需求 context）+ 可选 `?skill=` 提示词 + 完整度检查（缺项标红）。
- **TapdApi.get_related_bugs** + **upsert_story_from_source 支持 parent_key**。

### Fixed
- **sync-related-bugs 对旧 story 健壮** — 旧 story 的 `source_type`/`source_id` 可能为 NULL（story_key 含 TAPD id 但字段没存），从 story_key（`tapd-{id}`）提取 TAPD id。

## [0.11.4] - 2026-06-15

### Added
- **Story 上下文资料包（复制注入）** — 详情页新增「上下文」Tab + 「复制上下文资料包」按钮；一键导出中性、混合浓度的 Markdown 资料包（PRD/spec/plan/DDL 给 worktree 内路径，Nacos 配置正文内联），粘贴到任意 AI agent 即可开干，适配开发/改 bug/排查多场景。后端 `context/pack.py` + `GET /api/story/{key}/context/pack`。
- **Context 关系回填 API** — 新增 3 个写端点：`POST /context/documents`（PRD/spec/plan）、`POST /context/change-items`（DDL/Nacos）、`PUT /context/branch`（分支绑定）；每次写入 bump context revision。供 agent 半手动回填 story 关系。
- **hc-all `story-context` 通用 agent skill** — 放 `.agents/skills/`（不绑 claude），教任何 agent 用 curl 把分支/PRD/DDL/Nacos 关系写回 DB。

### Changed
- 删除过时的 hc-all `story-lifecycle` skill（.agents + .claude 版）并清理 dev-workflow / AGENTS.md 的悬空引用——该 skill 停留在旧命令 + PostgreSQL 描述，与当前 SQLite 系统不符（系统渐进演化，稳定后重写）。

### Fixed
- **测试不再污染主库** — `tests/conftest.py` 新增 autouse DB 隔离 fixture（`_isolated_db`），所有测试默认重定向到 tmp 目录，杜绝写真实 `~/.story-lifecycle/story.db`（根因：原 `isolated_story_home` 非 autouse，漏用的测试直连主库）。

## [0.11.3] - 2026-06-15

### Added
- **Profile 驱动的分支命名** — 按 profile 配置生成 worktree/分支，story 摘要经 LLM 翻译为英文 slug 作为分支名，隔离每个 story 的开发环境
- **Planner 注入项目绑定** — 规划阶段把关联项目（仓库路径、默认分支）注入 AI CLI prompt，支持 worktree 与分支隔离执行

### Changed
- **TAPD 需求页过滤子任务** — 需求 tab 只展示需求(story)和缺陷(bug)，不再展示子任务(subtask)；日历视图仍保留子任务
- minimal profile 默认 design 阶段使用 claude adapter

### Fixed
- **分支名 slug 用下划线分隔** — slug 分隔符从连字符改为下划线，避免分支名含连字符导致的命名/解析问题

## [0.11.2] - 2026-06-15

### Added
- **开始开发要求填 PRD** — `/start` 必填 story 内容/PRD；支持**上传本地文件**（只显示文件名，内容不灌进输入框）或**粘贴文本**，两者都落地成 `workspace/prd/{key}.md`，design 阶段把**文件路径**注入给 AI CLI（只注路径不内联内容，避免撑爆 CLI 上下文）。前端 `ProjectPickerModal` 加了必填 PRD 步骤。
- **执行可观测日志** — serve 启动时把 `story-lifecycle` logger 配到 INFO；planner 打 `EXECUTE / injecting(prompt 大小+是否含 context+头部) / PTY session started` 三条日志。

### Fixed
- **`/start` workspace 绑定** — 关联项目后 workspace 指向项目 `repo_path`（原来停在服务进程 cwd，CLI 跑错仓库）。
- **PRD 注入迁移回归** — `_build_cli_prompt` 补回 PRD 路径注入（LangGraph→Agent Function Calling 迁移时丢失）。
- **pty.kill 杀进程树** — Windows 用 Job Object（KILL_ON_JOB_CLOSE）+ `taskkill /T /F` 双保险回收 CLI 子进程；孤儿从 ~9 个（数百 MB）降到极少（主进程 + 绝大多数 helper 都被杀，winpty 不支持挂起启动，codex 个别早派生的 helper 仍可能逃逸）。
- **重启不再自动重跑执行** — `recover_orphan_stories` 不再 `resume_story_async` 重新执行（避免静默重启 codex），改把 active 孤儿标记 `paused`，由用户从 UI 手动「继续执行」。

## [0.11.1] - 2026-06-14

### Fixed
- **Dashboard 永远显示 0 个 Story** — WS 推送的 `_story_list_json` 与 REST `/api/story` 共用同一序列化（`_serialize_story_summary`）和取数（`list_visible_stories`），WS payload 补回 `tapdType`/`intakeState` 等字段，Dashboard 过滤不再恒为空（由 Playwright E2E 发现）
- **findings 双重 severity 过滤** — `/findings` 端点改用 `get_findings_by_story` + 单点过滤，low severity 的 open finding 不再被 `get_open_findings` 的默认 medium 门槛静默砍掉
- **CLI `story list` 漏 candidate story** — API 与 CLI 统一调用 `db.list_visible_stories(...)`，行为对齐；`COMPLETED_STATES` 提为共享常量

### Added
- `/api/story/{key}/stats` 端点（`code_changes` / `loop_rounds` / `findings_open`），供详情页概览的统计卡使用；之前该端点缺失导致 OverviewTab 每次加载打 404

### Changed
- 消除多处 copy-paste drift：event payload 解析抽 `parse_event_payload` / `is_adversarial_loop_event`（统一 stats / loop-trace / timeline / gate-history 四端点的失败语义）；story 列表序列化抽 `_serialize_story_summary`；severity 排序提为 `db.SEVERITY_ORDER` 常量；前端 `AgentAction` 类型三处定义统一 import 自 `api/client`

## [0.11.0] - 2026-06-13

### Changed
- **编排引擎从 LangGraph 切换为 Agent Function Calling** — 弃用 StateGraph，改用 Agent 函数调用编排；LLM 先规划再执行 CLI，精简编排 prompt，规划通过 SSE 流式生成。配套新增 Agent 架构测试并清理过期测试
- **StoryDetailPage 全面重构** — sidebar + content 布局，浅色主题，拆分为概览 / 代码变更 / 对抗循环 / 测试 / 质量门 / 终端六个 Tab

### Added
- **多会话 PTY 后端** — 1:N story→session 注册表，会话 list/spawn/kill 端点 + 多会话 WebSocket；前端 `usePTYSessions` hook 与终端 Tab 切换
- StoryDetailPage 组件族：StorySidebar、OverviewTab（进度条 + 规划 + 快速统计）、StageProgress、CodeChangesTab、QualityGateTab、AdversarialLoopTab、TestTab、TerminalTab、ActionCard
- 项目面板 UI + 「开始开发」自动绑定项目 + 项目选择弹窗 + 路径校验放宽
- 每阶段 LLM 规划 + 按阶段 adapter 配置
- stats 与多会话 PTY 前端 API

### Fixed
- 前端 lint 清零（54 项）：修复 WebSocket/PTY 重连 TDZ 自引用、prop 变化 state 重置、SSE 重入守卫等 React 反模式；`any` 全部替换为领域类型；顺带修掉终端 mount 闪烁、SSE EventSource 被立即销毁两个隐患
- `upsert_story` 补回缺失的 `intake_state` 列；candidate 推进为 ready 时正确设置 status=active
- 「我的 Story」与 tapdType 解耦 + 修正 TAPD 缺陷 owner 过滤
- 候选 story 纳入列表展示，intakeState 写入 API 响应
- `test_execution_mode` PTY spawn mock 返回 None 导致解包失败
- minimal.yaml profile 在根目录与打包目录间同步
- 点击后立即跳转详情页，规划改为异步生成

## [0.10.3] - 2026-06-11

### Added
- **Story 长期资料数据模型** — 6 张新表（project、story_project、project_runtime_fact、story_document、story_change_item、story_delivery_artifact），story 表新增 intake_state / context_revision
- **intake_state 边界守卫** — TAPD 同步创建 `candidate + idle`，禁止自动启动未审核 story；`list_active_stories` / `recover_orphan_stories` 过滤 candidate
- **项目注册表模块** — 路径规范化（Path.resolve）、git 可用性检测（rev-parse）、多运行时事实记录
- **Worktree 模块** — Resolver（解析 git worktree list --porcelain -z）+ Decider（纯函数决策表）+ Handler（安全执行），支持多仓库隔离、分支冲突检测、清理门禁
- **交付产物模块** — 统一 GitHub PR / GitLab MR / 本地合并模型，AI 禁止设置 `abandoned`，审查记录与清理门禁
- **Context 模块** — ContextResolver（实体组装 + 校验）、Snapshot（版本化 Markdown 快照）、AutoDiscovery（Scanner/Decider/Handler 自动发现 DDL/Nacos/PRD）
- **15 个新 API 端点** — context CRUD、project CRUD、worktree prepare/cleanup-preview/cleanup、delivery-artifacts CRUD、story start 验证、TAPD 回写建议
- 显式 story 执行模式与生命周期文档
- Dashboard 白底主题 + 日历视图 + 泳道布局 + TAPD 链接优化
- Workspace Onboarding & Project Profile
- 发布信号、探针验证、漂移检测测试增强

### Fixed
- `upsert_story_from_source` 不再覆盖 `intake_state`（本地生命周期字段）
- `create_story` 默认插入 `intake_state='ready'`，修复回归测试

## [0.10.2] - 2026-06-10

### Added
- `tapd_type` 字段 — 三维度区分需求/缺陷/子任务
- `story list -t` / `story calendar -t` — 按类型筛选
- 默认隐藏已完成 story（resolved/rejected/closed），`--completed` 查看

### Fixed
- TAPD `due` 字段名修复（`due_date` → `due`），deadline 优先 `custom_field_40`（预计上线时间）
- sync 按 `custom_field_25`（后端开发人员）过滤父需求，不再误拉非本人需求
- 子任务只拉用户的 + 测试任务
- 前端 ErrorBoundary 防白屏 + Dashboard undefined 兜底

## [0.10.1] - 2026-06-10

### Added
- `story sync --all` — 拉取全部 TAPD 需求（不再限于待处理状态）
- `story calendar` — 按 deadline 分组的日历视图，逾期/今天/近期颜色标记

## [0.10.0] - 2026-06-10

### Added
- Story 基础管理模块：TAPD 需求可视化与本地 story 同步
- `story sync` CLI 命令 — 拉取 TAPD 待处理需求/缺陷同步为本地 story
- `story list/show/advance/done` CLI 命令 — 查看、推进、完成 story
- story 表扩展 6 字段（deadline/priority/owner/branches_json/tapd_status/tapd_url）
- `SourceItem` 增加 `deadline` 字段 + TAPD 解析增强（提取 deadline/url）
- `upsert_story_from_source()` — 按 source_type + source_id 幂等 upsert
- Sync service 核心（dry_run/status_only 模式）
- API 扩展：`GET /api/story` 逾期筛选 + 新字段、`POST /api/sync/tapd`、`GET /api/sync/tapd/status`
- 全量回归测试（632 tests）

## [0.9.1] - 2026-06-10

### Added
- Web UI 全面演进：Dashboard、Story Detail、Terminal、Diagnostics、Quality 五大页面
- Timeline、Gate History、Loop Trace、Findings、Dependency Graph、Per-Story Workspace、Patterns POST 等功能
- Workspace lock 升级为 filelock，支持跨进程安全
- v0.6–v0.8 引擎特性补齐（phase5 + phase6）

### Changed
- 移除 TUI，清理 `_tui_app` 耦合，重构 CLI 入口
- 去重 `graph_nodes`，清理死代码，修复 quality_flywheel 测试

### Fixed
- Demo planner mock 修复，`resume_story` EmptyInputError 处理
- 全量回归测试通过（611 tests）

## [0.9.0] - 2026-06-09

### Added
- `ResolvedProfile` + `StageConfig` dataclass：Profile 在故事启动时一次性解析为不可变对象，存入 `state["_resolved_profile"]`，消除节点内重复的 `load_profile`/`get_stage_config` 调用
- `NodeError` 统一错误 dataclass（`errors.py`），提供 `.apply(state)` 方法，替代散落在各节点的 `state["last_error"] = ...` + `log_node_error(...)` 模式
- `_rp()`/`_stage_cfg()` 辅助函数，所有 profile 读取收敛为这两个入口

### Changed
- 子任务分发回退为 `interrupt()` + `_pending_sub_keys` 模式，删除 Send API fan-out（LangGraph Send 不支持并行终端启动）
- 对抗循环回退为 `evaluator_loop.run_plan_loop()`/`run_code_review_loop()` while 循环，删除 `adversarial_graph.py` 子图（子图仅支持 LLM API 调用，无法处理不同 CLI 间的 done-file 轮询）
- 所有测试 patch 路径从 `graph_nodes.load_profile`/`get_stage_config` 统一为 `profile_loader._load_raw`，消除 import 路径耦合

### Removed
- `adversarial_graph.py`（633 行）— LangGraph 子图方案不可行，回退到 while 循环
- `build_subtask_sends()`/`merge_subtask_results()` — Send API 代码一并移除

## [0.8.9] - 2026-06-08

### Added
- `LLMClient` 统一 LLM 调用层（`llm_client.py`），提供 invoke/invoke_json/invoke_structured/stream 四个方法，消除 7 个文件中重复的 httpx 调用和 JSON 解析
- Pydantic 模型替代手写 JSON schema 验证（`schemas.py`），覆盖 Plan/Review/Route/Semantic 等全部 LLM 输出结构
- 对抗循环 LangGraph 子图（`adversarial_graph.py`），3 节点 planner→reviewer→judge，每轮独立 checkpoint
- Send API fan-out 子任务分发，替代 interrupt + ThreadPoolExecutor 手动启动

### Changed
- Layer 1 图从 11 节点精简到 5 节点：plan_stage → execute_and_wait → review_stage → router → advance
- retry/skip/fail/wait_confirm 合并进 router_node 内部，不再作为独立节点
- execute_stage + poll_completion 合并为 execute_and_wait_node
- `observability.log_route_decision` 移除 `_router_decision` 字段依赖

### Removed
- 6+ 处重复的 `_call_llm`/`_api_config`/`_parse_llm_json` 等函数，统一到 `LLMClient`
- 6 个独立 action 节点（execute_stage, poll_completion, retry, skip, fail, wait_confirm）

## [0.8.8] - 2026-06-07

### Added
- `GithubCli.ensure_label()` 自动创建不存在的 label，publish 不再因 label 缺失而失败

### Changed
- `story plan` 发布步骤自动从 git remote 检测 repo，不再手动输入

## [0.8.7] - 2026-06-07

### Fixed
- publish 不再因自定义 label 不存在而失败：先创建 Issue，label 逐个添加、失败静默跳过
- `story plan` 发布步骤自动从 git remote 检测默认 repo，无需手动输入

## [0.8.6] - 2026-06-07

### Added
- `story plan` 一键自动跑完整个规划流程（探测→需求→路线图→拆解→发布），跳过已完成步骤，无需记多个子命令

### Changed
- 交互确认改为"按回车确认保存，或输入修改意见"，避免 click.confirm 的 Y/n 校验阻断用户反馈
- JSON 解析支持 `[]` 数组格式，修复 decompose 拆解结果无法解析的问题
- LLM 调用不再限制 max_tokens，由模型自身控制输出长度

## [0.8.5] - 2026-06-06

### Changed
- roadmap 和 decompose 命令支持反馈循环：拒绝后可输入修改意见，AI 带着上一版草稿+反馈重新生成
- generate_roadmap / decompose_phase 不再内部自动保存，由 CLI 层在用户确认后统一写入

## [0.8.4] - 2026-06-05

### Added
- 交互式确认流程：展示草稿 → 确认或补充修改 → 带反馈重新生成，遵循 brainstorming 交互模式
- Rich Status spinner 动效：LLM 调用时显示"AI 正在思考..."

### Changed
- 生成与保存分离：LLM 只生成内容，用户确认后才写入磁盘

## [0.8.3] - 2026-06-05

### Added
- LLM 驱动的代码库分析：已有代码的项目自动扫描目录结构、README、pyproject.toml、关键源文件，由 AI 生成需求文档

### Fixed
- has_code_no_plan 探测结果引导错误：应先运行 story plan idea 生成需求文档，再生成路线图

## [0.8.2] - 2026-06-05

### Fixed
- has_code_no_plan 探测结果引导错误：应先运行 story plan idea 生成需求文档，再生成路线图

## [0.8.1] - 2026-06-05

### Fixed
- planner/state.py PLANNING_DIR 类型错误：字符串不能直接用 `/` 拼接 Path

## [0.8.0] - 2026-06-05

### Added
- GitHub Issues 数据源适配器：通过 gh CLI 拉取 `lifecycle:accepted` 标签的 Issue 创建 Story
- 双写同步：Story 阶段推进时自动回写 Issue comment/labels，GitHub Issue 作为可视化仪表盘
- 异常约定：双写失败仅 log warning 不阻断 Story 流转，gh 未认证启动时 fail-fast
- AI 规划层：项目状态自适应探测（空项目/有代码无规划/有路线图/已有 Issue）
- 断点续传：规划流程中断后 `story plan init` 自动检测续传（`.story/planning/state.json`）
- 共享 LLM helper：OpenAI 兼容 API 封装，复用 `STORY_LLM_*` 环境变量
- `story plan idea`：idea → 需求文档（LLM 生成）
- `story plan roadmap`：需求 → 分阶段路线图（LLM 生成）
- `story plan decompose`：路线图 → Issue 草稿（LLM 拆解）
- `story plan publish`：Issue 批量创建（gh CLI），支持 `--dry-run` 预览

## [0.7.3] - 2026-06-03

### Fixed
- winpty spawn env 参数类型修复：dict → null-terminated string

## [0.7.2] - 2026-06-03

### Fixed
- winpty.PTY.spawn() 参数修正：cmdline 应为空格分隔的字符串而非 list

## [0.7.1] - 2026-06-03

### Fixed
- PTY spawn 500 错误：pywinpty 不可用时自动降级到 subprocess fallback
- pywinpty 检测改为 `importlib.util.find_spec`，避免 lint 误报

## [0.7.0] - 2026-06-03

### Added
- 跨平台 PTY 管理器 `terminal/pty.py`：Windows 用 pywinpty，Unix 用 stdlib pty，异步队列输出，atexit 清理防僵尸
- WebSocket endpoint `/ws/pty/{story_id}`：双向 PTY 流，输出推 xterm.js，键盘输入写回 PTY，支持 resize
- REST API `/api/pty/{story_id}/spawn` 和 `DELETE /api/pty/{story_id}`：按需启动/终止 PTY 进程
- 前端新增 xterm.js 终端组件（TerminalPanel），详情/终端 tab 切换，"启动终端"按钮 spawn shell

## [0.6.1] - 2026-06-03

### Added
- Graph action nodes（advance/retry/skip/fail/pause/execute）自动广播状态变更到 WebSocket 客户端，Web Board 实时更新无需刷新
- React 前端替代手写 HTML：Vite + React + TypeScript，组件化架构（StoryList、StoryDetail、useStories hook）
- StoryDetail 支持操作按钮：根据状态动态显示继续/跳过/终止/删除，直接调 REST API
- Vite 开发模式代理 API 到 FastAPI（`cd frontend && npm run dev`），支持 HMR 热更新
- 前端构建产物自动输出到 `src/story_lifecycle/web/`，pip 安装即用

## [0.6.0] - 2026-06-03

### Added
- `story --web` 启动浏览器端 Web Board，自动打开浏览器，实时显示 Story 列表和详情
- FastAPI 新增 `/ws/stories` WebSocket endpoint，支持 Story 状态实时推送
- 新增 `notify_story_update_sync()` 供 graph worker 线程安全地广播状态变更
- 新增手写 HTML 前端（`src/story_lifecycle/web/index.html`），暗色主题，支持 Story 选择、状态 badge、详情面板
- FastAPI 新增 StaticFiles mount，自动 serve `web/` 目录下的前端文件
- pip wheel 打包包含前端静态文件（`pyproject.toml` artifacts 新增 `web/**`）
- 新增设计文档 `docs/design-web-board.md`：Web Board 渐进式升级方案（Phase 1-4）

## [0.5.59] - 2026-06-02

### Added
- `init-knowledge` 重写为确定性探测模式：文件系统扫描 → 项目概览 → 范围确认 → 知识文件生成
- 新增 `knowledge/detector.py`：自动识别 Java/Spring 服务、前端应用、文档/PRD/Bug 目录，过滤 node_modules/target 等生成目录
- 新增 `knowledge/scope.py`：P0 范围推荐，核心业务服务默认纳入，审计/dms/网关/前端默认排除
- 新增 `knowledge/wizard.py`：交互式向导（accept/include/exclude/frontend/dry-run/quit）
- 新增 `knowledge/run_writer.py`：run artifacts 写入（detection-result.json、scope-decision.yaml）
- 新增 `knowledge/generator.py`：生成 product.yaml、manifest.yaml、search-catalog.md、候选业务域、pending-review-items.md 等知识文件
- `init-knowledge` 支持 `--yes`（非交互）、`--dry-run`、`--include`/`--exclude`、`--codegraph` 参数
- 旧 AI CLI 扫描模式通过 `--legacy` 保留

### Changed
- `knowledge/paths.py` 新增 `runs_dir()` 和 `run_dir()` 路径 helper

## [0.5.58] - 2026-06-02

### Changed
- bootstrap 扫描改为按业务域并行（每个域自包含扫服务+数据表+测试+域间依赖），不再按技术层拆开

## [0.5.57] - 2026-06-02

### Added
- 后端服务架构加入并行扫描维度（Controller、FeignClient、MQ、域边界识别）

### Fixed
- zellij 同名会话冲突时提示用户选择 attach 或 kill，不再自动杀掉
- Windows SendKeys 前先发 ESC 关闭输入法，避免中文候选窗干扰命令输入

## [0.5.56] - 2026-06-02

### Fixed
- bootstrap 提示注入文本改回中文（PowerShell Set-Clipboard 已支持 Unicode，SendKeys 仅处理 ASCII）

## [0.5.55] - 2026-06-02

### Fixed
- 修复 Windows 中文乱码：注入指令改英文（SendKeys 不支持 Unicode）
- 剪贴板改用 PowerShell `Set-Clipboard`（`clip.exe` 用 GBK 编码导致乱码）
- 修复 zellij 启动后注入指令乱码问题

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
