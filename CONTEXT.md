# CONTEXT — agent-transcript-miner

> 给接手的 agent 看：上手这个项目需要知道的一切。**先读这个，不用重新摸索。**

## 这是什么
把多个 coding agent（Claude Code / Codex / Kimi）的本地对话 transcript 归一化进 SQLite，做行为分析（工作量/工具/学习曲线/约束/债务/蒸馏/失败/阶段成本），并反哺业务项目（hc-all）的任务上下文索引（playbooks）。数据源是全局用户目录，本仓库只含代码。

## 快速上手
```bash
export PYTHONPATH=D:/github/agent-transcript-miner
python -m miner.store                    # 增量入库（按 mtime；adapter 改了要删 data/transcripts.db 重建）
python -m miner.story_ingest             # 解析各工作区 .story/ → stories 表
python -m miner.link                     # session↔story 关联（回填 sessions.story_id）
python scripts/generate_playbooks.py     # 生成 hc-all/.story/knowledge/playbooks/
python scripts/recommend.py "免息 清分"   # 智能推荐：关键词→相关会话+必看文件+playbook
python scripts/explore.py                # 探索性查询（三端对比/失败/任务分片/工作节律）
# 其他分析：workload / debt / constraint / toolopt / learn / distill / retrospect / predict / tri_efficiency / failure_mode
```

## 目录结构
```
miner/
  config.py        读 config.json → DB_PATH/WORKSPACES/CLAUDE_ENCODINGS
  common.py        统一 schema 契约 + mask/ws_of/real_user（含 INJECT_PREFIX 过滤注入）
  base.py          SourceAdapter 基类 + REGISTRY + @register_adapter
  store.py         入库（SQLite+FTS5+mtime 增量）
  story_ingest.py  解析 .story/ → stories 表
  link.py          session↔story 关联
  adapters/        claude/codex/kimi（加新端丢文件 + __init__ 加一行）
scripts/           分析脚本（只读 db，写 scripts/out/*.md 或反哺 hc-all）
data/transcripts.db  本地库（gitignore，含 PII）
config.json        db_path + workspaces
```

## db schema（data/transcripts.db）
- **sessions**(sid PK, src, ws, ts, title, turns, ntools, nerrs, cwd, branch, first_ucmd, path, story_id)
- **events**(id PK, sid, src, ws, ts, kind, name, cmd, code, ok, text, path)
  - kind: `ucmd`(用户指令) / `atext`(助手文本) / `tool`(工具调用) / `result`(工具结果,ok) / `code`(写入diff) / `think`
  - tool 的 `path`：Read/Write/Edit/Glob 目标文件；`cmd`：Bash 命令 / Grep pattern / mcp 输入
- **stories**(story_id, workspace, title, status, stage, spec_path, first_ts, last_ts, ...)
- **events_fts**(FTS5 on text/code/cmd)　**sources**(path PK, mtime, size, ...) 增量用

⚠️ **列值可信度**：turns 准；**ntools/nerrs 不可靠（Codex/Kimi）→ 分析一律用 events 重算，不用列值**。

## config.json
```json
{ "db_path": "data/transcripts.db", "workspaces": ["D:/hc-all","D:/java-agent","D:/github"] }
```
workspaces 决定 Claude adapter 扫哪些 projects 编码目录 + story_ingest 扫哪些 .story/。

## 加新端（adapter）
1. `miner/adapters/<name>.py` 写 SourceAdapter 子类（name/discover()/parse()），`@register_adapter`
2. `adapters/__init__.py` 加 `from . import <name>`
3. parse() 把本端记录映射到统一 event schema（见 common.py 顶部契约）
4. 删 `data/transcripts.db` 重 ingest

## story 桥接（连 story-lifecycle）
- story-lifecycle 状态是文件式（在各工作区 `.story/`）：
  - `<ws>/.story/context/<id>/` 进行中（阶段 gate 文件，用 mtime 切 design/build/verify）
  - `<ws>/.story/done/<id>/` 完成（design.json: spec_path, complexity）
  - `<ws>/spec/<id>-design.md` 设计文档
- **关联键**：cwd + ts 时间窗为主（branch 基本失效，story-id 稀疏）；story_id 关联率 ~18%（真实——多数会话是日常维护无 story 信号）
- stories.title 可能乱码（GBK 存储），generate_playbooks 有 `clean_title` 修复

## 反哺 hc-all（playbooks）
- 产出在 `hc-all/.story/knowledge/playbooks/`：7 个 task playbook（requirement-dev/debug/sms-marketing/deploy/data-sql/credit-risk/frontend）+ by-story/4 个 + INDEX
- 已接入 4 个 hc-all skill（env-debug→debug、backend-dev→requirement-dev 强制；sql-query→data-sql、deploy-test→deploy 可选）
- generate_playbooks.py：short() 规整路径（保留驼峰/服务前缀）、角色推断（Controller/ServiceImpl/Processor…）
- ⑩b（story-lifecycle prompt 动态注入 transcript 上下文）在另一窗口做，依赖 sessions.story_id

## 红线 🔴
- `data/*.db` 含 hc-all 真实对话（金融 PII），**绝不入 git**（已 gitignore）。开源只代码。
- `mask()` 只覆盖手机号/邮箱/长数字，导出/蒸馏前必须人工复核。

## 已知坑（多已修）
- ntools/nerrs 列值不可靠 → events 重算（仍未修列值，分析端绕开）
- ✅ Codex ok 曾误标成功为失败（已按 exit code 修）／ ✅ Claude nerrs 曾高估（已改仅 is_error）／ ✅ first_ucmd 曾被 AGENTS.md 注入污染（已加 INJECT_PREFIX）
- hc-all/.claude/skills 是 .agents 的 junction：改 skill 改 `.agents` 那份；**禁跑同步脚本**；Grep 跨 junction 丢匹配（用直接路径）

## 相关
- 架构/方向/决策详见 memory：`project-transcript-mining-arch`
- 对外说明见 README.md
