# story-miner

把多个 coding agent（Claude Code / Codex CLI / Kimi Code …）的本地对话 transcript 归一化进 SQLite，做行为分析：工作量、工具使用、学习曲线、约束库、技术债务、知识蒸馏、失败模式、阶段成本、自动复盘。

> 本包是 [`dev-flywheel`](https://github.com/iceCloudZ/story-lifecycle) monorepo 的一部分，与 [`packages/story-lifecycle`](../story-lifecycle) 共用统一知识飞轮。当前版本：**v0.12.0**。

## 架构

- **adapter 模式**：每端一个 adapter（`miner/adapters/`），`@register_adapter` 自动注册，统一映射到 events schema。store 与分析层不感知具体端。
- **加新端**（Cursor / Windsurf / Gemini CLI …）：在 `miner/adapters/` 丢一个 `<name>.py`（实现 `SourceAdapter`）+ `adapters/__init__.py` 加一行 `from . import <name>`。零改其他代码。
- **统一 schema**：`sessions` + `events`（kind: ucmd/atext/tool/result/code/think）+ `events_fts`(FTS5) + `stories`（来自 `.story/`）。
- **统一知识层**：`packages/knowledge/` 把 scenario / playbook / failure 合并到同一套 INDEX，供 `story-lifecycle` 与本包共用。

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

## 🔴 红线

`data/*.db` 含真实开发对话（含金融 PII），**绝不入 git**（已 gitignore）。开源的只有代码，数据留本地。
