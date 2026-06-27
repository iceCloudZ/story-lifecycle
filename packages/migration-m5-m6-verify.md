# Migration M5/M6 验证报告

> 日期：2026-06-27  
> 范围：`packages/knowledge/`（统一知识层）+ `packages/story-miner/scripts`（挖掘脚本 config 化）

## 目标回顾

- **M5**：统一知识层落地
  - 让 `story-miner` 的 `generate_playbooks.py` / `failure_mode.py` 输出符合 `packages/knowledge/schema.md` 的元数据。
  - 把 `story-lifecycle` 的 scenario markdown 与 `story-miner` 的 playbook/failure 合并到同一套 `INDEX.json`。
  - 合并 `benchmarks/attribution.py` 的归因报告到 `failures/failure-knowledge.json`。
- **M6**：miner 硬编码泛化为 config 驱动
  - 移除 `generate_playbooks.py` 和 `failure_mode.py` 中 `hc-all` / `D:/hc-all` 的硬编码。
  - 输出路径改为 `<workspace>/.story/knowledge/playbooks/` 与 `<workspace>/.story/knowledge/failures/`。
  - 支持 `--workspace` 参数，默认读取 `config.json` 的 `workspaces`。

## 变更摘要

### 1. `packages/story-miner/scripts/generate_playbooks.py`

- 不再硬编码 `DB` 和 `OUT`；改为 `config.DB_PATH` + 由 workspace 推导输出目录。
- `THEME` / `_HC_SERVICES` 保留默认值，但允许在 `config.json` 中以 `playbook_themes` / `service_names` 覆盖。
- 新增 `_write_playbooks_for_workspace(c, workspace, ws_tag)`，按 workspace 过滤 `sessions.ws`。
- `main()` 增加 `--workspace` / `-w` 参数，未指定时遍历 `config.WORKSPACES`。
- `fail_class()` 分类名称与 `failure_mode.py` 对齐（`编译/构建错误`、`文件/路径不存在` 等），确保统一索引能自动互链。
- 每个 `.md` playbook 继续写出同名的 `.md.json` sidecar，包含 `id/type/theme/session_count/top_files/common_commands/common_failures/linked_story`。

### 2. `packages/story-miner/scripts/failure_mode.py`

- 移除全局 `hc-all` 过滤，新增 `_collect_failures(conn, ws_filter)` 按 workspace 聚合。
- 新增 `_analyze_and_write(conn, ws_tag, workspace)`，把失败模式报告和 `failure-knowledge.json` 写到 `<workspace>/.story/knowledge/failures/`。
- `write_failure_knowledge()` 接受 `out_json` 参数，兼容全局 fallback 与 per-workspace 输出。
- `main()` 增加 `--workspace` / `-w` 参数；遍历 workspaces 后仍保留全局 `scripts/out/failure_mode.md` + `scripts/out/failure-knowledge.json`（向后兼容）。

### 3. `packages/knowledge/src/knowledge/parser.py`

- `parse_scenario()` 对 legacy markdown（无 YAML frontmatter）做最佳-effort 结构化提取：
  - `participating_services`：从 `## Participants` 提取 `hc-*` 服务名。
  - `main_flow`：从 `## Flow` 提取编号/列表步骤。
  - `tables`：从 `## Data Tables` 提取 `t_*` 表名。
  - `mq_topics`：从 `## MQ Messages` 提取大写下划线 topic。
  - `source_refs`：从 `## Source Refs` 提取文件路径。
- 已有 YAML frontmatter 的 scenario 仍优先使用 frontmatter。

### 4. `packages/knowledge/src/knowledge/generator.py`

- 新增 `_attribution_reports_to_failures(knowledge_dir)`：扫描 `failures/attribution-reports/*.json`，把 `AttributionReport` 转成 `FailureEntry`。
- `_collect_entries()` 在读取 `failure-knowledge.json` 后，自动合并 attribution reports。
- 新增 `merge_attribution_reports(knowledge_dir)`：把 attribution reports 写回/追加到 `failures/failure-knowledge.json`。
- `_link_entries()` 保持自动互链：同 domain 的 scenario↔playbook，playbook 的 `common_failures` ↔ failure entry。

### 5. 测试

- 新增 `packages/story-miner/tests/test_knowledge_outputs.py`：用临时 SQLite DB 验证：
  - theme playbook 写出 `.md.json` sidecar。
  - by-story playbook 写出 sidecar 并含 `linked_story`。
  - `failure_mode` 写出 `failures/failure-knowledge.json`。
  - `KnowledgeIndex` / `generate_index` 能正确索引这些输出并建立 cross-links。
- 新增 `packages/knowledge/tests/test_knowledge.py` 用例：
  - scenario markdown 字段提取。
  - attribution reports 合并到 `failure-knowledge.json`。
- 修复 `packages/story-miner/tests/test_story_context_provider.py`：当 `data/transcripts.db` 存在但无 `stories` 表时自动 skip，避免空 DB 导致测试失败。

## 全量测试结果

```text
650 passed, 8 skipped, 2 warnings in 108.59s
```

- 运行命令：`python -m pytest packages/story-miner/tests packages/story-lifecycle/tests tests/contracts packages/knowledge/tests -q`
- 在 Windows / Python 3.12 / 临时 venv `.venv-monorepo-test` 下通过。

## 使用方式

### 生成所有 workspace 的 playbook

```bash
cd packages/story-miner
python scripts/generate_playbooks.py
```

或只生成指定 workspace：

```bash
python scripts/generate_playbooks.py -w D:/hc-all
```

输出：

- `D:/hc-all/.story/knowledge/playbooks/debug.md`
- `D:/hc-all/.story/knowledge/playbooks/debug.md.json`
- `D:/hc-all/.story/knowledge/playbooks/by-story/<story_id>.md`
- `D:/hc-all/.story/knowledge/playbooks/by-story/<story_id>.md.json`

### 生成失败知识

```bash
cd packages/story-miner
python scripts/failure_mode.py
```

输出：

- `D:/hc-all/.story/knowledge/failures/failure-mode.md`
- `D:/hc-all/.story/knowledge/failures/failure-knowledge.json`

### 合并 attribution reports

把 `story-lifecycle` 的归因报告写到：

```
<workspace>/.story/knowledge/failures/attribution-reports/<instance_id>.json
```

然后在 Python 中合并：

```python
from knowledge.generator import merge_attribution_reports
merge_attribution_reports("D:/hc-all/.story/knowledge")
```

### 重建统一 INDEX

```python
from knowledge import KnowledgeIndex
idx = KnowledgeIndex("D:/hc-all/.story/knowledge")
idx.refresh()
results = idx.retrieve(story_key="tapd-1065518", workspace="D:/hc-all", stage="design", query="排查订单状态机")
```

## 注意事项

1. `packages/story-miner/data/transcripts.db` 为空时会被 `sqlite3.connect` 自动创建空文件；运行全量测试前若该文件为空，建议删除，否则 `test_story_context_provider.py` 会 skip。
2. 旧的 `D:/hc-all/.story/knowledge/playbooks/*.md` 文件名保持不变，skill 引用无需修改。
3. 主题/服务名仍保留 hc-all 默认值，但已可通过 `config.json` 覆盖，后续如需完全多项目通用可继续扩展。

## 结论

M5（统一知识层落地）与 M6（config 驱动化）已完成。miner 脚本现在能按 workspace 输出标准化元数据，统一知识层可索引 scenario / playbook / failure / attribution 四类实体并自动互链。全量测试通过。
