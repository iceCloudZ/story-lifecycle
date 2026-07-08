# PTY WebSocket 重连问题分析与产品设计

## 背景

Web Board 的 Terminal Tab 用于观察 Story 对应的 AI Agent CLI 会话（Claude / Codex / Kimi）。前端通过 WebSocket 连接到后端的 PTY 进程，实时展示终端输出。

生产环境中出现大量如下日志刷屏：

```text
INFO:     127.0.0.1:59604 - "WebSocket /ws/pty/1066915/pty-1066915-2" [accepted]
INFO:     connection open
INFO:     connection closed
INFO:     127.0.0.1:63793 - "WebSocket /ws/pty/1066915/pty-1066915-2" [accepted]
INFO:     connection open
INFO:     connection closed
...
```

同一 session 每秒被多次重连，端口不断变化，说明前端在**无意义地反复连接一个已经结束的 PTY 会话**。

---

## 问题定位

### 1. 后端：没有区分“会话不存在”和“进程已结束”

`packages/story-lifecycle/src/story_lifecycle/orchestrator/service/api.py` 中的 `_pty_ws_handler`：

- 当 `get_pty()` 返回 `None` 时返回 `4044`；
- 当 `pty` 对象存在但 `pty.alive == False` 时，仍然 `accept()`，随后 `read_and_send` 立即退出并发送 `{"type":"exit"}`，连接关闭。

后端没有把“进程已正常退出”作为一个明确的终态告诉前端。

### 2. 前端：任何关闭都无条件 3 秒重连

`packages/story-lifecycle/frontend/src/components/TerminalPanel.tsx`：

```javascript
ws.onclose = () => {
  setWsStatus('disconnected')
  if (spawned) {
    reconnectTimerRef.current = setTimeout(() => connectWsRef.current(), 3000)
  }
}
```

只要 `spawned === true`，任何 close（包括后端主动发送 exit 后的正常关闭）都会触发重连。

### 3. TerminalTab 会把已退出 session 自动选出来

`TerminalTab.tsx`：

```javascript
(sessions.find((s) => s.status === 'running') || sessions[0]).session_id
```

没有 running session 时，自动选择列表中的第一个 session，即使它已退出。这导致 TerminalPanel 不断尝试连接 dead session。

---

## 产品定位

PTY 终端在 story-lifecycle 中的定位是：

> **AI Agent 的可观测终端，不是通用 shell，也不是主交互界面。**

用户的主流程是通过结构化 UI（计划、审查、发现、状态）与 Agent 交互；Terminal Tab 只是让用户“看 Agent 在命令行里做了什么”的辅助调试窗口。

因此：

- Agent 活着时，终端应该能自动恢复（网络闪断、页面切换）；
- Agent 已退出时，终端应该明确显示“进程已结束”，而不是假装还在连；
- 找不到 session 时，应该提示用户新建，而不是无限重试。

---

## 状态机设计

PTY 连接应该使用显式状态机，而不是 `spawned` 一个布尔值：

```text
connecting → streaming → reconnecting → exited / not_found
```

| 状态 | 含义 | 前端行为 |
|---|---|---|
| `connecting` | 正在建立 WebSocket | 显示“连接中…” |
| `streaming` | 正常收发数据 | 正常显示终端输出 |
| `reconnecting` | 网络闪断，PTY 应仍存活 | 指数退避重连，显示“重新连接中…” |
| `exited` | PTY 进程已正常结束 | 停止重连，显示“进程已退出”，提供“启动新终端” |
| `not_found` | session 不存在 | 停止重连，显示“会话不存在”，提供新建入口 |

---

## 改动方案

### 后端（`api.py`）

在 `_pty_ws_handler` 中按 PTY 状态返回不同的 close code / 消息：

- `get_pty()` 返回 `None`：`4404`（session not found），不重连；
- `pty.alive == False`：发送 `{"type":"exit","reason":"process_ended"}` 后 close code `1000`（正常结束），不重连；
- `pty.alive == True`：正常建立双向流；
- 异常/服务器错误：`1011`，前端可指数退避重试。

### 前端（`TerminalPanel.tsx`）

1. 引入 `connectionState` 状态：`connecting | streaming | reconnecting | exited | not_found`。
2. 收到 `{"type":"exit"}` 或 close code 为 `1000/4404` 时进入 `exited`/`not_found`，停止重连。
3. 只有 transient close（如 `1011`、非干净关闭）才进入 `reconnecting`，使用指数退避 + 最大重试次数。
4. 在 UI 中显示对应状态徽章。

### 前端（`TerminalTab.tsx`）

- 没有 `running` session 时，不再自动选择第一个 session 连接；
- 显示“暂无活动会话”空状态，列出历史 session（标记为已退出）；
- 用户可手动点击历史 session 查看最终输出，或点击“新建”启动新会话。

---

## 参考

业界对 PTY / xterm.js WebSocket 重连的共识：

- Cloudflare Sandbox SDK：PTY 在客户端断开时保持运行，重连时回放缓冲输出；用 binary frame 传数据，JSON text frame 传控制消息。
- ttyd PR #1536：自动重连使用指数退避（1s → 2s → 4s…），并在 `visibilitychange` 时立即重连。
- Kilo Org cloud#1195：重连前先查询 session 状态，若 session 已消失则创建新 session；提供“Reconnecting…”视觉指示。
- Termly CLI：指数退避 0s/2s/4s/8s/16s，最大 10 次；移动端重连通过 `lastSeq` 做 catchup。

---

## 验收标准

1. 已退出 PTY session 不再触发无限重连，后端日志不再刷屏。
2. 网络闪断时，终端能在数秒内自动恢复，且用户看到“重新连接中…”状态。
3. 没有活动 session 时，Terminal Tab 显示空状态和新建按钮，不自动连接已退出 session。
4. 视觉状态栏能清晰区分：已连接 / 连接中 / 重新连接中 / 进程已退出 / 会话不存在。
