# Phase 3 实施结果（2026-08-03）

> 设计文档：`docs/project-intelligence/11-workspace-entity-design.md`（§4 wiki 条目设计、§5 多源探测模型、§8 Phase 3 行）
> 边界：hc 侧 L2-L4 probe 不在本仓库实现（§7 资产在 hc-pytest/hc-all，仅做契约对齐）；WikiTab 不挂载（挂载由汇总窗口做）

## 交付清单

**packages/knowledge（只追加，不改既有类/函数行为）**
- `src/knowledge/models.py` — 追加 `WikiEntry`（§4.1 schema：summary/review_state/evidence_refs/related/verified_at/reviewed_by/review_reason/probe_snapshot/content）
- `src/knowledge/parser.py` — 追加 `parse_wiki()`（frontmatter + 正文）；`parse_entry` 加 wiki 分支（追加分支，既有分支不动）
- `src/knowledge/generator.py` — `_collect_entries` 追加 wiki/ 目录扫描（追加块，既有扫描不变）
- `src/knowledge/index.py` — `_entry_from_dict` 加 `type == "wiki"` 分支（追加分支）
- `src/knowledge/__init__.py` — 导出 `WikiEntry`

**核心侧 probe 缝（§5.2/5.4）**
- `src/story_lifecycle/knowledge/wiki_probes/base.py`（新）— `Evidence` dataclass + `BaseWikiProbe` ABC（§5.2 原样契约）
- `src/story_lifecycle/knowledge/wiki_probes/__init__.py`（新）— `load_wiki_probes`：config `wiki_probes` 驱动 importlib，duck-type 校验 probe()，单条失败 print+跳过（mirror verify_providers）
- `src/story_lifecycle/knowledge/wiki_probes/code_scan.py`（新）— `CodeScanProbe`（L1 静态扫描：API 注解/表定义/MQ 监听/依赖文件 + 代码内 API 声明分歧检测）；只产聚合统计（I5）

**wiki draft 管线（§4.3/§5.3）**
- `src/story_lifecycle/knowledge/wiki_pipeline.py`（新）— `save_wiki_entry`（human→merged 直接生效；AI/probe→draft；AI 重写已 merge 页降级为 draft）/`list_wiki_entries`/`get_wiki_entry`/`review_wiki`（approve→merged+verified_at；reject→draft+reason）/`delete_wiki`/`generate_wiki_drafts`（probe→draft，已 merge 页跳过不覆盖）/`check_wiki_stale`（重跑 probe 对比快照 + git log %ct 比对 verified_at，无 mtime）
- `orchestrator/workspace/workspace_registry.py` — `_step_gen_wiki` 升级：跑 probe 生成 L1 draft（零配置只有 L1，§5.4）

**API**
- `orchestrator/service/api.py` — `GET/POST /api/workspace-entities/{slug}/wiki`、`POST .../wiki/{wiki_id}/review`（approve/reject）、`DELETE .../wiki/{wiki_id}`、`POST .../wiki/generate`；workspace detail 响应加 `wiki` 字段

**agent 注入（§4.2）**
- `knowledge/context_providers/knowledge_provider.py` — `_build_wiki_summary_section`：只取 merged 条目、只注入 summary+related 指针、降权（独立段放在知识库段之后）、stale 标注（"【可能过期，以代码为准】"）；draft 永不注入（I2）

**前端（不挂载）**
- `frontend/src/components/WikiTab.tsx` + `WikiTab.css`（新）— 条目列表（draft/已生效徽章）+ 正文渲染（MarkdownView 复用）+ review 收件箱（确认生效/打回+原因）；独立组件，未挂载，遵守 frontend/AGENTS.md（tokens/.ui-*/SVG，无 emoji）

**测试**
- `packages/knowledge/tests/test_wiki_entry.py`（新，5 例）— parse_wiki frontmatter+正文/缺省值/INDEX 索引/roundtrip/既有类型不受影响
- `packages/story-lifecycle/tests/test_wiki_phase3.py`（新，30 例）— 管线（human 直接生效/AI-probe draft/review merge-reject/合并页降级 draft/删除过滤）、CodeScanProbe（聚合统计+PII/分歧检测/缺失降级）、loader（空/失败跳过/duck-type）、generate_wiki_drafts（零配置 L1/配 probe 带 evidence_refs/merged 不覆盖）、stale（重跑对比/git 语义/无 mtime 误报）、注入（summary+related 只取/降权顺序/stale 标注）、API（CRUD/review/错误码/workspace detail）
- 全量回归：`pytest packages/story-lifecycle/tests packages/knowledge/tests` = **1344 passed, 2 skipped**（较上期 +35）

## 验收 checklist 结果（§9 Phase 3）

- [x] **`type: wiki` 条目可解析、进 INDEX.json；人写的直接生效，AI/probe 产出的一律 draft**：`parse_wiki` + `_collect_entries` wiki 扫描 + INDEX roundtrip 测试；human→merged、probe/story→draft（含 review merge/reject 路径）
- [x] **agent prompt 注入只含 summary + related，且 wiki 检索权低于 scenario/playbook；stale 标注生效**：注入段只含 summary+related 指针（全文不注入，测试断言"非常长的正文"不在段内）；独立段追加在知识库段之后（位置即降权）；git 变更晚于 verified_at → 标注"可能过期，以代码为准"（测试覆盖）
- [x] **不配 probe 时只有 L1 骨架；配了 DMS/ES probe 后 wiki draft 含 L3/L4 证据（带 evidence_refs）**：`generate_wiki_drafts` 零配置回退 `CodeScanProbe`（测试）；配置 probe 后 draft 带 evidence_refs（probe/query/observed_at）+ probe_snapshot（`_FakeL3Probe` 模拟 hc 侧 L3，测试覆盖；真实 hc probe 需联调，见遗留）
- [x] **分歧记录可用**：L1 CodeScanProbe 检测代码内 API 声明分歧（同路径多 method）→ `api_divergence` 证据 → draft 写现实 + 标注分歧；L3/L4 级分歧（代码 vs 线上）由 hc 侧 probe 产出，机制同构（evidence 含 summary/data 可写"代码定义 X 态，线上 Y 态"）
- [x] **stale 检测支持"重跑 probe 对比"，无 mtime 误报**：`check_wiki_stale` 重跑 probe 对比 probe_snapshot（聚合数据变 → 过期）+ git log %ct 比对 verified_at；测试覆盖"只 touch 文件不过期"（无 mtime 误报）
- [x] **PII 审计**：probe 只产聚合统计（计数/分布/名称清单封顶 20）；wiki 条目和注入只含聚合数据；测试断言 evidence.data 值类型均为计数/清单。**原始用户数据审计项：hc 侧 L2-L4 probe 在 hc 仓库，其实现需遵守 I5——本仓无 probe 会产出原始行**

## 与设计文档的偏差

1. **review_state 只有两态**（draft/merged）：设计 §4.3 描述 "draft → lint → review → 人工确认 → merge" 五步，实现收敛为 draft→(approve)merged / (reject)draft+reason 两态——lint/review 是质量流程不是存储状态，打回记录在 `review_reason`，不引入中间态（同状态机评审哲学的"先模型后实现"，两态已覆盖设计语义）。
2. **INDEX.json 携带 content 全文**：设计说 agent 注入只取 summary，但 INDEX 是"人优先"渲染的数据源（WikiTab 读 INDEX 渲染正文），全文进 INDEX 不违反 §4.2（注入侧仍只取 summary）。
3. **draft 不注入 agent prompt**：设计 §4.2 只定义 merged 语义，未明说 draft 是否可注入——实现按 I2 精神只注入 merged（未确认知识不污染执行 prompt），在 RESULT 中显式记录。
4. **gen_wiki 步骤跳过已存在条目（除 draft 更新）**：`generate_wiki_drafts` 对已 merge 页直接跳过（不自动覆盖，I2）；已 draft 页原地更新（内容+快照刷新），重跑幂等。stale 由 `check_wiki_stale` 标注、人工决定是否重生成。
5. **probe 名转 tag**：evidence_refs.probe 用 `_probe_tag(ClassName)`（CodeScanProbe→code-scan），类名→kebab 便于稳定 slug/对比；证据 kind 同样 kebab 化进 slug（api_endpoints→api-endpoints）。

## 遗留问题（给汇总窗口）

- **WikiTab 未挂载**：组件 + CSS 已就位（`frontend/src/components/WikiTab.tsx`），挂载进 WorkspacePage 需汇总窗口做（一个 tab + `<WikiTab slug={selected.slug}/>` 一行）；挂载后跑 `npm run build` 并验证 dist 更新。
- **真实 hc probe 联调**：L2-L4 专用 probe（DMS/ES/Mongo，薄封装 hc-pytest `data_sources/`）在 hc 侧仓库实现，本仓只验证了契约（`load_wiki_probes` + evidence_refs 机制，`_FakeL3Probe` 模拟）。联调时注意 `wiki_probes` config 的 `path` 字段可 prepend sys.path（同 verify_provider）。
- **stale 检测的接入点**：`check_wiki_stale` 已实现并有测试，但未接入任何定时/触发点（gen_wiki step 只生成不检查）——汇总窗口可考虑在 workspace detail API 或 UI 概览 tab 挂 stale 徽标。
- **review 收件箱入口**：WikiTab 内嵌 review 按钮（approve/reject），WorkSpacePage 级"待确认数"角标未做（挂载时顺手）。
- **parse_scenario 不传 trigger**（Phase 1 既有行为，不在本期改）：测试因此用 domain 命中检索而非 stage trigger——汇总窗口若修，顺带补 trigger 注入测试。
- **knowledge 包格式告警**：`ruff format --check` 在 `packages/knowledge/src/knowledge/{models,parser,index,generator}.py` 报既有格式违规（Phase 1 遗留，HEAD 即失败），本期未动（避免混合他人 diff）；新文件均已格式化。

## 关键 commit

- `0b2a7441` feat(wiki): Phase 3 — type:wiki 条目 + draft 管线 + BaseWikiProbe 缝 + CodeScanProbe(L1)
