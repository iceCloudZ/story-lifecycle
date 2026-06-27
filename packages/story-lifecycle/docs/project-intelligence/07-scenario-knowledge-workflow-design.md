# Scenario Knowledge Layer Design

> Former name: Scenario Knowledge Workflow Design.

This document defines the Scenario Knowledge Layer, not just a scenario scanning command. The layer turns user-confirmed business scenarios into computable project context for Context Builder, Planner, Reviewer, test assistants, regression selection, and the quality flywheel.

## 背景

`story project init-knowledge` 已经能生成 `.story/knowledge/` 的项目概览骨架，但当前产物更接近“服务、接口、表、MQ 的粗粒度项目地图”，还没有达到测试回归需要的“业务场景级知识包”。

测试真正需要的是按业务逻辑组织的文档，例如注册、登录、提现、授信、还款、逾期、提前还款、coolingoff、找回账号、账号注销。每个场景都需要说明入口、代码链路、数据交互、表结构字段、MQ、状态机、最近 bug 和回归测试点。

这个能力不应重新设计一套 agent 编排系统。`story-lifecycle` 当前执行阶段已经具备 Planner、CLI Executor、Reviewer、Gate、Adapter、done/context/event 等通用能力。Scenario Knowledge Layer 的目标是复用这些能力，把“业务场景知识”做成 Project Intelligence 的可计算上下文层。

Scenario scan 是这个 layer 的采集和更新工作流；最终价值不是生成一份 Markdown，而是维护以下映射：

```text
Business Scenario
  -> services
  -> APIs
  -> tables / fields
  -> MQ tags
  -> state machines
  -> bugs / findings
  -> regression test cases
```

Markdown 文档用于人和 AI 阅读；`scenario-index.json` 和 graph patch 用于系统检索、扩展、影响分析和回归选择。

## 目标

- 先生成项目概览，再让用户确认业务场景边界。
- 通过命令行交互选择、改名、合并、删除、新增业务场景。
- 将用户确认后的场景沉淀为 `.story/knowledge/declarations/business-scenarios.yaml`。
- 针对单个业务场景执行深度扫描，生成场景文档、结构化场景索引和 graph patch。
- 建立 scenario 与 Artifact Registry、Context Builder、Quality Flywheel、test-case index 的接口协议。
- 复用现有 CLI adapter、headless runner、planner/reviewer、gate、path registry 和 event log。
- 所有 LLM 产物先进入 run draft，经过 lint、review、用户确认后再 merge 到正式 knowledge pack。

## 非目标

- 不把业务场景扫描伪装成普通 story 的 `design/implement/test/review` 阶段。
- 不让 LLM 直接覆盖正式 knowledge 文件。
- 不让 AI 自动把业务结论标记为 `verified`。
- 不在 P0/P1 引入向量库、GraphDB、AST 专用扫描器。
- 不让 `ys-agent` 远程化阻塞本地工作流。

## 当前可复用能力

| 能力 | 当前位置 | 复用方式 |
| --- | --- | --- |
| CLI Adapter | `src/story_lifecycle/adapters/` | 直接复用 claude/codex/shell adapter。 |
| Headless 执行 | `orchestrator/tools/base.py` | 抽出通用 `AdapterRunner`，供 story stage 和 knowledge run 共用。 |
| Tool Registry | `orchestrator/tools/__init__.py` | 新增 knowledge scenario tool，沿用注册机制。 |
| Planner/Reviewer LLM | `orchestrator/planner.py` | 复用 LLM 调用方式，新增 knowledge 专用 prompt。 |
| GateDecision | `orchestrator/gate.py` | 复用决策模型，扩展为支持 non-story run。 |
| Adversarial Loop | `orchestrator/evaluator_loop.py` | 复用 plan/review loop 思路，用于扫描计划和结果审阅。 |
| Path Registry | `orchestrator/paths.py` + `knowledge/paths.py` | 对齐 `.story/knowledge/runs/` 路径。 |
| Validator | `knowledge/validator.py` | 升级为 `knowledge lint`，做确定性校验。 |

## 设计原则

### 1. 概览先行，场景后置

`init-knowledge` 只负责项目概览、基础索引和候选场景发现，不承担完整业务场景深挖。

```text
init-knowledge
  -> product.yaml
  -> manifest.yaml
  -> search-catalog.md
  -> coarse indexes
  -> candidate scenarios
```

业务场景深挖由后续命令单独触发：

```text
scenario scan <scenario-id>
```

这会重新划分 `init-knowledge` 的产物边界：

| 层级 | 命令 | 产物 | 用途 |
| --- | --- | --- | --- |
| Project Overview | `init-knowledge` | `product.yaml`, `manifest.yaml`, `search-catalog.md`, 粗粒度 `indexes/**`, 候选场景 | 让 AI 和用户先理解项目结构、服务、表、MQ、候选业务域。 |
| Scenario Declaration | `scenarios review` / `scenario add` | `declarations/business-scenarios.yaml` | 固化用户确认过的业务场景边界。 |
| Scenario Knowledge Layer | `scenario scan <id>` | `scenarios/<domain>/<scenario>.md`, `indexes/by-scenario/<id>.md`, `indexes/by-scenario/<id>.json`, `graph patch`, review queue | 生成可读文档和可计算索引，支撑 Context Builder、影响分析和测试回归。 |

原 `03-bootstrap-design.md` 中 `init-knowledge` 一次生成完整 `scenarios/**` 的设想，在本设计中降级为“可生成候选或粗略草稿”。正式场景文档必须由 `scenario scan <id>` 生成或更新。

如果用户只运行 `init-knowledge`，Context Builder 仍可使用项目概览、服务索引、API 索引、表索引和 MQ 索引，但不能假定已有完整业务场景链路。

### 2. AI 提案，用户定边界

业务场景边界不能由 AI 自动决定。AI 只生成候选，用户通过 CLI 选择、改名、合并、删除和新增。

确认后的业务场景写入：

```text
.story/knowledge/declarations/business-scenarios.yaml
```

### 3. Codex 计划和审阅，Claude 执行扫描

多 CLI 分工保持为协议层配置，不写死到某个工具：

```text
Planner / Reviewer adapter: codex
Executor adapter: claude
Controller: story-lifecycle
```

默认推荐：

- Codex 生成候选场景、扫描计划、审阅扫描结果。
- Claude 按扫描计划读代码、搜索、追链路、生成 draft。
- story-lifecycle 负责状态、校验、用户确认和正式合并。

### 4. Draft-first，Merge-controlled

LLM 只能写 run draft：

```text
.story/knowledge/runs/<run-id>/
```

正式知识包只能由 `story-lifecycle` 在校验和用户确认后合并：

```text
.story/knowledge/scenarios/**
.story/knowledge/indexes/**
.story/knowledge/graph/**
.story/knowledge/declarations/**
```

### 5. Evidence-first

每个关键结论必须带 `source_refs`。没有证据的内容只能是 `proposed`，进入 review queue。

### 6. Registry-aware

Scenario scan 不应绕过 Phase 1 的 Artifact Registry。它应优先读取 registry 中已有的 PRD、spec、plan、done、finding、pattern、test、code diff 和基础索引资产，再决定搜索计划。

扫描完成后，merge 阶段必须把新的场景文档、场景索引和关键事实反哺 registry，让后续 Context Builder 和 `sync-knowledge` 能发现它们。

### 7. Computable-before-graph-database

Scenario Knowledge Layer keeps computable mappings in files first. Graph databases and heavy code graphs are optional later infrastructure, not P0/P1 requirements.

#### CodeGraph-as-retrieval-provider

CodeGraph is an auxiliary retrieval layer, not the knowledge system of record. It can provide deterministic code facts with near-zero LLM cost, such as classes, methods, call relationships, API entrypoints, file locations, and some dependency relationships. The final project knowledge still lives in `.story/knowledge/` as Markdown, YAML, and JSON.

```text
Layer 4: .story/knowledge/ final knowledge
  - scenarios/**/*.md
  - indexes/**/*.md
  - indexes/by-scenario/*.json
  - graph/product-context-graph.json
  - declarations/business-scenarios.yaml

Layer 3: business mapping
  - LLM + human review
  - map extracted code facts to business scenarios
  - mark proposed / extracted / verified

Layer 2: auxiliary retrieval
  - CodeGraph MCP/CLI
  - rg
  - ast-grep
  - Sourcegraph/LSP, if available

Layer 1: source assets
  - source code
  - PRD/spec/plan/done/finding/test documents
  - SQL/MQ/config/frontend files
```

Boundary rules:

- CodeGraph indexes may live in `.codegraph/`, `.story/cache/codegraph/`, or an equivalent cache directory. They must not become the formal knowledge pack.
- CodeGraph indexes can be deleted and rebuilt at any time. `.story/knowledge/` must remain readable, diffable, auditable, and usable by Context Builder without CodeGraph.
- CodeGraph facts entering the knowledge pack must be converted into `scenario-index.json`, `graph-patch.json`, or Markdown facts with `source_refs`.
- `source_refs` must point to source code, SQL, config, or document files. A `codegraph_id` alone is not enough evidence.
- CodeGraph can prove code-structure relationships. It cannot prove business ownership. For example, `WithdrawController -> RiskService` can be `extracted`; mapping that chain to `order.withdraw` is still LLM/user business mapping and must keep the right evidence status.
- `product-context-graph.json` is a business/project graph. It must not directly copy the raw CodeGraph graph; it only accepts mapped, linted, merge-approved nodes and edges.

Scenario Knowledge Layer 需要可计算映射，但 P0/P1 不需要图数据库。优先使用 file-first 结构：

```text
scenario.md          # 人和 AI 读
scenario-index.json  # 系统 search / expand / impact analysis 读
graph-patch.json     # 增量更新轻量 product graph
```

这样可以先获得知识图谱的核心收益：实体、关系、多跳扩展、影响分析，同时避免过早引入 Neo4j、GraphRAG 或复杂 AST graph。

## 用户交互

### 1. 场景候选审阅

命令：

```bash
story project scenarios review
```

交互摘要：

```text
基于项目概览，我识别到 23 个候选业务场景：

P0 建议核心回归场景 10 个
P1 常规场景 8 个
低置信度/待确认场景 5 个

请选择操作：
1. 接受 P0 核心场景
2. 按业务域审阅
3. 查看低置信度场景
4. 手工新增场景
5. 导出 YAML 草稿
```

按业务域审阅：

```text
订单域候选场景：

[x] order.withdraw        提现           confidence: high
[x] order.repay           还款           confidence: high
[x] order.overdue         逾期           confidence: high
[?] order.advance_repay   提前还款       confidence: medium
[?] order.coolingoff      coolingoff     confidence: medium
[?] order.refund          退款           confidence: low

操作：
a 接受选中
r 重命名
m 合并
d 删除
n 新增
s 跳过
```

### 2. 生成业务场景声明

用户完成选择后生成：

```text
.story/knowledge/declarations/business-scenarios.yaml
```

示例：

```yaml
apiVersion: knowledge/v1
kind: BusinessScenarios
metadata:
  product: happycash
  generatedAt: "2026-06-02"
  status: user_confirmed
domains:
  user:
    name: 用户
    scenarios:
      - id: user.register
        name: 注册
        priority: P0
        status: confirmed
        source: user_confirmed
        seed_keywords:
          - register
          - sms
          - otp
          - createUser
          - t_user
  order:
    name: 订单
    scenarios:
      - id: order.withdraw
        name: 提现
        priority: P0
        status: confirmed
        source: user_confirmed
        seed_keywords:
          - withdraw
          - borrow
          - loan
          - disburse
          - payment
```

状态定义：

| 字段 | 含义 |
| --- | --- |
| `source: ai_proposed` | AI 建议，用户未确认。 |
| `source: user_confirmed` | 用户通过 CLI 确认。 |
| `source: user_declared` | 用户主动新增。 |
| `status: proposed` | 暂不进入默认扫描。 |
| `status: confirmed` | 可以进入默认扫描。 |
| `status: deprecated` | 已废弃，保留历史。 |

### 3. 对话式新增场景

命令：

```bash
story project scenario add
```

交互：

```text
业务域是什么？
> 订单

场景名称是什么？
> 展期

这个场景有哪些关键词？可直接回车让我建议。
> extension, rollover, defer

它属于核心回归场景吗？
1. P0 核心
2. P1 常规
3. P2 低频
```

### 4. 单场景扫描前确认扫描计划

命令：

```bash
story project scenario scan order.withdraw
```

扫描前先由 Planner 生成 `scan-plan.yaml`，再展示给用户：

```text
场景：订单 / 提现

AI 计划搜索：
- 入口关键词：withdraw, borrow, loan, disburse
- 表关键词：t_loan_order, t_loan_sub_order, withdraw_account
- MQ 关键词：RISK_ORDER, RISK_RESULT, USER_LOAN_PAYMENT_NOTIFY
- 服务范围：hc-order, hc-user, hc-limit, hc-risk-management, hc-third-party, hc-callback

请选择：
1. 开始扫描
2. 补充关键词
3. 限定服务范围
4. 返回修改场景定义
```

### 5. 扫描后确认结果

扫描完成后展示：

```text
order.withdraw 扫描完成。

高置信事实：32 条
中置信事实：11 条
待确认项：6 条
无效 source_refs：2 条

请选择：
1. 接受高置信事实，其他进入待确认
2. 打开待确认项
3. 重新扫描并补充关键词
4. 暂存，不更新正式知识包
```

## Workflow

### 总体流程

```text
story project scenarios review
  -> Planner proposes candidate scenarios
  -> User confirms scenario boundary
  -> story-lifecycle writes business-scenarios.yaml

story project scenario scan <scenario-id>
  -> Planner builds scan-plan.yaml
  -> User confirms scan plan
  -> Executor scans code and writes drafts
  -> Lint validates deterministic rules
  -> Reviewer reviews drafts and lint result
  -> User confirms merge
  -> story-lifecycle merges knowledge pack
```

### Knowledge Run 目录

每次扫描创建独立 run：

```text
.story/knowledge/runs/
  scenario-scan-order.withdraw-20260602-143000/
    input.yaml
    scan-plan.yaml
    executor-prompt.md
    executor-output.json
    draft-scenario.md
    draft-index.md
    graph-patch.json
    scenario-index.json
    review.md
    lint-result.json
    merge-decision.yaml
```

### Scan Plan

`scan-plan.yaml` 是 Planner 给 Executor 的结构化协议。

```yaml
apiVersion: knowledge/v1
kind: ScenarioScanPlan
scenario:
  id: order.withdraw
  domain: order
  name: 提现
priority: P0
adapters:
  executor: claude
  reviewer: codex
scope:
  services:
    - hc-order
    - hc-user
    - hc-limit
    - hc-risk-management
    - hc-third-party
    - hc-callback
codegraph_provider:
  name: colbymchenry-codegraph
  type: mcp_or_cli
  mode: optional
  index_location: .codegraph/
  fallback: rg
codegraph_facts:
  - type: Api
    id: api:hc-order:withdraw
    source: codegraph
    status: extracted
    codegraph_id: hc-order/.../WithdrawController.java#withdraw
    source_refs:
      - hc-order/.../WithdrawController.java:42
  - type: Call
    id: call:WithdrawController.withdraw->RiskService.check
    source: codegraph
    status: extracted
    source_refs:
      - hc-order/.../WithdrawController.java:58
      - hc-limit/.../RiskService.java:31
business_mapping_needed:
  - order.withdraw 场景是否包含 coupon final bind
  - withdraw 主流程和放款回调是否应拆成两个场景
artifact_registry_refs:
  - id: artifact:api-index
    path: .story/knowledge/indexes/api-index.md
    reason: existing API index for candidate entrypoints
  - id: artifact:bug-recent
    path: docs/bugs/
    reason: recent bug records may identify regression risks
search_keys:
  api:
    - withdraw
    - borrow
    - loan
    - disburse
  table:
    - t_loan_order
    - t_loan_sub_order
    - withdraw_account
  mq:
    - RISK_ORDER
    - RISK_RESULT
    - USER_LOAN_PAYMENT_NOTIFY
  enum:
    - OrderStatusEnum
    - RiskStatusEnum
required_outputs:
  - draft-scenario.md
  - draft-index.md
  - scenario-index.json
  - graph-patch.json
  - scan-result.json
rules:
  - conclusions_without_source_refs_must_be_proposed
  - do_not_mark_verified
  - write_only_to_run_directory
```

`artifact_registry_refs` 来自 `.story/artifacts/registry.json` 或后续等价 registry 文件。P0 可以先允许该字段为空；P1 开始，Planner 应优先利用 registry refs 生成搜索计划。

`codegraph_provider` 和 `codegraph_facts` 是可选增强。没有 CodeGraph 时，Planner 必须能回退到 `rg` 和现有索引；有 CodeGraph 时，Executor 应优先使用 `codegraph_facts` 作为确定性代码事实，再把业务归属、主流程边界和测试风险交给 LLM/用户判断。`business_mapping_needed` 用来明确哪些问题不能由 CodeGraph 直接证明。

### Scan Result

Executor 输出 `executor-output.json`：

```json
{
  "scenario_id": "order.withdraw",
  "artifacts": {
    "draft_scenario": ".story/knowledge/runs/scenario-scan-order.withdraw-20260602-143000/draft-scenario.md",
    "draft_index": ".story/knowledge/runs/scenario-scan-order.withdraw-20260602-143000/draft-index.md",
    "scenario_index": ".story/knowledge/runs/scenario-scan-order.withdraw-20260602-143000/scenario-index.json",
    "graph_patch": ".story/knowledge/runs/scenario-scan-order.withdraw-20260602-143000/graph-patch.json"
  },
  "stats": {
    "entrypoints": 4,
    "services": 6,
    "tables": 8,
    "mq_tags": 3,
    "pending_review_items": 6
  },
  "open_questions": [
    "coupon final bind 是否属于提现主流程，还是优惠券子场景？"
  ]
}
```

### Scenario Index

`scenario-index.json` 是 Scenario Knowledge Layer 的关键结构化产物。它把 `scenario.md` 中的人类可读内容转成 Context Builder、impact analysis 和 regression selection 可直接消费的实体关系。

示例：

```json
{
  "apiVersion": "knowledge/v1",
  "kind": "ScenarioIndex",
  "scenario_id": "order.withdraw",
  "domain": "order",
  "name": "提现",
  "status": "extracted",
  "completeness": "B",
  "source_run": ".story/knowledge/runs/scenario-scan-order.withdraw-20260602-143000",
  "artifact_ids": [
    "artifact:api-index",
    "artifact:bug-recent"
  ],
  "nodes": {
    "services": [
      {"id": "service:hc-order", "name": "hc-order", "source_refs": ["hc-order/pom.xml"]}
    ],
    "apis": [
      {
        "id": "api:hc-order:withdraw",
        "service": "hc-order",
        "method": "POST",
        "path": "/order/withdraw",
        "codegraph_id": "hc-order/.../WithdrawController.java#withdraw",
        "source_refs": ["hc-order/.../WithdrawController.java:42"]
      }
    ],
    "tables": [
      {
        "id": "table:hc_order.t_loan_order",
        "database": "hc_order",
        "table": "t_loan_order",
        "source_refs": ["hc-order/sql/init.sql:120"]
      }
    ],
    "fields": [
      {
        "id": "field:hc_order.t_loan_order.order_status",
        "table": "hc_order.t_loan_order",
        "field": "order_status",
        "source_refs": ["hc-order/.../LoanOrder.java:37"]
      }
    ],
    "mq_tags": [
      {
        "id": "mq:happyCash.RISK_ORDER",
        "topic": "happyCash",
        "tag": "RISK_ORDER",
        "source_refs": ["hc-limit/.../RiskProducer.java:51"]
      }
    ],
    "state_machines": [
      {
        "id": "state:order.withdraw.order_status",
        "field": "order_status",
        "enum": "OrderStatusEnum",
        "source_refs": ["hc-order/.../OrderStatusEnum.java:1"]
      }
    ],
    "bugs": [
      {
        "id": "bug:withdraw-callback-timeout",
        "status": "proposed",
        "source_refs": ["docs/bugs/withdraw-callback-timeout.md"]
      }
    ],
    "test_cases": [
      {
        "id": "test:order.withdraw.callback-success",
        "status": "proposed",
        "source_refs": [".story/knowledge/scenarios/order/withdraw.md#测试回归点"]
      }
    ]
  },
  "edges": [
    {"from": "scenario:order.withdraw", "to": "service:hc-order", "type": "INVOLVES_SERVICE"},
    {"from": "api:hc-order:withdraw", "to": "table:hc_order.t_loan_order", "type": "WRITES_TABLE"},
    {"from": "scenario:order.withdraw", "to": "mq:happyCash.RISK_ORDER", "type": "PRODUCES_MQ"}
  ],
  "pending_scan": [
    {
      "section": "state-machine",
      "reason": "状态枚举证据不足",
      "next_scan_keys": ["OrderStatusEnum", "withdraw status"]
    }
  ]
}
```

Merge 后的正式路径：

```text
.story/knowledge/indexes/by-scenario/order.withdraw.json
```

`scenario-index.json` 是 graph patch 和 Context Builder expand 的主要输入。Markdown 不再承担系统级关系解析职责。

### Graph Patch

`graph-patch.json` 是对 `graph/product-context-graph.json` 的增量修改，不直接写最终图。

示例：

```json
{
  "apiVersion": "knowledge/v1",
  "kind": "GraphPatch",
  "patch_id": "scenario-scan-order.withdraw-20260602-143000",
  "base_graph": ".story/knowledge/graph/product-context-graph.json",
  "scenario_id": "order.withdraw",
  "operations": {
    "add_nodes": [
      {
        "id": "scenario:order.withdraw",
        "type": "Scenario",
        "name": "提现",
        "domain": "order",
        "status": "extracted",
        "source_refs": [
          ".story/knowledge/scenarios/order/withdraw.md"
        ]
      },
      {
        "id": "service:hc-order",
        "type": "Service",
        "name": "hc-order",
        "status": "extracted",
        "source_refs": [
          "hc-order/pom.xml"
        ]
      },
      {
        "id": "api:hc-order:withdraw",
        "type": "Api",
        "name": "POST /order/withdraw",
        "status": "extracted",
        "codegraph_id": "hc-order/.../WithdrawController.java#withdraw",
        "source_refs": [
          "hc-order/.../WithdrawController.java:42"
        ]
      }
    ],
    "add_edges": [
      {
        "from": "scenario:order.withdraw",
        "to": "service:hc-order",
        "type": "INVOLVES_SERVICE",
        "status": "extracted",
        "source_refs": [
          ".story/knowledge/scenarios/order/withdraw.md#代码链路"
        ]
      }
    ],
    "update_nodes": [
      {
        "id": "scenario:order.withdraw",
        "set": {
          "last_scanned_run": "scenario-scan-order.withdraw-20260602-143000"
        }
      }
    ]
  }
}
```

Patch 应用规则：

- `add_nodes` 如果节点已存在，则按 `id` 合并 `source_refs`，不得覆盖已有 `verified` 字段。
- `add_edges` 如果边已存在，则合并 `source_refs`。
- `update_nodes` 只能更新非人工确认字段，例如 `last_scanned_run`、`last_seen_commit`、`summary`。
- 如果 patch 试图把 `proposed/extracted` 升级为 `verified`，lint 必须失败。
- 冲突写入 `reviews/pending-review-items.md`，不自动覆盖。
- `codegraph_id` 只是可选外键，用于未来回查辅助索引；merge 后的业务图必须仍以 `id` 和 `source_refs` 为主。
- patch 不得把 CodeGraph 的 raw method/call graph 整体导入 `product-context-graph.json`。只合并已经映射到 Scenario/Service/Api/Table/Mq/Bug/TestCase 等业务节点的事实。

## KnowledgeRunState

Scenario scan 使用独立 run state，不混用 `StoryState`。

```text
created
  -> planning
  -> plan_confirming
  -> executing
  -> linting
  -> reviewing
  -> merge_confirming
  -> merging
  -> completed
```

异常和人工路径：

```text
planning -> failed
executing -> failed
linting -> executing       # 用户选择补扫或修复 draft
linting -> plan_confirming # 用户选择调整 scan plan
reviewing -> executing     # reviewer 要求补扫
reviewing -> merge_confirming
merge_confirming -> rejected
merge_confirming -> merging
merging -> completed
```

状态字段：

```yaml
run_id: scenario-scan-order.withdraw-20260602-143000
kind: scenario_scan
scenario_id: order.withdraw
status: executing
workspace: .
created_at: "2026-06-02T14:30:00+08:00"
updated_at: "2026-06-02T14:40:00+08:00"
planner:
  adapter: codex
executor:
  adapter: claude
reviewer:
  adapter: codex
artifacts:
  scan_plan: scan-plan.yaml
  draft_scenario: draft-scenario.md
  lint_result: lint-result.json
decision:
  last_gate: lint
  allowed_actions: [rescan, adjust_plan, reject, merge]
```

## 场景文档模板

每个场景最终生成：

```text
.story/knowledge/scenarios/<domain>/<scenario>.md
```

模板：

```markdown
# 提现

## 业务说明

## 入口
| 类型 | 服务/模块 | 类/接口/页面 | 说明 | source_refs |
| --- | --- | --- | --- | --- |

## 主流程
1. ...

## 代码链路
| 顺序 | 服务 | 类/方法 | 调用类型 | 说明 | source_refs |
| --- | --- | --- | --- | --- | --- |

## 数据交互
| 步骤 | 读/写 | 库表 | 字段 | 说明 | source_refs |
| --- | --- | --- | --- | --- | --- |

## 表结构与关键字段
| 表 | 字段 | 含义 | 生成/更新时机 | source_refs |
| --- | --- | --- | --- | --- |

## MQ
| Topic | Tag | Producer | Consumer | 触发时机 | source_refs |
| --- | --- | --- | --- | --- | --- |

## 状态机
| 状态字段 | 枚举 | 流转 | source_refs |
| --- | --- | --- | --- |

## 最近 Bug / 风险
| 问题 | 关联代码 | 关联表 | 回归建议 | source_refs |
| --- | --- | --- | --- | --- |

## 测试回归点
| 用例方向 | 前置条件 | 操作 | 期望结果 | 数据校验 |
| --- | --- | --- | --- | --- |

## 待确认项
```

## 部分成功和增量补扫

单次 LLM 扫描不要求一次产出所有章节的高质量结果。扫描结果按完整度分级：

| 等级 | 条件 | 是否可 merge |
| --- | --- | --- |
| A | 入口、代码链路、数据交互、表字段、MQ、状态机、bug/风险、测试回归点均有证据或明确无相关项。 | 可 merge。 |
| B | 入口、代码链路、数据交互、表字段完整；MQ、状态机、bug/测试存在缺口但已标 `pending_scan`。 | 可 merge，但必须保留缺失章节标记。 |
| C | 只有入口和部分表/代码证据，无法形成可回归文档。 | 不进入正式场景文档，只保留 run draft。 |
| D | source_refs 大量无效、章节缺失严重或出现乱码。 | lint fail。 |

缺失章节必须显式写入：

```markdown
## MQ

status: pending_scan
reason: 本次扫描未找到明确 MQ producer/consumer 证据。
next_scan_keys:
- RISK_ORDER
- USER_LOAN_PAYMENT_NOTIFY
```

同一场景允许多次增量扫描。后续 run 可以只补一个章节，例如：

```bash
story project scenario scan order.withdraw --section mq
story project scenario scan order.withdraw --section state-machine
```

## Lint 规则

`story project knowledge lint` 和 `scenario scan` 后置 lint 必须检查：

- `manifest.yaml` 不得使用本地路径作为正式 source。
- AI 自动生成内容不得标记为 `verified`。
- `source_refs` 文件必须存在。
- Markdown/YAML/JSON 必须可解析或可读取。
- 文件不得出现乱码特征。
- 场景文档必须包含入口、代码链路、数据交互、表字段、MQ、状态机、测试回归点、待确认项章节。
- 表字段必须来自 SQL、Entity、Mapper XML、DTO 或明确代码引用。
- 图 patch 必须只包含轻量节点和边，不承载长文本。
- Executor 只能写 run 目录，不能直接覆盖正式 knowledge。
- CodeGraph facts 只能作为 `extracted` 代码事实，不能单独把业务结论升级为 `verified`。
- 带 `codegraph_id` 的节点必须同时包含有效 `source_refs`。
- `source_refs` 必须指向源代码、SQL、配置或文档文件，不能只引用 `.codegraph/`、`.story/cache/codegraph/` 或 CodeGraph 内部 ID。
- 业务场景归属、主流程边界、测试风险等映射结论如果没有人工确认或业务文档证据，必须保持 `proposed` 或进入 pending review。
- `product-context-graph.json` 不得直接导入 raw CodeGraph 节点/边；只能合并经过 scenario 映射的业务图节点/边。

Lint 结果写入：

```text
.story/knowledge/runs/<run-id>/lint-result.json
```

## Context Builder 集成

Scenario scan merge 后，Context Builder 通过以下入口发现和使用场景文档：

| Context Builder 阶段 | 使用的文件 | 作用 |
| --- | --- | --- |
| describe | `manifest.yaml`, `business-scenarios.yaml`, `search-catalog.md` | 让 LLM 知道项目有哪些业务场景和索引结构。 |
| search | `indexes/by-scenario/<scenario-id>.md`, `indexes/by-domain/*.md`, `scenarios/**/*.md` | 按 story 关键词定位候选场景。 |
| expand | `indexes/by-scenario/<scenario-id>.json`, `graph/product-context-graph.json` | 从场景扩展到服务、API、表、字段、MQ、bug、test。 |
| compose | `scenarios/<domain>/<scenario>.md` | 注入可读的业务链路和测试回归上下文。 |

场景 scan 的索引合并规则：

- `draft-index.md` merge 到 `indexes/by-scenario/<scenario-id>.md`。
- `scenario-index.json` merge 到 `indexes/by-scenario/<scenario-id>.json`。
- `indexes/by-domain/<domain>.md` 增加该场景的摘要和链接。
- 全局 `api-index.md`、`table-index.md`、`mq-index.md` 只追加经过 lint 的新增事实。
- `search-catalog.md` 增加或更新该场景的 search keys。

Context Builder 的协议补充：

```text
describe
  -> 返回 business-scenarios.yaml、by-scenario JSON 列表、graph schema

search
  -> 优先命中 by-scenario JSON，再回退到 Markdown 和全局索引

expand
  -> 使用 scenario-index.json 的 nodes/edges 做第一跳扩展
  -> 再使用 product-context-graph.json 做跨场景扩展

compose
  -> 结构化事实来自 scenario-index.json
  -> 可读说明来自 scenario.md
  -> pending_scan / proposed 内容必须显式标注
```

## Artifact Registry 集成

Scenario scan 与 Artifact Registry 的关系：

1. Planner 读取 registry，生成 `artifact_registry_refs`。
2. Executor 在扫描时优先使用 registry refs 指向的已有资产。
3. Lint 校验 draft 中引用的 registry artifact 是否存在。
4. Merge 成功后，写回或更新 registry 条目。

merge 后 registry 条目示例：

```json
{
  "id": "knowledge:scenario:order.withdraw",
  "type": "knowledge_scenario",
  "path": ".story/knowledge/scenarios/order/withdraw.md",
  "index_path": ".story/knowledge/indexes/by-scenario/order.withdraw.json",
  "domains": ["order"],
  "scenarios": ["order.withdraw"],
  "status": "extracted",
  "source_run": ".story/knowledge/runs/scenario-scan-order.withdraw-20260602-143000",
  "source_refs": [
    "hc-order/hc-order-business/src/main/java/..."
  ]
}
```

Registry 双向映射：

- Scenario index 写 `artifact_ids`，说明该场景视图引用了哪些 registry 资产。
- Registry artifact 写 `scenario_ids`，说明该资产被哪些业务场景使用。

Artifact 示例：

```json
{
  "id": "artifact:hc-order-api-index",
  "type": "api_index",
  "path": ".story/knowledge/indexes/api-index.md",
  "status": "extracted",
  "scenario_ids": ["order.withdraw", "order.repay"],
  "knowledge_links": {
    "services": ["hc-order"],
    "apis": ["api:hc-order:withdraw"]
  }
}
```

P0 可以只生成 `business-scenarios.yaml`，暂不写 registry；P1 起 scan-plan 和 merge 必须预留 registry 字段。

## Dual Flywheel 路由

Scenario scan 的产物分为两类：

| 类型 | 内容 | 目标 |
| --- | --- | --- |
| 知识类产出 | 入口、代码链路、服务、API、表、字段、MQ、状态机 | Project Knowledge Pack |
| 质量类产出 | 最近 bug、风险、测试回归点、缺失测试、易错链路 | Quality Flywheel |

merge 时必须按类型路由：

- 知识类产出写入 `scenarios/**`、`indexes/**`、`graph/**`。
- bug 风险同步到 `indexes/bug-risk-index.md`，并可进入 quality finding proposal。
- 测试回归点同步到 `indexes/test-case-index.md`，并可作为后续 test runner / regression pack 输入。
- 未确认质量判断不能自动变成 active learned pattern，只能作为 `proposed` finding 或 pending review。

质量飞轮反哺 Scenario Knowledge Layer：

```text
verified finding
  -> scenario-index.json nodes.bugs
  -> bug-risk-index.md
  -> scenario.md 最近 Bug / 风险

verification_result
  -> scenario-index.json nodes.test_cases
  -> test-case-index.md
  -> scenario.md 测试回归点

repeated failure / flaky test
  -> pending review
  -> proposed scenario risk
```

Scenario Knowledge Layer 不直接审批 learned pattern。它只提供场景视图和证据链接，由 Quality Flywheel 负责 finding/pattern 的状态升级。

## Run 生命周期

run 目录默认保留，用作审计轨迹。P0/P1 暂不自动清理。

后续增加命令：

```bash
story project knowledge runs list
story project knowledge runs cleanup --merged-older-than 30d --rejected-older-than 7d
```

建议保留策略：

| Run 类型 | 保留策略 |
| --- | --- |
| merged | 默认保留 30 天，或直到对应 knowledge commit 推送成功。 |
| rejected | 默认保留 7 天。 |
| failed | 默认保留 7 天，方便排查。 |
| active/pending | 不自动清理。 |

## 与现有 Orchestrator 的关系

### 应该复用

- Adapter 和 headless launch。
- Tool registry。
- Planner/Reviewer 的 LLM 调用通道。
- GateDecision 的决策模型。
- event log 和 retry 计数思想。
- path registry 的单一入口原则。

### 应该新增

- `KnowledgeRunState`：knowledge run 专用状态，不混用 `StoryState`。
- `AdapterRunner`：从 `BaseTool` 中抽出的通用 headless 执行器。
- `knowledge/planner.py`：知识场景专用 Planner/Reviewer prompt。
- `knowledge/runs.py`：run id、run 目录、artifact 写入。
- `knowledge/lint.py`：确定性校验。
- `knowledge/merge.py`：从 draft 合并到正式 knowledge pack。
- `orchestrator/tools/knowledge_scenario_tool.py`：执行 scan-plan 的工具。

### 不应该复用

- 不直接复用 `.story/done/<story_key>/<stage>.json`。
- 不直接复用 `planner.plan_stage()` 的开发阶段 prompt。
- 不直接复用 `ReviewTool` 的代码审查 prompt。
- 不把 knowledge scan 注册为普通 story stage。

## 命令设计

```bash
story project scenarios review
story project scenario add
story project scenario list
story project scenario scan <scenario-id>
story project scenario status <scenario-id>
story project knowledge lint
story project knowledge merge <run-id>
```

可选参数：

```bash
story project scenario scan order.withdraw --planner codex --executor claude --reviewer codex
story project scenario scan order.withdraw --headless
story project scenario scan order.withdraw --dry-run
story project scenario scan order.withdraw --services hc-order,hc-limit
```

## 分阶段落地

### P0: 交互和声明

- 新增 `scenarios review`。
- 新增 `scenario add/list`。
- 写入 `business-scenarios.yaml`。
- 修正 `.story/knowledge/.gitignore`，不要忽略核心 `indexes/` 和 `graph/`。
- 在设计和 CLI 帮助中使用 Scenario Knowledge Layer 定位。

验收标准：

- `story project scenarios review` 能展示候选业务场景，并支持接受核心场景。
- `story project scenario add` 能新增用户声明场景。
- `business-scenarios.yaml` 包含 `ai_proposed`、`user_confirmed`、`user_declared` 来源字段。
- `init-knowledge` 的边界被明确为概览层，不再要求一次产出完整场景链路。
- 文档和 CLI 帮助能说明下一步使用 `scenario scan <id>` 深扫。
- `business-scenarios.yaml` 能作为 Context Builder describe 阶段的输入。

### P1: 单场景深扫

- 新增 `scenario scan <id>`。
- 生成 `scan-plan.yaml`。
- 调 executor adapter 写 run draft。
- 生成 `draft-scenario.md`、`draft-index.md`、`scenario-index.json`、`graph-patch.json`。

验收标准：

- 对一个 confirmed 场景可以创建独立 run 目录。
- Planner 能生成结构化 `scan-plan.yaml`，包含 `scenario_id`、`scope`、`search_keys`、`required_outputs`。
- Planner 能在有 CodeGraph 时写入 `codegraph_provider` 和 `codegraph_facts`，在没有 CodeGraph 时回退到 `rg` 和已有索引。
- Executor 只能写 run 目录。
- 至少一个 P0 场景能生成 `draft-scenario.md` 和 `draft-index.md`。
- `scenario-index.json` 能表达 services、apis、tables、fields、mq_tags、state_machines、bugs、test_cases 和 edges。
- `graph-patch.json` 符合 schema，并能被 lint 读取。

### P2: Lint + Review + Merge

- 新增 `knowledge lint`。
- 新增 Reviewer 审查扫描结果。
- 新增 `knowledge merge <run-id>`。
- 支持用户确认后合并正式 knowledge。

验收标准：

- lint 能发现无效 source_ref、乱码、自动 verified、缺失必填章节。
- lint 能发现只有 `codegraph_id`、没有有效源文件 `source_refs` 的事实。
- lint 能阻止 raw CodeGraph 图直接进入 `product-context-graph.json`。
- B 级部分成功结果可 merge，但缺失章节必须标 `pending_scan`。
- merge 后正式写入 `scenarios/<domain>/<scenario>.md` 和 `indexes/by-scenario/<id>.md`。
- merge 后正式写入 `indexes/by-scenario/<id>.json`。
- graph patch 可应用到 `product-context-graph.json`，冲突进入 pending review。
- Reviewer 可以基于 draft 和 lint result 给出 pass/revise/fail。

### P3: 增量进化

- `sync-knowledge` 根据 Git diff 判断受影响场景。
- 最近 bug 自动关联到场景。
- PRD/spec/plan/done/finding/pattern 反哺到场景文档。
- review queue 支持人工确认后升级状态。

验收标准：

- 修改服务/API/表/MQ 相关代码后，`sync-knowledge` 能提示受影响场景。
- 如果存在 CodeGraph index，`sync-knowledge` 可用它发现变化的代码实体；正式知识仍通过 `source_refs` 标记 stale 或触发局部 rescan。
- 最近 bug 能关联到已有场景或进入候选场景 review。
- Context Builder 能基于 story 描述命中相关场景文档。
- Context Builder 能使用 `scenario-index.json` 从场景扩展到服务/API/表/MQ/bug/test。
- Quality Flywheel 能接收 scenario scan 产生的 bug 风险和测试回归点。
- registry 能记录 scenario knowledge artifact，并被后续 scan plan 引用。

## 风险和处理

| 风险 | 处理 |
| --- | --- |
| AI 误判业务边界 | AI 只提候选，用户确认后才进入 YAML。 |
| 扫描范围过大 | 单场景扫描，扫描前确认 plan，支持限定服务和补充关键词。 |
| Claude 直接改正式文件 | Executor prompt 和 lint 双重限制，只允许写 run 目录。 |
| Codex/Claude 输出互相误解 | 使用 `scan-plan.yaml`、`executor-output.json`、`lint-result.json` 结构化协议。 |
| 产物不可信 | 未经 source_refs 和 lint 的内容只能是 `proposed`。 |
| 运行状态丢失 | run 目录记录所有中间产物，可审计、可重试。 |
| CodeGraph 被误当知识库本体 | CodeGraph 只放 cache/外部索引；正式知识只 merge 到 `.story/knowledge/` 文本文件。 |
| CodeGraph provider 不可用 | `scan-plan.yaml` 声明 fallback，回退到 `rg`、Artifact Registry 和现有 indexes。 |

## 成功标准

- 用户可以通过 CLI 确认 hc 的业务场景，并生成 `business-scenarios.yaml`。
- 可以对 `user.register` 或 `order.withdraw` 单场景扫描并生成可读场景文档。
- 可以生成结构化 `scenario-index.json`，供 Context Builder 和影响分析使用。
- 可以选择性接入 CodeGraph 作为辅助检索来源，但删除 CodeGraph index 后正式 knowledge pack 仍可使用。
- 场景文档包含入口、代码链路、数据交互、表字段、MQ、状态机、测试回归点。
- 所有关键结论都有有效 `source_refs` 或进入待确认项。
- Lint 能阻止本地路径、乱码、无效 source_ref、自动 verified 和缺失必填章节。
- 正式 knowledge pack 只通过 merge 更新。
