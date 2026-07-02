---
name: run-story-serve
description: 启动/重启/停止 story-lifecycle 后台服务（Story Lifecycle orchestrator，FastAPI+uvicorn，127.0.0.1:8180）。Use when user asks to 启动/重启/停掉/看状态 of 后台服务/orchestrator/story serve/8180，或服务/UI 不对要排查。NOT for 跑测试或构建前端（除非显式要 web 面板 story board）。
---

# Run Story Serve — 启动后台服务

story-lifecycle 的后台服务 = Story Lifecycle orchestrator（FastAPI + uvicorn），默认 `127.0.0.1:8180`。

## 启动（必须用 editable 环境）

```bash
.venv-monorepo-test/Scripts/python.exe -m story_lifecycle serve > .story-serve.out.log 2> .story-serve.err.log
```

后台运行（detached）。**必须用 `.venv-monorepo-test`** —— 它是 editable 安装，加载 `packages/story-lifecycle/src` 最新源码 + 最新 `web/` 前端。

### 环境陷阱（勿踩，本次会话已逐一踩过）
- **勿用全局 `py` / `python`**：全局装的是 site-packages **独立拷贝**，其 `web/` 是旧构建 → UI 变成旧版本/颜色不对。
- **勿用 `.venv`**：没装 `story_lifecycle`（`No module named story_lifecycle`）。
- 仓库根的 `story` 是**目录**不是入口脚本。
- 探测哪个 env 装了包：`<venv>/Scripts/python.exe -c "import story_lifecycle; print(story_lifecycle.__file__)"` —— 指向 `packages/.../src` 才是 editable（对），指向 `site-packages` 是拷贝（旧，勿用）。

## 健康检查

```bash
curl http://127.0.0.1:8180/api/session/health   # → 200 {"status":"ok","version":"0.1.0"}
```

**勿用 `/`、`/health`、`/healthz`**：返回 500 是 SPA 兜底路由（`api.py` 末尾 `/{path:path}`）误报 —— web 前端未构建时这些路径必 500，不代表服务挂。真实接口都在 `/api/*` 下。

## 重启 / 停止

```bash
netstat -ano | grep ':8180.*LISTENING'     # 找 PID
taskkill //F //PID <pid>                    # 停（Git Bash 双斜杠）
# 再用上面「启动」命令重启
```

**改 `~/.story-lifecycle/config.yaml` 的 `api_key` 后必须重启 serve**（key 启动时 load 进环境变量）；改 `tapd` 段无需重启（`_load_tapd_config` 每次请求重读）。

## 桌面双击启动脚本（.bat）

给用户一个双击即启动的 `启动story-serve.bat`（放桌面）。进程归用户自己的 shell，不归 Claude → 不会被会话清理 kill（Claude 后台 task 启的 serve 会被反复停）。完整模板：

```bat
@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
title Story Lifecycle Serve - 127.0.0.1:8180
cd /d D:\github\story-lifecycle
REM stop any existing serve on 8180, then start fresh
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8180.*LISTENING"') do (
  echo [!] Stopping existing serve ^(PID %%P^) on port 8180...
  taskkill /F /PID %%P >nul 2>&1
  goto :killed
)
goto :start
:killed
ping -n 3 127.0.0.1 >nul
:start
.venv-monorepo-test\Scripts\python.exe -m story_lifecycle serve
pause >nul
```

### .bat 陷阱（务必，本会话踩过）
- **`for /f ... do (` 或 `if (...)` 块内的 `echo` 若含圆括号 `()`，必须转义成 `^( ^)`** —— 否则 `)` 被当成块的结束符，报 `on was unexpected at this time` 并闪退（本会话 `(PID %%P)` 未转义 → 启动即崩）。
- 用 `ping -n 3 127.0.0.1 >nul` 代替 `timeout /t 2`（更通用，避免个别环境的 timeout 冲突）。
- `chcp 65001` + `PYTHONUTF8=1` + `PYTHONIOENCODING=utf-8` 确保窗口里中文日志正常显示。
- 顶层加 for 循环 kill 占 8180 的旧 PID，避免"端口占用"卡住。

## 常见故障速查
- deepseek 401（planner/PRD 生成失败、err.log 刷 `Planner failed: 401`）→ `config.yaml` 的 `api_key` 失效（曾为占位 `test`）。
- TAPD 接口 404 `source story not found` → `config.yaml` 缺 `tapd.workspace_id`（本项目 = `44381896`，从 story_key 长ID `11`+ws+9位 反推）。
- design stage 卡 `blocked` + `HeadlessNoDoneFile` → 多半是上游 LLM/planner 401 的连带症状，先修 key。
- 详见 memory：`story-serve-backend`、`tapd-deepseek-config-gaps`。
