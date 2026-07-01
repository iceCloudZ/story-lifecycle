> ⚠️ **历史快照（归档于 2026-07）**：描述的架构可能已被后续演进取代。当前架构见 [../../ARCHITECTURE.md](../../ARCHITECTURE.md)。本文件保留作决策记录（ADR），正文未修改。

---

# TUI Entry State Machine Design

日期：2026-05-25

## 背景

Story Lifecycle Manager 的 TUI 是用户管理 story 的主入口。用户在 `story board`
里通过快捷键创建、恢复、进入、跳过、失败、终止或删除 story。表面上这些都是
简单按键，但实际会跨越四个系统边界：

- SQLite 中的 story 记录和 stage 状态
- LangGraph 后台执行线程和 checkpoint
- `.story-done/{story_key}/{stage}.json` 完成握手文件
- Windows 终端里的 Zellij/tmux session 和 Claude CLI 进程

最近在 Windows + Zellij 环境中连续暴露了几个交互问题：

1. 按 `e` 进入 story 后看到的是空 PowerShell，而不是 Claude。
2. 按 `e` 在没有运行 session 时没有明显反馈，用户感知为“没反应”。
3. 按 `r` 后再按 `e`，Zellij 一闪而退。
4. `zellij list-sessions` 中的 `EXITED - attach to resurrect` session 被误判为
   healthy session。
5. foreground Zellij 启动时，如果同名 dead session 仍存在，
   `zellij --session ... --new-session-with-layout ...` 会直接返回 `1`。

这些问题不是独立小 bug，而是同一类设计缺口：TUI 按键逻辑直接混合了状态判断、
DB 修改、graph 启动、Zellij attach 和错误提示，没有把“事实状态”和“用户动作”先
归一化成明确状态机。

## 目标

本文档定义 TUI 入口状态机，用于约束 `e`、`r`、`n`、`q`、`s`、`f`、`a`、`x`、
`R/F5` 和 watchdog 的行为。

目标：

- 明确每个按键的职责边界。
- 明确 `.done`、graph running、DB status、session status 的优先级。
- 区分 live session、dead/exited session、missing session 和 unknown session。
- 避免进入空 shell、重复启动 Claude、静默失败和 Zellij 闪退。
- 让状态判断变成可单测的纯逻辑。
- 让所有不可执行分支都有可见反馈和日志。

非目标：

- 不重写完整 TUI。
- 不改变 story/profile/stage 的业务含义。
- 不引入新的 multiplexer。
- 不解决所有 ttyd/web terminal 场景，本文重点覆盖本地 TUI + Windows Zellij。

## 核心原则

后续 TUI 入口逻辑必须按以下顺序设计：

```text
事实状态 -> 归一化状态 -> 用户意图 -> 决策表 -> action -> handler 副作用
```

硬性约束：

- resolver 只读，不改 DB，不启动进程，不 attach，不删除文件。
- decider 是纯函数，只把 `(state, user_action)` 映射为 action。
- handler 才能执行副作用，例如更新 DB、启动 graph、打开 Zellij、删除 dead session。
- handler 必须处理 TOCTOU 竞态：resolver 读到的状态只代表决策时刻。执行
  `ATTACH`、`START_OR_RESUME`、`CLEANUP_*` 等副作用时，外部状态可能已经变化。
  handler 必须捕获失败并重新给出可见反馈，而不是让 TUI 静默退出或异常崩溃。
- 每个非执行 action 必须 `notify()`，并尽量更新 detail panel。
- 每个外部命令失败必须记录 command、returncode、stderr 或可诊断信息。
- 每个历史 bug 必须沉淀为回归测试。

## 事实状态

事实状态直接来自系统，不在读取时做业务推进。

### StoryStatus

```text
active
paused
blocked
waiting_subtasks
completed
failed
aborted
```

`completed`、`failed`、`aborted` 是终态。普通 `r` 和 `e` 不能隐式重开终态 story。

非终态 story 的基础映射：

| StoryStatus | GraphRunState | 默认归一化倾向 |
| --- | --- | --- |
| `active` | `running` | 根据 session 映射到 `RUNNING_WITH_*` |
| `active` | `not_running` | 根据 done/session 映射到 `DONE_*` 或 `IDLE_WITH_*` |
| `paused` | `not_running` | 可由 `r` 恢复，通常映射为 `IDLE` / `IDLE_WITH_*` |
| `blocked` | `not_running` | 默认不可自动恢复；若阻塞原因已解除，可映射为 `IDLE` |
| `waiting_subtasks` | `not_running` | 由 parent/subtask watcher 管理，不应由普通 `r` 隐式推进 |

`blocked` 和 `waiting_subtasks` 需要结合上下文判断。若没有明确解除条件，TUI 应显示
状态说明，而不是直接启动 Claude。

### GraphRunState

```text
running
not_running
unknown
```

`running` 表示当前进程内 `_running_stories` guard 认为 story 有后台执行任务。

注意：`story.status = active` 不等于 graph 一定在跑。TUI 必须同时看 DB 状态和
graph run guard。

### DoneState

```text
ok
corrupted
missing
```

`.done` 是 stage 完成的最高优先级事实。

- `ok`：JSON 可解析，且包含有效数据。
- `corrupted`：文件存在但不可解析或内容为空。
- `missing`：文件不存在。

只要 `.done ok`，就不能再启动 Claude；应由 graph 消费 `.done` 后进入 review/router/advance。

### SessionState

```text
live
exited
missing
unknown
```

Zellij 场景下必须区分：

- `live`：可 attach 的真实 session。
- `exited`：`zellij list-sessions` 中存在名称，但标记为 `EXITED`。
- `missing`：不存在该 session。
- `unknown`：查询失败或 multiplexer 异常。

`exited` 不是 healthy。它不能用于 `e` attach，也不能用于 `r` 注入 prompt。
foreground Zellij 启动前需要删除同名 `exited` session，否则新 session 会因为占名失败。

### CliExitState

```text
exited_without_done
none
unknown
```

foreground Zellij 启动脚本会写入 `story-exit-{story_key}` marker。若 CLI 退出但没有
`.done`，TUI 应提示执行失败或允许 `r` 重启，而不是假装还在运行。

### WorkspaceState

```text
locked_by_self
locked_by_other
free
unknown
```

同一 workspace 同时执行多个 story 有冲突风险。当前实现已有 workspace mutex，但 TUI
层需要能把“等待 workspace”解释给用户，而不是让用户误以为按键无效。

## 归一化状态

TUI 不应直接基于多个事实分支散写判断，而应先归一化为 `StageEntryState`。

```text
STORY_FINISHED
DONE_OK
DONE_CORRUPTED
CLI_EXITED_WITHOUT_DONE
BLOCKED_BY_WORKSPACE
RUNNING_WITH_LIVE_SESSION
RUNNING_WITH_DEAD_SESSION
RUNNING_WITH_UNKNOWN_SESSION
IDLE_WITH_LIVE_SESSION
IDLE_WITH_DEAD_SESSION
IDLE
UNKNOWN
```

归一化优先级固定如下：

1. `STORY_FINISHED`
2. `DONE_CORRUPTED`
3. `DONE_OK`
4. `CLI_EXITED_WITHOUT_DONE`
5. `BLOCKED_BY_WORKSPACE`
6. graph running + session state
7. graph not running + session state
8. `UNKNOWN`

重要规则：

- `.done` 优先级高于 session。即使 session dead，只要 `.done ok`，也应按完成处理。
- `DONE_CORRUPTED` 高于 `DONE_OK` 之外的所有可执行状态。损坏 `.done` 不能触发重跑，
  必须提示用户修复或删除。
- `RUNNING_WITH_DEAD_SESSION` 不等于 `IDLE`。它表示 graph 认为仍在跑，但可观察
  session 不可用，需要给出更明确的恢复策略。

## 用户动作

按键先表达用户意图，不直接执行副作用。

```text
e = enter/observe
r = resume/execute
n = create_and_execute
q = quit_tui
s = skip_stage
f = fail_story
a = abort_story
x = delete_story
R/F5 = refresh_only
watchdog = background_reconcile
```

语义约束：

- `e` 是观察入口，只进入已有 live session，不启动 Claude。
- `r` 是执行入口，负责启动、恢复、消费 `.done`、触发 foreground Zellij。
- `n` 是创建并执行入口，创建成功后行为等价于一次受控的 `r`。
- `R/F5` 必须是纯刷新，不得启动 graph、消费 `.done` 或 attach session。
- watchdog 是隐形入口，必须限制副作用范围并写清楚自动推进条件。

## Action 集合

decider 输出 `EntryAction`，handler 根据 action 执行副作用。

```text
ATTACH
START_OR_RESUME
CONSUME_DONE_RESUME
CLEANUP_DEAD_AND_START
CLEANUP_DEAD_AND_RESTART
PROMPT_KEY_EXISTS
CONFIRM_AND_DESTROY
PROMPT_PRESS_R
PROMPT_FIX_DONE
SHOW_STATUS
SHOW_RUNNING
SHOW_WORKSPACE_BUSY
SHOW_SESSION_UNKNOWN
SHOW_CLI_EXIT_ERROR
NOOP
```

Action 语义：

- `ATTACH`：退出 Textual，把真实终端交给 `zellij attach` 或 `tmux attach`。
- `START_OR_RESUME`：设置 story active，并调用 `start_story_async()`。
- `CONSUME_DONE_RESUME`：让 graph 恢复并优先消费 `.done`。
- `CLEANUP_DEAD_AND_START`：删除 `EXITED` session 后再启动 graph。
- `CLEANUP_DEAD_AND_RESTART`：用于 graph 仍被认为 running 但 session 已 dead 的场景。
  handler 必须先通过受控方式终止/释放当前 graph guard，再清理 dead session 并重启。
  不能在旧 graph 仍可能写 DB 时直接双开新 graph。
- `PROMPT_KEY_EXISTS`：`n` 创建 story 时 key 已存在，提示用户使用现有 story 或换 key。
- `CONFIRM_AND_DESTROY`：`x` 删除 story 的确认动作。handler 必须按
  stop graph guard -> kill/delete session -> stop ttyd -> release port -> delete DB -> refresh UI
  的顺序执行。
- `PROMPT_*` / `SHOW_*`：只做用户反馈和日志，不推进状态。
- `NOOP`：明确无操作，也要有用户可见解释。

## `e` / `r` 决策表

| StageEntryState | `e` | `r` |
| --- | --- | --- |
| `STORY_FINISHED` | `SHOW_STATUS` | `NOOP` / `SHOW_STATUS` |
| `DONE_CORRUPTED` | `PROMPT_FIX_DONE` | `PROMPT_FIX_DONE` |
| `DONE_OK` | `PROMPT_PRESS_R` | `CONSUME_DONE_RESUME` |
| `CLI_EXITED_WITHOUT_DONE` | `SHOW_CLI_EXIT_ERROR` | `START_OR_RESUME` |
| `BLOCKED_BY_WORKSPACE` | `SHOW_WORKSPACE_BUSY` | `SHOW_WORKSPACE_BUSY` |
| `RUNNING_WITH_LIVE_SESSION` | `ATTACH` | `SHOW_RUNNING` |
| `RUNNING_WITH_DEAD_SESSION` | `PROMPT_PRESS_R` | `CLEANUP_DEAD_AND_RESTART` with confirm |
| `RUNNING_WITH_UNKNOWN_SESSION` | `SHOW_SESSION_UNKNOWN` | `SHOW_SESSION_UNKNOWN` |
| `IDLE_WITH_LIVE_SESSION` | `ATTACH` | `START_OR_RESUME` with notice |
| `IDLE_WITH_DEAD_SESSION` | `PROMPT_PRESS_R` | `CLEANUP_DEAD_AND_START` |
| `IDLE` | `PROMPT_PRESS_R` | `START_OR_RESUME` |
| `UNKNOWN` | `SHOW_SESSION_UNKNOWN` | `SHOW_SESSION_UNKNOWN` |

`IDLE_WITH_LIVE_SESSION` 需要特别谨慎。live session 可能是人工 review shell、旧的
Claude 会话或用户手动打开的 pane。`e` 可以 attach 观察；`r` 不应向未知 pane 注入
prompt。推荐行为是启动新的受控 foreground execution，并提示用户存在可观察 session。

## 其他按键行为

### `n` 创建 story

职责：

1. 校验 story key 不存在。
2. 写入 DB 和初始 context。
3. 触发受控执行入口，等价于 `START_OR_RESUME`。

禁止：

- story key 已存在时隐式退化为 `r`。
- 创建后直接创建空 Zellij session。
- 绕过 graph 自己渲染 prompt。

若 story key 已存在，decider/handler 应走 `PROMPT_KEY_EXISTS`，明确提示用户选择已有 story
或使用新 key。

### `q` 退出 TUI

职责：

- 退出 Textual。
- 默认只退出 TUI，不自动 pause active story。

禁止：

- 杀掉 Zellij session。
- 假设退出 TUI 等于 Claude 停止。
- 隐式改变后台 graph/Claude 生命周期。

用户要让 Claude 继续跑，应在 Zellij 内使用 detach：`Ctrl+o` 后按 `d`。

如果未来需要“退出并暂停所有 active story”，应设计为显式按键或确认动作，而不是 `q`
的默认行为。

### `s` 跳过 stage

风险：

- graph 可能仍在后台执行当前 stage。
- `.done` 可能稍后写入，覆盖跳过后的状态认知。

要求：

- 若 graph running，先提示不可跳过或要求确认。
- 记录 skip event。
- 清理或解释当前 stage 的 `.done` 状态。

### `f` 标记失败 / `a` abort

风险：

- 后台 graph 线程可能在终态后继续写回状态。

要求：

- 终态写入后，runner 必须能检测并停止推进。
- TUI 应提示是否清理 session。
- 保留审计日志。

### `x` 删除 story

最高风险操作。推荐顺序：

```text
stop graph guard
kill/delete live or exited session
stop ttyd
release port
delete DB rows/events
refresh UI
```

删除操作必须二次确认。若 graph 正在运行，应提示会中止执行。

### `R/F5` 刷新

必须保持纯读：

- 重新读取 DB。
- 重新渲染 UI。
- 不启动 graph。
- 不消费 `.done`。
- 不 attach session。

### watchdog

watchdog 是隐形状态推进源，应拆成多个职责清楚的小任务：

```text
watch_terminal_requests
watch_done_files
watch_parent_resume
watch_blocked_substories
watch_cli_exit_markers
```

每个 watcher 都应有独立日志和明确副作用边界。不要在一个通用 watchdog 中混合多种
推进逻辑。

防重入约束：

- watcher 触发状态推进前必须拿到 story 级锁或执行原子 compare-and-set。
- 同一个 story 同一时刻只能有一个来源触发 `CONSUME_DONE_RESUME`、`START_OR_RESUME`
  或 `CLEANUP_*`。
- watcher 频率应有下限，避免在 foreground terminal request、`.done` 写入和 TUI
  前台按键之间制造忙等竞态。
- `watch_done_files` 只能在安全状态下自动消费 `.done`：推荐限制为
  `StoryStatus = active` 且 `GraphRunState = running` 且 `DoneState = ok`。
  若 `GraphRunState = not_running`，watchdog 只更新 UI/提示用户按 `r`，不应自行启动 graph。

## Windows + Zellij 约束

Windows 下不能依赖后台创建执行 pane：

```text
zellij attach --create-background ...
```

该方式可能创建不可交互 pane，或者导致空 PowerShell。可靠路径应为：

1. graph 渲染 prompt。
2. graph/terminal 生成启动脚本和 Zellij layout。
3. graph 发出 foreground terminal request。
4. TUI 退出 Textual。
5. TUI 在真实终端执行 foreground Zellij 命令。
6. 用户 detach 后回到 TUI。

foreground 启动前必须处理同名 dead session：

```text
if session_state == exited:
    zellij delete-session <name>
then:
    zellij --session <name> --new-session-with-layout <layout>
```

如果 delete 或 foreground command 失败，TUI 必须显示错误，不得静默重启 TUI。

TOCTOU 示例：

- resolver 判断 session 是 `live`，但 handler 执行 `ATTACH` 时 session 已退出。
  handler 应回到 TUI，提示 session 已丢失，并重新渲染状态。
- resolver 判断 session 是 `exited`，但 handler 删除前用户已手动清理。
  handler 应把 delete-not-found 视为可恢复状态，继续执行后续 start。
- resolver 判断 `.done missing`，但 handler 启动前 `.done` 已写入。
  handler 或 graph 必须重新检查 `.done`，优先消费结果，不启动 Claude。

## 诊断与日志

至少记录以下事件：

- `enter_terminal_decision`
- `resume_story_decision`
- `terminal_request_received`
- `run_tui_attach_start`
- `run_tui_attach_return`
- `run_tui_zellij_delete_exited`
- `session_state_resolved`
- `done_state_resolved`
- `cli_exit_marker_detected`

外部命令失败时记录：

```text
command
cwd
returncode
stdout tail
stderr tail
story_key
session_name
stage
```

用户可见反馈要求：

- `e` 没有 live session：提示“没有运行中的 session，按 r 启动或恢复执行。”
- `.done ok` 时按 `e`：提示“Stage 已完成，按 r 继续推进。”
- `.done corrupted`：提示修复或删除具体文件。
- Zellij foreground 启动失败：提示 returncode 和可执行的下一步。

## 测试策略

单元测试：

- `EXITED` Zellij session 不算 healthy。
- `list_sessions()` 不返回 `EXITED` session。
- foreground 启动前只删除 `EXITED` session，不删除 live session。
- `e` + `IDLE` 返回 `PROMPT_PRESS_R`。
- `e` + `RUNNING_WITH_LIVE_SESSION` 返回 `ATTACH`。
- `r` + `IDLE_WITH_DEAD_SESSION` 返回 `CLEANUP_DEAD_AND_START`。
- `DONE_OK` 优先级高于 running/session。
- `DONE_CORRUPTED` 不触发启动。
- `entry_action_notice()` 对所有不可执行 action 返回非空提示。

集成测试：

- 模拟 `r` 触发 terminal request，TUI 收到后退出并执行 foreground args。
- 模拟 Zellij foreground command returncode=1，TUI 记录错误并显示提示。
- 模拟 CLI exit marker 存在但 `.done` 缺失，进入 `CLI_EXITED_WITHOUT_DONE`。

手工测试：

1. 清理所有 Zellij session。
2. `story board` 选中 story，按 `e`，应提示按 `r`。
3. 按 `r`，应进入 foreground Zellij 并启动 Claude。
4. 在 Zellij 中 `Ctrl+o d` detach。
5. 回到 TUI 后按 `e`，应 attach 到 live session。
6. 让 Zellij session 变为 `EXITED`，再按 `r`，应自动删除 dead session 并重新启动。
7. 手工创建损坏 `.done`，按 `r/e` 均应提示修复，不启动 Claude。

## 当前实现差距

当前代码已经补上部分防线：

- `EXITED` Zellij session 不再算 healthy。
- `list_sessions()` 过滤 `EXITED` session。
- foreground Zellij 启动前可删除同名 dead session。
- `e` 无 live session 时有 `notify()`。

仍建议后续补齐：

- 将 `StageEntryState` 扩展为本文档的完整状态，而不是只有
  `DONE/RUNNING_HEALTHY/RUNNING_DEAD/IDLE`。
- 增加 `SessionState` 解析函数，明确返回 `live/exited/missing/unknown`。
- 将 watchdog 拆成多个 watcher。
- 为 Zellij foreground command 失败增加用户可见错误，而不是只写 debug log。
- 为 `s/f/a/x` 增加 graph-running 防护和确认逻辑。
- 把 CLI exit marker 纳入 resolver，而不是只在 poll node 内部处理。
- 将 `q` 改为只退出 TUI，不自动 pause active story；暂停应成为显式动作。
- 为 `n` 的 key 冲突和 `x` 的彻底删除补充显式 action。
- 为 watcher 增加 story 级防重入锁或原子状态转换。

## 评审问题

请重点评审：

1. `IDLE_WITH_LIVE_SESSION` 下按 `r`：建议提示用户确认“存在未管理的旧 session，
   是否清理并重启”。确认后杀旧 session，再启动新的 foreground execution。
2. `RUNNING_WITH_DEAD_SESSION` 下按 `r`：必须要求确认，并执行
   `CLEANUP_DEAD_AND_RESTART`。确认文案应说明 graph 仍被认为 running，强制重启可能
   中止当前后台执行。
3. `q`：建议只退出 TUI，不自动 pause。TUI 是观察和调度入口，退出 TUI 不应隐式改变
   后台 Claude/graph 生命周期。
4. watchdog：可自动消费 `.done`，但仅限 `active + graph running + done ok`。若 graph
   不在运行，只提示用户按 `r` 恢复，不自动启动 graph。
5. `x delete`：建议默认清理相关 Zellij session，包括 live 和 exited。删除前必须二次
   确认，并说明会终止后台执行、清理 session 和删除 DB 记录。
