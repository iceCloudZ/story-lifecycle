# Phase 2 实施结果（2026-08-03）

> 设计文档：`docs/project-intelligence/11-workspace-entity-design.md`（§1 实体模型、§2 WorkspacePage、§3 初始化管线、§8 Phase 2 行）
> **本文件由汇总窗口补写**——Phase 2 窗口完成了开发与提交（`0a9469a6`），但未按交接约定留下 RESULT 记录；以下依据 commit `0a9469a6` 的内容、测试与代码核査补记。

## 交付清单

- `infra/db/models.py` — `workspace` 表（§1.1 原样 DDL）+ `project.workspace_id` 幂等迁移 + CRUD / `update_workspace_init_state` / `list_stories_by_workspace`（经 story_project 反查）
- `orchestrator/workspace/workspace_registry.py`（新）— create/get/list/delete + `run_init_pipeline` 5 步（register_repos / detect_runtime / gen_wiki 骨架 / register_integrations / init_scenarios），每步幂等、失败带 reason 不阻塞后续、`--step` 单步重跑；知识根从 repos 推断并持久化
- `entry/cli/workspace_cmd.py` + `main.py` — `story workspace create/init/list/show/delete`
- `orchestrator/service/api.py` — `/api/workspace-entities`（list/create/detail/init），刻意避开旧 `/api/workspaces`（intake 目录选项，不动）
- `frontend/src/pages/WorkspacePage.tsx` + `.css`（新）— 顶层路由 `/workspaces`（MoreMenu 视图区），三 tab 只读版：旅程（scenario 投影，D6）/ Stories（Repo 反查）/ 概览（Repo + 集成 + init_state）；tokens + `.ui-*` + SVG 图标
- 术语：agent 面向 prompt 改 Sandbox 表述（`### 工作沙箱` + workspace_slug 说明）；`workspace_path`/slug 字段名不动（D8）
- `tests/test_workspace_entity.py` — 21 例

## 验收 checklist 结果（§9 Phase 2）

- [x] **不建 Workspace 时行为与今天完全一致**：零配置路径保留（页面空态明示"不创建时行为与之前完全一致"）；全量回归 1309 passed
- [x] **`story workspace init` 5 步可跑、幂等、单步可重跑**：`run_init_pipeline` 每步幂等 + `--step` 参数；init_state 正确推进（测试覆盖）
- [x] **WorkspacePage 三 tab 数据正确**：旅程 tab 为 scenario 条目投影（id/domain/apis 数/status 徽章）；Stories 经 Repo 反查可跳转详情；概览含 Repo/运行时事实/集成/init_state。**注：旅程 tab 展示的是 scenario 的 `status`，journey 的 `last_status`（Phase 1 新字段）展示未单独验证**
- [x] **Sandbox 术语**：agent 面向 prompt 已切换；残留的两处代码注释措辞由汇总窗口补齐（`planner.py:421/502`），`src/` 全文已无"工作空间"表述

## 与设计文档的偏差

1. **API 路径用 `/api/workspace-entities`**：设计未规定路径，实现刻意避开既有 `/api/workspaces`（intake 目录选项 API），防止与旧"workspace"语义冲突。
2. **gen_wiki 步在 Phase 2 只是骨架**：设计 §3 step 3 的 L1 生成 + probe 增补由 Phase 3 的 `_step_gen_wiki` 升级落地（`wiki_pipeline.generate_wiki_drafts`），两期衔接无缝。
3. **integrations_json 与 verify_provider 非同源读取**：verify_provider 仍读全局 `config.yaml`，`workspace.integrations_json` 只存集成元数据/指针（§6 定位是"登记 + 展示"），未做"integrations_json 驱动 verify 加载"——与 §6 的 D7 克制定位一致，但「一处登记多处消费」目前只有登记侧。

## 遗留问题（给汇总窗口）

- WikiTab 挂载（Phase 3 组件就绪，挂载是汇总窗口交界工作 b）
- 旅程 tab 的 `last_status` 展示可后续补强（scenario sidecar 有数据后）
- PHASE-2-RESULT 缺失——本文件补写

## 关键 commit

- `0a9469a6` feat(workspace): Phase 2 — workspace 实体化(表/init 管线/CLI/WorkspacePage)
