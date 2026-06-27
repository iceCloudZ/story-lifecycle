# Project Knowledge Bootstrap Prompt

你正在为当前项目生成本地 Project Knowledge Pack。

## 目标

请只读当前工作区，生成 `.story/knowledge/` 下的项目知识包。不要修改业务代码，不要调用外部服务，不要写入 `.story/knowledge/` 之外的文件。

## 必须读取

- 根目录项目说明，例如 `README.md`、`AGENTS.md`、`CLAUDE.md`。
- 业务上下文文档。
- 代码目录。
- SQL、Mapper、Entity、DTO、Controller、Feign、MQ、配置。
- bug 文档。
- 测试用例和测试报告。

## 必须生成

- `.story/knowledge/product.yaml`
- `.story/knowledge/manifest.yaml`
- `.story/knowledge/search-catalog.md`
- `.story/knowledge/scenarios/**`
- `.story/knowledge/indexes/**`
- `.story/knowledge/graph/product-context-graph.json`
- `.story/knowledge/graph/product-context-graph.md`
- `.story/knowledge/reviews/pending-review-items.md`
- `.story/done/PROJECT-KNOWLEDGE-INIT/knowledge_bootstrap.json`

## 状态规则

- 代码直接抽取的事实标记为 `extracted`。
- AI 推断的业务解释标记为 `proposed`。
- 只有明确人工确认过的内容才能标记为 `verified`。

## 证据规则

任何关键结论必须有 source refs。包括但不限于：

- 服务
- API
- 表和字段
- MQ
- 状态机
- 历史 bug
- 测试用例
- 生产排查路径

没有证据的内容写入 pending review，不要伪装成事实。

## 输出风格

- 面向人和 AI 双读。
- Markdown 简洁但完整。
- graph JSON 只做节点、关系和 source refs，不放长正文。
- 不确定就明确写“不确定”。
