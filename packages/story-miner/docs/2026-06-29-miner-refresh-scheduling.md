# miner refresh 定时化

> 2026-06-29 · L（follow-up #2）。refresh 机制已测通；这里给定时化命令。

## refresh 已验证

`packages/story-miner/scripts/refresh.sh`（增量模式）跑通：
- ingest hc-all 新 session（本次 +8 session、+503 token_usage 行）。
- 重生成 per-task_type playbook（hc-all 9 个）。
- 增量（mtime），不全量重扫。

## 定时化命令（自己装，未自动装）

**Windows（计划任务，每天 03:00）：**
```bash
schtasks /create /tn "story-miner-refresh" /tr "cmd /c cd /d D:\github\story-lifecycle\packages\story-miner && bash scripts\refresh.sh >> logs\refresh.log 2>&1" /sc daily /st 03:00 /f
# 查看 stato:  schtasks /query /tn "story-miner-refresh"
# 删除:        schtasks /delete /tn "story-miner-refresh" /f
```

**Linux/macOS（cron，每天 03:00）：**
```cron
0 3 * * * cd /path/to/story-miner && bash scripts/refresh.sh >> logs/refresh.log 2>&1
```

## 已知限制（follow-up）

`refresh.sh` 扫的是**工作区 `.claude`**（hc-all/.claude）。**全自动 `claude -p` 执行器的 session 存在全局 `~/.claude/projects`**，不在工作区 → 现有 refresh **抓不到执行器 token**（G-run 的 session 就因此没进 token_usage）。

补法（later）：① miner 也扫 `~/.claude/projects` 里 cwd 匹配工作区的 session；或 ② headless adapter 自己记 claude -p usage 进 llm_trace/token_usage。
