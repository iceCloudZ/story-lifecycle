# Phase 1 实施结果（2026-08-03）

> 设计文档：`docs/project-intelligence/10-test-framework-integration-design.md`
> 范围：改动 1-4（改动 5 在 hc-pytest 仓库，只做契约对齐，未写 hc 侧代码）

## 交付清单

**改动 1：BaseVerifyProvider 扩展点（核心）**
- `packages/story-lifecycle/src/story_lifecycle/orchestrator/verify_providers/base.py`（新）— `VerifyResult` dataclass + `BaseVerifyProvider` ABC（异步产物/同步冒烟两种运行模式契约，R1）
- `packages/story-lifecycle/src/story_lifecycle/orchestrator/verify_providers/__init__.py`（新）— `load_verify_provider`：config 驱动 importlib 加载，duck-type 校验（只查 `verify()` 方法，R6），失败降级 None 不阻断
- `orchestrator/evaluation/unified_gate.py` — LLM 判定后（成功 + fallback 两路）合并外部验证结果：`_run_external_verify`（R8：把 `ctx["_agent_actions"]` 合入 done_data）+ `_merge_external_verify_result`（R2：外部 FAIL 强制 retry + 计 reject budget，超限 force-escalate→fail；R3：外部 PASS 只合并 findings 不覆盖 decision）
- `orchestrator/evaluation/reject_budget.py` — `check_reject_budget` 加 `trigger` 参数（默认 `boundary_judge`，外部测试走 `external_verify`），外部失败与边界判定共用 ≤3 + 理由去重的预算

**改动 2：规划注入 scenario_catalog**
- `orchestrator/engine/prompt_sections.py` — `build_scenario_catalog_section`（候选测试场景渲染，容错空串不阻断）
- `orchestrator/engine/planner.py` — `_build_agent_system_prompt` 注入目录段 + 输出 schema 加 `selected_scenarios` + 规则 7；`StagePlan` 加 `selected_scenarios` 字段（:322，未碰 `infra/schemas.py` 废弃 PlanResult）；launch action 持久化带 `selected_scenarios` 进 `ctx["_agent_actions"]`

**改动 3：board「测试场景」tab**
- `frontend/src/pages/StoryDetailPage.tsx` — MODULES 加 `scenarios` 项 + `{validTab === 'scenarios' && <DocsTab/>}` 复用
- `frontend/src/components/StorySidebar.tsx` — `MODULE_ICONS` 加 `scenarios` 烧瓶 SVG（16×16 / stroke 1.4 / 描边，遵守 frontend/AGENTS.md，无 emoji）

**改动 4：scenario 知识闭环**
- `packages/knowledge/src/knowledge/models.py` — `ScenarioEntry` 加 `test_ref`/`last_run_at`/`last_status`/`verified_at` + `to_dict()`
- `packages/knowledge/src/knowledge/parser.py` — `parse_scenario` 读 sidecar JSON（`<scenario>.md.json`，playbook 同款机制），sidecar 覆盖 frontmatter
- `packages/knowledge/src/knowledge/index.py` — `_entry_from_dict` 补新字段
- `knowledge/knowledge_store/stale.py` — scenario 级 stale 检测：`git log -1 --format=%ct -- <path>` 比对 verified_at（R5b，不用 mtime）+ `last_status == "FAIL"` 判 journey 失败
- `entry/cli/project.py` — `sync-knowledge` 输出过期 scenario 明细（id + 具体原因）

**测试**
- `tests/test_verify_providers.py`（新）— 加载/duck-type/容错 + R2/R3/R8 合并语义（9 例）
- `tests/test_prompt_sections_catalog.py`（新）— 目录渲染含 sidecar 详情 + 空/异常容错（3 例）
- `tests/test_knowledge_stale.py`（新）— 真 git 仓库 fixture 的 stale 场景（5 例）
- `tests/test_agent_planner.py` — 新增 selected_scenarios 持久化 + prompt schema 断言（2 例）
- `packages/knowledge/tests/test_knowledge.py` — 新增 sidecar 字段进 INDEX 测试（1 例）

## 验收 checklist 结果

- [x] **不配 verify_provider 时零影响**：`load_verify_provider({}) → None`，gate 原样返回（`test_external_none_no_behavior_change`）；全量回归 `pytest packages/story-lifecycle/tests packages/knowledge/tests` = **1309 passed, 2 skipped**
- [x] **异步产物路径**：provider 返回 None（起跑即返）不阻塞 poll loop；同步 FAIL 路径验证了「外部失败 → verify 转 retry + 计 reject budget」；异步产物落地由既有 artifact-driven 机制承接（`check_artifacts_landed` 看 `<workspace>/story/` 产物，下轮 gate 证据包自然带上）
- [x] **同步冒烟路径**：`sync: true` 时 provider 返回 `VerifyResult` 本轮合并（PASS/FAIL 均有测试覆盖）；timeout 由 provider 自行控制（契约规定，不在本仓代码里）
- [x] **confirm-gate**：外部 PASS 只合并 findings、不覆盖 LLM decision（`test_external_pass_merges_findings_keeps_decision`）；planner 的 stage/state confirm 闸零改动
- [x] **single-pass**：single-pass profile 唯一 stage 名即 `verify`（single-pass.yaml），走 `run_unified_verify_gate` 同一 hook，无需在 judge_boundary 另挂
- [x] **规划注入**：prompt schema 含 `selected_scenarios`（`test_system_prompt_contains_selected_scenarios_schema`）；`_agent_actions` 持久化后经 R8 合入 done_data（`test_selected_scenarios_persisted_into_actions` + `test_r8_wires_agent_actions_into_done_data`）
- [x] **board tab**：详情页有「测试场景」tab（SVG 烧瓶图标），复用 DocsTab（doc_type 开放，`scenario_report` 自动可见）；`npm run build` 通过，构建产物随代码提交
- [x] **stale 检测**：真 git 仓库测试覆盖——代码变更（verified_at < 文件最后 git 变更）→ 过期、`last_status=FAIL` → 过期、verified_at 新 + PASS → 不过期、commit 变化仍第一层优先
- [x] **apis 填充**：生成器在 hc-pytest 仓（改动 5，未在本仓实现）；本仓侧 sidecar 消费机制已测（`test_scenario_sidecar_fields_in_index`），`generate_scenarios.py` 产出的 `<scenario>.md.json` 零改动即可索引
- [x] **ScenarioEntry 新字段**：INDEX.json 条目含 `test_ref`/`last_run_at`/`last_status`/`verified_at`（generate_index 走 `to_dict()`，测试断言）

## 与设计文档的偏差

1. **`build_scenario_catalog_section` 取数方式**：设计草稿用 `idx.retrieve(top_k=20)` 再过滤 scenario——但 `retrieve` 只返回 score>0 的条目，静态 scenario 无 domain/query 命中时 score 恒 0，候选永远为空。改为 `idx.all()` 过滤 `type == "scenario"` + 按标题排序取前 20。意图一致（候选清单给 LLM 选），且实测真实知识库（14 个 scenario）渲染正常。
2. **selected_scenarios 持久化粒度**：挂在**每个 launch action dict** 上（actions 是列表，无顶层键位），provider 按 stage 读对应 action 的 `selected_scenarios` 即可（single-pass 读唯一 action）。
3. **record_finding 映射**：`VerifyResult.findings` 是 `{scenario, status, detail}`，映射为 `source=test_failure / severity=high(可覆写) / category=test_failure / location=scenario / description=detail`（设计草稿的 `record_finding(story_key, category="test_failure", **f)` 是示意图，实际签名是 `(story_key, stage, finding_dict)`）。
4. **前端 icon 字段**：MODULES 的 `icon` 用字符串占位，SVG 加在 StorySidebar 的 `MODULE_ICONS` 注册表（既有 tab 模式），未新建独立 `IconFlask` 组件文件——渲染路径一致，满足 R7 无 emoji。
5. **外部 FAIL 超预算的决策枚举**：gate 契约只有 advance/retry/fail，超预算时 `decision="fail"` + `repair_action.kind="escalate"`（planner 对 escalate 的既有语义 = 标 failed 转人，与 boundary_judge 的 paused 语义不同但都是转人，复用现有枚举无新状态）。

## 遗留问题（给汇总窗口）

- hc-pytest 侧实现（`HcPytestVerifyProvider` + `generate_scenarios.py` + journey 回写）未在本仓验证——按改动 5 契约实现后需真机联调（本机 `D:/hc-all/hc-pytest` 未提供 provider 入口模块时，`load_verify_provider` 会静默降级 LLM-only，已测）。
- 工作树里有另一个窗口（Phase 2）未提交的前端改动（App.tsx/client.ts/WorkspacePage）；本次 `npm run build` 的产物是该工作树的完整编译结果（含其路由），已随本 commit 提交——Phase 2 窗口提交前应再跑一次 `npm run build` 确认产物与最终源码一致。
- `scenario_report` 的 `story tool declare` 支持（doc_type 开放字符串）未单测——`declare_artifact` 既有机制，未做改动。
- `last_run_at/last_status` 回写 sidecar 的 hc 侧行为未验证（改动 5 范围）。

## 关键 commit

- `45a91fb8` feat(test-framework): Phase 1 — verify provider 扩展点 + scenario_catalog 注入 + 测试场景 tab + scenario 知识闭环
