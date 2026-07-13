# 深度任务：Story Lifecycle 代码冻结、设计对账与 v0.6 Reliability Loop 收敛

## 角色

你是一个资深代码审计与架构收敛助手。你的任务不是继续扩展功能，而是帮我把 Story Lifecycle 当前代码、设计文档和 roadmap 对齐，降低项目心智负担。

当前项目正在从 Story Lifecycle 演进为 StoryOS / Code Agent 操作层，但现在设计文档很多、实现也很多，需要先冻结现状，做一次深度盘点。

你可以深入扫描代码，可以多轮阅读文件、运行非破坏性命令、跑测试、查 CLI help、对照文档。你不能实现新功能。

## 总目标

完成一次“代码事实 ↔ 设计文档 ↔ roadmap”的深度对账，并输出收敛文档：

`docs/code-freeze-and-reliability-loop-plan.md`

最终目标是把近期开发收敛到：

```text
v0.6 Reliability Loop
```

核心验收口径：

```text
可安装
可启动
可跑通
可卡住
可排查
```

## 工作边界

### 允许做

- 深入阅读代码
- 深入阅读 docs
- 多轮 grep / rg / find
- 运行非破坏性命令
- 运行测试
- 运行 CLI help
- 运行 dry-run / demo 类命令
- 分析 pyproject / package data / prompts / profiles
- 生成分析文档
- 提出 roadmap 调整建议

### 禁止做

- 不实现新功能
- 不重构代码
- 不修改业务逻辑
- 不删除文档
- 不移动文件
- 不改 prompts/profiles
- 不打 tag
- 不 push
- 不执行 destructive 命令
- 不自动清理用户文件

如必须修改，只允许新增或修改这一个文档：

`docs/code-freeze-and-reliability-loop-plan.md`

## 关键上下文

项目当前方向：

```text
Story Lifecycle = StoryOS 的当前实现
StoryOS = the operating layer for coding agents
当前应该先让最小闭环稳定，而不是继续扩展智能能力
```

近期必须收敛的目标：

```text
可安装
可启动
可跑通
可卡住
可排查
```

## 推荐审计方法

请多轮执行，不要只做浅层扫描。

### 第 1 轮：项目入口扫描

阅读：

- `AGENTS.md`
- `pyproject.toml`
- `README.md`
- `CHANGELOG.md`
- `docs/roadmap-v0.5-to-v1.0.md`

检查：

- CLI 入口
- package version
- console_scripts
- package data
- 当前 roadmap 说已完成什么
- 当前 roadmap 计划做什么

建议命令：

```bash
rg "console_scripts|story|version|packages|artifacts|profiles|prompts" pyproject.toml README.md CHANGELOG.md
rg "v0.6|Reliability|Diagnostics|StoryOS|Project Intelligence|done|setup|doctor" docs/roadmap-v0.5-to-v1.0.md docs -n
```

### 第 2 轮：CLI 能力扫描

阅读：

- `src/story_lifecycle/cli/main.py`
- `src/story_lifecycle/cli/setup.py`
- `src/story_lifecycle/cli/doctor.py`
- `src/story_lifecycle/cli/tui.py`
- `src/story_lifecycle/cli/demo.py`
- `src/story_lifecycle/cli/swebench.py`

运行非破坏性命令：

```bash
python -m story_lifecycle --help
story --help
story setup --help
story doctor --help
story serve --help
story demo --help
story swebench --help
```

如果本地 `story` 命令不可用，可以用：

```bash
PYTHONPATH=src python -m story_lifecycle --help
```

需要回答：

- 哪些命令真实存在？
- 哪些命令文档有但代码没有？
- 哪些命令未配置 LLM 时也应该可运行？
- setup/doctor 是否真的不会被 API key 检查挡住？
- Windows 下是否存在 PATH/story.exe 风险？

### 第 3 轮：Orchestrator 流程扫描

阅读：

- `src/story_lifecycle/orchestrator/graph.py`
- `src/story_lifecycle/orchestrator/nodes.py`
- `src/story_lifecycle/orchestrator/router.py`
- `src/story_lifecycle/orchestrator/planner.py`
- `src/story_lifecycle/orchestrator/gate.py`
- `src/story_lifecycle/orchestrator/validation.py`
- `src/story_lifecycle/orchestrator/tools/base.py`

需要画出真实流程：

```text
create story
-> graph nodes
-> plan/design/implement/review/test/finalize
-> done file
-> gate
-> router
-> advance/retry/fail/wait_confirm
```

重点回答：

- done 文件路径到底是什么？
- done 文件 schema 当前要求是什么？
- malformed done 怎么处理？
- consumed done snapshot 是否实现？
- headless stdout synthetic done 是否实现？
- review gate 真实阻塞条件是什么？
- router 是否真的是 LLM / fallback / rule-based？
- wait_confirm / paused / blocked 真实语义是什么？

### 第 4 轮：TUI / Board 扫描

阅读：

- `src/story_lifecycle/cli/tui.py`
- `docs/design-tui-entry-state-machine.md`
- `docs/design-terminal-entry-lifecycle.md`
- `docs/design-board-diagnostics-panel.md`

检查：

- Board 当前布局
- 当前快捷键
- 当前 detail panel
- 当前进入终端逻辑
- 当前 watchdog 能处理哪些场景
- 是否已有右侧诊断面板
- 是否已有 diagnostics 快捷键
- 卡住时用户当前能看到什么

### 第 5 轮：Diagnostics / Observability 扫描

阅读：

- `src/story_lifecycle/orchestrator/observability.py`
- `src/story_lifecycle/orchestrator/debug_packet.py`（如果存在）
- `src/story_lifecycle/orchestrator/diagnostics.py`（如果存在）
- `src/story_lifecycle/orchestrator/entry.py`
- `src/story_lifecycle/db/models.py`

检查：

- event_log 是否实现
- stage_log 是否实现
- gate_result 是否实现
- debug API 是否实现
- build_debug_response 是否实现
- debug_packet 是否实现
- diagnostic bundle 是否实现
- stuck_reason 是否实现
- terminal recent output 是否实现
- 是否可满足“可排查”

### 第 6 轮：Workspace / Git / Repo Scope 扫描

阅读：

- `docs/problem-workspace-git-constraint.md`
- `docs/design-workspace-onboarding-project-profile.md`
- `src/story_lifecycle/orchestrator/service.py`
- `src/story_lifecycle/orchestrator/graph.py`
- `src/story_lifecycle/prompts/design.md`
- `src/story_lifecycle/prompts/implement.md`
- `src/story_lifecycle/prompts/review.md`

检查：

- workspace 是否必须是 git repo？
- 是否扫描子 git repo？
- 是否支持 multi repo workspace？
- 是否已有 affected_repos？
- design prompt 是否要求 affected_repos？
- implement prompt 是否要求读取 affected_repos？
- 系统层是否校验 affected_repos？
- 是否有 branch / dirty gate？
- workspace lock 当前是什么粒度？

### 第 7 轮：Story Source / Project Intelligence 扫描

阅读：

- `src/story_lifecycle/sources/`
- `docs/design-story-source-integration.md`
- `docs/idea-project-intelligence-pipeline.md`
- `docs/idea-storyos-project-intelligence-control-plane.md`
- `docs/design-workspace-onboarding-project-profile.md`

检查：

- ManualSource / TapdSource 是否实现
- source_type/source_id 是否入 DB
- TAPD 拉取、状态回写做到什么程度
- Project Profile 是否实现
- Workspace Onboarding 是否实现
- Project Intelligence Probe 是否实现
- Test Source 是否实现

### 第 8 轮：SWE-bench / Benchmark 扫描

阅读：

- `src/story_lifecycle/benchmarks/`
- `src/story_lifecycle/cli/swebench.py`
- `docs/design-swebench-runner.md`
- `docs/design-swebench-gradient-data-flywheel.md`
- `docs/design-three-layer-validation.md`

检查：

- prepare / solve / export / eval / summarize / run 是否实现
- analyze 是否实现
- gradient attribution 是否实现
- preference dataset 是否实现
- artifact gate 是否实现
- benchmark 能不能作为 v0.6 reliability 的回归资产

### 第 9 轮：测试现状扫描

阅读 / 运行：

```bash
rg "def test_|pytest|CliRunner|swebench|diagnostics|setup|doctor|done|tui" tests -n
pytest -q
```

如果全量测试太重，可以先跑：

```bash
pytest tests/test_cli_commands.py -q
```

需要回答：

- 当前测试覆盖哪些核心能力？
- 哪些 v0.6 reliability 能力没有测试？
- 有没有测试依赖本地环境导致不稳定？

## 设计文档状态分类

扫描 `docs/` 下所有主要设计/idea/problem 文档，输出状态表：

| 文档 | 类型 | 状态 | 关键实现文件 | 说明 |
|---|---|---|---|---|

状态枚举：

- `implemented`
- `partial`
- `planned`
- `idea_backlog`
- `obsolete`
- `needs_review`

判断规则：

- `implemented`：核心设计已在代码中实现，有关键文件支撑
- `partial`：部分实现，需列出已实现/未实现
- `planned`：明确进入 roadmap，但尚未实现
- `idea_backlog`：只是方向，不进入近期开发
- `obsolete`：已被新设计替代
- `needs_review`：状态不清，需要人工确认

## 最小可靠闭环分析

围绕下面五个词写差距分析：

```text
可安装
可启动
可跑通
可卡住
可排查
```

每个都要包含：

- 当前已具备什么
- 缺什么
- 风险是什么
- v0.6 必须做什么
- 哪些可以推迟

### 可安装

重点看：

- pip install / editable install
- package data
- console script
- Windows PATH / story.exe
- version / upgrade

### 可启动

重点看：

- story setup
- story doctor
- story serve
- story board / story 默认入口
- 未配置 LLM 的提示

### 可跑通

重点看：

- 最小 story 生命周期
- done 文件协议
- prompts/profiles
- zellij/headless
- review gate / router

### 可卡住

重点看：

- paused / blocked / wait_confirm
- done malformed
- CLI exited without done
- session dead
- workspace locked
- gate blocked

### 可排查

重点看：

- event_log
- debug API
- debug packet
- diagnostic bundle
- terminal recent output
- stuck reason
- board 诊断面板

## v0.6 Reliability Loop 重定义

请把 v0.6 从宏大的 Control Plane Foundation 收敛为：

```text
v0.6 Reliability Loop
```

v0.6 只允许包含：

1. CLI 稳定
2. setup / doctor 稳定
3. 最小 story 跑通
4. done 协议稳定
5. workspace 基础校验
6. diagnostics / debug packet / bundle
7. Board 右侧只读诊断面板
8. 最小回归测试

明确推迟：

- StoryOS branding 重构
- Project Intelligence
- Workspace Onboarding
- Project Intelligence Probe
- Test Source
- 双飞轮
- Meta-Planner
- Dynamic Stage Graph
- Policy Engine 完整版
- Copilot 对话
- 多模型并行

## 输出文档要求

请新增或更新：

`docs/code-freeze-and-reliability-loop-plan.md`

文档结构必须包含：

1. 背景
2. 为什么需要冻结
3. 冻结原则
4. 当前代码事实清单
5. 设计文档状态表
6. 已实现设计归档建议
7. 未实现设计 backlog 建议
8. 最小可靠闭环差距分析
9. v0.6 Reliability Loop 范围
10. v0.6 非目标
11. v0.6 验收标准
12. 推荐执行顺序
13. 风险与注意事项
14. 后续 roadmap 调整建议

## 输出质量要求

- 不要泛泛总结，要有文件路径和证据
- 对“已实现”的判断必须引用代码文件
- 对“未实现”的判断必须说明缺哪个模块/命令/API
- 表格要能直接用于后续 roadmap 整理
- 结论要克制，不要继续扩展新方向

## 最终回复要求

完成后请汇报：

1. 生成/修改的文档路径
2. 当前代码与设计最大的 3 个错位
3. v0.6 最应该先做的 5 个任务
4. 明确推迟的能力列表
5. 是否运行了测试，结果如何

