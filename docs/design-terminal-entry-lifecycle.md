# Terminal Entry Lifecycle Design

日期：2026-05-24

## 背景

`story` TUI 目前有三个容易混淆的入口：

- `n` 创建 story，并启动当前 stage。
- `r` 恢复 story，并继续或重跑当前 stage。
- `e` 进入 story 对应的终端/session。

Windows + Zellij 下已经确认：

- 在 Textual TUI 内直接 `zellij attach` 不可靠，需要先退出 Textual，再把真实终端交给 Zellij。
- `zellij attach --create-background ...` 在 Windows 下可能创建出空 pane 或不可交互 pane。
- `.story-done/{story_key}/{stage}.json` 是 stage 是否已完成的事实来源，入口行为必须优先考虑它。

## 目标语义

### `n` 创建 story

职责：从零创建 story，并启动当前 stage 的 AI 执行。

行为：

1. 写入 story 记录和初始上下文。
2. 调用 graph 执行当前 stage。
3. graph 负责渲染 prompt、启动 AI CLI、等待 `.done`。
4. 如果 `.done` 已经存在，graph 必须优先消费 `.done`，不能重复启动 AI。
5. 如果 story key 已存在，`n` 必须拦截并提示用户使用已有 story；不能隐式退化为 `r`，避免误操作。

### `r` 恢复 story

职责：恢复或重跑当前 stage，是“执行入口”。

行为优先级：

1. 如果当前 stage 已有 `.done`，恢复 graph 消费 `.done`，不启动 AI。
2. 如果 story 正在运行，不重复启动。
3. 如果没有 `.done` 且未运行，启动当前 stage 的 AI CLI 并注入 prompt。
4. 如果之前 session 已死，按一次新的 stage 执行处理。

### `e` 进入终端

职责：查看或接管已经存在的 AI session，是“观察入口”，不是“执行入口”。

行为优先级：

1. 如果当前 stage 已有 `.done`，不进入空终端，也不触发 graph；只提示“Stage 已完成，按 `r` 继续推进”。
2. 如果 story 正在运行且 session 存活，attach 进入该 session。
3. 如果没有运行 session，不自动启动 Claude，也不创建空 PowerShell；提示用户按 `r` 启动或恢复执行。
4. 对 completed/failed/aborted story，默认只读查看状态，不启动 AI。

## 状态决策表

| 状态 | `n` | `r` | `e` |
| --- | --- | --- | --- |
| 当前 stage 有 `.done` | 不适用 | 消费 `.done`，进入后续 review/advance | 不启动 Claude；提示按 `r` 继续 |
| story 正在运行且 session 存活 | 不适用 | 不重复启动 | attach 进去查看/接管 |
| story 正在运行但 session 不存活，且有 `.done` | 不适用 | 消费 `.done`，不重启 AI | 提示 `.done` 已存在，按 `r` 继续 |
| story 正在运行但 session 不存活，且无 `.done` | 不适用 | 视为需要重启执行 | 提示 session 不可用，按 `r` 重启 |
| story paused/blocked，且无 `.done` | 不适用 | 启动 Claude + 注入 prompt | 不创建空 shell，提示按 `r` |
| story completed/failed/aborted | 不适用 | 不启动，除非以后增加显式 retry/reopen | 不启动，显示状态 |
| story key 已存在时按 `n` | 拒绝创建，提示使用已有 story | 不适用 | 不适用 |

说明：表中 `.done` 的优先级高于 running/session 状态。即使 session dead，只要 `.done` 存在，也应按成功完成处理。

## Windows + Zellij 约束

Windows 下不要在后台创建用于执行 AI 的 Zellij session：

```text
zellij attach --create-background ...
```

该方式可能没有可用 ConPTY，导致 session 看似存在但 pane 为空。

可接受方式：

- attach 已存在且健康的 session。
- 在真实终端中执行 foreground `zellij attach --create ...`。
- 对执行入口，如果无法可靠注入到健康 session，应退回到独立终端启动 AI CLI，而不是创建空 Zellij pane。

## `.done` 处理原则

`.done` 文件是最高优先级信号：

- 有 `.done` 时，不重复启动 Claude。
- 消费 `.done` 后由 graph 继续 review/router/advance。
- TUI 的 startup sweep、watchdog、`r`、`e` 都应使用同一个判断逻辑，避免分叉。
- `e` 只能提示 `.done` 存在，不能主动触发 graph 消费；状态推进由 `r`、startup sweep 或 watchdog 负责。
- AI 写 `.done` 时应使用原子写入：先写临时文件，再 rename 到目标路径，避免半截 JSON 被消费。
- 判断 `.done` 存在时应做最小 schema/JSON 校验；损坏文件不能触发重复启动，应提示用户修复或删除。

建议提取 helper：

```python
def stage_done_file(story: dict) -> Path: ...
def has_stage_done(story: dict) -> bool: ...
def validate_stage_done(story: dict) -> DoneValidationResult: ...
```

这些 helper 只能读取和校验状态，不能触发 graph、启动 CLI、attach session 或修改 story。状态推进必须由 TUI handler 在拿到明确 action 后显式调用。

如果 graph checkpoint 丢失但 `.done` 存在，`r` 应至少能重新构造当前 story 的 state 并消费当前 stage `.done`。若无法可靠进入后续节点，应停留在可解释状态并提示人工确认，而不是重启 AI。

## 实现建议

1. 先统一 TUI 内的 `.done` 判断 helper，并确保 `e` 只提示不推进。
2. 修改 `e`：
   - 有 `.done`：提示用户按 `r` 消费结果，不 attach。
   - 无 session：提示按 `r`，不创建空 session。
   - 有 session：只 attach。
3. 修改 `r`：
   - 有 `.done`：恢复/启动 graph 消费结果。
   - 无 `.done`：启动 graph 执行当前 stage。
4. 修改 graph/tool 启动逻辑：
   - Windows + Zellij 下不依赖后台 create + send_keys + paste_text。
   - 如果没有健康 session，使用可靠的独立终端启动 AI CLI。
5. 为 `n/r/e/.done/session` 决策表补单元测试。

## 可测试抽象

入口决策应尽量从 Textual 和 Zellij 调用中剥离，变成可单测的纯逻辑。

建议抽象：

```python
class StageEntryState(Enum):
    DONE = "done"
    DONE_CORRUPTED = "done_corrupted"
    RUNNING_HEALTHY = "running_healthy"
    RUNNING_DEAD = "running_dead"
    IDLE = "idle"
    STORY_FINISHED = "story_finished"


class StageEntryAction(Enum):
    ATTACH = "attach"
    START_OR_RESUME = "start_or_resume"
    PROMPT_PRESS_R = "prompt_press_r"
    PROMPT_DONE_PRESS_R = "prompt_done_press_r"
    PROMPT_FIX_DONE = "prompt_fix_done"
    NOOP = "noop"
```

`StageStateResolver` 负责读取 story、`.done`、running guard、session health，输出状态枚举。

状态说明：

- `DONE`：当前 stage 的 `.done` 存在且通过最小 JSON/schema 校验。
- `DONE_CORRUPTED`：`.done` 存在但校验失败。不能启动 AI，必须提示用户修复或删除损坏文件。
- `RUNNING_HEALTHY`：story 正在运行，且对应 session 健康可 attach。
- `RUNNING_DEAD`：story 运行态存在，但 session 不健康或进程不可用，且没有有效 `.done`。
- `IDLE`：story 未运行、未完成当前 stage、无健康 session。
- `STORY_FINISHED`：story 处于 completed/failed/aborted 等终态。

`SessionBackend` 负责隔离环境依赖：

```python
class SessionBackend:
    def is_healthy(self, session_id: str) -> bool: ...
    def attach_foreground(self, session_id: str) -> None: ...
    def launch_independent_terminal(self, command: list[str]) -> None: ...
```

第一阶段可以不完整引入抽象类，但至少把决策函数做成纯函数，输入状态，输出 action。TUI 只负责展示和执行 action。

严格边界：

- resolver 只能判断状态。
- decision function 只能把 state 映射为 action。
- TUI handler 才能执行副作用，例如调用 graph、attach、启动 CLI、显示提示或修改 DB。
- `request_story_progress()` 这类有副作用的函数不应出现在 resolver 或 decision function 内部，只能在 `r` handler 收到 `START_OR_RESUME` 后显式调用。

## 后续增强

- 增加显式 force retry/reopen 操作，例如 `Shift+R`，用于 completed/failed story 的人工重跑。该操作必须先确认并清理相关 `.done`，不能由普通 `r` 隐式触发。
- 对独立终端启动增加轻量级 PID/lock/heartbeat 文件，帮助 TUI 区分“还在运行但无 session”和“进程已死且无 `.done`”。
- 在 TUI 提示中给出明确下一步，例如“Stage 已完成，按 `r` 继续推进”或“当前没有运行中的 session，按 `r` 启动执行”。
