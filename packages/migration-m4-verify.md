# M4 验证报告：统一知识层 schema 设计

## 目标

设计统一知识层 schema，让 `story-lifecycle` 的静态知识（scenarios/indexes/graph）与 `story-miner` 的动态知识（playbooks/failure_mode/stage_cost/retrospect）对齐到同一套模型，并产出可执行的架构文档。

## 新增文件

| 文件 | 说明 |
|------|------|
| `packages/knowledge/schema.md` | 统一知识层 schema 设计文档 |
| `packages/knowledge/src/knowledge/` | 预留给 M5 实现统一生成器/INDEX/检索的目录 |

## 设计要点

### 1. 统一实体基座 `KnowledgeEntry`

所有知识条目共享字段：`id`、`type`、`title`、`source`（static/dynamic）、`domain`、`status`、`trigger`、`must_read`、`roles`、`tags`、`source_refs`、`created_at`、`updated_at`。

### 2. 三类核心实体

- **Scenario**（static）：保留业务结构语义，扩展 `participating_services`、`main_flow`、`apis`、`tables`、`mq_topics`、`state_machines`、`known_risks`。
- **Playbook**（dynamic）：保留任务经验语义，扩展 `theme`、`session_count`、`top_files`、`common_commands`、`common_failures`、`linked_scenarios`、`linked_story`。
- **Failure**（dynamic/merged）：合并 `story-lifecycle/benchmarks/attribution.py` 的 stage_log 归因与 `story-miner/scripts/failure_mode.py` 的 transcript 失败分类。

### 3. 统一 `INDEX.json`

`INDEX.json` 同时挂载 scenarios、playbooks、failures，通过 `links` 字段互链。检索时优先召回：

1. 精确匹配 story_key 的 by-story playbook
2. 同一 domain 的 scenario
3. 关键词命中的 playbook
4. 相关 failure knowledge

### 4. 目录结构

保持 `<workspace>/.story/knowledge/` 不变，新增 `failures/failure-knowledge.json`，playbooks 输出 markdown + 同名 `.json` 元数据。

### 5. 向后兼容

- 现有 playbook 文件名（`debug.md`、`requirement-dev.md` 等）稳定，不破坏 hc-all skill 引用。
- `story-lifecycle` 的 knowledge 模板和输出目录不变。
- `attribution.py` 继续输出 `AttributionReport`；统一生成器读取后合并。

## 验收检查

- [x] `packages/knowledge/schema.md` 已创建
- [x] Schema 能容纳现有 scenarios（15+）和 playbooks（7 主题 + by-story）
- [x] INDEX 设计覆盖 scenarios + playbooks + failures
- [x] 失败知识合并方案明确
- [x] 现有 playbook 文件名稳定

## 结论

M4 验收通过。下一步 M5：在 `packages/knowledge/src/knowledge/` 实现统一生成器、INDEX、检索，并迁移现有知识产物。
