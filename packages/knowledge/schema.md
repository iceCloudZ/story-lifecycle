# 统一知识层 Schema 设计

> 目标：把 `story-lifecycle` 的静态知识（scenarios/indexes/graph）和 `story-miner` 的动态知识（playbooks/failure_mode/stage_cost/retrospect）对齐到同一套 schema，让 agent 一次拿到“业务结构 + 历史经验”。

## 1. 设计原则

1. **语义保留**：scenarios 保留“业务结构”语义；playbooks 保留“任务经验”语义。
2. **来源显式**：每个知识条目标记 `source: static | dynamic`，说明是代码扫描生成还是 transcript 挖掘生成。
3. **统一索引**：一个 `INDEX.json` 同时挂载 scenarios 和 playbooks，通过 `links` 字段互链。
4. **向后兼容**：现有 playbook 文件名（`debug.md`、`requirement-dev.md` 等）稳定，不破坏 hc-all skill 引用。
5. **失败知识合并**：`story-lifecycle/benchmarks/attribution.py` 的 stage_log 归因与 `miner/scripts/failure_mode.py` 的 transcript 失败分类合并为统一 `failure_knowledge`。

## 2. 目录结构

```
<workspace>/.story/knowledge/           # 项目级知识库（每个项目一份）
├── manifest.yaml                       # 知识包清单
├── product.yaml                        # 产品概述
├── search-catalog.md                   # 检索目录
├── INDEX.json                          # 统一索引（scenarios + playbooks + failures）
├── graph/
│   └── product-context-graph.json      # 关系图（扩展支持 Playbook / Failure 节点）
├── scenarios/
│   └── <domain>/<scenario>.md          # 静态业务场景（story-lifecycle 生成）
├── playbooks/
│   ├── <theme>.md                      # 动态任务经验（miner 生成，文件名稳定）
│   ├── by-story/<story_key>.md         # 按 story 聚合的经验
│   └── INDEX.md                        # playbook 子索引（兼容旧格式）
├── indexes/
│   ├── service-index.md                # 服务索引
│   ├── api-index.md                    # 接口索引
│   ├── table-index.md                  # 数据表索引
│   └── by-domain/<domain>.md           # 按域索引
└── failures/
    └── failure-knowledge.json          # 统一失败知识
```

## 3. 统一实体 Schema

### 3.1 KnowledgeEntry（基座）

所有知识条目共享以下字段：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | 是 | 全局唯一标识 |
| `type` | string | 是 | `scenario` / `playbook` / `index` / `failure` / `graph_node` |
| `title` | string | 是 | 人读标题 |
| `source` | string | 是 | `static`（代码扫描）或 `dynamic`（transcript 挖掘） |
| `domain` | string | 否 | 所属业务域，如 `order`、`payment` |
| `status` | string | 否 | `proposed` / `extracted` / `verified` |
| `trigger` | object | 否 | 触发条件（见 3.5） |
| `must_read` | list[string] | 否 | 必看文件/路径 |
| `roles` | list[string] | 否 | 涉及角色/代码角色，如 `Controller`、`ServiceImpl` |
| `tags` | list[string] | 否 | 自定义标签 |
| `source_refs` | list[string] | 否 | 来源引用（文件路径:行号 或 session_id） |
| `created_at` | string | 否 | ISO 时间戳 |
| `updated_at` | string | 否 | ISO 时间戳 |

### 3.2 Scenario（静态业务结构）

扩展字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `participating_services` | list[string] | 参与服务 |
| `main_flow` | list[string] | 主流程步骤 |
| `apis` | list[string] | 涉及接口 |
| `tables` | list[string] | 涉及数据表 |
| `mq_topics` | list[string] | 涉及 MQ |
| `state_machines` | list[string] | 涉及状态机 |
| `known_risks` | list[string] | 已知风险 |

### 3.3 Playbook（动态任务经验）

扩展字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `theme` | string | 任务类型主题，如 `debug`、`requirement-dev` |
| `session_count` | int | 基于多少个 session 挖掘 |
| `top_files` | list[FileRef] | 高频文件（带角色、次数） |
| `common_commands` | list[CommandRef] | 常用操作 |
| `common_failures` | list[FailureRef] | 常见失败 |
| `linked_scenarios` | list[string] | 关联的 scenario id |
| `linked_story` | string | 关联的 story_key（by-story playbook） |

#### FileRef

```json
{
  "path": "hc-order/src/main/java/com/ys/hc/order/controller/OrderController.java",
  "role": "Controller",
  "count": 12,
  "source": "dynamic"
}
```

#### CommandRef

```json
{
  "class": "cli_sql(查库)",
  "count": 8,
  "examples": ["cli_sql -e 'select ...'"]
}
```

#### FailureRef

```json
{
  "category": "编译错误",
  "count": 5,
  "sample_text": "cannot find symbol",
  "mitigation": "检查依赖/重新编译"
}
```

### 3.4 Index（检索目录）

统一 INDEX.json 结构：

```json
{
  "version": 1,
  "product": {"name": "...", "description": "..."},
  "updated_at": "2026-06-27T12:00:00",
  "entries": [
    {
      "id": "scenario:order:create",
      "type": "scenario",
      "title": "下单场景",
      "source": "static",
      "path": "scenarios/order/create.md",
      "domain": "order",
      "links": ["playbook:requirement-dev", "playbook:debug"]
    },
    {
      "id": "playbook:debug",
      "type": "playbook",
      "title": "排查/Debug Playbook",
      "source": "dynamic",
      "path": "playbooks/debug.md",
      "theme": "debug",
      "links": ["scenario:order:create", "failure:compile"]
    },
    {
      "id": "failure:compile",
      "type": "failure",
      "title": "编译错误",
      "source": "dynamic",
      "path": "failures/failure-knowledge.json",
      "links": ["playbook:debug"]
    }
  ]
}
```

### 3.5 Trigger（触发条件）

用于 agent 判断何时应该召回某条知识：

```json
{
  "keywords": ["debug", "排查", "报错"],
  "stage": "design",
  "story_key_prefix": "tapd-",
  "workspace_keyword": "hc-all"
}
```

## 4. 统一失败知识（Failure Knowledge）

合并两个来源：

1. **story-lifecycle `benchmarks/attribution.py`**：stage_log 级失败归因
   - 字段：`instance_id`, `failure_stage`, `failure_node`, `root_cause_category`, `root_cause_detail`, `counterfactual_candidates`
2. **story-miner `scripts/failure_mode.py`**：transcript 级失败分类
   - 字段：`category`, `count`, `sample_text`, `tool_name`, `workspace`, `source`

统一 FailureEntry：

```json
{
  "id": "failure:compile",
  "type": "failure",
  "title": "编译错误",
  "source": "dynamic",
  "category": "compile_error",
  "display_category": "编译错误",
  "detail": "cannot find symbol / BUILD FAIL",
  "frequency": {"hc-all": 12, "java-agent": 3},
  "common_tools": ["Bash", "Read"],
  "stages_affected": ["build", "verify"],
  "mitigations": ["检查依赖版本", "重新编译", "查看 BUILD 日志"],
  "counterfactuals": ["增加编译前检查", "更严格的类型提示"],
  "source_refs": ["scripts/failure_mode.py", "benchmarks/attribution.py"]
}
```

## 5. 统一 INDEX 生成规则

### 5.1 静态部分（scenarios/indexes/graph）

由 `story-lifecycle` 的 `knowledge` 模块在 `init-knowledge` / `bootstrap` 时生成：

- 扫描代码 → 生成 `scenarios/<domain>/<scenario>.md`
- 生成 `indexes/` 下各类索引
- 生成 `graph/product-context-graph.json`

### 5.2 动态部分（playbooks/failures）

由 `story-miner` 的离线挖掘脚本生成：

- `scripts/generate_playbooks.py` → `playbooks/<theme>.md` + `playbooks/by-story/<story_key>.md`
- `scripts/failure_mode.py` → `failures/failure-knowledge.json`
- `scripts/stage_cost.py` / `scripts/retrospect.py` → 可选补充到 `failures/` 或 `playbooks/`

### 5.3 合并 INDEX

`packages/knowledge/src/knowledge/index.py` 统一读取：

1. 扫描 `scenarios/` 下所有 `.md`
2. 扫描 `playbooks/` 下所有 `.md`
3. 读取 `failures/failure-knowledge.json`
4. 读取 `graph/product-context-graph.json` 中的节点（可选）
5. 生成 `INDEX.json`

互链规则（v1）：

- 同一 `domain` 的 scenario 与 playbook 自动互链
- playbook 的 `common_failures` 链接到对应 failure entry
- by-story playbook 链接到对应的 scenario（通过 story_key → domain 映射，未来可显式）

## 6. API 接口（packages/knowledge）

```python
from knowledge import KnowledgeIndex

index = KnowledgeIndex("D:/hc-all/.story/knowledge")

# 召回与当前任务相关的知识
results = index.retrieve(
    story_key="tapd-1065518",
    workspace="D:/hc-all",
    stage="design",
    query="排查订单状态机",
    top_k=10,
)
# returns list[KnowledgeEntry]
```

检索优先级：

1. 精确匹配 `story_key` 的 by-story playbook
2. 同一 domain 的 scenario
3. 关键词命中的 playbook
4. 相关 failure knowledge

## 7. 与现有系统的兼容

- `story-lifecycle` 的 `knowledge/templates/*.md` 继续作为生成模板；输出目录不变。
- `story-miner` 的 `scripts/generate_playbooks.py` 输出格式从纯 markdown 改为 **markdown + 同名的 `.json` 元数据**，便于统一 INDEX 读取。
- 现有 hc-all skill 对 `playbooks/debug.md` 等文件名的引用保持不变。
- `story-lifecycle/benchmarks/attribution.py` 继续输出 `AttributionReport`；统一生成器读取这些报告并合并到 `failures/failure-knowledge.json`。

## 8. 验收标准（M4）

- [x] `packages/knowledge/schema.md` 定义统一 schema
- [x] schema 能容纳现有 scenarios（15+）和 playbooks（7 主题 + by-story）
- [x] INDEX 设计覆盖 scenarios + playbooks + failures
- [x] 失败知识合并方案明确（attribution + failure_mode）
- [x] 现有 playbook 文件名稳定

## 9. 下一步（M5）

1. 创建 `packages/knowledge/src/knowledge/` 实现统一生成器/INDEX/检索
2. 迁移现有 scenarios + playbooks 到新 schema（输出 `.json` 元数据）
3. 合并 attribution + failure_mode → `failures/failure-knowledge.json`
4. 在 hc-all/.story/knowledge/ 重建统一格式并验证 agent 召回
