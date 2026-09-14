# v1.0 — 工作特化 Agent（TAPD 工单环 / 发版评估 / 巡检告警 / 排查 / 知识库）

> 状态：已实施（v1.0.0 七 WP + v2.0.0 增补 §13）。创建：2026-09-12。取代同日早先的 DESIGN-v1-governance-ledger.md（其账本 API 四洞与知识库两包已吸收为本文 WP-B / WP-F）。
> 范围：`packages/story-lifecycle`。`D:\java-agent`（aiops-mcp 等）与 `D:\agent-assets` 只读消费不改；hc-all 侧 skill 改造只立契约（§10），不在本仓库动刀。
> **本文自包含**：每个 WP 的改动点、验收线、测试要求全部内联，执行者无需阅读其他文档即可开工。

---

## 0. TL;DR（执行者先读）

**定位迁移：story-lifecycle 从"需求编排器"转型为工作特化 agent 的常驻大脑。** agent 的手脚是 agent 会话里的 skill（story-loop / bug-track / prod-patrol / pre-release-review…），公司资产接口是 aiops-mcp（java-agent，~70 个 MCP 工具：TAPD/ES/SkyWalking/ODPS/Nacos/DingTalk）。本仓库提供别的层都没有的东西：**常驻状态与账本、节律（提醒/告警）、知识库**。

```
┌ agent 会话层（手脚，已有）─────────────────────────────┐
│ ZCode 主会话 + skills：story-loop / bug-track / prod-patrol /  │
│ pre-release-review / sql-query / call-api / hc-knowledge …     │
│ （hc-all .claude/skills + D:\agent-assets 公司级 skill 库）      │
└───────────────┬─────────────────────────────────────┘
                │ MCP 工具面（butler 六工具 + knowledge_search）/ REST
┌───────────────▼─────────────────────────────────────┐
│ story-lifecycle（大脑，本仓库，常驻 serve :8180）         │
│ 账本：story/bug 状态 + lifecycle + gates + 审计            │
│ 节律：日清 digest / 超龄升级 / 巡检 FAIL 告警（outbox→微信） │
│ 知识：.story/knowledge 进（pitfall import）出（search）      │
└───────────────┬─────────────────────────────────────┘
                │ 只读事实查询（ES/TAPD/ODPS/日志…）
┌───────────────▼─────────────────────────────────────┐
│ aiops-mcp（java-agent，资产接口，不改）                  │
└─────────────────────────────────────────────────────┘
```

七个 WP，串行实施，每 WP 一个 commit：

| WP | 内容 | 对应需求 | 风险 |
|---|---|---|---|
| WP-A | 冻结宣告 + 定位改写（文档+模块标注，零逻辑改动） | 前提 | 极低 |
| WP-B | 账本 API 四洞（base_commit / stage complete / advance 500 / context 丢弃） | 支撑 story-loop | 中 |
| WP-C | TAPD 工单环：bug 时间字段 + 超龄计算 + 日清 digest + 提醒路由 | 需求① | 中 |
| WP-D | 巡检 FAIL 告警闭环（emit_event → 微信） | 需求③ | 低 |
| WP-E | 发版窗口评估（train 聚合 + 回滚风险数据面） | 需求② | 中 |
| WP-F | 知识进出（RUN 新坑摄入 + knowledge_search API/MCP/CLI） | 需求⑤ | 低 |
| WP-G | 版本收口 1.0.0（CHANGELOG/README/AGENTS.md/tag） | — | 极低 |

**关键设计决策（已拍板，不再讨论）**：

- **serve 不加定时器**：常驻线程只保留编排线程 + 通知线程两个（"唯一调度入口"硬规则的外延）。日清/巡检的**触发**由外部定时任务系统（LifeOS）调 CLI wrapper（`story daily --push`，PLAN-proactive-cadence 已规划该模式）；serve 保持事件驱动——被调时计算并 emit。
- **提醒分两档**：`daily_digest`（digest 档，晨报推微信）+ `bug_aging`/`story_overdue`/`patrol_failed`（interrupt 档，打断级推微信+桌面）。路由走既有 outbox/router 机制，config 可覆盖。
- **回滚判定 = 证据存在性，内容研判归 skill**：serve 的发版评估端点只聚合"有没有 DDL / 有没有 api-bump / 有没有配置变更 / MR 状态 / gates / 巡检"，给出 rollback_risk 分级；"这条 DDL 能不能回"由 agent 会话里的 pre-release-review skill 消费该 bundle 判断。账本不做内容猜测。
- **知识库不做通用 RAG**：JIT 检索复用 `KnowledgeIndex.retrieve`（md+frontmatter+INDEX.json），条目必须挂 `source_refs` 证据链。
- **冻结 = 停止投入 + 宣告**：不删代码不删测试；编排器链路留档（WP-A）。

## 1. 六项能力 × 现有资产 × 缺口（侦察结论，2026-09-12）

| 需求 | 已有资产 | 缺口 → 本版落点 |
|---|---|---|
| ① TAPD 任务/bug 日清 + 挂龄达标 | TapdSource 已同步 story+bug（current_owner 过滤、severity 已捕获）；BugsPage/CalendarPage；`story daily` 纯读简报；外部定时任务系统 + wrapper 模式 | **未捕获 created/modified/resolved → 无法算挂龄**；无周期同步；无提醒 → WP-C |
| ② 发版窗口批量评估 + 回滚 | `release_train` 列（schema.py:277）+ ReleaseTrainBoard 页 + train 巡检聚合端点；skill 侧 pre-release-review / prod-release-apply / post-deploy-verify 链 | 无 train 级**需求集合**聚合视图（DDL/MR/gates/巡检打包）→ WP-E |
| ③ 定时巡检 + 预警 | patrol 三表+五端点（Phase 2）；执行在 hc-all 侧 prod-patrol skill（serve 只记账）；aiops-mcp 有 alert_scheduler + 日报 → 钉钉 | **patrol FAIL 无告警**（只写 event_log，无 emit_event）→ WP-D |
| ④ 日常排查/业务链路（同事来问） | skill 侧极厚：call-api（业务链驱动）/sql-query(+playbook)/env-debug/data-map-maintain/user-query-troubleshoot；aiops-mcp ES/SkyWalking/ODPS 工具 | 不缺手脚，缺**沉淀回路**（问过→知识库→下次秒回）→ WP-F + §10 |
| ⑤ 知识库越问越厚 | `.story/knowledge`（scenario/playbook/failure/wiki+staleness）；miner 摄入；skill-retro 三级分流；RUN 文档新坑表；hc-knowledge skill | 摄入靠人肉；检索面没有 API/MCP 入口（provider 只接规划 prompt）→ WP-F |
| ⑥ story-loop 改造 | 203 行 SOP skill，含 5 处 venv python 直写 DB 绕行（全是 WP-B 的洞） | → §10 契约 |

## 2. 目标架构补充说明

- **数据流（需求①的日环）**：LifeOS 定时器（晨）→ wrapper 调 `story daily --push` → serve 计算（active story/超龄 bug/昨日巡检 FAIL）→ `emit_event("daily_digest")` → outbox → 微信（clawbot@101）/桌面。超龄/巡检 FAIL 是事件驱动：同步（`POST /api/sync/tapd`）与巡检回填（`POST /api/patrol/run`）时即时 emit。
- **数据流（需求②）**：story 收口时登记 `release_train`（§10 契约进 story-loop 第 7 步）→ 发版窗口日，agent 会话跑 pre-release-review skill → skill 调 `GET /api/trains/{train}/release-review` 拿聚合 bundle → LLM 研判逐条+汇总回滚可行性 → 结论回贴 gate（`POST /gate-results`，已有端点）。
- **数据流（需求④⑤）**：同事来问 → agent 会话（call-api/sql-query 排查）→ `story pitfall import` / skill-retro 沉淀 → 下次 `knowledge_search`（MCP 第 7 工具）先查后答。

## 3. WP-A — 冻结宣告 + 定位改写

纯文档与标注，零逻辑改动：

1. `docs/ARCHITECTURE.md` 新增「冻结范围（v1.0）」一节：冻结能力清单（`OrchestratorThread._tick` 的 spawn/监督/判定链、PTY spawn 家族（`_spawn_story_agent_pty`/`arm_sid_capture`）、full-auto profile、`continue_orchestrator_agent` 同步 shim）+ maintained 边界（`engine/planner.py` 不整体冻结——`_read_prd_snippet` 等仍被 `story tool context` 用；`knowledge/adapters/` 不冻结——知识 bootstrap 的 headless AI 调用在用）+ 测试策略（冻结模块测试留作档案回归守卫，不加新特性测试）。
2. 4 个模块 docstring 加冻结标注：`orchestrator/scheduler.py`、`orchestrator/executors.py`、`orchestrator/engine/supervisor.py`、`orchestrator/engine/claude_stream.py`（措辞：冻结于 v1.0，见本设计文档；修 bug 可以，不加新能力）。
3. ARCHITECTURE.md 开头定位段落改写为「工作特化 agent 的常驻大脑」一句话级（大改留 WP-G README）。

**验收线**：节存在且边界准确；4 模块有标注；包级 pytest 全绿（证明零逻辑改动）。

## 4. WP-B — 账本 API 四洞（支撑 story-loop 直跑）

原则：每个洞必须有回归测试；每个非执行分支必须有可见反馈；新端点先立 `state x action` 决策表。

### 4.1 洞① `base_commit` 静默丢弃
`SetBranchRequest`（`routers/context.py:35-40`）加 `base_commit: str | None = None`，`api_set_branch`（:298-308）fields 透传（列/DAO 早已支持）。回归：PUT 带值 → DB 行有值；不带 → 行为不变。

### 4.2 洞② 新端点 `POST /api/story/{key}/stages/{stage}/complete`
动机：外部调用者没有任何 API 能写 `_completed_stages`（只有编排器 judge 路径 `handlers.py:80-85` 写；`code` gate 存在性只看它，`deliverables.py:168-171`），逼出直写 DB。`deliverables/code/skip` 语义是"合法不需要"，不能顶替。

| story 状态 | stage 输入 | 结果 |
|---|---|---|
| 不存在 | — | 404 |
| 终态 | — | 409「已到终态」 |
| 任意 | ∉ profile launch stages | 400 + 列出合法 stages |
| 任意 | 已在 `_completed_stages` | 200 幂等返回 gate 状态，不重复 append/事件 |
| 活跃 | 合法未完成 | append + `log_event("stage_completed_manual")` + 200 返回 gate 摘要 |

请求体 `{"evidence": {"note": str, "commits": [str]}}` 全可选进事件。**明确不做**：不自动 advance（确认门纪律）、不释放 PTY、不触发 judge、不 auto-commit。推进仍走 `/lifecycle/advance`。回归测试覆盖五行决策表 + append 后 `code.exists` 变 true。

### 4.3 洞③ advance 500 + 管家事件挂钩
`/lifecycle/advance`（lifecycle.py:296-438）包结构化错误处理：预期失败 → 4xx 中文反馈；意外异常记日志、500 带 story_key context。409（gate 未满足，:401-406）与 428（upgrade pending）路径 `emit_event("gate_waiting", ...)`（复用 emitter 的 outbox 语义）。回归：gate 未满足 → 409 + outbox 出现 `gate_waiting` 行；内部异常 → 结构化错误非裸炸。

### 4.4 洞④ `PUT /context` 收 body 丢弃
`PUT /api/story/{key}/context`（context.py:68）只做 revision CAS + bump，`projects`/`documents`/`change_items` 收下即丢。改：白名单键持久化进 `context_json`，CAS 保持。回归：PUT 携带 projects → `GET /context` 读回；revision 冲突仍 409。

## 5. WP-C — TAPD 工单环（需求①）

1. **字段补齐**：`tapd_source._fetch_bugs` extra 增加 `created`/`modified`/`resolved`/`expected_fix_time`（TAPD Bug API 自有字段，`cli_tapd.py` 客户端原样返回）；落点与现有 extra（severity/url）一致——实现时核实现有 extra 落库路径（`stories.py:492-550` upsert 链）并保持同型。存量 story 不回填（下次同步自然带上）。
2. **超龄纯函数**（新 `sourcing/aging.py`，Decider 纯读）：`bug_active_days(bug)` = today − effective_start（status=reopened → `modified`，否则 `created`）；`story_overdue_days(story)` = today − deadline。阈值 config.yaml `tapd.bug_aging_warn_days`（默认 3）。同步时（sync router 落库后）对超龄者 `emit_event("bug_aging"/"story_overdue", tier=interrupt, payload={key,title,age_days,url})`——同一天同 key 去重（outbox 无内建去重，emit 前查当日已发）。
3. **日清 digest**：`POST /api/digest/daily`（新 `routers/digest.py`）聚合：今日/近 3 日到期 story、超龄 bug 排行（按 age_days 降序）、active story 清单（state+stage+最近 gate）、昨日 patrol FAIL 摘要（读 patrol 表）→ `emit_event("daily_digest", tier=digest, payload=markdown)`。`story daily` CLI（`entry/cli/daily_cmd.py`）加 `--push` 标志调同一逻辑（复用聚合函数，不复制）。
4. **路由**：`TIER_CHANNELS` digest 档 → (wechat, desktop)（现 desktop-only；digest 档目前仅日清使用，放大无副作用）；`DEFAULT_ROUTES` 加 `daily_digest: digest`、`bug_aging/story_overdue: interrupt`、`patrol_failed: interrupt`。config 覆盖机制不动。
5. **不做的**：不做小时级轮询提醒、不改前端 BugsPage（aging 列记 follow-up）、不捕获 tasks 实体（TAPD 任务暂不同步，bug+story 够日清）。

**验收线**：fixture bug（created=5 天前, status=in_progress）→ `bug_active_days`=5；同步后 outbox 出现 `bug_aging` 行且当日去重成立；`POST /api/digest/daily` 返回的 markdown 四节齐且 outbox 有 `daily_digest` 行；路由表单测覆盖新事件。

## 6. WP-D — 巡检 FAIL 告警闭环（需求③）

`post_patrol_run`（`routers/patrol.py:155-192`）在 run 落库后：`summary == "FAIL"` → `emit_event("patrol_failed", tier=interrupt, payload={train, story_key, run_id, failed_items:[{name, result, evidence_ref}]})`。仅在**新建 run** 时告警（若 post_patrol_run 对同 run_id 幂等重放，则重放路径不发）。日清 digest 的巡检节由 WP-C 读表实现，本 WP 只做即时告警。回归测试：POST FAIL run → outbox 出现 `patrol_failed` 行且 payload 含 evidence；PASS run 不发。

## 7. WP-E — 发版窗口评估（需求②）

1. **train 登记**：`PUT /api/story/{key}/release-train` body `{"train": "app-1.2.33"}` → 写 story 列（若既有 story 更新通道已可写该列则复用，缺则加薄端点）。回归：写后 `GET /api/story` 返回带 train。
2. **聚合端点** `GET /api/trains/{train}/release-review`（新 `routers/release_review.py`，纯读聚合）：按 `release_train` 捞 story 集，每条产出：
   - lifecycle 状态 + 四件套 gate 状态（复用 `GET /deliverables` 内部逻辑）
   - 分支/base_commit（story_project 表）
   - delivery artifacts（MR：source/target/状态/evidence）
   - DDL 证据：registered documents + workspace story 目录 `ddl.sql` 扫描（best-effort，复用 checker 已知约定路径；找不到记 `"ddl": null`）
   - 巡检：该 train 的最近 patrol run 摘要
   - `rollback_risk` 分级（**证据存在性规则**，不做内容判断）：DDL 非空 → `high`；impact.md 存在且提及 Nacos/配置/api-bump（关键词匹配，标注"待 skill 研判"）→ `medium`；纯代码 → `low`
   rollup：train 级汇总表（各风险级计数 + gates 未齐清单 + MR 未合清单 + 最近巡检结论）。
3. **不做**：不做回滚可行性结论（pre-release-review skill 消费 bundle 研判）、不做 REST 之外的 MCP 工具（skill 调 REST 够用）、不做前端页（ReleaseTrainBoard 加 tab 记 follow-up）。

**验收线**：fixture 两 story（一带 DDL 一纯代码）同 train → 端点返回 per-story + rollup，风险分级 high/low 正确；空 train → 404。

## 8. WP-F — 知识进出（需求⑤）

### 8.1 写侧：RUN 新坑摄入
- 新模块 `knowledge/knowledge_store/run_pitfalls.py`：解析 `docs/test-runs/RUN-*.md` 的「本轮新坑（候选回写）」表（`| 坑 | 规则 |` 两列）；容错（缺列跳过 + WARN）。每条 → `FailureEntry`：`category="run-pitfall"`、`title=坑`、`detail=规则`、`tags=[story_key, "run-pitfall"]`、`source_refs=[RUN md 路径]`；id 由 story-key+坑标题 hash 派生（幂等）。写后刷新 INDEX.json。
- CLI `story pitfall import <file-or-dir> [--root DIR]`（root 缺省 `resolve_knowledge_root`）。重复 import 幂等。
- 验收：对 `docs/test-runs/RUN-tapd-1069389-20260908.md` 真实导入 6 条；重复导入 no-op；`KnowledgeIndex.retrieve("联系人")` 命中。

### 8.2 读侧：检索面三入口
- serve：`GET /api/knowledge/search?q=&story_key=&stage=&top_k=`（新 `routers/knowledge.py`，薄包 `KnowledgeIndex.retrieve`，响应带 `source_refs`；失败降级空结果 + warning，不 500）。
- MCP：`butler_server.py` 第 7 工具 `knowledge_search`（只读走 REST；BUTLER_TOOLS schema + dispatch 各加一条；错误降级友好文本）。
- CLI：`story tool context` 末尾追加「### 相关知识」节（`KnowledgeIndex.retrieve(query=标题+stage)` top-3，best-effort 吞异常打 WARN）。
- 回归：router 单测（tmp knowledge root）+ butler 工具 schema/dispatch 单测 + story tool 输出包含节（有知识）/不崩（无知识）。

## 9. WP-G — 版本收口 1.0.0

1. `pyproject.toml` version → `1.0.0`；`service/api.py:125` 硬编码 `"0.1.0"` 改为读包版本（`importlib.metadata` 或 `__version__`）。
2. `CHANGELOG.md` 顶部加 1.0.0 条目（定位迁移一句话 + 七 WP 要点）。
3. `README.md`（包级）定位段改写：工作特化 agent 常驻大脑；三层架构图（§0）；六项能力表。
4. 仓库根 `AGENTS.md`：story-lifecycle 行的 Role 描述更新（"Core orchestrator: drives AI coding agents..." → 工作特化 agent 常驻大脑表述），并在 Real-story 跑测节前加一行指向本设计文档。**只动这两处，别碰其他段**（并行会话在用）。
5. 打 tag `v1.0.0`（检查点验收后由主会话执行）。

## 10. story-loop 改造契约（需求⑥，hc-all 侧后续执行）

分析结论：story-loop（203 行）是执行层 SOP，与新定位**不冲突**——它就是"手脚"里最重的一个 skill。改造三件事，等本仓库 WP 落地后做：

| # | 改动 | 依赖 | 删什么 |
|---|---|---|---|
| 1 | 删 5 处 venv python 直写 DB 绕行 | WP-B | 第 4 步核对表 base_commit 回填（:89）、第 7 步 base_commit 警告块（:127-130）、`_completed_stages` 回填块（:131-137）、踩坑速查"PUT /context/branch 丢 base_commit"行（:191）、"serve HTTP advance 500"行的 venv 兜底（:180，保留 500 现象描述等 WP-B 验证后删） |
| 2 | 收口第 7 步加 release_train 登记（`PUT /release-train`）+ 发版窗口日先跑 `GET /trains/{train}/release-review` 再研判 | WP-E | 无（新增步骤） |
| 3 | 第 8 步记录回写加 `story pitfall import <RUN md>`（RUNS.md 手抄新坑 → 机器入库） | WP-F | 无（新增步骤） |

## 11. 测试与验收策略

- 子代理每 WP 跑定向测试；检查点（主会话）跑包级全量 `pytest packages/story-lifecycle/tests`（repo root，`.venv-monorepo-test`）。
- 四洞 + 超龄 + 告警各有一条回归测试（历史 bug 必须有回归测试——硬规则）。
- 不碰 `packages/testing`、root `tests/`、miner/knowledge 包、`D:\java-agent`、`D:\agent-assets`、hc-all 仓。

## 12. 实施顺序

WP-A → WP-B → WP-D → WP-F → WP-C → WP-E → WP-G（先小后大：D/F 小而独立，C/E 依赖聚合面较大）。每 WP：子代理实现（**不 commit**）→ 检查点审查（diff + 定向测试 + 包级 pytest）→ 返修（子代理）→ 验收后主会话 commit（只暂存本 WP 文件）。WP-G 收口打 tag `v1.0.0`。

## 13. v2.0 增补（2026-09-13，自动执行退役收口）

v2.0.0 的版本语义：**自动执行退役**——skill 只以「`GET` deliverables → 照 remediation 补缺口 → `advance`」循环消费服务器，编排器驱动的自动执行链正式成为冻结档案（v1.0 是定位宣告，v2.0 是把 v1.0 留在线上的执行链也收掉）。砍主线能力，故升 major。

### 13.1 去重第一步落地（v1.1 设想的执行）

- **409/428 自描述**：`GAP_REMEDIATION` 单一事实源在 `sourcing/deliverables.py`（与 `LIFECYCLE_GATES` 同处）；缺口响应带 `{message, missing[], remediation}`（按缺口给 endpoint/method/hint + skip 备选），`gate_waiting` 事件 payload 携带同一份。前端 client/StoryDetail 与管家 MCP 三消费端适配 dict detail。
- **skill §7 收口循环化**：story-loop 收口段改为纯循环消费者（外部契约，hc-all 侧执行）。
- **四处重复点消解状态**：FLOW 图红圈转绿——①②已消（409/428 自描述 + 收口循环化），③④已缓（判据索引声明/速查表冻结）。见 `FLOW-v1-work-agent.html`。
- **`GET /next` 仍后置**：本轮不做，等循环消费模式跑出真实手感再评估。

### 13.2 SOP V1.7 嵌入 + grill-me（story-loop 契约 v2）

§10 契约升级（hc-all 侧 story-loop skill）：需求意图分析（TAPD 详情 + 附件用例解析）→ 探索轮（三方对账表 PRD×用例×代码）+ Grill 轮（带证据发问，一轮收齐；挂起出口 TAPD 评论/ASSUMED 标记）；用例处置表（`PRD.md` `## 用例处置` 节）为唯一载体、机器可校验；test-report 双落位确认门（PUT + confirm 授权合一，点头后自动传 TAPD 附件）；冒烟门 ≥80%、发布窗口周二/周四、TAPD 状态按 SOP 回写。经 kimi 双轮评审修补。grill-me 设计见 [`DESIGN-task-actions-and-grill-me.md`](DESIGN-task-actions-and-grill-me.md)。

### 13.3 TAPD 附件上传链（外部基建，指针）

官方 `files/upload_attachment` 被公司授权面拒绝（个人令牌 + API 账号双实测 403）→ 决策：走网页内部端点 `add_attachment_drag` + 会话自动续期。部件（均在仓库外，改这些不需动本仓库）：

- `~/.claude/scripts/tapd_web_session.py` — 专用 Edge 档案收割 + 账密自愈（WAF 拒 headless）
- token_manager「tapd-web」服务 — 10h TTL 自动续期
- `cli_tapd upload-attachment-web` — 上传 CLI
- `D:/hc-all/scripts/report_to_docx.py` — 报告转 docx 后上传

### 13.4 版本语义

v2.0.0 = 自动执行退役（skill 变纯循环消费者）；v1.0.0 = 工作特化定位宣告（七 WP）。两个 tag 均在 origin。
