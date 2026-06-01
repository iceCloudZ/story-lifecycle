# Project Knowledge Search Catalog

## Product

- code: `happycash-ph`
- name: `HappyCash Philippines`

## Domains

- `user`
- `credit`
- `order`
- `repayment`
- `message`
- `marketing`
- `operations`

## Searchable Sources

- `.story/knowledge/scenarios/{domain}/{scenario}.md`
- `.story/knowledge/indexes/service-index.md`
- `.story/knowledge/indexes/api-index.md`
- `.story/knowledge/indexes/feign-index.md`
- `.story/knowledge/indexes/table-index.md`
- `.story/knowledge/indexes/field-index.md`
- `.story/knowledge/indexes/mq-index.md`
- `.story/knowledge/indexes/state-machine-index.md`
- `.story/knowledge/indexes/bug-risk-index.md`
- `.story/knowledge/indexes/test-case-index.md`
- `.story/knowledge/graph/product-context-graph.json`

## Search Keys

- scenario id: `order.withdraw`, `repayment.normal-repayment`
- service: `hc-user`, `hc-order`, `hc-limit`
- API path: `/api/v1/...`
- Java symbol: `OrderWithdrawService`, `UserProfileController`
- table: `hc_order.t_order`
- field: `user_id`, `order_id`, `live_image_path`
- MQ: topic, tag, consumer group
- bug keyword: `liveness`, `account merge`, `Maya`, `PAYMAYA`, `CLOSE loop`

## Recommended Search Flow

1. Read this catalog and `manifest.yaml`.
2. Build a search plan from story title, PRD, and target stage.
3. Search exact symbols first with `rg`.
4. Read matching scenario and index snippets.
5. Expand from graph seed ids.
6. Compose a small context packet with source refs.
