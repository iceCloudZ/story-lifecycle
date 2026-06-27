# agent-transcript-miner

把多个 coding agent（Claude Code / Codex CLI / Kimi Code …）的本地对话 transcript 归一化进 SQLite，做行为分析：工作量、工具使用、学习曲线、约束库、技术债务、知识蒸馏、失败模式、阶段成本。

## 架构

- **adapter 模式**：每端一个 adapter（`miner/adapters/`），`@register_adapter` 自动注册，统一映射到 events schema。store 与分析层不感知具体端。
- **加新端**（Cursor / Windsurf / Gemini CLI …）：在 `miner/adapters/` 丢一个 `<name>.py`（实现 `SourceAdapter`）+ `adapters/__init__.py` 加一行 `from . import <name>`。零改其他代码。
- **统一 schema**：`sessions` + `events`(kind: ucmd/atext/tool/result/code/think) + `events_fts`(FTS5)。
- **桥接（规划中）**：`story_ingest` 读各工作区 `.story/` → `stories` 维度表，按 cwd+branch+时间窗关联 session，做阶段成本画像。

## 数据源（全局用户目录，本仓库不含）

| 端 | 位置 |
|---|---|
| Claude | `~/.claude/projects/<编码路径>/*.jsonl` |
| Codex | `~/.codex/sessions/` |
| Kimi | `~/.kimi-code/sessions/wd_<cwd>_<hash>/.../wire.jsonl` |

## 配置

编辑 `config.json`：`db_path`、`workspaces`（决定扫哪些工作区）。

## 运行

```bash
python -m miner.store                 # 增量入库（按文件 mtime）
PYTHONPATH=. python scripts/explore.py  # 探索分析
```

adapter 代码变更需删 `data/transcripts.db` 重建（约 40s）。

## 🔴 红线

`data/*.db` 含真实开发对话（含金融 PII），**绝不入 git**（已 gitignore）。开源的只有代码，数据留本地。
