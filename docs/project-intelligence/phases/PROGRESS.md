# 进度记录（2026-08-03）

> 下次继续时读本文件即可恢复上下文。设计文档自包含：10（测试接入）/ 11（Workspace 实体 + wiki）。

## 已完成

| 项 | 内容 | commit |
|---|---|---|
| 设计文档 10 | 测试框架接入设计（含 2026-08-03 修订 R1-R8） | `15fd9a9e` / `edcd0165` |
| 设计文档 11 | Workspace 实体化 + wiki + L1-L4 探测缝 | `15fd9a9e` / `edcd0165` |
| 分期交接 | phases/README.md（1+2 并行 → 3 → 汇总） | `6e73c6d3` |
| Phase 1 | BaseVerifyProvider 缝、scenario_catalog 注入、测试场景 tab、scenario 知识闭环（test_ref/last_status/stale） | `5f0c7b54` |
| Phase 2 | workspace 表、init 管线 5 步、CLI、WorkspacePage 三 tab、Sandbox 术语（prompt 侧） | `0a9469a6` |
| Phase 3 | WikiEntry、BaseWikiProbe 缝 + CodeScanProbe(L1)、draft 管线 + review API、WikiTab 组件 | `0b2a7441` |
| 汇总收口 | WikiTab 挂载（补 CSS import）、Sandbox 注释收尾、根级 pytest 修复、PHASE-2-RESULT 补写、文档勘误 | `18a9e29a` |

**验证状态**：全量 pytest 1394 passed / 9 skipped；前端 build 通过；ruff 干净。

## 当前形态

- WorkspacePage（`/workspaces`）四 tab：旅程 / Stories / 概览 / Wiki（含 review 收件箱）
- 不配 verify_provider / 不建 workspace = 行为与旧版完全一致（零配置路径已测）
- 术语：Workspace=业务项目实体，Sandbox=per-story 目录（字段名 workspace_path 不动）

## 下次继续的待办（按优先级）

1. **hc-pytest 侧实现**（唯一阻塞真实价值项，在 `D:/hc-all/hc-pytest` 仓）：
   - `integrations/story_lifecycle_provider.py` 的 `HcPytestVerifyProvider`（duck-type，契约见文档 10 改动 5；默认异步产物模式）
   - `scripts/generate_scenarios.py`（扫 hc-* 注解填 apis，写 sidecar JSON，文档 10 §4.2/R5）
   - journey 回写：declare scenario_report 到 `<sandbox>/story/` + POST gate-results + 回写 scenario last_status
   - L2-L4 probe 薄封装（`data_sources/` es_loader/behavior_loader → EsEndOfRequestProbe/MongoBehaviorProbe，契约见文档 11 §5.2）
   - 完成后真机联调：config.yaml 配 `verify_provider:` + `wiki_probes:` 段
2. **重写两个陈旧集成测试**：`tests/integration/test_full_story_lifecycle.py`、`test_anchor_link_context_flow.py`（现 importorskip 跳过，2026-06 重构前的 API）
3. `check_wiki_stale` 接触发点（workspace detail API 或概览 tab 挂 stale 徽标）
4. 旅程 tab 展示 journey `last_status`（等 scenario sidecar 有数据）
5. `packages/knowledge` 4 个文件的 ruff format 既有告警（HEAD 遗留）

## 恢复上下文的最小读集

1. 本文件
2. `docs/project-intelligence/phases/README.md`（分期机制 + RESULT 模板）
3. 三份 `phases/PHASE-N-RESULT.md`（各期交付/偏差/遗留明细）

## 测试方法

见 2026-08-03 会话「怎么测试」一节：自动化按测试文件跑（test_verify_providers / test_workspace_entity / test_wiki_phase3 等），手动走查 = `story serve` → `/workspaces` → create → `story workspace init` → Wiki tab review；Phase 1 真联调依赖待办 1。
