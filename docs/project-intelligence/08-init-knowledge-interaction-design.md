# Init Knowledge 交互设计

本文定义 `story project init-knowledge` 的初始化交互。

核心思路：可以借鉴 CodeGraph 类工具的交互方式，先扫描，再展示项目概览，再让用户确认扫描范围，最后生成稳定的项目知识文件。但不能把 Story Lifecycle 做成单纯的代码索引工具。

Story 初始化比 CodeGraph 初始化更宽：它不仅看代码，还要识别文档、测试、bug 记录、产品边界、候选业务域，以及下一步应该执行的知识动作。

## 定位

`init-knowledge` 是项目知识库的概览初始化阶段。

它应该做：

- 识别本地项目结构。
- 在写入正式知识文件前，先展示扫描概览。
- 给出推荐的 P0 初始化范围。
- 允许用户接受、编辑、排除或手工新增扫描范围。
- 生成 `.story/knowledge/` 下的项目概览文件。
- 给出下一步建议命令，例如场景确认和单场景深扫。

它不应该做：

- 不生成完整业务场景文档。
- 不在初始化阶段要求用户确认每一条业务流程。
- 不强依赖 CodeGraph。
- 不把 CodeGraph index 当成知识库本体。
- 不扫描前端 `node_modules` 或其他生成/依赖目录。
- 不因为远端 `ys-agent` 不可用而阻塞本地初始化。

## 设计原则

借鉴 CodeGraph 的交互，不借鉴 CodeGraph 的知识归属。

```text
可以借鉴：
  - 扫描概览
  - 语言和文件数量统计
  - include / exclude 范围确认
  - 进度反馈
  - 下一步建议命令

不能借鉴：
  - 把代码索引当最终知识
  - 把全量代码扫描当唯一初始化目标
  - 把 raw graph 当产品知识图谱
```

最终知识本体仍然是：

```text
.story/knowledge/
  product.yaml
  manifest.yaml
  search-catalog.md
  indexes/**
  graph/**
  declarations/**
```

辅助索引可以放在：

```text
.codegraph/
.story/knowledge/cache/codegraph/
```

这些只是 cache，可以删除、重建，不能作为正式知识来源。

## 可选方案

### 方案 A：默认交互式向导

先做项目探测，展示 CLI 向导，让用户确认扫描范围，再写入 `.story/knowledge`。

优点：

- 最适合首次初始化。
- 可以避免扫错目录。
- 能让用户在消耗扫描成本前排除前端或低价值服务。

缺点：

- 比非交互模式稍慢。

建议：作为默认方案。

### 方案 B：非交互默认执行

自动接受推荐 P0 范围，不询问用户。

优点：

- 适合 CI 或重复执行。
- 适合交给其他 AI 自动执行。

缺点：

- 如果项目结构特殊，可能包含过多或漏掉关键目录。

建议：通过 `--yes` 支持，但首次运行不默认使用。

### 方案 C：CodeGraph Provider 优先

先调用 CodeGraph 做代码结构探测，再展示向导。

优点：

- Java/Spring 代码事实更稳定。
- 有助于识别 API、Feign、Mapper、Entity、MQ、Job 等节点。

缺点：

- provider 可用性和 ignore 规则可能不稳定。
- 不能成为硬依赖。

建议：只作为可选 provider。失败时回退到 `rg` 和文件系统探测。

## 命令形态

默认交互命令：

```bash
story project init-knowledge
```

非交互命令：

```bash
story project init-knowledge --yes
```

显式指定范围：

```bash
story project init-knowledge --include hc-user,hc-order,hc-limit --exclude frontends
```

启用可选 CodeGraph provider：

```bash
story project init-knowledge --codegraph optional
```

关闭 CodeGraph provider：

```bash
story project init-knowledge --codegraph off
```

只预览不写入：

```bash
story project init-knowledge --dry-run
```

## 交互流程

### Step 1：识别项目结构

CLI 只扫描足够生成项目概览的信息，不在这一步做深度业务扫描。

需要识别：

- 产品根目录。
- 服务目录。
- 前端应用。
- 文档目录。
- PRD / spec / plan 目录。
- bug 记录目录。
- 测试目录。
- 已存在的 `.story/knowledge` 文件。
- 可选 CodeGraph cache。

示例输出：

```text
Project: HappyCash
Root: D:\hc-all

Detected:
  Java services: 15
  Frontend apps: 1
  Python/test assets: 2
  PRD/spec directories: 3
  Bug record directories: 1
  Existing knowledge pack: yes
  CodeGraph cache: no
```

### Step 2：展示语言和文件概览

统计时要区分业务代码和依赖/生成文件。

示例：

```text
Files by language:
  java        4399
  xml          422
  yaml/yml     545
  sql           23
  ts/tsx      1073  (frontend app only, node_modules excluded)

Generated/dependency folders excluded:
  frontends/hc-admin/node_modules
  target
  .story/knowledge/cache
  .codegraph
```

如果存在 Java 服务目录，但 Java 文件数量为 0，要给出诊断：

```text
Warning:
  Java service directories were detected, but no Java files were included.
  Possible causes:
    - .gitignore excluded service directories
    - scan scope only included frontend
    - provider respected ignore rules unexpectedly
```

### Step 3：推荐初始化范围

CLI 根据项目类型给出 P0 推荐范围。

对 `hc-all`，推荐默认范围：

```text
Recommended P0 bootstrap scope:
  [x] hc-user
  [x] hc-order
  [x] hc-limit
  [x] hc-message
  [x] hc-coupon
  [x] hc-marketing
  [x] hc-callback
  [x] hc-third-party
  [x] hc-risk-management
  [x] hc-config
  [x] hc-job
  [ ] frontends/hc-admin
  [ ] hc-audit
  [ ] hc-dms
  [ ] hc-aiops
```

推荐理由：

- P0 先覆盖用户、订单、额度、消息、券、营销、回调、三方、风控、配置、任务等核心业务链路。
- 前端 P0 先排除，减少噪音。
- `hc-audit`、`hc-dms`、`hc-aiops` 默认不进第一轮，除非用户明确要覆盖后台审核或运维域。

### Step 4：用户确认范围

用户可以选择：

```text
Actions:
  a  accept recommended scope
  e  edit service selection
  f  include frontend app
  x  exclude service
  m  manually add path
  d  dry-run only
  q  quit
```

初始化阶段不要问太细的业务流程问题。

不合适的问题：

```text
coupon final bind 是否属于 withdraw？
```

合适的问题：

```text
是否把 frontends/hc-admin 加入本次初始化扫描？
```

业务边界问题留给：

```bash
story project scenarios review
story project scenario scan <scenario-id>
```

### Step 5：生成项目概览知识

用户确认后，写入概览文件：

```text
.story/knowledge/product.yaml
.story/knowledge/manifest.yaml
.story/knowledge/search-catalog.md
.story/knowledge/indexes/service-index.md
.story/knowledge/indexes/api-index.md
.story/knowledge/indexes/table-index.md
.story/knowledge/indexes/mq-index.md
.story/knowledge/graph/product-context-graph.json
.story/knowledge/reviews/pending-review-items.md
```

允许存在可选 cache：

```text
.story/knowledge/cache/codegraph/**
```

cache 不能作为正式知识。

### Step 6：生成候选业务域

`init-knowledge` 可以生成粗粒度业务域和候选场景，但必须标记为候选。

示例：

```text
Candidate domains:
  user       register, login, profile, account recovery, logoff
  order      withdraw, repay, overdue, advance repay, coolingoff
  limit      credit apply, credit audit, limit change
  message    sms, voice, push notification
  coupon     coupon issue, coupon bind, coupon budget
  marketing  campaign, channel, activity
```

这些不是最终业务场景边界。

正式场景确认通过以下命令完成：

```bash
story project scenarios review
```

写入：

```text
.story/knowledge/declarations/business-scenarios.yaml
```

### Step 7：输出下一步建议

初始化完成后，输出可执行的下一步：

```text
Knowledge bootstrap completed.

Recommended next commands:
  story project scenarios review
  story project scenario scan user.register
  story project scenario scan order.withdraw
  story project scenario scan limit.credit

Optional:
  story project init-knowledge --include frontends/hc-admin
```

## 数据协议

### Detection Result

探测结果写入 run 目录：

```text
.story/knowledge/runs/init-knowledge-<timestamp>/detection-result.json
```

示例：

```json
{
  "apiVersion": "knowledge/v1",
  "kind": "ProjectDetectionResult",
  "root": "D:/hc-all",
  "product_guess": "happycash",
  "services": [
    {
      "id": "hc-order",
      "path": "hc-order",
      "type": "java-spring-service",
      "included": true,
      "reason": "core order domain"
    }
  ],
  "frontends": [
    {
      "id": "hc-admin",
      "path": "frontends/hc-admin",
      "included": false,
      "reason": "excluded from P0 to reduce noise"
    }
  ],
  "ignored_or_generated": [
    "frontends/hc-admin/node_modules",
    "target",
    ".codegraph",
    ".story/knowledge/cache"
  ],
  "warnings": []
}
```

### User Scope Decision

用户确认结果写入：

```text
.story/knowledge/runs/init-knowledge-<timestamp>/scope-decision.yaml
```

示例：

```yaml
apiVersion: knowledge/v1
kind: InitKnowledgeScopeDecision
product: happycash
mode: interactive
include:
  - hc-user
  - hc-order
  - hc-limit
  - hc-message
  - hc-coupon
  - hc-marketing
  - hc-callback
  - hc-third-party
  - hc-risk-management
  - hc-config
  - hc-job
exclude:
  - frontends/hc-admin
  - hc-audit
  - hc-dms
  - hc-aiops
provider:
  codegraph: optional
fallback:
  - rg
  - filesystem
```

### Manifest Scope

`manifest.yaml` 记录确认后的初始化范围：

```yaml
sources:
  - id: hc-order
    type: git-or-local-service
    path: hc-order
    included: true
  - id: frontends-hc-admin
    type: frontend-app
    path: frontends/hc-admin
    included: false
    phase: P1
```

长期远程化时，`path` 应该替换或补充为 Git source 坐标。本地路径只适合作为本地 bootstrap metadata，不适合作为公司级知识源身份。

## CodeGraph Provider 行为

CodeGraph 是可选能力。

如果启用且可用，它可以生成：

```text
codegraph_facts:
  - API entrypoints
  - controller/service methods
  - call relationships
  - Feign clients
  - Mapper interfaces
  - Entity/TableName links
  - job handlers
```

它不应该生成：

```text
verified business scenario boundaries
formal product graph directly
final scenario documents
```

如果 CodeGraph 没识别出 Java 文件，CLI 应诊断 ignore/scope 问题，并继续走 fallback。

示例：

```text
CodeGraph warning:
  Java services are present, but provider returned 0 Java files.
  Falling back to filesystem + rg detection.
```

## 前端处理

P0 默认排除前端。

原因：

- 前端文件量大，容易引入依赖噪音。
- 第一阶段目标是后端业务场景骨架。
- 前端映射有价值，但不是初始化核心业务域的必要条件。

P1 可以把前端作为 UI/API 入口层纳入。

纳入时只扫描：

```text
frontends/hc-admin/src
frontends/hc-admin/config/routes.ts
frontends/hc-admin/src/services
```

始终排除：

```text
frontends/hc-admin/node_modules
frontends/hc-admin/dist
frontends/hc-admin/.umi
frontends/hc-admin/.umi-production
```

## 异常处理

| 情况 | 处理 |
| --- | --- |
| 有 Java 服务目录但没识别出 Java 文件 | 警告，并提示可能是 ignore/scope/provider 问题，继续 fallback。 |
| CodeGraph 不可用 | 继续使用 `rg` 和文件系统探测。 |
| 已存在 knowledge pack | 展示摘要，询问 update、dry-run 或 cancel。 |
| 用户排除了所有服务 | 不生成正式知识，只允许 dry-run。 |
| 用户选择前端且存在 `node_modules` | 自动排除依赖目录。 |
| run 中断 | 保留 run 目录，支持未来 resume/retry。 |

## 验收标准

P0 完成标准：

- `story project init-knowledge` 在写正式文件前展示项目结构概览。
- 用户可以接受或编辑推荐初始化范围。
- 对 `hc-all`，Java 服务不会因为 ignore 规则被静默漏掉。
- 前端默认排除，并可显式纳入。
- 命令会把 `detection-result.json` 和 `scope-decision.yaml` 写入 run 目录。
- 命令会写入或更新 `.story/knowledge` 下的概览知识文件。
- 结束时会推荐 `scenarios review` 和核心场景 scan 命令。
- CodeGraph 是可选能力，并有 fallback。

P1 完成标准：

- CodeGraph facts 可以进入 run artifacts，但不会成为正式知识。
- 前端 API 入口可以作为 P1 scope 纳入。
- 已存在 `.story/knowledge` 时，更新不会覆盖 user-confirmed declarations。
- 向导支持 `--yes` 自动化模式。

## 给执行 AI 的实现顺序

不要一上来实现完整 scanner。

推荐顺序：

1. 实现确定性的文件系统探测。
2. 实现 ignore / generated folder 过滤。
3. 实现推荐 scope 生成。
4. 实现交互式 accept/edit 流程。
5. 写入 run artifacts。
6. 生成项目概览 knowledge 文件。
7. 接入可选 CodeGraph provider。
8. 增加前端 P1 处理。

第一版要稳定、克制。价值在于正确确认范围和生成可持久化文件，不在于一次性抽出完美图谱。

## 与其他文档的关系

- `03-bootstrap-design.md`：定义 `.story/knowledge` 作为 file-first 知识包。
- `07-scenario-knowledge-workflow-design.md`：定义场景确认、场景深扫、`scenario-index.json` 和 CodeGraph-as-retrieval-provider。
- 本文档补齐 `init-knowledge` 的用户交互层。

