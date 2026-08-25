# 知识飞轮闭环 —— 设计文档

> 状态：**M1 已实现（2026-08-21，B1+B3+B4）；M2 已实现（2026-08-24，B2）；M3 已实现（2026-08-24，B5）**。创建：2026-08-21。
> M1 实测：story-lifecycle + knowledge 包 1459 tests 全绿；sourced 创建路径已打标，无 task_type story 降级注入，reflection 落盘即重建 INDEX，写读共用 `resolve_knowledge_root`。
> M2 实测：`learning/mining_trigger.py` 单测 7 绿 + handlers/scheduler/reflection/flywheel-e2e 回归 64 绿；防抖(30min/3 story 任一满足)、单飞、miner 缺失 no-op 均覆盖。**遗留：真实 story 完成走查（§5 M2 第 2 条）待下次 test-run。**
> M3 实测：`knowledge_injected` 事件单测 4 绿 + prompt/knowledge 回归 9 绿；事件随 prompt_export 导出（零新代码验证）；离线 SQL 见 §4.5。设计稿的 entry_ids 未做（provider 只返回 markdown，条目归因归 hc-all 引用制）。
> 范围：`packages/story-lifecycle`（context_providers / reflection / handlers / scheduler / 创建路径）；连带 `packages/knowledge`（index 自刷新）、`packages/story-miner`（增量挖掘触发）。
> **本文自包含**：起因、实测数据、代码现状（带文件:行号）、断点清单、方案、分阶段实现、风险全部内联。

---

## 0. TL;DR（评审者先读）

**现状**：飞轮链路的每一段代码都存在且有真实数据痕迹（miner 脚本齐、INDEX.json 有 71 条、prompt 注入已接线、reflection 回写已通过），但实测**飞轮对绝大多数 story 不转**。

**实测证据**（2026-08-21 核查）：

- 最近 30 个 story 只有 4 个 `context_json.task_type` 有值（87% 缺失）→ `KnowledgeContextProvider.get_context` 第一步就 return None，**87% 的 story 执行时飞轮知识注入量为零**。
- 根因不是分类器质量：TAPD bug/sync 两条创建路径（`routers/bugs.py:55`、`routers/sync.py:59`）走 `db.upsert_story_from_source`，**完全绕过** `create_and_start_story`（`story_service.py:120`）里的 LLM+关键词分类。`tapd-bug_*` 种群全军覆没。
- miner 挖掘产物停在 2026-06-28/29（`scripts/out/`、hc-all `failures/failure-knowledge.json`）——**快两个月没人跑 `refresh.sh`**，注入的知识是陈旧的。
- reflection 落盘的 playbook（`playbooks/<task_type>/failure-patterns.md`，hc-all 真实存在）**写完后没有任何人重建 INDEX.json**；`KnowledgeIndex._load` 只在 INDEX.json *缺失*时才重建（`packages/knowledge/src/knowledge/index.py:31-32`）→ 新沉淀的经验永远不可召回，除非 wiki_pipeline 碰巧跑。
- 写读路径错位：reflection 写到 `story.workspace/.story/knowledge/`，provider 固定从单一全局根读（`knowledge_provider.py:52-54`，`STORY_KNOWLEDGE_ROOT` 默认 `D:/hc-all`）→ workspace 不是 hc-all 的 story（如本仓库的 `tapd-bug_*`）沉淀的经验进不了召回源。

**方案（四个断点 + 一个缺失环，按影响排序）**：

| # | 断点 | 修法（一句话） |
|---|------|----------------|
| 1 | task_type 覆盖 | 分类逻辑收成唯一入口 `ensure_task_type`，所有创建路径调用；provider 侧 keyword 级 lazy backfill 自愈；task_type 缺失时**降级注入全局层**而非整条放弃 |
| 2 | 挖掘不自动 | story completed 的 Handler 里挂**防抖增量挖掘触发器**（story_ingest + link + generate_playbooks 三步，subprocess，best-effort）；重活（TAPD/git/LLM）仍走 `refresh.sh full` 人工/定时 |
| 3 | 回写→索引脱节 | `persist_playbook` 落盘后调 `write_index`；`KnowledgeIndex._load` 加 mtime 自刷新（任何 .md 新于 INDEX.json 即重建）——索引新鲜度不再依赖调用方记性 |
| 4 | 写读根不一致 | 单一解析函数 `resolve_knowledge_root(workspace)`：优先 `<workspace>/.story/knowledge`，回退全局默认；provider 和 reflection 共用 |
| 5 | 效果度量缺失 | 注入时落 `knowledge_injected` 事件（entry ids + 字符数），复用 `prompt_export.py` 离线关联 outcome——只度量，不做实时判 prompt（遵守现有约定） |

**明确不做（本期）**：

- reflection 规则种类扩列（judge reject/escalate 经验、stuck 诊断经验进 playbook）——  backlog，先把现有三类的管道闭环。
- 挖掘全量自动化（`refresh.sh full` 的 TAPD/git/LLM 重步骤挂定时）——  增量先跑通，全量调度是运维决策。
- A/B 实验框架 —— 断点 5 只落事件，分析离线做。
- 多 workspace 知识**合并**（跨项目共享 playbook）—— 本期只做写读同根，合并策略另立设计。

---

## 1. 起因与背景

### 1.1 触发

用户追问"知识库、知识飞轮相关内容够用吗，感觉仍未闭环"→ 全链路实测核查（2026-08-21），确认管道通、飞轮不转。

### 1.2 飞轮应有之义

```
story 执行 → transcripts/events/judge 决策落地            （产出）
  → miner 挖掘成 playbook / failure / 统计基线             （加工）
  → knowledge 包统一 INDEX + retrieve                      （契约）
  → 下一个 story 的 prompt 注入                            （消费）
  → story 完成时 reflection 沉淀规则回写 playbook           （回写）
  → 注入有无效果被度量，反哺模板/检索策略                   （验证）
```

六段里，产出（transcripts/anchors/events 落地）和消费（prompt 接线）是好的；加工靠手动、契约不自刷新、回写进不了索引、验证环缺失。

### 1.3 为什么现在修

- 断点 1 是**覆盖率**问题：87% story 零注入，意味着飞轮对主场景（tapd-bug 修复流）完全空转，其余断点修了也白修。
- 断点 2/3 是**新鲜度**问题：知识陈旧两个月，注入越多误导越多。
- 都是小改动（每个断点 1-2 个文件），不改架构。

---

## 2. 代码现状（改哪里）

### 2.1 task_type 分类与注入门槛

- 分类只在 `create_and_start_story`（`orchestrator/service/story_service.py:174-187`）：LLM 分类（`_classify_task_type_llm`，87 行起）失败回退关键词（`classify_task_type`，`engine/prompt_sections.py:88`）。
- **sourced 创建路径不分类**：`routers/bugs.py:55`、`routers/sync.py:59` 直接 `db.upsert_story_from_source`。
- 注入门槛：`KnowledgeContextProvider.get_context`（`knowledge/context_providers/knowledge_provider.py:281-285`）`task_type` 为空即 return None——包括与 task_type **无关**的段（wiki 摘要、知识库检索）也一起被砍。
- provider 数据根 `_KNOWLEDGE_ROOT`（52-54 行）硬编码默认 `D:/hc-all/.story/knowledge`；`base`（63-67 行）默认**相对路径** `packages/story-miner/scripts/out`，依赖 serve 启动 cwd。

### 2.2 挖掘触发

- `packages/story-miner/scripts/refresh.sh`：incremental（store --since-days 1 / story_ingest / link / generate_playbooks）+ full（加 failure_mode / bug_story_graph / classify / story_commits / infer / result_axis_phase2）。注释写明 "Hermes cron can call this script directly"——**实际无定时**，实测停跑两个月。
- story 完成路径有两个 Handler：`orchestrator/handlers.py:_handle_all_stages_done`（196-213 行）和 `orchestrator/scheduler.py`（642-645 行附近），均已挂 `_write_retrospect` + `_persist_playbook_for_story`——**挖掘触发器挂这里**，与既有回写并列。

### 2.3 INDEX 生成与检索

- `packages/knowledge/src/knowledge/generator.py:write_index`（174 行）：os.walk 递归扫 scenarios/playbooks/wiki/failures——**task_type 子目录会被正确扫到**，缺的不是扫描能力是触发。
- `KnowledgeIndex._load`（`index.py:29-36`）：仅 INDEX.json 缺失时重建；`refresh()`（38-42 行）存在但全仓库无 runtime 调用方（只有 `wiki_pipeline.py:40-42` 在 wiki 流程里调 `write_index`）。

### 2.4 reflection 回写

- `orchestrator/learning/reflection.py`：`reflect` 纯函数（38 行）、`write_playbook_file`（157 行，写 `<workspace>/.story/knowledge/playbooks/<task_type>/<dimension>.md`）、`persist_playbook`（258 行）。
- 调用方 `_persist_playbook_for_story`（`engine/planner.py:644-683`）：`workspace = story.get("workspace")`——story 注册 workspace，与 provider 读的全局根**不同源**。
- 只在 completed 路径触发，task_type 为空跳过——断点 1 连带影响回写率。

### 2.5 效果度量

- `orchestrator/observability/prompt_export.py` 已导出 (prompt + outcome + events + llm_calls) 元组——注入事件落 event_log 后即可被它自然带出，**不需要新导出器**。

---

## 3. 断点清单（验收对照表）

| # | 断点 | 证据 | 影响 |
|---|------|------|------|
| B1 | task_type 覆盖 4/30 | DB 实测（2026-08-21）；bugs.py/sync.py 无分类调用 | 87% story 注入为空 |
| B2 | 挖掘手动 | scripts/out/ mtime=6-28/29；refresh.sh 无调度 | 知识陈旧两月 |
| B3 | 回写不进索引 | write_index 无 runtime 调用方；_load 不查新鲜度 | 新经验不可召回 |
| B4 | 写读根错位 | knowledge_provider.py:52 硬编码 vs planner.py:679 传 story.workspace | 非 hc-all 项目经验流失 |
| B5 | 无效果度量 | event_log 无注入事件 | 闭环缺验证环 |

---

## 4. 方案

### 4.1 B1 —— task_type 覆盖：一个入口 + 懒自愈 + 降级注入

三层，缺哪层补哪层，互不依赖：

**① 创建路径归一。** 抽 `ensure_task_type(story_key, title, description)` 进 `story_service.py`（内部 = 现有 LLM 分类 + 关键词回退 + `db.update_context`）。调用点：`create_and_start_story`（替换 174-187 行内联逻辑）、`routers/bugs.py`（upsert_from_source 后）、`routers/sync.py`（同）。LLM 失败/超时静默回退——与现状语义一致，tagging 永不阻塞创建。

**② provider 懒自愈（keyword-only）。** `_task_type_for` 两级都没命中时，用 `classify_task_type(title)`（纯字符串、零成本）再试一次；命中则 `db.update_context` 回写（自愈，下次直接用）。**热路径不引入 LLM**（prompt 渲染加几秒不可接受；LLM 分类已前移到创建路径，懒自愈只兜存量故事）。

**③ 降级注入。** `get_context` 去掉"无 task_type 即 None"的早退，改为分层：

```
task_type 有 → 现状全量（bootstrap 项目结构 + 高风险文件 + 基线 + bug 磁铁 + 知识库 + wiki）
task_type 无 → 降级层：知识库检索（不按 domain 过滤，trigger/query 命中即返）
             + wiki 摘要（本就不依赖 task_type，当前被误伤连坐）
             + 全局 failure top（failures/failure-knowledge.json 按 frequency 排序前 5）
```

降级层的设计依据：wiki/failure 的召回价值不依赖任务分类，连坐砍掉是 §2.1 早退的误伤。

### 4.2 B2 —— 挖掘自动化：completed 钩子 + 防抖增量

新模块 `orchestrator/learning/mining_trigger.py`：

- **触发点**：`_handle_all_stages_done`（handlers.py + scheduler.py 两处）在 `_persist_playbook_for_story` 之后调 `maybe_trigger_incremental(story_key)`。Handler 是唯一允许起线程/子进程的层（AGENTS.md 硬规则），挂这里合规。
- **干什么**：subprocess 顺序跑 `python -m miner.story_ingest` → `python -m miner.link` → `python scripts/generate_playbooks.py`（cwd=packages/story-miner）。**不跑** store 全量扫和 full 档重活。
- **防抖**：进程内记录上次触发时间/计数，两次触发间隔 ≥30min 且距上次 ≥3 个 story 完成才真跑（满足其一即可跑——低频环境不被计数饿死，高频环境不被打爆）。单飞：同一时刻至多一个挖掘子进程，在跑则跳过。
- **软 seam**：`import miner` 失败（lifecycle 独立运行）→ 整体 no-op。任何子进程非零退出 → warning 落日志，不影响 story 完成路径。与 provider 的 lenient 语义对齐。
- **全量重活不动**：`refresh.sh full` 保持手动/外部 cron，文档里写明建议频率（周级）。

### 4.3 B3 —— 回写→索引闭合：写后重建 + 读时自愈

双保险，各自独立成立：

**① 写后重建（主路径）**。`reflection.write_playbook_file` 成功落盘后：`from knowledge.generator import write_index`（try/except 软 import），对**同一知识根**调一次。write_index 是纯扫盘重写单 JSON，成本可忽略。

**② 读时自愈（兜底）**。`KnowledgeIndex._load` 加新鲜度检查：INDEX.json 存在但 playbooks/scenarios/wiki/failures 下任一 `.md`/`.json` 的 mtime > INDEX.json mtime → 先 `write_index` 再加载。从此索引新鲜度是类的内部不变量，不依赖任何调用方的记性——即使外部脚本（含 miner 的 `task_type_playbooks.py:445` 自己的 write_index）绕路写了文件，下次检索也会自愈。

### 4.4 B4 —— 知识根归一：单一解析函数

`knowledge/knowledge_store/paths.py` 加：

```python
def resolve_knowledge_root(workspace: str | Path) -> Path:
    """知识根解析顺序（写读共用同一函数）：
    1. config.yaml knowledge_root（显式配置，最高优先）
    2. env STORY_KNOWLEDGE_ROOT
    3. <workspace>/.story/knowledge 且内含 manifest.yaml 或 INDEX.json
    4. 全局默认（D:/hc-all/.story/knowledge → ~/hc-all/.story/knowledge 便携回退）
    """
```

- `KnowledgeContextProvider` 的 `_KNOWLEDGE_ROOT` 模块级常量改为**每次调用解析**（story 级 workspace 不同根不同）。
- `reflection.write_playbook_file` / `_persist_playbook_for_story` 改用同一函数——**写读同源是这次修复的核心不变量**，测试要断言两者解析结果相等。
- 顺带修 `KnowledgeContextProvider.base` 的相对路径问题：解析为相对 monorepo 根（`Path(__file__).parents[N]`）或 env `STORY_MINER_OUT`，不再依赖 serve 的 cwd。

### 4.5 B5 —— 效果度量：注入事件 + 离线关联

- 注入点（`prompt_sections.build_knowledge_section` 成功返回非空时）落 event_log：
  `knowledge_injected {story_key, stage, task_type 或 "none", entry_ids: [...], chars, degraded: bool}`。
  `degraded=true` 标记降级层注入（B1③），供区分全量/降级的效果。
- 分析侧零新代码：`prompt_export.py` 已按 (story, stage) 导出 events，`knowledge_injected` 自然随出。离线关联维度：注入 vs 未注入 story 的 judge approve 率 / reject 次数 / stage 时长。
- **遵守现有约定**：不做实时 prompt 质量裁判（AGENTS.md「Offline prompt analysis」节），度量环只落事实、离线分析。

**离线算注入效果（M3 落地，2026-08-24）**——`knowledge_injected` 事件已实现（`prompt_sections.build_knowledge_section` 非空即落，payload `{task_type, chars, degraded}`，DB 失败静默）。事件随 `GET /api/analysis/prompts` 的 events 自然导出（prompt_export 按 (story, stage) 聚合 event_log，零新代码）。手工 SQL（story.db）：

```sql
-- 注入 vs 未注入 story 的 judge 决策分布
SELECT CASE WHEN e.story_key IS NULL THEN 'no_inject' ELSE 'inject' END AS bucket,
       d.decision, COUNT(*) AS n
FROM orchestrator_decision d
LEFT JOIN event_log e
  ON e.story_key = d.story_key AND e.event_type = 'knowledge_injected'
GROUP BY bucket, d.decision;

-- 降级层（B1③）单独看效果
SELECT json_extract(e.payload, '$.degraded') AS degraded, COUNT(*) FROM event_log e
WHERE e.event_type = 'knowledge_injected' GROUP BY degraded;
```

维度：注入 vs 未注入的 judge approve 率 / reject 次数 / stage 时长（时长从 events 时间戳推）。**不建实时看板**（§4.5 约定：只落事实）。

> 已知取舍：payload 未含设计稿里的 `entry_ids`——provider 接口只返回 markdown，条目级使用归因由 hc-all 侧引用制（test-impact 方案 §2.4）承担，不在本表强凑。

### 4.6 不变量与 Anti-pattern

- **写读同根**：任何新增知识写入/读取点必须经 `resolve_knowledge_root`，禁止再出现模块级硬编码路径常量（B4 的形态）。
- **索引新鲜度是 KnowledgeIndex 的内部不变量**：调用方不再需要"记得重建"。新增知识文件类型时同步更新 `_load` 的新鲜度扫描清单。
- **tagging/mining/indexing 全是 best-effort**：任何一步失败不得阻塞 story 创建、prompt 渲染、story 完成。这是现有 lenient 语义的延续，不是新要求。
- **Anti-pattern**：在 provider 热路径加 LLM 调用（B1②只准 keyword）；在 Decider（reflection.reflect）里触发挖掘子进程（Handler-only）；给 reflection 写盘加"顺手重建全局 hc-all 索引"之类的跨 workspace 副作用（B4 修的就是这个）。

---

## 5. 分阶段实现

### M1 —— 覆盖与新鲜度（B1 + B3 + B4）

纯 lifecycle/knowledge 包改动，无子进程，风险最低，收益最大。

1. `story_service.ensure_task_type` 抽取 + bugs.py/sync.py 接入 + 单测（mock LLM，断言 sourced 路径也打标）。
2. provider 懒自愈 + 降级注入 + `resolve_knowledge_root` 改造 + base 相对路径修复 + 单测（无 task_type story 断言降级层非空；写读同根断言）。
3. reflection 写后 `write_index` + `KnowledgeIndex._load` mtime 自愈 + 单测（写 playbook 后 INDEX mtime 更新；外部改文件后检索自愈）。
4. 回归测试（AGENTS.md 硬规则：每个历史 bug 要有回归测试）：构造 sourced 创建的 story，端到端断言 prompt 里出现知识段。

### M2 —— 挖掘触发器（B2）

1. `mining_trigger.py` + 两个 Handler 挂载点 + 防抖/单飞单测（mock subprocess，断言间隔内不重复触发、miner 缺失时 no-op）。
2. test-runs 走查一次真实 story 完成 → 确认增量挖掘真跑、产物 mtime 更新。

### M3 —— 度量环（B5）

1. `knowledge_injected` 事件落库 + 单测。
2. `prompt_export` 导出样例确认事件随出；docs 补一节"怎么离线算注入效果"（SQL/步骤），不建实时看板。

---

## 6. 风险与防护

| 风险 | 防护 |
|------|------|
| 降级注入把无关 failure 塞给 agent，稀释 prompt | 降级层限 top 5 + 按 frequency 排序；`degraded` 事件可观测，误导严重可回退 |
| 挖掘子进程拖慢 serve / 刷失败日志 | 防抖 + 单飞 + best-effort；子进程与 serve 进程隔离，崩了不影响编排 |
| write_index 高频重写（每次 completed） | 单 JSON 扫盘重写，实测 hc-all 71 条 <100ms；若成瓶颈再加写侧防抖 |
| mtime 自愈误判（clock skew / 触摸文件） | 误判代价仅是多重建一次索引，幂等无危害 |
| sourced 路径补分类增加创建延迟 | LLM 分类本就存在于主路径（几秒），bugs/sync 是低频后台路径，可接受；失败静默回退 |

---

## 7. 验收标准

- [ ] 最近新建 30 个 story（含 bugs/sync 来源）task_type 覆盖率 ≥90%（DB 查询复核）
- [ ] 无 task_type 的存量 story prompt 中出现降级知识段（prompt_export 抽查）
- [ ] story 完成后 30min 内 miner 增量产物 mtime 自动更新（不再依赖手动 refresh.sh）
- [ ] reflection 落盘 playbook 后，下一次 `KnowledgeIndex.retrieve` 能召回该条（端到端测试）
- [ ] 非 hc-all workspace 的 story 沉淀的 playbook 能被同 workspace 的后续 story 召回（写读同根测试）
- [ ] event_log 出现 `knowledge_injected` 且 prompt_export 导出包含该事件

---

## 8. 附：核查原始数据（2026-08-21）

- `scripts/out/` 最新 mtime：2026-06-29 19:26（result_axis_phase2.json）；`retrospect_--story.md` 7-02 后无更新。
- `D:/hc-all/.story/knowledge/INDEX.json`：71 entries（playbook 42 / failure 15 / scenario 14），mtime 2026-08-19（wiki_pipeline 刷新）。
- `D:/hc-all/.story/knowledge/failures/failure-knowledge.json`：mtime 2026-06-29。
- story DB task_type 覆盖：4/30（全部缺失者为 `tapd-bug_*` / `INTK` / `tapd-123`）。
- reflection 已落盘证据：`playbooks/order/failure-patterns.md`、`playbooks/credit-limit/failure-patterns.md`（hc-all）。
