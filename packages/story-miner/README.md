# story-miner

把多个 coding agent（Claude Code / Codex CLI / Kimi Code …）的本地对话 transcript 归一化进 SQLite，做行为分析：工作量、工具使用、学习曲线、约束库、技术债务、知识蒸馏、失败模式、阶段成本、自动复盘。

> 本包是 [`dev-flywheel`](https://github.com/iceCloudZ/story-lifecycle) monorepo 的一部分，与 [`packages/story-lifecycle`](../story-lifecycle) 共用统一知识飞轮。当前版本：**v0.12.0**。

## 架构

- **adapter 模式**：每端一个 adapter（`miner/adapters/`），`@register_adapter` 自动注册，统一映射到 events schema。store 与分析层不感知具体端。
- **加新端**（Cursor / Windsurf / Gemini CLI …）：在 `miner/adapters/` 丢一个 `<name>.py`（实现 `SourceAdapter`）+ `adapters/__init__.py` 加一行 `from . import <name>`。零改其他代码。详见下方「加新端 adapter」。
- **统一 schema**：`sessions` + `events`（kind: ucmd/atext/tool/result/code/think）+ `events_fts`(FTS5) + `stories`（来自 `.story/`）。详见下方「db schema」。
- **统一知识层**：`packages/knowledge/` 把 scenario / playbook / failure 合并到同一套 INDEX，供 `story-lifecycle` 与本包共用。

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

## 加新端 adapter

1. `miner/adapters/<name>.py` 写 `SourceAdapter` 子类（实现 `name` / `discover()` / `parse()`），`@register_adapter`
2. `adapters/__init__.py` 加 `from . import <name>`
3. `parse()` 把本端记录映射到统一 event schema（见 `common.py` 顶部契约）
4. 删 `data/transcripts.db` 重 ingest

## 数据源（全局用户目录，本仓库不含）

| 端 | 位置 |
|---|---|
| Claude | `~/.claude/projects/<编码路径>/*.jsonl` |
| Codex | `~/.codex/sessions/` |
| Kimi | `~/.kimi-code/sessions/wd_<cwd>_<hash>/.../wire.jsonl` |

## 配置

编辑 `config.json`：`db_path`、`workspaces`（决定扫哪些工作区）。

## 运行（在 monorepo venv 中）

```bash
cd ../..
source .venv-monorepo-test/Scripts/activate   # Windows Git Bash

python -m miner.store --since-days 1          # 增量入库（每日）
python -m miner.story_ingest                  # 刷新 .story/ -> stories 表
python -m miner.link                          # 刷新 session↔story 绑定
python scripts/generate_playbooks.py          # 重算 playbook
python scripts/failure_mode.py                # 重算失败模式
```

定时调度直接调用：

```bash
packages/story-miner/scripts/refresh.sh       # 每日增量
packages/story-miner/scripts/refresh.sh full  # 每周全量
```

## 与 story-lifecycle 的集成（I1–I4，v0.12.0+）

- **I1 定时扫描**：`scripts/refresh.sh` 提供每日增量/每周全量入口。
- **I2 精确绑定**：读取 `story-lifecycle` 在 `inject_prompt` 时写的 `.story/runs/<key>/anchors.jsonl`，用锚点精确回填 `sessions.story_id`；未命中者退回 `id-mention` / `branch-match` 启发式，已去掉旧宽窗兜底。
- **I3 上下文注入**：`miner.story_context_provider.TranscriptStoryContextProvider` 为 `story-lifecycle` 的 `{transcript_context}` 提供历史摘要。
- **I4 Done 复盘**：`story-lifecycle` 的 `story done <key>` 调用 `scripts/retrospect.py --story <key>` 生成合并复盘。

详见顶层 [`docs/INTEGRATION.md`](../docs/INTEGRATION.md)。

## 分析脚本

| 脚本 | 用途 | 输出 |
|---|---|---|
| `scripts/constraint.py` | 沉淀用户强制约束 | `docs/constraint-rules.md` |
| `scripts/debt.py` | 扫描 TODO/FIXME/HACK | `scripts/out/debt.md` |
| `scripts/failure_mode.py` | 高频失败模式分析 | `scripts/out/failure-knowledge.json` + `docs/failure-checklist.md` |
| `scripts/generate_playbooks.py` | 按任务类型生成经验手册 | `<workspace>/.story/knowledge/playbooks/` |
| `scripts/predict.py` | 工作量基线估算 | `scripts/out/effort-estimate.md` |
| `scripts/recommend.py` | 任务历史上下文推荐 | 列表 / `--package` 上下文包 |
| `scripts/retrospect.py` | 单会话 / 批量 / Story 级复盘 | `scripts/out/retrospect*.md` |
| `scripts/distill.py` | 轨迹蒸馏为 SFT 语料 | `data/distill/sft-*.jsonl` |

## 已知坑（待代码验证，源自历史 CONTEXT）

- ntools/nerrs 列值不可靠 → events 重算（分析端绕开，列值未修）
- Codex ok 曾误标成功为失败（已按 exit code 修）
- Claude nerrs 曾高估（已改仅 is_error）
- first_ucmd 曾被 AGENTS.md 注入污染（已加 INJECT_PREFIX 过滤）
- hc-all/.claude/skills 是 .agents 的 junction：改 skill 改 `.agents` 那份；禁跑同步脚本；Grep 跨 junction 丢匹配（用直接路径）
- stories.title 可能乱码（GBK 存储），`generate_playbooks` 有 `clean_title` 修复
- session↔story 关联键：cwd + ts 时间窗为主（branch 基本失效，story-id 稀疏）；story_id 关联率 ~18%（多数会话是日常维护无 story 信号）

## 🔴 红线

`data/*.db` 含真实开发对话（含金融 PII），**绝不入 git**（已 gitignore）。开源的只有代码，数据留本地。
