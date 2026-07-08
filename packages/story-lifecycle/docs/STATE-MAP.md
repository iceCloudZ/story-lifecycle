# story-lifecycle 真实状态机地图（现状还原，非理想设计）

> ⚠️ **末尾"北极星"章节部分订正（2026-07-09）**：其中"Story 业务状态从驱动层进度派生，不另存字段"是**错误建模**。正确模型见 [`STORY-STATE-MODEL.md`](./STORY-STATE-MODEL.md)：Story 状态是独立第一公民（`story.lifecycle_state` 新字段），不从阶段派生。本图正文（现状诊断、9 值 status、5 入口、4 真相源、灰区矛盾）仍准确，只是"朝哪治"以 STORY-STATE-MODEL 为准。
>
> 目的：把现在代码**实际怎么跑**画成一张表，让你一眼看全 story 的状态从哪来、到哪去、谁改的。
> 这样复杂性就从"藏在 500 行代码里的一团乱麻"变成"一眼能看全的一张图"。
> 创建：2026-07-08。纯现状还原，不改代码。
> 范围：`packages/story-lifecycle`。

---

## TL;DR — 为什么这套流程看不懂

三个根因（详见每个状态转移表的"灰区/冗余"注释）：

1. **状态散落在 4 个地方，没有单一真相源**：DB `status` 字段（9 个值）、`context_json` 里一堆 `_` 前缀字段（`_plan_confirmed`/`_agent_actions`/`_active_execution`）、文件系统 done file、内存 PTY/`_running_stories`。四者常不一致。
2. **5 个入口都能驱动同一个 story，无唯一调度者**：`/plan/confirm`、`/sessions/spawn`、`/advance`、`/skip`、1 秒后台轮询。语义打架（今天 bug 的根因）。
3. **化石堆积**：`StageConfig.confirm`/`review`（死配置）、对抗循环（死代码）、`nodes/` 兼容 facade。读代码三分之一时间在分辨死活。

---

## status 取值（9 个，且 DB 默认值与实际首态不一致）

DB schema 默认 `status='active'`，但代码创建时显式设值，所以默认值几乎不触发。实际值域：

| 值 | 含义 | 谁会设它 |
|---|---|---|
| `planning` | LLM 规划完成，待用户确认 | `run_orchestrator_agent`(planner:347)、`/plan/regenerate`(api:2948)、promote(api:2728,2777) |
| `active` | 自动链路正在跑（或被 resume） | `api_confirm_plan`(2925)、`/advance`(803)、`/skip`(818)、story_service 多处 |
| `paused` | 暂停等确认/等子任务（明天加确认闸会用） | `recover_orphan_stories`(graph:414)、`resume_parent`(story_service:297) |
| `failed` | 自动链路某 stage 失败 | planner 7 处、graph:209、create_story_from_source |
| `completed` | 全部 stage 完成 | planner:1032、bug resolve |
| `blocked` | 人工标记受阻 | `/fail`(api:831)、`fail_story`(story_service:183) |
| `aborted` | 终止 | `abort_story`(story_service:276) |
| `implementing` | rescue 换 adapter 重试中（graph 内部瞬态） | graph:251,254 |
| `idle` | DB 里出现过（可能历史遗留） | — |
| `archived` | 归档（只读） | `/archive` |

**注意 `intake_state`**（另一维度，独立于 status）：`candidate` | `ready`。
- `candidate`：TAPD/GitHub 拉来还没 promote，`start_story_async` 会**跳过**它（graph:293-297）。
- `ready`：promote 后才能跑。

---

## 核心状态转移表（`状态 × 触发入口 → 新状态 + 副作用`）

### 主流程：planning → active → (paused) → completed

| 当前状态 | 触发（谁/什么） | → 新状态 | 副作用 / 关键代码 |
|---|---|---|---|
| (新建) | `create_story` / TAPD promote | `planning` | 设 `intake_state=ready`；`/plan/stream` 触发 LLM 规划写 `_agent_actions`+`_plan_confirmed=False`（planner:347） |
| `planning` | **`POST /plan/confirm`**（前端「确认并执行」） | `active` | 设 `_plan_confirmed=True`；`start_story_async`（api:2928）→ 线程池跑 `run_story`→`continue_orchestrator_agent` |
| `planning` | **`POST /sessions/spawn`**（前端「启动终端HITL」）⚠️ | `planning`（**不改 status！**） | **只 spawn 孤立 PTY，不写 `current_stage`/`_active_execution`，不启动自动链路** ← 今天 bug 根因：用户以为开始干活了，其实 status 没动 |
| `active` | 自动链路 poll 到 done file，`confirm=False` | `active` | 推进下一 stage；**当前不 kill 旧 PTY**（进程堆积） |
| `active` | 自动链路 poll 到 done file，`confirm=True`（明天加） | `paused` | 写 `_stage_gate`，等 `/advance` |
| `active` | stage 失败（spawn 失败/done 超时/解析错） | `failed` | planner 7 处 + graph:209；recovery 决策可换 adapter 重跑（`implementing`） |
| `active` | 服务器重启 | `paused` | `recover_orphan_stories`（graph:414）把 active→paused，不自动 resume（避免重启时狂起 CLI） |
| `paused` | **`PUT /advance`**（前端「继续执行」） | `active` | `start_story_async` resume（api:803） |
| `paused` | 1 秒后台轮询发现 done file ready | `active` | `resume_ready_interactive_stories`（graph:391）**自动 resume** ← 与确认闸潜在冲突，需 gate |
| `active`(最后stage) | done + verify gate pass | `completed` | planner:1032；写 retrospect.md |
| `completed`/`failed` | `/delete` | (删除) | api:836 |

### 异常/人工干预转移

| 当前状态 | 触发 | → 新状态 | 代码 |
|---|---|---|---|
| 任意 | `PUT /fail` | `blocked` | api:831 |
| 任意 active/planning | `POST /abort` | `aborted` | story_service:276 |
| 任意 | `PUT /skip/{stage}` | `active` + `start_story_async` | api:818（**注意：skip 后会重新启动链路**） |
| `failed` | recovery 判 `retry_new_adapter` | `implementing` → `active` | graph:251，换 adapter 重跑 |
| 父子 story | 子任务完成 | 父 `paused`→`active` | story_service:297,303,316 |

---

## 5 个驱动入口（核心问题：无唯一调度者）

| 入口 | 端点 | 改 status？ | 起自动链路？ | 问题 |
|---|---|---|---|---|
| 确认执行 | `POST /plan/confirm` | ✅→active | ✅ | **正道** |
| 启动终端 | `POST /sessions/spawn` | ❌ | ❌ | **旁路**，用户以为开始了其实没（bug 根因） |
| 继续/推进 | `PUT /advance` | ✅→active(paused时) | ✅ | resume 用 |
| 跳过阶段 | `PUT /skip/{stage}` | ✅→active | ✅ | 会重启链路 |
| **后台轮询** | 1 秒一次 | ✅→active | ✅ | **自动 resume**，与确认闸冲突 |

**5 个入口互相不知道对方在干嘛**。任何一个都可能在你不知道的时候改了 status、起了进程。

---

## 状态真相源（4 处，常不一致）

要回答"这 story 现在到底在干嘛"，得**同时**看：

1. **DB `status`**（9 个值）— 表面状态
2. **DB `context_json._xxx`**：
   - `_plan_confirmed` — 规划确认了吗
   - `_agent_actions` — 计划的 stage 列表
   - `_active_execution` — 当前在跑哪个 stage（仅自动链路写）
   - 明天加：`_completed_stages`、`_stage_gate`
3. **文件系统 done file**（`.story/done/<key>/<stage>.json`）— **stage 真做完了吗的真相**，但 DB 无字段直接反映
4. **内存**：`_running_stories`（进程内）、`driver_claim`（跨进程 DB CAS）、`_ptys`（PTY 注册表）

**今天的 bug 就是四者打架的活例**：DB 说 planning，done file 说 design 完成，PTY 说有 claude 在跑。

---

## 灰区 / 矛盾 / 死代码（清理候选）

### 矛盾（会导致 bug）
- `planning` 状态下两个并列入口（`/plan/confirm` 走链路 vs `/sessions/spawn` 旁路）语义打架。→ 明天 plan 解决（去掉 spawn 主按钮）。
- `paused` 既被"确认闸"用，又被"服务器重启孤儿恢复"用，还被"父子任务"用。三种 paused 语义不同，但 status 字段无区分。→ 确认闸用 `_stage_gate` 标记区分（明天 plan 的 R2）。
- 1 秒后台轮询会**自动 resume** 任何 done-file-ready 的 active story，与"确认闸要等人"直接冲突。→ 明天实现时必须在轮询处 gate（带 `_stage_gate` 的不自动 resume）。

### 死配置（解析了从不读）
- `StageConfig.confirm` / `review`（profile_loader:25,138）— grep 确认执行路径零读取。明天 plan 激活 `confirm`。
- profile 的 `adversarial:` 块（minimal.yaml:56）— 解析进 `ResolvedProfile.adversarial`，从不读。对抗循环是死代码（evaluator_loop.py:1-8 声明）。

### 冗余入口（可收敛）
- `POST /api/story/{key}/start`（api:2707 `api_start_story`）和 `POST /plan/confirm`（api:2910）功能重叠（都 start_story_async）。
- `POST /pty/{id}/spawn`（api:371 旧单会话）和 `POST /sessions/spawn`（api:348 多会话）重叠。
- `story_service.py` 是 `api.py` 的薄封装，状态改写分散两处。

### 化石
- `nodes/` 子包（"thin facade 保 nodes.xxx 调用兼容"）
- `idle`/`archived` status 值在 DB 出现但代码路径不明

---

## 找回掌控的第一步建议（零风险）

**不要大重构。** 先用这张表做 3 件事：

1. **删死代码**：`confirm`/`review` 要么激活要么删（明天 plan 激活 confirm）；对抗循环配置要么接线要么删；`nodes/` facade 评估能否清。
2. **收敛入口**：`/sessions/spawn` 弱化成 debug；`/start` 和 `/plan/confirm` 二选一；明确"只有自动链路能改 status"。
3. **单一真相源**：考虑给 `status` 加一个**派生的 stage 进度字段**（从 done file + `_completed_stages` 算），让前端不用同时猜 4 个地方。

---

## 附：继续编排改造时，盯住这张表的哪几格

明天的确认闸 plan（`PLAN-stage-confirm-gate.md`）改动落点，映射到本表：

| 改动 | 影响的状态转移 |
|---|---|
| 确认闸 | 新增 `active --(done,confirm=T)--> paused` 转移 |
| `/advance` 清 `_stage_gate` | `paused --/advance--> active` 转移细化 |
| resume 跳过已完成 | `_completed_stages` 成为第 5 个 context_json 真相源字段 |
| PTY clean-exit+kill | 消除"进程堆积"副作用 |
| 后台轮询 gate | 修"1 秒轮询自动 resume 与确认闸冲突"这个矛盾 |

实施时**每改一个转移，回来更新这张表**，保证表和代码同步 —— 表就是你的掌控仪表盘。

---

## 附：目标分层架构（北极星，2026-07-08 联网查证后落盘）

> 查证来源：Praetorian 五层架构、Microsoft Conductor、AWS Step Functions、Temporal+YAML、Mario Hayashi spec-driven FSM。
> 结论：当前的直觉（Story 状态 / 驱动层 / PTY 层解耦，驱动层可配置）**是业界验证过的主流模式**，方向正确。

### 当前问题（"厚协调器干了执行的活"）

`continue_orchestrator_agent`（planner.py:530-995）这一个函数同时跨 4 层：改 Story status、推进 stage、spawn/kill PTY、poll done file、起 supervisor 线程。Praetorian 的结论：**协调器越界干执行的活，是规模化失败的头号原因**。正确模式是 Orchestrator-Worker 权限互斥——协调器不干活，执行器不协调。

### 目标分层（终局，朝此方向靠）

```
配置层（profile yaml）— 单一真相源：
  定义阶段序列、转移规则、确认闸、重试上限
  现状：yaml 在这，但 confirm/review/adversarial 是死配置（解析了从不读）
  目标：代码不硬编码阶段逻辑，全从 yaml 读（像 AWS Step Functions / Conductor）

驱动层（StageDriver / 状态机）— 厚、确定：
  读配置 → 按阶段跑 → 推进状态机
  只管编排，不碰 PTY 细节
  状态机是单一真相源：设计done→开发→开发done→测试→测试done→上线→上线done→完成

PTY 层（PtyManager）— 薄、无状态：
  单进程生命周期：spawn / alive / kill / resume
  不知道 Story，不知道阶段，给命令就跑
  对应 Praetorian 的 "Thin Worker"：<150 行，零跨阶段状态

Story 业务状态（开发/测试/上线）— 派生视图，不是独立维护：
  从驱动层进度【派生】，不另设字段
  避免重蹈"多处真相源"老坑
```

### 一个关键修正（相对原始直觉）

直觉是把"Story 业务状态"和"驱动阶段"当两个独立状态机。Praetorian/AWS 的做法是 **一个 workflow 状态机**，业务状态是其高层抽象视图（派生），非独立状态机。原因：两个独立状态机又得互相同步 → 回到"4 处真相源"老坑。**一个真相源（驱动阶段进度）→ 派生业务状态**，更干净。

### 明天确认闸改造，如何朝北极星靠

| 改动 | 朝北极星的一步 |
|---|---|
| 激活 `StageConfig.confirm`（死配置→活） | 配置层开始被真正消费，不再是装饰 |
| `_completed_stages`（驱动层进度） | 单一真相源雏形，业务状态可从它派生 |
| PTY clean-exit+kill（done 后回收） | PTY 生命周期归还 PtyManager，驱动层不攥着 |
| 确认闸（stage 间 paused） | 驱动层状态机显式化，业务状态（开发done→测试）有处可落 |
| 去掉孤立 `/sessions/spawn` 主入口 | 收敛到"唯一调度者"，不再 5 入口打架 |

**北极星不在明天 scope**（完整分层重构是把 planner.py 拆成 StageDriver / PtyManager / 派生状态三块，是更大的事）。但明天每一步都应朝它靠，而不是反向堆积。判据：每加一个 `_xxx` 到 context_json，问"这属于驱动层进度，还是 PTY 层状态，还是派生视图？"——放对位置，别都塞 context_json。
