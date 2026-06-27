# Windows 下 Zellij attach 卡住排查总结

日期：2026-05-24

## 背景

在 `story` TUI 中按 `e` 进入 AI session 时，预期行为是：

- 当前 TUI 让出同一个终端窗口。
- 在同一个终端里执行 `zellij attach <session>`。
- detach/退出 zellij 后回到 `story` TUI。
- 不新开窗口。

实际现象：

- 按 `e` 后 TUI 退出或消失。
- 终端停在类似 `PS D:\hc-all> story` 的位置。
- 输入字符不显示。
- 按 `Ctrl+C` 后回到 PowerShell prompt。
- 手动执行 `zellij attach s-1065520` 也没有画面。

## 排查过程

### 1. 确认不是按键绑定问题

代码中 `e` 绑定到 `StoryBoardApp.action_enter_terminal()`。

初始逻辑：

- 有 multiplexer 时，创建/检测 session。
- 调用 `zellij attach <session>`。
- 没有 multiplexer 时才走直接启动 CLI 的 fallback。

说明按键确实进入了正确入口。

### 2. 加 TUI 诊断日志

新增日志文件：

```powershell
$HOME\.story-lifecycle\tui.log
```

关键日志点包括：

- `enter_terminal_start`
- `enter_terminal_session_check`
- `enter_terminal_session_after_create`
- `enter_terminal_attach_args`
- `enter_terminal_defer_windows`
- `run_tui_app_return`
- `run_tui_attach_start`
- `run_tui_attach_stdio`
- `run_tui_attach_return`
- `run_tui_attach_exception`
- `prepare_terminal_windows_modes`

日志确认：

```text
run_tui_app_return attach_args=['zellij', 'attach', 's-1065520']
run_tui_attach_start args=['zellij', 'attach', 's-1065520']
run_tui_attach_stdio stdin_isatty=True stdout_isatty=True stderr_isatty=True
```

结论：

- `story` TUI 已经退出。
- `zellij attach s-1065520` 已经被启动。
- stdin/stdout/stderr 都是真 TTY。
- 卡住点在 zellij attach 进程内部，不是 TUI 按键链路。

### 3. 排除 Textual suspend 半释放问题

尝试过：

- 避免 `os.system()`，改成 `subprocess.run([...])`。
- Windows 下不在 Textual `suspend()` 生命周期中启动 zellij。
- 改为先退出 `app.run()`，再执行 `zellij attach`。
- attach 前显式重置 Windows console mode。
- attach 前输出退出 alt screen、显示 cursor、关闭 mouse/bracketed paste 等控制序列。

这些修复能保证终端交给子进程，但实际仍然卡住。

### 4. 验证 zellij session/server 是否响应

以下命令曾经卡住或超时：

```powershell
zellij --session s-1065520 action list-clients
zellij --session s-1065520 action list-panes --json
zellij --session s-1065520 action dump-screen
zellij watch s-1065520
```

这些命令是普通 zellij 查询/观察命令，不应该依赖 TUI 代码。因此说明 zellij session/server 本身已经不健康。

### 5. 查看 Zellij 自己的日志

日志位置：

```powershell
$env:TEMP\zellij\zellij-log\zellij.log
```

发现关键错误：

```text
failed to set terminal 0 to size (...)
no ConPTY terminal found for id 0
Failed to run command: program not found
BrokenPipe
Client sent over 1000 consecutive unknown messages
```

结论：

- zellij 的 Windows session 状态损坏或残留。
- 服务器侧找不到对应 ConPTY。
- 默认 shell/command 也有 `program not found` 问题。
- 这解释了为什么 `attach` 进程运行中但没有画面。

## 已执行的恢复操作

### 1. 停止残留 zellij 进程

```powershell
Get-Process zellij -ErrorAction SilentlyContinue | Stop-Process -Force
```

### 2. 清理 stale session id 文件

清理位置：

```powershell
$env:TEMP\zellij\contract_version_1\s-1065520
$env:TEMP\zellij\contract_version_1\s-123
```

这些文件导致 `zellij list-sessions` 仍认为 session 存在，即使实际进程已经异常。

### 3. 重新创建 session

```powershell
zellij attach --create-background s-1065520 options --default-cwd D:\story-lifecycle --default-shell powershell.exe
```

### 4. 验证 session 恢复

```powershell
zellij --session s-1065520 action list-panes --json
```

该命令已经能正常返回 pane 列表，说明 zellij server/session 至少恢复到可查询状态。

## 代码改动

### `src/story_lifecycle/terminal/ttyd.py`

1. `create_session()` 的 zellij 分支从 `os.system()` 字符串改成 `_run([...])` 参数数组，避免 shell 解析问题。

2. Windows 下创建 zellij session 时显式传：

```text
--default-shell powershell.exe
```

避免 Zellij 日志里的：

```text
Failed to run command: program not found
```

3. 新增：

```python
attach_args(name: str) -> list[str]
```

用于无 shell 启动 attach。

### `src/story_lifecycle/cli/tui.py`

1. Windows 下按 `e` 时，不在 Textual `suspend()` 内直接启动 zellij。

2. 改为：

- 记录 `_pending_attach_args`
- `self.exit()`
- `run_tui()` 在 `app.run()` 返回后执行 attach
- attach 返回后重新启动 TUI

3. 新增 `_tui_debug()`，写入：

```powershell
$HOME\.story-lifecycle\tui.log
```

4. attach 前新增 `_prepare_terminal_for_child()`，用于重置 Windows console mode 和终端控制序列。

## 验证命令

已通过：

```powershell
python -m pytest tests\test_terminal_multiplexer.py -vv
ruff check src tests\test_terminal_multiplexer.py
```

测试结果：

```text
4 passed
All checks passed
```

## 当前判断

根因不是 `story` 的按键绑定，也不是 Textual 没调用到 attach。

更准确的根因是：

> Windows 下 Zellij session/server 残留了损坏状态，日志显示 `no ConPTY terminal found for id 0`，导致 `zellij attach` 进程启动后接管终端但不渲染画面。

代码侧应保留：

- zellij 参数数组调用。
- Windows 创建 session 时指定 `--default-shell powershell.exe`。
- TUI 诊断日志。
- Windows 下退出 Textual 后再 attach 的流程。

环境侧如果复发，可以按以下顺序恢复：

```powershell
Get-Process zellij -ErrorAction SilentlyContinue | Stop-Process -Force
Remove-Item -Force $env:TEMP\zellij\contract_version_1\s-1065520 -ErrorAction SilentlyContinue
Remove-Item -Force $env:TEMP\zellij\contract_version_1\s-123 -ErrorAction SilentlyContinue
zellij attach --create-background s-1065520 options --default-cwd D:\story-lifecycle --default-shell powershell.exe
zellij --session s-1065520 action list-panes --json
```
