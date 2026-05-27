# Idea: Board 右侧常驻诊断面板与 Copilot 后置增强

## 背景

Story Lifecycle 当前的 `story board` 是用户管理 Story 的主入口。它已经能展示 Story 列表、进入终端、跳过/失败/恢复阶段、查看 detail panel、运行 doctor/setup、查看 inbox 等。但这些能力仍然是“命令式”的：用户需要知道按什么键、当前状态代表什么、卡住时该看哪些日志。

随着系统从简单状态机走向 Orchestrator Agent，用户真正需要的不只是更多快捷键，而是一个能解释状态、汇总证据、提供诊断出口的常驻区域。这个区域应该在用户最常停留的地方出现：Board 右侧。

因此新增一个 **Board Diagnostics Panel**：在 TUI 右侧固定区域中提供面向当前选中 Story 的运行态摘要、规则解释、最近事件和诊断打包入口。Copilot 问答是后置增强，不是 P0 的主线。

它不是一个通用聊天窗口，也不是让 LLM 直接接管工作流。它首先是 Story Board 的“常驻诊断台”，后续才升级成“副驾驶”。

核心原则：

```text
Diagnostics core emits facts
Board panel consumes facts
LLM optionally explains / proposes
Policy and TUI confirm state changes
Existing handlers execute
Event log records
```

也就是：P0 先把事实、诊断包和右侧常驻可见性做扎实；P1 之后 LLM 才基于脱敏 Debug Packet 做解释、建议、生成待确认动作。真正修改配置、推进任务、恢复 Story、跳过阶段、失败 Story，都必须经过确定性 handler 和可审计日志。

## 目标

P0 目标：

1. 在 `story board` 右侧新增常驻诊断面板。
2. 面板跟随当前选中 Story，展示当前阶段、状态、错误、done/session 状态和最近事件。
3. 先用规则解释“为什么可能卡住”，不默认调用 LLM。
4. 提供卡住时的一键诊断打包能力，生成可发给维护者排查的诊断包。
5. 新增 `story diagnostics` CLI，让诊断能力不依赖 TUI。
6. 所有诊断动作写入 `event_log`，便于回放。

非目标：

1. P0 不做全局聊天助手。
2. P0 不做 Copilot 输入框和 LLM 问答。
3. P0 不让 LLM 直接调用 skip/fail/resume/setup 等 destructive 或 state-changing 操作。
4. P0 不做长期记忆、自动学习或 pattern 激活。
5. P0 不把完整业务源码、完整日志默认发给外部 LLM。

## 产品判断：常驻右侧区域，但诊断优先

右侧常驻区域是产品形态，不是工程重心。工程重心必须是可复用的诊断核心：

```text
Diagnostics Signals / Debug Packet / Bundle
  -> CLI story diagnostics
  -> Board right panel
  -> optional Ask Copilot
  -> future Policy-integrated actions
```

这样可以同时满足两点：

1. 用户在 board 里天然看到当前 Story 为什么停，不需要猜日志位置。
2. 诊断能力不被 TUI 绑死，CLI、未来 Web Sidecar、VS Code 插件都能复用同一套 Debug Packet 和 Bundle。

因此 P0 坚持做“右侧常驻”，但它是很薄的一层消费 UI：只读事实、规则解释、一键打包。复杂 LLM 对话、动作建议和 Policy 集成都后置。

## 产品形态

Board 从单列布局变成左右布局：

```text
┌──────────────────────────────────────────────────────────────────────────┐
│ Header: Story Lifecycle v0.5.x | Router | Active | Completed             │
├──────────────────────────────────────────────┬───────────────────────────┤
│ Plan panel / notification                    │ Diagnostics panel         │
├──────────────────────────────────────────────┤                           │
│ Story list                                   │ 当前 Story 摘要            │
│                                              │ 状态解释                  │
│                                              │ 最近事件                  │
│                                              │ 可用诊断                  │
│ Completed section                            │ 诊断打包                  │
├──────────────────────────────────────────────┴───────────────────────────┤
│ Footer: key hints                                                         │
└──────────────────────────────────────────────────────────────────────────┘
```

左侧保留现有故事列表和操作习惯。右侧面板承担常驻状态解释和诊断入口。Copilot 问答后续复用同一区域，但不是第一版必需能力。

建议快捷键：

| 快捷键 | 行为 |
|---|---|
| `tab` | 在 Story 列表和诊断面板之间切换焦点 |
| `?` | 仍显示帮助 |
| `o` | 展开/聚焦右侧诊断面板 |
| `p` | 对当前 Story 打包诊断 |
| `shift+p` | 打包全局诊断 |
| `ctrl+enter` | P1 后发送 Copilot 问题 |
| `y` | P2 后对当前建议动作确认执行 |
| `esc` | 回到列表焦点 |

P0 可以先不实现复杂焦点系统，只要右侧面板常驻展示摘要，并提供 `p` 打包诊断。真正的输入框、建议动作确认和 Copilot 对话后续再补。

## 右侧面板内容

### 当前 Story 摘要

面板顶部展示当前选中 Story：

```text
STORY-001
stage: implement
status: paused
workspace: D:\...
last_error: Review gate blocked: missing tests
```

摘要必须是确定性读取，不依赖 LLM。

数据来源：

- `story` 表：`story_key`、`title`、`status`、`current_stage`、`workspace`、`last_error`
- `event_log`：最近关键事件
- `.story/done/{story_key}/{stage}.json`：当前 done 文件是否存在
- `.story/context/{story_key}`：已消费 done 快照、malformed done、stage context
- terminal/session resolver：CLI 是否退出、session 是否仍活着

### 状态解释

P0 状态解释先走规则：

| 状态 | 解释 |
|---|---|
| `active` + session alive | Agent 正在执行或等待 done 文件 |
| `active` + session dead + no done | CLI 可能退出但未写 done |
| `paused` + last_error | Gate 或人工确认阻塞 |
| `blocked` | 当前流程已失败，需要恢复/重试/人工处理 |
| done malformed | done JSON 损坏，需保留损坏文件排查 |
| waiting_subtasks | 父 Story 等待子 Story 完成 |

LLM 可以在规则解释的基础上补充自然语言说明，但不能替代事实判断。

### 最近事件

展示最近 5-10 条事件：

- `stage_start`
- `stage_complete`
- `route_decision`
- `gate_decision`
- `review_feedback`
- `error`
- `terminal_request`
- `diagnostic_bundle_created`
- `copilot_suggestion`
- `copilot_action_confirmed`

P0 可以仅展示事件类型、stage、时间、摘要。

### 诊断动作

P0 右侧面板只列出诊断动作，不列出 workflow state-changing 建议动作：

| 动作 | 执行方式 |
|---|---|
| package story diagnostics | 调用新增 diagnostics bundler |
| package global diagnostics | 调用新增 global diagnostics bundler |
| show detail | 调用现有 detail render |
| run doctor | 调用现有 `action_run_doctor`，只展示结果 |

P1/P2 后才允许 Copilot 生成“可建议动作”，但不自动执行：

| 建议动作 | 执行方式 |
|---|---|
| 进入终端 | 调用现有 `action_enter_terminal`，用户确认 |
| resume | 调用现有 `action_resume_story`，用户确认 |
| skip stage | 调用现有 `action_skip_stage`，用户确认 |
| fail story | 调用现有 `action_fail_story`，用户确认 |
| run setup | 调用现有 `action_run_setup`，用户确认 |

P2 的 Copilot 只生成 `SuggestedAction`：

```json
{
  "story_key": "STORY-001",
  "action": "package_diagnostics",
  "reason": "Story is paused with done parse failure and needs maintainer inspection.",
  "risk": "read_only",
  "requires_confirm": false
}
```

TUI handler 决定是否展示确认、执行哪个确定性函数。

## Debug Packet 与 Copilot 对话模型

P0 必须先定义 Debug Packet。它是诊断面板、CLI 诊断包和未来 Copilot 的共同输入。没有 Debug Packet，右侧面板就会被迫到处读取内部状态，最终反向绑架引擎。

### 输入上下文

P0 右侧面板和 P1 Copilot 都只拿当前 Story 的 Debug Packet，不读取全仓库。

Debug Packet 包括：

```json
{
  "story": {
    "story_key": "...",
    "title": "...",
    "status": "...",
    "current_stage": "...",
    "last_error": "..."
  },
  "workspace": "...",
  "done_state": {
    "current_done_exists": true,
    "current_done_valid": false,
    "malformed_path": "..."
  },
  "recent_events": [],
  "recent_stage_logs": [],
  "session_state": {
    "session_alive": false,
    "cli_exit_state": "exited_without_done"
  },
  "available_actions": []
}
```

### Copilot 输出结构

P1 后 Copilot 不能只输出纯文本，必须输出结构化响应：

```json
{
  "summary": "当前 story 卡在 implement 阶段，CLI 已退出但没有写 done 文件。",
  "evidence": [
    "status=active",
    "session_alive=false",
    "done file missing"
  ],
  "suggestions": [
    {
      "action": "package_diagnostics",
      "label": "打包诊断给维护者",
      "risk": "read_only",
      "requires_confirm": false,
      "reason": "需要查看 terminal 输出和 done 状态。"
    }
  ],
  "questions": [
    "用户端执行的是 story setup 还是 story doctor？"
  ]
}
```

TUI 可以把 summary 和 evidence 渲染出来，把 suggestions 渲染成可确认动作。P0 不需要该结构，只需要规则解释和诊断动作。

### 权限边界

权限分级：

| 类型 | 例子 | 是否可自动执行 |
|---|---|---|
| read_only | 状态解释、debug packet、诊断打包 | P0 可以 |
| local_config | run setup、修改 provider/model | 需要确认 |
| workflow_state | resume、skip、fail、abort | 需要确认 |
| destructive | delete story、清理目录 | 不允许由 Copilot 发起 |
| external_send | 上传日志、发给远程服务 | P0 不做，只生成本地包 |

## 诊断打包功能

用户反馈“story 进不去”“setup 阶段卡住”“doctor 用法不对”时，现在通常需要反复问日志。右侧面板应该提供一键诊断包：

```text
[p] Package diagnostics
```

打包后输出：

```text
.story/diagnostics/{story_key}-{timestamp}.zip
```

也可以复制一个摘要路径到面板：

```text
Diagnostic package created:
D:\project\.story\diagnostics\STORY-001-20260527-142033.zip
Send this file to maintainer.
```

### 包内容

建议结构：

```text
diagnostics/
  manifest.json
  summary.md
  story.json
  events.jsonl
  stage_logs.jsonl
  gate_results.jsonl
  debug_packet.json
  config.redacted.yaml
  environment.txt
  done/
    current-stage.json
    current-stage.malformed
    snapshots/
  context/
    working_memory.json
    plan_summary.md
  terminal/
    session_state.json
    recent_output.txt
  workspace/
    git_status.txt
    git_diff_stat.txt
```

### 脱敏规则

诊断包默认必须脱敏：

- API key、token、password、secret、authorization header
- 用户家目录可以保留盘符和项目名，但可以隐藏用户名
- `.env` 默认不打包，只记录是否存在
- git diff 默认只放 `--stat`，完整 diff 需要用户显式确认
- terminal 输出默认截取末尾 N 行，避免无限日志和敏感信息

红线：

1. 不默认上传到任何远程服务。
2. 不默认发送给 LLM。
3. 不默认包含完整源码。
4. 不默认包含完整业务日志。

### Story 级与全局级

P0 支持 Story 级诊断：

```text
story diagnostics STORY-001
```

Board 右侧快捷键调用同一能力。

P1 支持全局诊断：

```text
story diagnostics --global
```

全局诊断用于排查安装、PATH、setup、doctor、profile loading、wheel packaging 等问题。

全局包内容：

- story-lifecycle version
- Python version
- executable path
- PATH 摘要
- `story --help`
- `story setup --help`
- `story doctor --help`
- config.redacted.yaml
- installed package location
- profiles/prompts resource probe
- recent application logs

## 架构设计

### 新增模块

```text
src/story_lifecycle/
  cli/
    tui.py                    # 增加右侧 diagnostics panel 和快捷键
    diagnostics.py            # CLI 命令入口
  orchestrator/
    copilot.py                # P1: Debug Packet -> LLM response
    diagnostics.py            # 诊断包生成
    debug_packet.py           # 构造 story debug packet
```

P0 可以把 `debug_packet.py` 和 `diagnostics.py` 合并在一个文件中，后续再拆。

### 数据流

```text
TUI selected story
  -> build_debug_packet(story_key)
  -> render deterministic panel
  -> package diagnostics on user request
  -> db.log_event(...)
```

P1 之后追加 Copilot 数据流：

```text
User asks question
  -> build_redacted_debug_packet(story_key)
  -> ask_copilot(question, packet)
  -> CopilotResponse(summary/evidence/suggestions)
  -> TUI renders suggestions
  -> user confirms state-changing action
  -> existing action handler executes
  -> db.log_event(...)
```

诊断打包：

```text
TUI [p] / CLI story diagnostics STORY-001
  -> build_debug_packet(story_key)
  -> collect DB events/logs/gates
  -> collect workspace .story files
  -> collect safe env/package info
  -> redact
  -> write zip
  -> log_event(diagnostic_bundle_created)
```

### 与现有代码的关系

| 现有模块 | 使用方式 |
|---|---|
| `cli/tui.py` | 新增右侧布局、快捷键和渲染 |
| `db/models.py` | 读取 story、event_log、stage_log、gate_result |
| `orchestrator/observability.py` | 复用 debug response 思路 |
| `orchestrator/entry.py` | 复用 done/session/CLI exit 状态判断 |
| `orchestrator/service.py` | 继续作为状态变更入口 |
| `cli/setup.py` | 只通过现有 setup flow 修改配置 |
| `cli/doctor.py` | 诊断包可包含 doctor 摘要 |

## TUI 布局改造

当前 `StoryBoardApp.compose()` 是：

```python
yield Static(id="header-bar")
yield Static(id="plan-panel")
yield VerticalScroll(id="story-list")
yield Static(id="completed-section")
yield Static(id="detail-panel")
yield Static(id="footer-bar")
yield Footer()
```

目标改成：

```text
Screen
  header-bar
  body-row
    left-pane
      plan-panel
      story-list
      completed-section
      detail-panel
    diagnostics-panel
  footer-bar
  Footer
```

CSS：

```css
#body-row {
  height: 1fr;
  layout: horizontal;
}

#left-pane {
  width: 1fr;
}

#diagnostics-panel {
  width: 42;
  min-width: 32;
  max-width: 56;
  border-left: solid $accent;
  padding: 1;
  background: $panel;
}
```

窄屏策略：

- 终端宽度不足时，诊断面板默认隐藏。
- 按 `o` 或 `tab` 可临时展开。
- P0 可以先不做复杂响应式，只在宽度足够时展示，否则显示一行提示。

## LLM 调用策略

LLM 是后置增强，不是 P0 诊断面板的依赖。

### 规则优先模式

P0 默认不调用 LLM。右侧面板根据 debug packet 做确定性解释。

优点：

- 快
- 稳定
- 不消耗 token
- 不担心敏感信息外发

缺点：

- 自然语言解释有限
- 不能很好回答开放问题

### Ask 模式

P1 后，用户明确输入问题时才调用 LLM。

```text
User asks -> redact packet -> LLM -> structured CopilotResponse
```

这符合最小惊讶原则：系统不会在用户没要求时自动把诊断数据发给模型。

最终策略：

```text
P0: 右侧面板默认规则解释
P1: 用户主动提问才调用 LLM
```

## 与 Orchestrator Agent 的关系

Board Diagnostics Panel 是 Orchestrator Agent 的人机交互壳层，不是智能内核本身。Copilot 能力是这个壳层上的后置增强。

对应关系：

| Orchestrator Agent 概念 | Board 右侧区域映射 |
|---|---|
| DecisionEnvelope | P2/P3 SuggestedAction |
| Policy Engine | TUI 确认 + 未来 policy check |
| Working Memory | 右侧摘要和历史事实 |
| Runtime Blackboard | 未来显示 provider/model 健康度 |
| Shadow Mode | 展示“AI 建议但未执行”的动作 |
| Debug Packet | Copilot 输入上下文 |

这意味着右侧常驻区域不需要等完整 Orchestrator Agent 落地。它可以先作为观测和诊断界面存在，后续逐步接入 Copilot、Policy Engine 和 DecisionEnvelope。

## 版本拆分

### P0: 诊断核心 + 右侧常驻面板

目标：先解决“用户在 board 里不知道为什么卡住、维护者拿不到上下文”的问题。

内容：

1. 新增 `orchestrator/debug_packet.py`，构造稳定 Debug Packet。
2. 新增 `story diagnostics STORY_KEY`。
3. 新增 `story diagnostics --global`。
4. 新增 `orchestrator/diagnostics.py`，生成 zip。
5. TUI 右侧常驻面板展示当前 Story 摘要、规则解释、最近事件。
6. TUI `[p]` 对当前 Story 打包诊断。
7. 诊断包生成写入 `event_log`。

### P1: Copilot 问答

目标：让用户能问“为什么卡住了”和“下一步怎么办”。

内容：

1. 新增 `orchestrator/copilot.py`。
2. 构造 redacted Debug Packet。
3. 用户主动提问时调用 LLM。
4. LLM 输出结构化 `CopilotResponse`。
5. 右侧面板渲染 summary/evidence/suggestions。
6. 不自动执行 suggestions。

### P2: 受控动作建议

目标：Copilot 可以提出动作，但执行仍走 TUI 确认。

内容：

1. 定义 `SuggestedAction`。
2. 支持 `package_diagnostics`、`run_doctor`、`resume_story` 等动作。
3. 对 state-changing 动作弹确认。
4. 写入 `copilot_suggestion` / `copilot_action_confirmed` / `copilot_action_rejected`。

### P3: Policy Engine 集成

目标：与 Orchestrator Agent 设计合流。

内容：

1. SuggestedAction 升级为 DecisionEnvelope。
2. 接入 Policy Engine。
3. 根据 autonomy level 决定 shadow/confirm/apply。
4. 在右侧面板展示 policy decision 和 reason。

## 风险与约束

### 1. LLM 误导用户

应对：

- 所有事实必须来自 debug packet。
- 回答中必须列出 evidence。
- UI 区分“事实”和“建议”。
- P0 没有 LLM 问答，P1 后也只在用户主动提问时调用。

### 2. 敏感信息泄漏

应对：

- 诊断包默认本地生成，不上传。
- LLM 输入使用 redacted packet。
- 默认不包含完整源码、完整 diff、完整日志。
- 外发前需要用户显式确认。

### 3. 面板喧宾夺主

应对：

- 左侧仍是主工作区。
- 右侧默认只展示当前 Story 摘要、规则解释、最近事件和诊断动作。
- 窄屏隐藏。
- 不在面板中堆满长日志，长内容写入诊断包。

### 4. 绕过状态机

应对：

- Copilot 不直接改 DB。
- 所有动作调用现有 service/action handler。
- 所有动作写 event_log。
- destructive 动作禁止由 Copilot 发起。

## 测试策略

单元测试：

- `build_debug_packet()` 能处理 active/paused/blocked/completed。
- done 文件缺失、损坏、存在三种状态判断正确。
- redaction 能屏蔽 key/token/password/authorization。
- diagnostics zip 包含 manifest、summary、events、debug packet。
- global diagnostics 不泄漏 API key。

TUI 测试：

- board 能渲染右侧 panel。
- 切换 selected story 后 panel 更新。
- `[p]` 能生成诊断包并展示路径。
- 无 story 时 panel 显示空状态。
- 窄屏下 panel 可隐藏或降级展示。

集成测试：

- 构造一个 CLI exited without done 的 Story，诊断包能记录该状态。
- 构造 malformed done，诊断包包含 malformed 文件而不是吞掉错误。
- `story diagnostics --global` 在未配置 API key 时仍可运行。

## 推荐落地顺序

1. 先做 `Debug Packet`。
2. 再做 `story diagnostics STORY_KEY` 和 `--global`。
3. 再做右侧常驻只读面板。
4. 再把 `[p]` 接到诊断打包。
5. 再做 Ask Copilot 的 LLM 问答。
6. 最后做 SuggestedAction 和 Policy Engine 集成。

原因：Debug Packet 和诊断打包的价值最高、风险最低，而且能直接解决真实用户卡住后排查成本高的问题。右侧常驻面板先展示确定性事实，等事实管道稳定后再接 LLM，会更稳。

## Open Questions

1. 诊断包默认放在业务 workspace 的 `.story/diagnostics/`，还是用户级 `~/.story-lifecycle/diagnostics/`？
   - 建议 Story 级放 workspace，全局级放用户级目录。
2. 是否允许一键复制诊断包路径到剪贴板？
   - 建议允许，但失败时只展示路径。
3. 是否需要“发送给维护者”的远程上传？
   - P0 不做。只生成本地 zip。
4. Copilot 是否默认自动总结卡住原因？
   - P0 不自动调用 LLM，只做规则解释。用户主动提问才调用。
