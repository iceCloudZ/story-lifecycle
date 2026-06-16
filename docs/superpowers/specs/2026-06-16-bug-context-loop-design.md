# 改 Bug 闭环：bug↔关联需求 + pack 解析

- **日期**: 2026-06-16
- **状态**: 已批准，待写实施计划
- **测试 case**: bug 1009779（客户UID千分位）↔ 需求 1065460（删除联系人）

## 背景与动机

手动开发闭环的"改 bug"环节（用户流程步骤 13）：测试发现 bug → 复制 pack → 开 claude 改。两个断点：

1. **bug 不在 story-lifecycle**：TAPD sync 按 owner+status 拉 bug（`_fetch_bugs`），但需手动 `story sync` 触发；新 bug（如今日提交的 1009779）没同步进来。
2. **pack(bug) 不含关联需求 context**：`_parse_bug` 用 TAPD bug 的 `story_id` 字段当关联，但该字段在 TAPD 是 **null**（关联在 TAPD"链接"里，不在字段）。所以 bug 同步进来 `parent_key` 也是 null，pack 拿不到关联需求。

需求 1065460 的 context 已有（spec/plan/branch/ddl-summary），但 ddl.ref 空。bug 1009779 不在 DB。

## 目标

改 bug 时，`pack(bug)` 自动包含**关联需求**的全套 context（spec/plan/分支/DDL），开 claude 粘贴即可改。

## 设计

### Part 1 — 同步关联 bug（进 story 详情触发，自动+节流）

- **端点** `POST /api/story/{story_key}/sync-related-bugs`
  - 拿 story 的 `source_id`（TAPD full id，`source_type=tapd`）
  - 调 `TapdApi.get_related_bugs(tapd_id)`（**新方法**，调 TAPD relations API，参数同 `cli_tapd.py` 的 `get-related-bugs`，已验证能查 story→bug）
  - 每个 bug：`upsert_story_from_source`（`tapd_type=bug`, **`parent_key=当前 story_key`**, intake_state=candidate, status=idle）
  - 返回同步的 bug 数
- **前端**：`StoryDetailPage` 加载时触发该端点；**节流 5min**（react-query staleTime 或时间戳，同一 story 5min 内不重复打 TAPD）

关联方向：**从 story 侧**（get-related-bugs(story) 天然反向），不依赖 bug.story_id 那个 null 字段。

### Part 2 — pack(bug) 解析 parent + 可选 skill 提示词

- `pack.py`：story 有 `parent_key` 时，读 parent（需求）的 context，拼进 pack 一个 **"关联需求"** 节（需求的 spec/plan/分支/DDL）
- bug 自己（pack 头部）：bug 标题 + TAPD url + bug 描述
- pack 结构：`# bug 标题` → bug 元信息 → **## 关联需求：{parent title}** → parent 的绑定项目/文档/DDL/Nacos
- **可选 skill 提示词**：`GET /context/pack?skill=<skill_name>` 传 skill 时，pack 头部加一行 `## 建议调用 /{skill} 处理`；不传则中性（默认）。前端 ContextTab 复制时可选拖 skill（下拉 hc-all skill 列表），bug 类型默认建议**新建的改 bug skill**（hc-all 侧建，本 spec 范围外；`hotfix-deploy-verify` 待删）。skill 未建好前默认不指定。需求 pack 保持中性（多场景）。

### Part 3 — pack 完整度检查

- pack 生成时检查该有的 ref：spec / plan / branch / DDL（按 story 类型）；**bug 改完还该有 bugfix-report**（见 Part 4）
- 缺失的标红 `⚠ 缺 {kind}`（如需求 1065460 的 ddl.ref 空 → 标红"⚠ 缺 ddl 文件路径"）
- story-context skill 已教填 ddl ref，标红提示补

### Part 4 — 改完收尾：bugfix-report 证据 + 更新状态（TAPD + 本地）

bug 是重要资产，改 bug 要留证据（与需求的 spec/plan/test-report 对等）。

**bugfix-report**（hc-all 新建改 bug skill 产出，story-lifecycle 不管产出）：
- **结构化三节**：`## 根因` / `## 修复` / `## 验证`（为数据飞轮 P3 抽取铺路：根因→bug-risk-index，验证→verification_result，模式→learned_pattern）
- 落 **关联需求证据目录的 bugs 子文件夹**：`D:/hc-all/story/<需求id>-摘要>/bugs/<bugid>/bugfix-report.md`
- story-context 记 `document(kind=bugfix-report, ref=<上述路径>)`——**该 document ref 即数据飞轮 P2 的数据源**（P2 Registry 从 `story_document` 表读，零重复扫描）

**resolve 端点** `POST /api/story/{bug_key}/resolve`：
- 调 TAPD `update-bug(status=resolved)`（复用 TapdApi；resolved 是 TAPD bug 合法状态）
- 更新本地 bug story `status=completed`, `tapd_status=resolved`
- 返回 bugfix-report ref 是否就位（前端提示：缺证据则警告但仍允许 resolve）

**前端**：bug 详情（`tapd_type=bug`）加「标记已修复」按钮 → 调 resolve → 刷新。pack 完整度对 bug 检查 bugfix-report。

## 数据流

```
进 story(需求) 详情
  → POST sync-related-bugs（节流）
  → TapdApi.get_related_bugs(story TAPD id)
  → bug upsert (parent_key=story) → DB

进 bug 详情 → GET /context/pack
  → pack.py 读 bug + parent_key → 读 parent(需求) context
  → 拼包 [bug 元信息 + 关联需求 spec/plan/分支/DDL]（可选带 skill 提示词）
  → 复制 → 开 claude 用 skill 改 bug
  → 改完 → 产出 bugfix-report 落 bug 证据目录 → story-context 记 ref
  → POST /resolve → TAPD update-bug(resolved) + 本地 completed（确认 bugfix-report 就位）
```

## 测试

- **单测**（TDD）：
  - `TapdApi.get_related_bugs`（mock httpx，断言参数 + 解析 bug_id）
  - `sync-related-bugs` 端点（mock TapdApi，断言 bug upsert + parent_key）
  - `pack.py` parent 解析（bug 有 parent → pack 含"关联需求"节）
  - `pack.py` 完整度检查（缺 spec/DDL → 标红）
  - `pack.py` skill 提示词（`?skill=` → pack 含"建议调用 /skill"行）
  - `resolve` 端点（mock TapdApi update_bug + 断言本地 status=completed / tapd_status=resolved）
- **端到端**（bug 1009779）：进需求 1065460 详情 → 同步 → bug 1009779 进来（parent=1065460）→ pack(1009779) 含 1065460 的 spec/plan/分支/DDL(标红) → 复制验证

## 关键决策

1. **关联从 story 侧拿**（get-related-bugs），不依赖 bug.story_id null 字段。
2. **进 story 详情触发同步**（自动+节流），不是手动 link、不是全量扫、不是 pack 即时参数——触发时机自然（要复制 pack 时），一次 TAPD 调用。
3. **节流 5min**：避免每次进详情都打 TAPD。
4. **pack 完整度标红**：解 skill 软约束（ref 可能漏记），缺了能发现。
5. **bug 是资产，改 bug 留证据**：bugfix-report（根因/修复/验证）落 bug 证据目录（`story/bug-<id>-摘要>/`，和需求同级），story-lifecycle 记 document ref + 完整度检查 + resolve 确认就位——与需求证据对等，不只状态流转。

## 非目标

- 不全量扫描所有需求的 related bugs（只当前进详情的 story）。
- 不改 TAPD bug story_id（TAPD 数据问题，绕过）。
- 不改 story-context skill（已教填 ref，pack 标红提示补即可）。
- bug→需求关联只支持 bug.parent_key（单个需求）；一个 bug 多需求关联不在本次范围。

## 服务于全自动 AI 开发

这套是**共用底座**的一部分（driver-agnostic）：手动模式进详情触发同步 + 复制 pack；全自动模式 AI 跑 story 时同样调 sync-related-bugs + pack。关联持久化（parent_key 在 DB），两种模式共用。

## 与数据飞轮的关系（P2/P3 衔接）

bugfix-report 的 document ref + 结构化三节是**数据飞轮的输入**（见 `docs/project-intelligence/02-data-flywheel-design.md`）：

- **P2（登记）**：飞轮 Artifact Registry 从 `story_document` 表读，bugfix-report 自动进 registry，零重复扫描。
- **P3（抽取反哺）**：sync-knowledge 抽取三节 → `bug-risk-index` / `learned_pattern` → 下个需求 planning 注入。

P2/P3 是独立飞轮工程（前置：建 Artifact Registry + `index-assets`，当前未实现），不属本 spec。本 spec 只保证产出**结构化、可抽取**的 bugfix-report。
