# 状态归一维护手册 — 从 4 处真相源收敛到 1 处

> ⚠️ **部分订正（2026-07-09）**：本文原主张"Story 业务状态从 `_completed_stages` 派生，不另存"是**错误的**。正确模型见 [`STORY-STATE-MODEL.md`](./STORY-STATE-MODEL.md)：**Story 状态是独立第一公民，不从阶段派生**（Temporal/Jira/Process Manager 三个来源印证）。
> 本文以下内容里：
> - ❌ 步骤 4「status 语义分层」中"业务状态从 `_completed_stages` 派生"→ 错，应为独立字段 `story.lifecycle_state`。
> - ❌「目标分层架构」中"业务状态派生视图"→ 错，应为独立第一公民。
> - ✅ 步骤 1-3（`_completed_stages` 作 driver 层真相源、删冗余字段）→ **仍有效**，只是它定义的是 driver 层内部进度，不定义 Story 业务状态。
> 以 STORY-STATE-MODEL.md 为准。
>
> ---
> STATE-MAP.md 的姊妹篇。STATE-MAP 画"现状怎么跑"，本文档定"怎么收敛到单一真相源"。
> 创建：2026-07-08。可执行治理清单，非一次性重构。
> 范围：`packages/story-lifecycle`。

---

## 诊断：同一个信息现在存几处

扫描代码后，"当前 story 进展到哪了"这个问题，散落在 **4 处**，且常不一致（你那个 story 就是活例）：

| # | 存储位置 | 字段 | 谁写 | 问题 |
|---|---|---|---|---|
| 1 | DB `story.status` | planning/active/paused/failed/completed/blocked/aborted/implementing | 9+ 处（planner 7、api 5、story_service 6、graph 2） | **引擎内部状态**，不是业务状态；语义过载（9 值混 3 层） |
| 2 | DB `story.current_stage` | design/build/verify/... | planner.py:550、list_cmd.py:254、story_service:130,253 | 和 status 不同步（status=planning 时 current_stage 可能已是 design） |
| 3 | DB `context_json._active_execution.stage` | 当前在跑的 stage | 仅 planner.py:808（自动链路） | **冗余** current_stage；孤立终端路径不写它 |
| 4 | 文件系统 done file | `.story/done/<key>/<stage>.json` 存在与否 | claude CLI 写 | **stage 真做完了吗的真相**，但 DB 无字段反映，靠 poll |

**"哪个 stage 完成"这一个事实，存在 #2(current_stage) + #3(_active_execution.stage) + #4(done file) 三处。** 加上明天要加的 `_completed_stages`，会变成四处。这就是乱的根源。

---

## 归一目标：1 个真相源 + N 个派生视图

按 STATE-MAP 北极星：**一个 workflow 状态机（驱动层进度）作为单一真相源，业务状态和 stage 进度都从它派生，不另存。**

```
单一真相源（驱动层进度）:
  context_json._completed_stages: ["design", "build"]   ← 明天 plan 引入
  context_json._stage_gate: {...}                        ← 确认闸标记

派生视图（不另存，读时计算）:
  业务状态（开发/测试/上线）= 从 _completed_stages 算
  current_stage            = _completed_stages 之后的第一个 launch stage
  done 进度                = 扫 done file（仍是 claude 写，但只作 _completed_stages 的输入，不独立读）
```

**判据**：任何新字段，先问"它是真相源，还是派生视图？"——派生的不存，读时算。

---

## 归一步骤（与明天确认闸 plan 同步执行）

### 步骤 1 — 确立 `_completed_stages` 为 stage 进度唯一真相源

明天 plan 已含此字段。归一的关键：**done file 降级为输入，不再被业务逻辑直接读**。

- 现在：`find_ready_interactive_stories`(graph.py:360)、`continue_orchestrator_agent`(planner.py:873)、`_write_retrospect`(planner.py:401) **各自直接扫 done file**。
- 归一后：**只有一处**（`continue_orchestrator_agent` done 检测处）扫 done file → 确认后写入 `_completed_stages`。其余全读 `_completed_stages`。
- `find_ready_interactive_stories` 改为读 `_completed_stages` 判断"当前 stage 是否完成"，不再扫文件（文件只是 _completed_stages 的写入依据）。

### 步骤 2 — 消除 `current_stage` 冗余写路径

`current_stage` 现有两个写路径：
- 自动链路：planner.py:550（正确）
- 旧 CLI：list_cmd.py:254,275（legacy，硬编码 STAGE_ORDER）

**`current_stage` 改为派生字段**：从 `_completed_stages` + `_agent_actions` 算（第一个未完成的 launch stage）。不再由任何代码显式 `update_story(current_stage=...)`。
- 短期（明天）：保留显式写，但前端/后端读 stage 进度时**优先读 `_completed_stages`**，current_stage 作 fallback。
- 中期：`current_stage` 改为 DB 视图或 API 计算属性，删掉所有 `update_story(current_stage=)`。

### 步骤 3 — 删 `_active_execution.stage`（与 current_stage 完全冗余）

`_active_execution`（planner.py:808）存 `{mode, adapter, stage, start_time}`。其中 `stage` 与 `current_stage` 100% 冗余。

- 保留 `_active_execution` 的 `mode`/`adapter`/`start_time`（PTY 执行元信息，有用）。
- **删 `stage` 字段**，读 `current_stage`/`_completed_stages`。
- `find_ready_interactive_stories`(graph.py:356) 原本要 `_active_execution.stage == current_stage`，归一后改为查 `_completed_stages` 含 current_stage。

### 步骤 4 — status 语义分层（业务状态 vs 引擎状态）

这是最大的归一。现在 `status` 9 个值混了三层语义：

| 层 | 当前混在 status 里的值 | 该去哪 |
|---|---|---|
| 业务状态（你关心的） | planning / completed | **派生自 `_completed_stages`**：全完成=completed，否则=进行中 |
| 引擎执行态 | active / paused / implementing / failed / blocked / aborted | 保留为 `status`，但**只表执行健康度**（在跑/暂停/出错/终止） |
| 历史化石 | idle / archived | archived 保留（只读归档），idle 评估能否删 |

**分离后**：
- 前端"开发/测试/上线"视图 = `_completed_stages` 派生（design done=开发完成，verify done=测试完成...）。
- 前端"引擎是否健康" = `status`（active=正常跑，paused=等人，failed=出错）。
- 两个维度正交，不再用 9 值塞一个字段。

> 注：这是中期目标，明天只做步骤 1-3。步骤 4 触及前端展示逻辑和多个 status 读取点，单独评估。

### 步骤 5 — 收敛驱动入口（5 入口 → 1 调度者）

明天 plan 已含"去掉孤立 `/sessions/spawn` 主按钮"。完整收敛：

| 入口 | 归一后 |
|---|---|
| `/plan/confirm` | **唯一入口**：开始/推进自动链路 |
| `/sessions/spawn` | 降级 debug（不写任何 DB 状态，纯 PTY 工具） |
| `/advance` | 保留，但只 resume（从 paused→active），不另起逻辑 |
| `/skip/{stage}` | 保留，写入 `_completed_stages`（标记跳过）而非另设机制 |
| 1 秒后台轮询 | **gate**：只 resume 无 `_stage_gate` 的，确认闸的不动 |

---

## 执行优先级

| 步骤 | 时机 | 风险 | 价值 |
|---|---|---|---|
| 1 `_completed_stages` 唯一真相 | 明天（plan 已含） | 低 | 高 |
| 3 删 `_active_execution.stage` | 明天 | 低 | 中 |
| 2 `current_stage` 派生化 | 短期（含前端改） | 中 | 高 |
| 5 入口收敛 | 短期（plan 部分） | 中 | 高 |
| 4 status 语义分层 | 中期（触前端） | 高 | 高 |

---

## 验收判据（归一完成的标志）

改完后，回答"story X 现在进展到哪了"，只需要读 **一个地方**：

```python
ctx = json.loads(story["context_json"])
completed = ctx.get("_completed_stages", [])
gate = ctx.get("_stage_gate")
# 业务进度：completed 列表
# 当前在跑：completed 之后的第一个 launch stage
# 是否等人确认：gate.awaiting_confirm
# 引擎健康度：story.status（active/paused/failed）
```

而**不是**现在这样同时查 status + current_stage + _active_execution.stage + done file + PTY 注册表。

每加一个 `_xxx` 字段到 context_json，回头查这份文档的"判据"——它是真相源还是派生视图？派生的不存。

---

## 与 STATE-MAP.md / PLAN-stage-confirm-gate.md 的关系

- **STATE-MAP.md**：现状地图（问题在哪）。读完知道为什么乱。
- **本文档（STATE-CONSOLIDATION.md）**：归一路径（怎么治）。读完知道朝哪收敛。
- **PLAN-stage-confirm-gate.md**：明天的施工图（第一步）。步骤 1、3 在此 plan 内。

三个文档一条线：看清现状 → 定归一方向 → 落第一步。后续每个改动，回到 STATE-CONSOLIDATION 对齐步骤。
