> ⚠️ **历史快照（归档于 2026-07）**：描述的架构可能已被后续演进取代。当前架构见 [../../ARCHITECTURE.md](../../ARCHITECTURE.md)。本文件保留作决策记录（ADR），正文未修改。

---

# Foreground Zellij Execution Design

日期：2026-05-24

## 背景

`design-terminal-entry-lifecycle.md` 已经明确：

- `e` 是观察入口，只 attach 已存在的 session。
- `r` 和创建 story 是执行入口。
- `.done` 是最高优先级完成信号。
- Windows 下不能用后台 `zellij attach --create-background` 创建执行 pane。

当前实现为了避开 Windows + Zellij 的空 pane 问题，在没有健康 session 时退回到独立终端启动 Claude。这可以作为 fallback，但不应成为有 Zellij 环境下的主路径。

前面排查 Zellij 的结论是：

- 问题不在 `zellij attach` 本身。
- 问题在后台创建 session/pane 时没有可靠 ConPTY。
- 前台真实终端 attach/create Zellij 是可用方向。

因此需要新增一个 **foreground Zellij execution mode**：执行入口在需要启动 AI 时，让 TUI 退出 Textual，把真实终端交给 Zellij，并在 Zellij 的第一个 pane 里直接运行 Claude 启动脚本。

## 目标

在 Windows + Zellij 可用时：

- `n` 创建 story 后，优先在前台 Zellij session 中启动 Claude。
- `r` 恢复执行时，优先在前台 Zellij session 中启动 Claude。
- `e` 仍然只观察已有 session，不启动 Claude。
- 如果 `.done` 已存在，`r` 消费 `.done`，不启动 Claude。
- 如果 Zellij 不可用或前台 Zellij 启动失败，才退回当前独立终端 fallback。

## 非目标

- 不恢复后台 `zellij attach --create-background` 作为执行主路径。
- 不在 `e` 中启动 Claude 或注入 prompt。
- 不重写完整 session backend 抽象。
- 不解决所有 ttyd/web terminal 场景，先覆盖本地 TUI + Windows Zellij。

## 核心思路

执行入口不再让后台 graph 直接创建 Zellij session。Graph 仍负责渲染 prompt 和生成启动资产，但真正的前台 Zellij 启动由 TUI 在 Textual 退出后执行。

最小闭环：

1. graph/render tool 准备 prompt 文件。
2. 生成 story 专属启动脚本。
3. 生成 story 专属 Zellij layout，layout 的 pane command 指向启动脚本。
4. 向 TUI 发出“需要前台执行”的 pending command。
5. TUI 退出 Textual。
6. TUI 在真实终端执行 foreground Zellij command。
7. 用户 detach/退出 Zellij 后回到 TUI。
8. graph 等 `.done`。

## 命令形态

推荐使用 layout 文件，而不是先创建 shell 再 send_keys。

PowerShell/Bash 启动脚本由现有 `ttyd.launch_cli()` 的脚本生成逻辑复用或轻微改造。

Zellij layout 示例：

```kdl
layout {
    pane command="bash" {
        args "C:/Users/<user>/AppData/Local/Temp/story-launch-1065520.sh"
    }
}
```

前台命令：

```powershell
zellij --session s-1065520 --new-session-with-layout C:\Users\<user>\AppData\Local\Temp\story-zellij-1065520.kdl
```

如果 session 已存在且健康，可以继续 attach：

```powershell
zellij attach s-1065520
```

但启动执行时推荐创建新的 execution layout，避免向不确定 pane 注入 prompt。

## TUI 与 Graph 协作

现有 `run_tui()` 已支持 `_pending_attach_args`：

- TUI action 设置 pending args。
- `app.run()` 返回。
- `run_tui()` 执行 command。
- command 返回后重新启动 TUI。

foreground Zellij execution 可以复用这条机制，但 pending command 不只来自 `e`，也可以来自 `r` 或新建 story 后的执行请求。

建议新增：

```python
app._pending_terminal_args: list[str] | None
```

或复用 `_pending_attach_args` 并重命名，避免它只表达 attach。

## 最小实现方案

### 1. terminal/ttyd.py

新增函数：

```python
def zellij_execution_args(story_key: str, workspace: str, launch_cmd: str, prompt_file: str) -> list[str]:
    ...
```

职责：

- 生成 `story-launch-{story_key}.sh`。
- 生成 `story-zellij-{story_key}.kdl`。
- 返回 foreground Zellij argv。

注意：

- 不调用 `subprocess.run()`。
- 不创建后台 session。
- 只返回命令参数。

### 2. BaseTool._launch_in_session()

当前 fallback 独立终端逻辑保留，但调整优先级：

1. 如果已有健康 session：可以继续注入或 attach 观察。
2. 如果 Windows + Zellij 可用：不要直接 `launch_cli()`；改为登记 foreground execution request。
3. 如果没有 Zellij 或无法登记请求：才 `launch_cli()`。

这里有一个现实约束：`BaseTool` 在 background worker 中运行，不能直接让当前 TUI 退出。因此需要一个跨线程/进程的 request bus。

最小方式：

- 仿照现有 `emit_terminal_opened()`，新增 `emit_terminal_request(story_key, args)`。
- TUI tick/watchdog 读取 request 后，设置 pending args 并退出。

### 3. orchestrator/graph.py

新增内存状态 bus：

```python
_terminal_requests: dict[str, list[str]]

def emit_terminal_request(story_key: str, args: list[str]) -> None: ...
def take_terminal_request(story_key: str) -> list[str] | None: ...
```

这和现有 `take_terminal_opened()` 风格一致，最小改动。

### 4. TUI watchdog/tick

在 TUI 定时刷新中检查当前 story 或所有 active story 的 terminal request：

```python
args = take_terminal_request(story_key)
if args:
    self._pending_attach_args = args
    self.exit()
```

`run_tui()` 已经会在 `app.run()` 返回后执行 pending args。

### 5. `r` 行为

`r` 仍只负责启动 graph：

- 如果 `.done` OK：graph 消费。
- 如果无 `.done`：graph 执行 stage。
- graph 需要启动 Claude 时发出 terminal request。
- TUI 收到 request 后进入 foreground Zellij。

`r` 不直接构造 Zellij 命令，避免 TUI 和 graph 双重实现 prompt 渲染。

## 状态行为

| 场景 | 行为 |
| --- | --- |
| `e` + 无 session | 提示按 `r`，不创建 session |
| `e` + 有健康 session | attach |
| `r` + `.done` OK | graph 消费 `.done`，不启动 Zellij/Claude |
| `r` + 无 `.done` + Windows + Zellij | graph 发 foreground Zellij execution request，TUI 前台执行 |
| `r` + 无 `.done` + 无 Zellij | 独立终端 fallback |
| graph 后台执行但 TUI 不在线 | 独立终端 fallback 或记录 pending request，待 TUI 启动后接管；P0 可先 fallback |

## 测试建议

单元测试：

- `zellij_execution_args()` 生成 layout 和 argv，不调用 subprocess。
- `BaseTool._launch_in_session()` 在 Windows + Zellij 可用且无 session 时发 terminal request，不调用 `launch_cli()`。
- 无 Zellij 时仍调用 `launch_cli()` fallback。
- TUI 收到 terminal request 后设置 pending args 并 exit。

手动测试：

1. 清理 Zellij session。
2. `story board`。
3. 选中 story，按 `e`，确认只提示无 session。
4. 按 `r`，确认 TUI 退出并进入 Zellij。
5. Zellij pane 内启动 Claude，并能看到 prompt 已传入。
6. detach 后回到 TUI。
7. Claude 写 `.done` 后，`r` 消费 `.done`，不重复启动 Claude。

## 风险

- TUI 不在线时，graph 无法交出当前真实终端。P0 可以退回独立终端 fallback。
- Zellij layout command quoting 在 Windows/Git Bash 下需要手动验证。
- 如果用户已有同名 session，`--new-session-with-layout` 可能失败或 resurrect 旧 session。P0 可以先要求执行前清理同名 exited session，后续再做自动清理。

