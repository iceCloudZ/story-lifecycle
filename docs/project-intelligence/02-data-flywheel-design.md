# Project Intelligence Data Flywheel Design

## 背景

`Project Intelligence Bootstrap` 解决的是“如何在项目级初始化 `.story/knowledge/`，让 AI 在 story 执行前拥有项目上下文”。但 Story Lifecycle 本地已经积累了大量过程资产：PRD、spec、plan、done、context、review finding、learned pattern、verification result、code diff 和测试报告。

如果这些资产只停留在文件和数据库里，它们只能被人偶尔翻看；如果进入数据飞轮，它们就能持续反哺下一次 story：

```text
PRD / spec / plan / code / test / review / done
  -> Artifact Registry
  -> Knowledge Pack / Project Graph
  -> Context Packet
  -> Story Execution
  -> Finding / Verification / Pattern
  -> Next Knowledge Update
```

本设计定义 **Project Intelligence Data Flywheel**：把本地已实现资产统一登记、抽取、归档、评估和反哺，让 `.story/knowledge/` 不只是一次性生成物，而是随 story 执行不断进化的项目智能资产。

## 目标

- 统一登记本地 PRD、spec、plan、done、context、finding、pattern、test 等资产。
- 让 `init-knowledge` 和 `sync-knowledge` 能消费这些资产，而不是只读代码。
- 让 Context Builder 能根据 story/stage 选择相关历史资产。
- 让 story 执行结果反哺知识包、风险库、测试清单和 learned patterns。
- 保持本地优先，远程 `ys-agent` 只消费稳定后的知识包和事件。

## 非目标

- P0/P1 不训练模型。
- P0/P1 不把所有历史原文塞进 prompt。
- P0/P1 不做复杂数据仓库。
- P0/P1 不把未审核的 AI 推断直接升级为 verified 知识。
- P0/P1 不把业务项目知识提升为全局 engine rule。

## 核心判断

Story Lifecycle 现在已经有两类飞轮基础：

1. **质量飞轮**
   - `finding`
   - `learned_pattern`
   - `verification_result`
   - `quality_packet`
   - `seed-quality`

2. **项目知识飞轮**
   - PRD
   - spec
   - plan
   - done/context
   - code diff
   - test report
   - bug/review/release 过程记录

质量飞轮偏“执行质量和工程规则”，项目知识飞轮偏“产品/业务/代码上下文”。两者应该相互引用，但不能混成一个池子。

## 现有资产分类

### 1. 需求和设计资产

来源：

```text
prd/
docs/superpowers/specs/
docs/superpowers/plans/
.story/prd-task-*.json
story.context_json.prd_path
```

价值：

- 需求背景。
- 用户目标。
- 业务流程。
- 验收标准。
- 影响范围。
- 设计决策。
- 实施任务拆解。
- 预期验证命令。

进入知识包：

```text
scenarios/
indexes/test-case-index.md
indexes/by-domain/
playbooks/regression-playbook.md
reviews/pending-review-items.md
```

### 2. 执行和产出资产

来源：

```text
.story/done/<story_key>/<stage>.json
.story/context/<story_key>/done/*.json
.story/context/<story_key>/*.md
git diff / changed files
stage_log
event_log
```

价值：

- 每个阶段实际做了什么。
- 实际改了哪些文件。
- 设计和实现是否偏离计划。
- 哪些阶段卡住或失败。
- 哪些修复包有效。

进入知识包：

```text
events/local-skill-events.jsonl
graph/source_refs
reviews/pending-review-items.md
playbooks/production-troubleshooting-playbook.md
```

### 3. 验证和质量资产

来源：

```text
verification_result events
test reports
review feedback
finding table
learned_pattern table
seed-quality proposals
```

价值：

- 哪些问题被发现。
- 哪些问题被确认。
- 哪些问题已经修复。
- 哪些修复通过测试验证。
- 哪些模式值得沉淀为规则。

进入知识包：

```text
indexes/bug-risk-index.md
indexes/test-case-index.md
playbooks/regression-playbook.md
reviews/review-log.md
```

### 4. 代码和结构资产

来源：

```text
source tree
git commit
git diff
Controller / API
Entity / DTO
Mapper / SQL
MQ / Feign / config
frontend route / service calls
Python route / scripts / MCP tools
```

价值：

- 事实索引。
- source refs。
- graph seed nodes。
- stale 检测。
- 影响分析。

进入知识包：

```text
indexes/
graph/product-context-graph.json
search-catalog.md
```

## Artifact Registry

为了避免 bootstrap prompt 到处乱搜，先建立统一资产登记。

建议目录：

```text
.story/artifacts/
  registry.json
  by-story/
    <story_key>.json
```

`registry.json` 是项目级资产目录，`by-story/<story_key>.json` 是单 story 资产清单。

### Story Artifact Schema

```json
{
  "schema_version": 1,
  "story_key": "STORY-12345",
  "title": "修复提现失败错误提示",
  "source": {
    "type": "tapd",
    "id": "12345"
  },
  "status": "completed",
  "created_at": "2026-06-01T10:00:00+08:00",
  "updated_at": "2026-06-01T18:00:00+08:00",
  "artifacts": [
    {
      "type": "prd",
      "path": "prd/STORY-12345.md",
      "status": "verified",
      "role": "requirement_source"
    },
    {
      "type": "spec",
      "path": "docs/superpowers/specs/2026-06-01-story-design.md",
      "status": "verified",
      "role": "design_source"
    },
    {
      "type": "plan",
      "path": "docs/superpowers/plans/2026-06-01-story.md",
      "status": "verified",
      "role": "implementation_plan"
    },
    {
      "type": "done",
      "path": ".story/context/STORY-12345/done/design.json",
      "status": "extracted",
      "role": "stage_output"
    },
    {
      "type": "finding",
      "id": "finding-20260601-001",
      "status": "verified",
      "role": "quality_signal"
    }
  ],
  "knowledge_links": {
    "domains": ["order"],
    "scenarios": ["order.withdraw"],
    "services": ["hc-order"],
    "tables": ["hc_order.t_order"],
    "bugs": []
  }
}
```

## 命令设计

### story project index-assets

扫描本地已实现资产，生成 Artifact Registry。

输入：

- `prd/`
- `docs/superpowers/specs/`
- `docs/superpowers/plans/`
- `.story/context/`
- `.story/done/`
- DB: story、event_log、finding、learned_pattern

输出：

```text
.story/artifacts/registry.json
.story/artifacts/by-story/*.json
```

P1 可以先做规则扫描：

- 文件名包含 story key。
- PRD path 来自 `context_json.prd_path`。
- spec/plan path 来自 done JSON 或文件 frontmatter。
- finding/pattern 来自 DB。

### story project init-knowledge

在 Bootstrap 时优先读取：

```text
.story/artifacts/registry.json
```

然后再读代码和 docs。这样 CLI 不需要每次从零发现资产。

### story project sync-knowledge

增量更新时检查：

- Git commit 是否变化。
- PRD/spec/plan 是否新增或修改。
- done/context 是否有新阶段产出。
- finding/pattern 是否新增或状态变化。
- test/verification 是否新增。

输出：

- stale warning。
- 局部刷新任务。
- pending review item。
- knowledge update suggestion。

## 飞轮流程

### 1. Intake

story 从 PRD、TAPD、手工输入或 source adapter 进入系统。

产出：

- story row。
- `prd_path`。
- `story_intake` event。
- Artifact Registry 初始记录。

### 2. Planning

design/spec/plan 产生。

产出：

- spec artifact。
- plan artifact。
- 影响范围。
- 预期测试和验收。

反哺：

- `scenarios/` 候选更新。
- `test-case-index.md` 候选更新。
- `indexes/by-domain` 候选更新。

### 3. Execution

Executor 修改代码、运行命令、写 done。

产出：

- changed files。
- stage done。
- event_log。
- context files。

反哺：

- graph source refs。
- service/api/table 影响边。
- context builder 的历史样本。

### 4. Review

Reviewer 或外部 review 产生 finding。

产出：

- finding。
- review feedback。
- pending approval。

反哺：

- bug-risk-index。
- regression checklist。
- learned pattern candidate。

### 5. Verification

测试、lint、smoke、integration、manual check 产生验证证据。

产出：

- verification_result。
- covered_findings。
- regression tests。

反哺：

- finding status。
- test-case-index。
- scenario risk confidence。

### 6. Learning

verified finding 或稳定成功路径沉淀为 learned pattern。

产出：

- proposed learned pattern。
- approved/active pattern。
- playbook update。

反哺：

- Quality Packet。
- Context Builder。
- Project Knowledge Pack。

## 资产到知识包的映射

| 资产 | 主要用途 | 知识包落点 |
| --- | --- | --- |
| PRD | 业务目标、验收标准 | `scenarios/`, `test-case-index.md` |
| Spec | 设计决策、影响范围 | `scenarios/`, `indexes/by-domain/` |
| Plan | 执行步骤、风险和验证 | `playbooks/`, `reviews/` |
| Done | 阶段实际产出 | `events/`, `graph/source_refs` |
| Context | 修复包、阶段交接 | `playbooks/`, context examples |
| Finding | 质量问题和根因 | `bug-risk-index.md` |
| Verification | 测试和修复证据 | `test-case-index.md`, finding status |
| Learned Pattern | 可复用经验 | `playbooks/`, Quality Packet |
| Code Diff | 真实改动 | `indexes/`, `graph/` |

## 与 seed-quality 的关系

`seed-quality` 已经能基于 manifest 分析 story artifacts，提出 findings/patterns。Data Flywheel 不替代它，而是给它更稳定的输入。

关系：

```text
Artifact Registry
  -> seed-quality manifest
  -> seed-quality analyze
  -> proposal
  -> reviewed proposal
  -> finding / learned_pattern
  -> Knowledge Pack update
```

也就是说，`seed-quality` 可以成为数据飞轮里的 Distill 阶段。

## 与 Context Builder 的关系

Context Builder 不只读取 `.story/knowledge`，还应该读取 Artifact Registry。

优先级：

1. 当前 story 的 PRD/spec/plan。
2. 当前 story 相关 context packet 和 done。
3. 相似 story 的 verified findings/patterns。
4. 项目级 `.story/knowledge`。
5. proposed 内容和 pending review。

这样 Planner 能得到：

- 当前需求的直接上下文。
- 项目级业务上下文。
- 历史类似问题。
- 已验证质量规则。

## 状态和可信度

资产状态：

```text
raw
  刚发现，未解析。

indexed
  已进入 Artifact Registry。

extracted
  已抽取成事实索引。

proposed
  AI 推断出的候选知识。

verified
  人工或验证证据确认。

stale
  来源发生变化，需要刷新。

deprecated
  失效，不再用于默认注入。
```

原则：

- raw/indexed 不能直接注入为关键结论。
- extracted 可以作为代码/文件事实。
- proposed 必须标待确认。
- verified 才能作为强依据。
- stale 必须在 Context Builder 输出中显式告警。

## 存储边界

```text
.story/
  artifacts/
    registry.json
    by-story/

  knowledge/
    scenarios/
    indexes/
    graph/
    playbooks/
    reviews/
    events/

  context/
    <story_key>/

  done/
    <story_key>/
```

数据库继续保存：

- story。
- stage_log。
- event_log。
- finding。
- learned_pattern。
- verification event。

文件系统保存：

- 文档资产。
- knowledge pack。
- context packet。
- registry。
- prompt-friendly artifact index。

## P0/P1 落地顺序

### P0: 设计和协议

- 本设计文档。
- Artifact Registry schema。
- asset type 定义。
- 资产到知识包映射。

### P1: index-assets

- 新增 `story project index-assets`。
- 扫描 PRD/spec/plan/context/done。
- 读取 DB findings/patterns。
- 生成 `.story/artifacts/registry.json`。

### P2: init-knowledge 消费 registry

- Bootstrap prompt 先读 Artifact Registry。
- Knowledge Pack 中引用 registry artifacts。
- pending review 关联 artifact id。

### P3: sync-knowledge 增量反哺

- PRD/spec/plan/done/finding/pattern 变化触发局部刷新。
- stale 检测覆盖 artifact。
- 生成 update suggestion。

### P4: seed-quality 联动

- 从 Artifact Registry 生成 seed-quality manifest。
- proposal 审核后写 finding/pattern。
- finding/pattern 再反哺 knowledge。

### P5: 远程 ys-agent

- 本地 registry 和 knowledge pack 可导出。
- 远程只接收稳定版本。
- 远程发布必须绑定 Git repo + commit。

## 验收标准

- `story project index-assets` 能生成 registry。
- registry 能列出 PRD、spec、plan、done、context、finding、pattern。
- `init-knowledge` 能使用 registry 中的资产作为 source refs。
- Context Builder 能优先读取当前 story 的 PRD/spec/plan。
- 一个 verified finding 能反哺到 bug-risk-index 或 playbook。
- 一个 verification_result 能更新 test-case-index 或 finding 状态。

## 成功标准

数据飞轮成功不是“收集了很多文件”，而是：

- 下一个 story 能用上上一个 story 的经验。
- PRD/spec/plan 不再是一次性文档，而是可被检索和引用的项目知识。
- review finding 不再停留在问题列表，而能变成风险库、测试清单或 learned pattern。
- verification 不再只是终端输出，而是知识可信度提升的证据。
- AI 的下一次 planning 和 execution 明显更少重复踩坑。
