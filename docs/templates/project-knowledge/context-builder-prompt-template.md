# Story Knowledge Context Builder Prompt

你正在为一个 story 的指定阶段生成 knowledge context packet。

## 输入

- story key: `<story_key>`
- target stage: `<design|implement|test|review>`
- story title: `<title>`
- story description / PRD: `<content>`
- knowledge root: `.story/knowledge/`

## 任务

请按以下四步执行，不要修改业务代码。

### 1. Describe

读取：

- `.story/knowledge/manifest.yaml`
- `.story/knowledge/search-catalog.md`
- `.story/knowledge/graph/product-context-graph.json`

理解产品、业务域、可搜索文件、节点类型、关系类型。

### 2. Search

根据 story 和 target stage 生成搜索计划。优先搜索精确符号：

- 场景名
- 服务名
- API path
- Java/Python/前端符号
- 表名和字段名
- MQ topic/tag
- 历史 bug 关键词

使用 `rg` 搜索 `.story/knowledge/`，读取命中的相关片段。

### 3. Expand

从命中的 scenario/service/table/bug/test seed ids 出发，读取 `product-context-graph.json` 扩展邻接关系。

只选择与当前 story 和 target stage 相关的上下文。

### 4. Compose

生成：

- `.story/context/<story_key>/knowledge-context/<target_stage>.md`
- `.story/context/<story_key>/knowledge-context/<target_stage>.json`

Markdown 必须包含：

- 为什么选择这些上下文
- 相关业务场景
- 涉及服务/API/表/MQ/状态机
- 历史 bug 风险
- 测试/回归关注点
- 待确认项
- source refs

JSON 必须包含：

- `story_key`
- `target_stage`
- `selected_context`
- `context_packet`

## 约束

- 不要把整个知识包复制进 context packet。
- 每个关键结论必须引用 source。
- `proposed` 内容必须标记为待确认。
- 输出要短而有用，优先给 Planner 和 Executor 使用。
