# Project Intelligence Bootstrap Design

## 背景

Story Lifecycle 当前能编排一个 story 经过 design、implement、test、review 等阶段，但每个阶段的 AI 仍然主要依赖当前 prompt 和临时搜索来理解项目。对 HappyCash 这类多服务业务系统来说，AI 在进入需求前需要先知道产品、业务域、服务、接口、表、MQ、状态机、历史 bug 和测试资产之间的关系。

本设计引入 **Project Intelligence Bootstrap（项目智能初始化）**：在项目级别生成本地知识包和轻量关系图，保存在 `.story/knowledge/`，让本地 AI 在后续 story 中按需构建上下文。第一阶段采用 Prompt/CLI-first，不先实现复杂扫描器、向量库或远程平台。

## 目标

- 项目初始化时生成 `.story/knowledge/`。
- 让本地 CLI agent 读取代码、文档、bug、测试和配置，生成 Knowledge Pack。
- 将服务、接口、表、字段、MQ、状态机、bug、测试用例连接成轻量图。
- 在 story 阶段前，通过 CLI headless 生成可审计的 knowledge context packet。
- 跑稳定后，再同步到 `ys-agent` 做远程审核、发布、公司级 Skill 和权限审计。

## 非目标

- P0 不实现 AST/Java/Python/前端扫描引擎。
- P0 不上向量库、Embedding、GraphRAG 或图数据库。
- P0 不实现远程 `ys-agent` API。
- P0 不把本地路径作为远程可信来源。
- P0 不要求每个 story 都全量重建知识包。

## 总体原则

1. **Local-first**：先在 `story-lifecycle` 本地跑通，再远程化到 `ys-agent`。
2. **Prompt/CLI-first**：先让 CLI headless 生成知识包和上下文包，稳定后再工具化。
3. **File-first**：产物是 Markdown、YAML、JSON 文件，优先可读、可 diff、可审计。
4. **Evidence-first**：关键结论必须带 `source_refs`。没有证据的内容标记为 `proposed` 或进入 pending review。
5. **Graph-as-navigation**：图只做导航和多跳扩展，不承载长文本正文。
6. **Product-first**：知识包以产品/业务系统为单位，不以代码仓为单位。

## 本地目录

所有本地产物放在项目工作区的 `.story/knowledge/` 下，不新增第二个点目录。

```text
.story/
  knowledge/
    product.yaml
    manifest.yaml
    search-catalog.md

    scenarios/
      <domain>/

    indexes/
      service-index.md
      api-index.md
      feign-index.md
      table-index.md
      field-index.md
      mq-index.md
      state-machine-index.md
      enum-index.md
      bug-risk-index.md
      test-case-index.md
      by-domain/
        <domain>.md

    graph/
      product-context-graph.json
      product-context-graph.md

    playbooks/
      regression-playbook.md
      production-troubleshooting-playbook.md
      release-review-playbook.md

    declarations/
      manual-context.yaml
      critical-flows.yaml

    reviews/
      pending-review-items.md
      review-log.md

    events/
      local-skill-events.jsonl

    cache/
```

建议生成 `.story/knowledge/.gitignore`：

```gitignore
/indexes/
/graph/
/events/
/cache/
/reviews/pending-review-items.md
```

可入 Git 的内容：

- `product.yaml`
- `manifest.yaml`
- `search-catalog.md`
- `scenarios/**`
- `playbooks/**`
- `reviews/review-log.md`

默认不入 Git 的内容：

- `indexes/**`
- `graph/**`
- `events/**`
- `cache/**`
- `reviews/pending-review-items.md`

索引目录规则：

- `indexes/*.md` 是产品级全局事实索引，面向 AI 和精确检索。
- `indexes/by-domain/<domain>.md` 是业务域视图，面向人读和测试回归。
- 每个全局索引条目必须至少被一个 `by-domain` 文件引用，避免全局索引演进成不可读的超长列表。

## 触发方式

显式命令：

```bash
story project init-knowledge
```

增量更新命令：

```bash
story project sync-knowledge
```

`sync-knowledge` 是 P1 必须补齐的能力，避免知识包在代码日更后快速腐烂。P1 可以先做轻量版本：

- 读取当前 Git commit、dirty 状态和 `.story/knowledge/manifest.yaml` 中的 source 信息。
- 如果 commit 变化或关键源文件修改时间晚于知识包生成时间，标记知识包为 stale。
- 基于 Git diff 或修改文件路径生成局部刷新任务。
- 暂时无法可靠刷新时，生成强警告并写入 `reviews/pending-review-items.md`。

首次创建 story 时，如果缺少 `.story/knowledge/manifest.yaml`，只提示，不自动生成：

```text
当前项目尚未初始化项目知识包。建议先运行：
story project init-knowledge

继续创建 story 也可以，但 AI 将缺少项目级业务/代码上下文。
```

## Bootstrap 工作流

`story project init-knowledge` 启动一个特殊 profile 或一次性 stage：

```text
PROJECT-KNOWLEDGE-INIT
  -> knowledge_bootstrap
```

`knowledge_bootstrap` 使用 CLI headless 执行 `project-knowledge-bootstrap` prompt。CLI 可以使用只读工具搜索和读取项目文件，但不能修改业务代码。

输出：

```text
.story/knowledge/manifest.yaml
.story/knowledge/product.yaml
.story/knowledge/search-catalog.md
.story/knowledge/scenarios/**
.story/knowledge/indexes/**
.story/knowledge/graph/product-context-graph.json
.story/knowledge/reviews/pending-review-items.md
```

完成文件：

```text
.story/done/PROJECT-KNOWLEDGE-INIT/knowledge_bootstrap.json
```

Done JSON 示例：

```json
{
  "knowledge_manifest": ".story/knowledge/manifest.yaml",
  "scenario_docs": [".story/knowledge/scenarios/order/withdraw.md"],
  "index_docs": [".story/knowledge/indexes/api-index.md"],
  "graph_json": ".story/knowledge/graph/product-context-graph.json",
  "search_catalog": ".story/knowledge/search-catalog.md",
  "pending_review": ".story/knowledge/reviews/pending-review-items.md",
  "summary": "Generated initial project knowledge pack."
}
```

## Context Builder 工作流

在正式阶段前，`story-lifecycle` 可以调用 CLI headless 生成某个 story/stage 的知识上下文包。

输入：

- story title
- story description / PRD / TAPD 内容
- target stage，例如 `design`、`implement`、`test`
- `.story/knowledge/manifest.yaml`
- `.story/knowledge/search-catalog.md`
- `.story/knowledge/graph/product-context-graph.json`

输出：

```text
.story/context/<story_key>/knowledge-context/<stage>.md
.story/context/<story_key>/knowledge-context/<stage>.json
```

JSON 示例：

```json
{
  "story_key": "STORY-12345",
  "target_stage": "design",
  "selected_context": [
    {
      "type": "scenario",
      "id": "order.withdraw",
      "source": ".story/knowledge/scenarios/order/withdraw.md",
      "status": "verified",
      "reason": "story 涉及提现失败提示"
    }
  ],
  "context_packet": ".story/context/STORY-12345/knowledge-context/design.md"
}
```

## describe/search/expand/compose 协议

P0/P1 不把 Retriever 做成 Python API，而是把以下四步写进 CLI prompt，由 CLI headless 执行。

### 1. Describe

读取以下文件，理解项目知识结构：

- `.story/knowledge/manifest.yaml`
- `.story/knowledge/search-catalog.md`
- `.story/knowledge/graph/product-context-graph.json`

输出当前 story 的初步检索计划，包括可能的业务域、关键词、目标索引和需要的上下文类型。

### 2. Search

P0 可以让 CLI 直接使用 `rg`，但 P1 应该收敛成结构化 Search Tool，避免 LLM 自由拼 shell 或正则导致空转。工具接口可以先保持很小：

```json
{
  "type": "api|table|field|mq|service|scenario|bug|test_case|text",
  "keyword": "withdraw",
  "target_paths": [".story/knowledge/indexes/api-index.md"],
  "limit": 20
}
```

工具内部负责转成安全的文件搜索、正则转义、结果截断和路径限制。CLI 仍然负责生成搜索计划，但不直接拼接 shell。

根据检索计划执行精确搜索，优先使用符号、路径、表名、字段名、API path、MQ tag、bug 关键词。

示例：

```bash
rg -n "withdraw|提现|OrderWithdraw|Maya|PAYMAYA|t_order" .story/knowledge
```

### 3. Expand

从命中的 seed ids 出发读取 `product-context-graph.json`，扩展到相邻节点：

```text
scenario -> service -> api -> table -> field -> mq -> bug -> test_case
```

### 4. Compose

后续版本需要加入 token budget 控制：按 target stage 限制 context packet 大小，超预算时按 verified > extracted > proposed、scenario > bug > api/table > long explanation 的优先级裁剪，并记录被舍弃的节点。该能力不阻塞 P1，可放入 P2/P3。

生成精简、可注入、可审计的 context packet。packet 必须包含：

- 为什么选择这些上下文
- 关键证据和 source refs
- 与当前 stage 相关的风险点
- 建议执行 AI 关注的文件、接口、表、测试或回归点
- 不确定项和待确认项

## Knowledge 状态

本地知识不做完整审核系统，但所有内容必须带轻量状态：

```text
extracted
  自动从代码/文件中抽取的事实。

proposed
  AI 根据证据推断的语义内容。

verified
  人工确认过的内容。
```

测试助手或执行 AI 使用时：

- `verified` 可作为正式依据。
- `extracted` 可作为代码事实引用。
- `proposed` 必须标记为待确认，不能作为关键结论单独使用。

## Graph Schema

P0 使用轻量关系图。节点只保存摘要和 `source_refs`，详细内容留在 Markdown 和 indexes 中。

核心节点类型：

```text
Product
Domain
Scenario
Repository
Service
Api
Feign
Table
Field
MqMessage
StateMachine
State
Bug
TestCase
Playbook
CodeSymbol
Doc
```

核心关系类型：

```text
HAS_DOMAIN
HAS_SCENARIO
USES_SERVICE
EXPOSES_API
CALLS_FEIGN
READS_TABLE
WRITES_TABLE
HAS_FIELD
PUBLISHES_MQ
CONSUMES_MQ
HAS_STATE_MACHINE
HAS_STATE
AFFECTS_SCENARIO
COVERS_SCENARIO
GUARDS_BUG
DESCRIBES
SOURCE_REF
```

## Scan Profiles

设计上支持多个扫描 profile，但第一阶段仍由 CLI 通过 prompt 使用这些 profile 指南，不实现专用 parser。

第一批 profile：

```text
java-spring-microservice
frontend-react-umi
python-service
```

Java/Spring 深度：

- 服务目录
- Controller/API
- Feign client
- Entity/DTO
- Mapper XML
- SQL 文件
- 表/字段
- MQ producer/consumer/tag
- enum/status constant

前端深度：

- 路由
- 页面
- API service 调用
- TypeScript types
- 权限点
- 用户入口

Python 深度：

- FastAPI/Flask 路由
- CLI/脚本入口
- SQL
- 配置
- 定时任务
- MCP tools

## Manual Declarations

Spring AOP、动态 Feign URL、运行时配置、反射、拦截器和隐式状态流转可能无法通过 Prompt/CLI 稳定发现。设计需要保留人工声明入口，作为扫描和推断的兜底。

目录：

```text
.story/knowledge/declarations/
  manual-context.yaml
  critical-flows.yaml
```

`manual-context.yaml` 用于补充服务依赖、隐式切面、动态 URL、特殊配置和 owner 信息。`critical-flows.yaml` 用于声明必须被知识包覆盖的核心链路，例如注册、授信、提现、还款、账号合并、coolingoff。

Context Builder 必须优先读取 declarations，并把其中内容标记为 `verified` 或 `manual` 来源。

## 与 Story Lifecycle 集成

P1 集成点：

- 新增 `story project init-knowledge` 命令。
- 生成 `PROJECT-KNOWLEDGE-INIT` 特殊 story 或直接执行一次性 workflow。
- 校验 done JSON 和关键文件存在。

P2 集成点：

- 在 `plan_stage` 前可选执行 Context Builder。
- Planner 读取 `.story/context/<story>/knowledge-context/<stage>.md` 后生成阶段任务书。
- Executor 只接收 compact context packet，不接收完整知识包。

## 与本地 Skill 集成

本地 skill 继续作为现场事件来源：

```text
knowledge-capture -> knowledge.captured
bug-track -> bug.recorded
test-runner -> test.completed / test.failed
pre-release-review -> release.reviewed
product-health-monitor -> inspection.suggested / inspection.failed
```

事件先写入：

```text
.story/knowledge/events/local-skill-events.jsonl
```

后续同步到 `ys-agent`。

## 与 ys-agent 的关系

本地阶段：

```text
story-lifecycle
  生成 .story/knowledge
  生成 context packet
  支撑本地 AI 跑通需求生命周期
```

远程阶段：

```text
ys-agent
  接收稳定后的 Knowledge Pack
  做审核、发布、权限、审计
  提供公司级测试助手、发布评审助手、生产排查助手
```

`ys-agent` 不应依赖本地路径作为正式 source。远程 manifest 必须使用 Git repo + commit。

## 优先级

### P0：设计和模板

- 本设计文档。
- `.story/knowledge` 目录标准。
- manifest、search-catalog、graph、scenario 模板。
- bootstrap prompt 模板。
- context builder prompt 模板。

### P1：本地 Bootstrap

- `story project init-knowledge` 命令。
- `story project sync-knowledge` 轻量增量更新和 stale 检测。
- 调 CLI headless 执行 bootstrap prompt。
- 生成 `.story/knowledge`。
- 校验关键产物和 done JSON。
- 提供结构化 Search Tool 的最小版本，限制 LLM 直接拼 shell。

### P2：本地 Context Packet

- 在 stage 前调用 CLI headless 执行 context builder prompt。
- 生成 `.story/context/<story>/knowledge-context/<stage>.md`。
- Planner 读取 context packet。

### P3：稳定步骤工具化

- 将高频稳定的 describe/search/expand/compose 沉淀成本地 helper。
- 保留 CLI fallback。
- 加入 context token budget、裁剪策略和丢弃节点记录。

### P4：远程化到 ys-agent

- 同步 Knowledge Pack。
- 远程审核和发布。
- 公司级 Skill 使用。

### P5：语义检索和 GraphRAG

- 仅在 agentic search 不够时引入 embedding、rerank、GraphRAG。
- 接入 `aiops-mcp` 支撑巡检、日志、DB、trace。

## 验收标准

- 运行一次初始化后，`.story/knowledge/manifest.yaml`、`search-catalog.md`、`product-context-graph.json` 存在。
- 至少一个业务域、一个场景、一个索引文件被生成。
- 所有关键结论都有 `source_refs` 或进入 pending review。
- 对一个 story 可以生成 stage-specific knowledge context packet。
- context packet 小而可读，包含证据来源和待确认项。
- source commit 或关键源文件变化时，系统能识别 stale 并提示运行 `story project sync-knowledge`。
- Search 阶段支持结构化参数，不要求 LLM 直接拼接 shell。
- `indexes/by-domain` 能覆盖全局索引中的业务域条目。
- 不修改业务代码。
