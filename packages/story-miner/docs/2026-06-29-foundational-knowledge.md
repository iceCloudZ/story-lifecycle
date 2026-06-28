# 基础知识（项目本身）— 现状 + 待办

> 2026-06-29 · 简单备忘，细节后面再弄。

## 现状：hc-all 已有基础知识（不是从零）

`bootstrap-prompt.md`（"项目知识包生成助手"，9 步交互式：探测 → 确认技术栈/域 → 并行扫 DB/前端/测试/CI → 逐域扫 → 生成产物 + 健康评估）已在 hc-all 跑过，**2026-06-02，`status: verified`**。

产物（`D:/hc-all/.story/knowledge/`）：
- `manifest.yaml` / `product.yaml` — 产品/域/技术栈（5 域、15 服务、200 表、50 feign、50 MQ、22 三方）
- `scenarios/<domain>/*.md` — 业务场景
- `indexes/` — service / api / table / mq index + by-domain
- `graph/product-context-graph.json` — 节点（Domain/Service/Api/Table/Mq/Scenario…）+ 关系
- `reviews/health-assessment.md`

→ **"基础知识怎么做"已有答案**：就是 `bootstrap-prompt.md` 这套生成器。`product-context-graph.json` 是天然的"项目本身知识"。

## 缺口（后面再弄）

1. **没接进注入**：`knowledge_provider` 现在只注**增量**（bug-prone / cycle-time / 磁铁），**基础**（scenario / index / 域 / architecture）没注 → AI 拿不到"项目长啥样"。
2. **可能 stale**：06-02 生成，代码在变，需增量 refresh。

## 待办（later）

- `knowledge_provider` 扩成"**基础**（按 task_type / 域 注入 scenario/index）+ **增量**"两层。
- 给 bootstrap 加**增量 refresh**（codegraph diff → 更新 index/graph 节点，不全量重扫）。
