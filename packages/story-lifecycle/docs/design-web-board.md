# 设计文档：Web Board — 浏览器端 Story 管理与可视化

## 版本记录

| 版本 | 日期 | 说明 |
|---|---|---|
| v0.1 | 2026-06-02 | 初版，定方向与 Phase 划分 |

## 背景

`story board` 当前是 Textual TUI（2644 行），功能完整但受限于终端渲染能力：

- 无法渲染代码依赖关系图（React Flow 等）
- 终端交互门槛高，不利于演示和团队协作
- xterm.js 在浏览器中体验优于终端内嵌方案
- Windows 上 TUI 的终端模式切换（`_prepare_terminal_for_child`）是持续维护负担

**不放弃 TUI**。Web Board 作为 `story board --web` 模式新增，TUI 保持默认。等 Web 功能完全覆盖后自然退役。

## 目标

P0（MVP，跑通最小闭环）：

1. `story board --web` 启动 FastAPI + 静态前端，自动打开浏览器
2. 浏览器内显示 Story 列表 + 状态，通过 WebSocket 实时更新
3. 浏览器内嵌入 xterm.js 终端，可交互查看 AI agent 执行过程
4. 前端静态文件打包进 pip wheel，`pip install` 即可用

P1（可视化增强）：

5. React Flow 渲染 Story 的 stage 流转 DAG，实时变色
6. 文件变更事件映射到图谱节点高亮
7. 多 Story 并排视图

非目标：

- 不替换 Python 后端（不引入 Node.js runtime）
- 不重写 LangGraph 状态机
- 不做远程部署/多用户（单机本地工具）
- P0 不做 React Flow

## 架构

```
浏览器 (React + xterm.js)
    │ WebSocket 双向
    │
FastAPI (api.py，现有)
    ├── REST API（现有，不动）
    ├── WebSocket endpoint（新增）
    │     ├── /ws/stories — Story 状态推送
    │     └── /ws/pty/{story_id} — PTY 输入输出流
    │
    ├── PTY 管理层（新增，替换 ttyd.py 的 tmux 依赖）
    │     └── pywinpty (Win) / pty (Unix)
    │
    └── StaticFiles（新增，serve 前端构建产物）
          └── src/story_lifecycle/web/
```

关键决策：

1. **前端只是构建时依赖**。开发时需要 Node.js，运行时纯 Python。
2. **PTY 输出通过 WebSocket 推送**，前端 xterm.js 渲染 ANSI 码。后端用 `asyncio.Queue` + `run_in_executor` 做流控。
3. **状态推送复用现有 `_status_lock` 机制**。`graph.py` 里线程安全的 status bus 已经存在，WS endpoint 订阅它即可。

## 与现有代码的关系

| 现有模块 | 变更 |
|---|---|
| `orchestrator/api.py` | 新增 WS endpoints、StaticFiles mount |
| `orchestrator/graph.py` | 新增 WS 状态广播（从 `_status_lock` 推送到 WS clients） |
| `terminal/ttyd.py` | 新增 `terminal/pty.py` 作为 Web 模式的 PTY 管理，ttyd 保留给 TUI |
| `cli/main.py` | `_run_board()` 增加 `--web` 分支 |
| `pyproject.toml` | 新增 `pywinpty` 依赖，`artifacts` 加入 `web/**` |
| `.github/workflows/ci.yml` | 新增前端构建步骤 |
| `.github/workflows/release.yml` | 发布前构建前端 |
| `src/story_lifecycle/web/` | 新增目录，存放前端构建产物 |

## Phase 划分

### Phase 1：打通血管（FastAPI + WebSocket）

预计 2-3 天。

**改动清单：**

- `api.py`：新增 `/ws/stories` WebSocket endpoint
- `graph.py`：在状态变更时（`advance_node`、`retry_node` 等）广播到 WS clients
- `cli/main.py`：`--web` flag 走新的启动路径，自动开浏览器
- 前端：极简 HTML 页面（手写，不用 React），连 WS，显示 Story 列表 JSON

**完成标志：** 浏览器能看到 Story 状态实时更新。

### Phase 2：前端框架 + xterm.js

预计 7-10 天（含 React 学习曲线）。

**改动清单：**

- 新建 `frontend/` 目录：Vite + React + TypeScript 项目
- 组件：StoryList、StoryCard、Terminal（xterm.js）
- 连接 `/ws/stories` 渲染列表
- 连接 `/ws/pty/{story_id}` 渲染终端
- 构建脚本：`npm run build` → 产物拷贝到 `src/story_lifecycle/web/`

**完成标志：** 浏览器里能看到 Story 列表 + 可交互的 AI 终端。

### Phase 3：PTY 管理层（替换 tmux）

预计 3-5 天。

**改动清单：**

- 新增 `terminal/pty.py`：基于 `pywinpty`（Win）/ `pty`（Unix）的进程管理
- `api.py`：`/ws/pty/{story_id}` 读取 PTY 输出，通过 WS 推送
- PTY 输入：前端 xterm.js 键盘事件 → WS → 后端写入 PTY stdin
- 进程清理：注册 `atexit` 清理，防止僵尸 PTY

**完成标志：** Web 模式下 AI agent 在浏览器终端中完整运行。

### Phase 4：React Flow 可视化

预计 1-2 周。

**改动清单：**

- 引入 React Flow
- 从 `/api/stories/{key}/profile` 获取 stage DAG 定义
- Story 状态变更映射为节点颜色
- 文件变更事件（来自 Handoff JSON）映射为节点高亮

**完成标志：** 浏览器中看到 Story stage 流转的交互式 DAG。

## CI 变更

### ci.yml 新增前端构建检查

```yaml
  frontend-build:
    name: Frontend build check
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: '20'
      - name: Install & build frontend
        run: |
          cd frontend
          npm ci
          npm run build
      - name: Verify output
        run: test -f src/story_lifecycle/web/index.html || echo "warn: web/ not populated (expected on non-release)"
```

### release.yml 新增前端构建步骤

在 `build` job 的 `pipx run build` 之前加入：

```yaml
      - uses: actions/setup-node@v4
        with:
          node-version: '20'
      - name: Build frontend
        run: |
          cd frontend
          npm ci
          npm run build
          cp -r dist/* ../src/story_lifecycle/web/
```

确保 wheel 打包时 `web/` 目录已填充。

## PTY 输出流控设计

```python
# terminal/pty.py 核心逻辑

import asyncio
from typing import Optional

_pyt_registry: dict[str, "ManagedPty"] = {}


class ManagedPty:
    def __init__(self, story_id: str, command: list[str], cwd: str):
        self.story_id = story_id
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
        self._process = self._spawn(command, cwd)

    def _spawn(self, command: list[str], cwd: str):
        if sys.platform == "win32":
            import winpty
            return winpty.PTY(len(command), command, cwd=cwd)
        else:
            import pty as _pty
            # Unix PTY spawn logic
            ...

    async def read_loop(self):
        """阻塞读取 PTY，放入异步队列。"""
        loop = asyncio.get_event_loop()
        while True:
            data = await loop.run_in_executor(None, self._process.read, 4096)
            if not data:
                break
            await self._queue.put(data)

    async def write(self, data: bytes):
        self._process.write(data)

    def kill(self):
        self._process.close()
```

```python
# api.py WS endpoint

@app.websocket("/ws/pty/{story_id}")
async def pty_ws(ws: WebSocket, story_id: str):
    await ws.accept()
    pty = get_pty(story_id)
    if not pty:
        await ws.close(code=4044)
        return

    async def read_and_send():
        while True:
            data = await pty._queue.get()
            await ws.send_bytes(data)

    async def recv_and_write():
        while True:
            data = await ws.receive_bytes()
            await pty.write(data)

    try:
        await asyncio.gather(read_and_send(), recv_and_write())
    except Exception:
        pass
    finally:
        # 不 kill PTY，story 可能还在跑
        pass
```

## 目录结构

```
story-lifecycle/
├── frontend/                     # 前端源码（开发时用，不打包进 wheel）
│   ├── package.json
│   ├── vite.config.ts
│   ├── tsconfig.json
│   └── src/
│       ├── App.tsx
│       ├── components/
│       │   ├── StoryList.tsx
│       │   ├── StoryCard.tsx
│       │   ├── Terminal.tsx      # xterm.js 封装
│       │   └── StageGraph.tsx    # Phase 4: React Flow
│       └── hooks/
│           ├── useWebSocket.ts
│           └── useStories.ts
│
├── src/story_lifecycle/
│   ├── web/                      # 前端构建产物（gitignore，打包进 wheel）
│   │   └── index.html            # + assets/
│   ├── terminal/
│   │   ├── ttyd.py               # 现有，TUI 继续用
│   │   └── pty.py                # 新增，Web 模式用
│   ├── orchestrator/
│   │   ├── api.py                # 改动：+WS、+StaticFiles
│   │   └── graph.py              # 改动：+WS 广播
│   └── cli/
│       └── main.py               # 改动：--web flag
│
├── .github/workflows/
│   ├── ci.yml                    # 改动：+frontend-build job
│   └── release.yml               # 改动：build 前 npm run build
│
├── .gitignore                    # 改动：+src/story_lifecycle/web/
├── pyproject.toml                # 改动：+pywinpty、+web/** artifact
└── Makefile / build.sh           # 新增：一键构建前后端
```

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| React 学习曲线 | Phase 1 用手写 HTML 验证通路，不急于上框架 |
| pywinpty 在某些 Windows 版本不稳定 | pywinpty 被 VS Code 验证过，且有纯文本 fallback |
| 前端构建产物膨胀 wheel 体积 | Vite 构建后通常 < 500KB，可接受 |
| WebSocket 断线 | 前端指数退避重连，后端无状态不丢数据 |
| TUI 和 Web 同时运行 | 共享 SQLite + 状态机，已有 thread-safe 机制 |
