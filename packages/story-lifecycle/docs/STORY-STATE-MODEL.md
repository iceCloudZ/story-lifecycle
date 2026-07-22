# Story 状态模型（权威设计，订正版）

> 订正 `STATE-CONSOLIDATION.md` 的"派生"错误。本文是状态建模的**新地基**。
> 创建：2026-07-09。
> 范围：`packages/story-lifecycle`。

---

## 0. 为什么有这份文档

`STATE-CONSOLIDATION.md` 原主张"Story 业务状态（开发/测试/上线）从 `_completed_stages` 派生，不另存"。**这个主张是错的。** 它让 Story 业务状态依附于阶段执行进度，等于让 workflow engine 拥有业务状态——这恰恰是业界（Temporal 社区）**明确否决**的方案。

本文订正为：**Story 状态是独立的第一公民，不从阶段派生；驱动阶段是 Story 状态的展开（Execute），服务于 Story 状态。** 三个独立来源（Temporal、Jira/Atlassian、Process Manager vs Saga）印证这是业界推荐的正道。

旧文档 STATE-CONSOLIDATION.md 顶部已加订正指引，以本文为准。

---

## 1. 业界验证（三个独立来源指向同一模型）

### Temporal：状态机实体 vs 标准 workflow（最直接命中）

[Temporal 社区：状态机 vs 标准 workflow](https://community.temporal.io/t/workflow-maintainability-abstract-into-a-state-machine-vs-standard-workflow-function/7220) 明确**推荐**：

> 业务状态（created/paid/fulfilled）建模成**独立的状态机实体**。一个**通用 workflow 循环**调当前状态的 `Execute` 函数，再转移。**workflow engine 不拥有业务状态。状态机实体拥有它。workflow 只是通用 driver。**

被**否决**的替代方案："把步骤硬编码进单个 workflow 函数"——理由是"业务逻辑泄漏进编排层，加/改一个状态要改 workflow 代码"。

**这正好描述了现在 `continue_orchestrator_agent`（planner.py:530-995）的病**：它把 Story 业务状态（开发/测试）和阶段执行（design/build）焊在同一个函数里，业务语义泄漏进编排代码。

### Jira/Atlassian：业务生命周期默认独立于执行状态

Jira 的 parent issue **业务生命周期默认独立于** sub-task 执行状态。两者只在**显式 wiring** 时联动（"当所有子任务 Done 时自动关父"是可选 automation，不是默认绑定）。印证"阶段 done 是必要条件，但 Story 状态转移是独立的闸"。

参考：[HeroCoders: Status, Subtask or Checklist](https://www.herocoders.com/blog/status-subtask-or-checklist-how-to-divide-work-in-jira)、[Atlassian: parent status automation](https://support.atlassian.com/automation/kb/automation-rule-or-update-the-parent-status-to-done-only-when-all-subtasks/)

### Process Manager vs Saga：有状态 vs 无状态

[Stack Overflow](https://stackoverflow.com/questions/15528015/what-is-the-difference-between-a-saga-a-process-manager-and-a-document-based-ap)：**Process Manager = 有状态的状态机**（持有业务状态，决定下一步），**Saga = 无状态**（只按消息路由）。

你的 Story 应该是 **Process Manager**（持有状态），不是 Saga（无状态路由）。**现在代码的 Story 是无状态的**——它的"状态"全是从阶段投影的，所以你感觉"丢了 story 的状态"。它真的从来没被建模过。

---

## 2. 正确的三层模型

```
┌─────────────────────────────────────┐    ┌──────────────────────────────┐
│ Story 状态机实体 (第一公民)           │    │ 通用 driver                   │
│                                     │    │                              │
│ [待启动] → 开发 ──→ 测试 ──→ 上线 ──→ 结项 │←──│ continue_orchestrator_agent   │
│ (独立持久化, 不依附阶段)              │    │ loop {                        │
│                                     │    │   跑当前状态定义的阶段(Execute)│
│ 每个状态:                            │    │   问状态机: 能否转移?          │
│   - execute = 要跑的阶段序列          │    │   if 转移 → 换到新状态         │
│   - transition.confirm = 转移闸       │    │ }                             │
│     (人工 UI 推进 OR 配置项)          │    │ driver 不持业务语义, 只驱动    │
│                                     │    │ 持有的只是执行进度(_completed) │
└─────────────────────────────────────┘    └──────────────────────────────┘

> **「待启动」是规划前的前态**:新 story 默认 `lifecycle_state='待启动'`(DB DEFAULT)。
> 它**不在 `story_states` 拓扑里**(没有 stages/next/confirm),因为它还没进状态机。
> `/plan/confirm` 是「待启动→开发」的唯一推进点(详见 `TABS-LIFECYCLE-STATE.md`)。
                                                     │
                                                     │ 起进程/读done
                                                     ▼
                                           ┌──────────────────┐
                                           │ PTY 层 (无状态)   │
                                           │ spawn/alive/kill │
                                           │ 不知 Story/阶段   │
                                           └──────────────────┘
```

### 映射到 Temporal 推荐模式

| Temporal 推荐 | 本模型 | 关系 |
|---|---|---|
| state machine entity (created/paid/fulfilled) | Story 状态机（开发/测试/上线） | **业务状态，独立持久化，第一公民** |
| state's Execute function | 驱动阶段（Story 在"开发"时跑 design/build） | 阶段是当前状态的**展开/执行** |
| generic workflow (loop: Execute + transition) | continue_orchestrator_agent | driver 只做"跑阶段 + 问状态机能否转移"，不持业务语义 |
| business state separate from workflow execution state | Story 状态 vs 阶段进度 | **显式解耦，不互相派生** |

---

## 3. Story 状态 ≠ 阶段状态（核心区分）

这是之前所有混乱的根因。区分清楚：

| | Story 状态 | 阶段状态 |
|---|---|---|
| 例子 | 开发 / 测试 / 上线 / 结项 | design pending / running / done |
| 归属 | **Story 自己（第一公民）** | 驱动层（Story 的工具） |
| 数量 | 固定几个（业务定义，与阶段数无关） | = 阶段数 × 5 态 |
| 转移依据 | 业务规则（阶段 done 必要 + 确认闸） | PTY done file |
| 谁改 | Story 状态机（经确认闸裁决） | 驱动层（poll done） |
| 持久化 | `story.lifecycle_state`（**新独立字段**） | `_completed_stages`（driver 层） |

**阶段全 done ≠ Story 自动进下一状态。** Story 状态机有自己的闸：可能等人 UI 点推进，可能查配置项，可能要额外业务条件。阶段只是"把活干完"，干完是 Story 状态转移的**材料之一（必要条件）**，不是**决定权**。

---

## 4. 11 个阶段怎么建模（最终答案）

阶段数可变（现在 profile 有 2/3/4 个，未来可能 11 个），但**归到固定的几个 Story 状态里**。每个 Story 状态"拥有"自己的阶段子集：

```yaml
# Story 状态机定义 (第一公民, 业务定义, 固定几个 — 不随阶段数变)
story_states:
  开发:
    execute: [intake, design, plan, build]          # 这个状态下跑的阶段 (Temporal 的 Execute)
    transition:
      to: 测试
      when: all_stages_done                          # 必要条件: 本状态阶段全 done
      confirm: {type: ui_button, label: "完成开发，进入测试"}   # 充分条件: 人工 OR 配置
  测试:
    execute: [test, review]
    transition:
      to: 上线
      when: all_stages_done
      confirm: {type: config, key: auto_advance_test}   # 配置项驱动, 无人值守
  上线:
    execute: [staging, deploy, smoke]
    transition:
      to: 结项
      when: all_stages_done
      confirm: {type: ui_button}
  结项:
    execute: [retro]
    transition: {}                                     # 终态

# 11 个阶段是配置 (Story 状态的展开), 不是状态本身
# driver 层阶段状态固定 5 态 (pending/running/awaiting/done/skipped)
# Story 状态 (待启动/开发/测试/上线/结项) 独立存, 不从阶段派生
# 「待启动」= 规划前前态(DB DEFAULT), 不在 story_states 拓扑里;
#   /plan/confirm 推进到「开发」后才进状态机。
```

### 真相源分离（各是各的第一公民，不互相派生）

```
Story 状态    → story.lifecycle_state (独立字段)
                待启动 / 开发 / 测试 / 上线 / 结项
                「待启动」是前态; 后四态转移由 Story 状态机裁决 (阶段done + 确认闸)

阶段进度      → context_json._completed_stages (driver 层)
                ["intake", "design"]
                服务于"当前 Story 状态", 不定义 Story 状态

PTY           → 内存 _ptys
                不知 Story, 不知阶段
```

**不再互相派生。** driver 跑完当前 Story 状态的所有阶段后，**问 Story 状态机**"要不要转移"，由 Story 状态机按它的 confirm 规则裁决。driver 无权直接改 Story 状态。

### 判据（每加一个字段问自己）

| 字段属于 | 放哪 | 例子 |
|---|---|---|
| Story 业务状态 | `story.lifecycle_state`（独立） | 开发/测试/上线 |
| 阶段执行进度 | `_completed_stages`（driver） | design done |
| 派生视图 | **不存，读时算** | "当前在跑哪个阶段"= `_completed_stages` 后第一个 launch |

---

## 5. 两个不同位置的"确认闸"（别再混成一个）

之前 STATE-MAP 指出"paused 三义"是病。正确模型里，闸分两层，语义不同：

```
阶段间闸 (driver 层内部):
  design done → build 开始 之间
  作用: 控制阶段是否自动连跑 (StageConfig.confirm)
  例: design 做完, 停一下让人看 spec, 再开始 build
  → 这是 driver 内部细节, Story 状态不变

Story 状态间闸 (Story 状态机):
  开发 done → 测试 开始 之间
  作用: 业务状态的正式转移 (人工 UI 推进 OR 配置项)
  例: 开发阶段全 done, 但要人 UI 点"进入测试", 或配置项 auto_advance=true
  → 这是 Story 业务状态的转移, driver 要问状态机
```

**明天的确认闸 plan（PLAN-stage-confirm-gate.md）本质是"阶段间闸"**（design→build），还停留在 driver 层。但这正是通往正确模型的第一步：先把阶段间停下来，Story 状态机才有机会插入自己的裁决。完整的 Story 状态机是后续工作。

---

## 6. UI 落地（按新的 Story 状态展示）

当前 UI 按引擎 `status`（planning/active/...）+ `current_stage` 展示，**找不到业务语义**。新 UI 以 **Story 业务状态（开发/测试/上线）为主视图**，阶段进度是次要的执行细节。

### 6.1 主视图改造：StageProgress → StoryStateProgress

`OverviewTab.tsx:31-35` 写死的 design/implement/test 删除。替换为从 `story.lifecycle_state` 驱动的主进度条：

```
┌──────────────────────────────────────────────────────────┐
│  Story 业务状态 (主, 醒目)                                 │
│  ●开发 ───── ○测试 ───── ○上线 ───── ○结项                │
│  ↑当前                                                    │
│                                                           │
│  当前状态执行进度 (次, 折叠/小字)                           │
│  └ 开发: design ✓ | build ●运行中 | (2/4 阶段)            │
└──────────────────────────────────────────────────────────┘
```

- **主进度条**：Story 状态（开发/测试/上线/结项），从 `story.lifecycle_state` 读。固定 4 个节点，不随阶段数变。
- **次进度**：当前状态下的阶段执行，从 `_completed_stages` + 当前 Story 状态的 `execute` 列表算。小字、可折叠——它是执行细节，不是主信息。

### 6.2 状态转移闸的 UI（Story 状态间）

当 Story 状态的所有阶段 done，且 `transition.confirm.type == ui_button` 时，主视图显示醒目卡片：

```
┌─────────────────────────────────────────┐
│ ✅ 开发阶段全部完成                       │
│    design ✓  plan ✓  build ✓            │
│                                         │
│    [完成开发，进入测试 →]  ← ui_button   │
│    (transition.confirm.label)           │
└─────────────────────────────────────────┘
```

点击 → 调用新的 Story 状态转移 API（见 6.4），Story 状态机裁决转移 → `lifecycle_state: 开发 → 测试` → driver 开始跑测试状态的阶段。

若 `transition.confirm.type == config` 且配置为 auto：不显示按钮，driver 自动转移（无人值守场景）。

### 6.3 阶段间闸的 UI（driver 层，次要）

明天的确认闸（design→build 之间，`StageConfig.confirm`）仍保留，但它是**次要**的执行细节，显示在阶段进度行内，不抢主视图：

```
  开发: design ✓ | [build 等待确认开始] | ...
                    ↑ 小按钮, 阶段间闸, 不抢主视图
```

### 6.4 需要的新 API / 字段

| 新增 | 作用 |
|---|---|
| `story.lifecycle_state`（DB 字段） | Story 业务状态独立持久化（开发/测试/上线/结项） |
| `GET /api/story/{key}` 响应加 `lifecycleState` + `lifecycleStates`（状态列表+当前+各状态 execute 进度） | 前端主视图数据源 |
| `POST /api/story/{key}/lifecycle/advance`（新端点） | Story 状态间转移（ui_button 触发）——区别于 `/advance`（那是 driver resume） |
| profile yaml `story_states:` 段 | Story 状态机定义（execute + transition.confirm） |

### 6.5 不再展示的东西

- 引擎 `status`（planning/active/paused/failed）**降级**到诊断/高级视图，不占主位。它是引擎健康度，不是业务语义。
- 写死的 design/implement/test 进度条删除。

---

## 7. 与现有文档/plan 的关系

| 文档 | 状态 | 与本文关系 |
|---|---|---|
| **本文 STORY-STATE-MODEL.md** | **权威（新地基）** | 状态建模的真相源 |
| STATE-MAP.md | 有效（现状地图） | 现状诊断仍准确，但"北极星"章节里"业务状态派生"要按本文订正 |
| STATE-CONSOLIDATION.md | **部分订正** | "派生"主张错误，顶部已加订正指引；归一步骤里"`_completed_stages` 唯一真相源"仍有效（指 driver 层内部） |
| STATE-DIAGRAMS.md | 部分订正 | 图 4（归一目标）"业务状态从 _completed_stages 派生"错误；图 5（三层）方向对但缺 Story 状态层 |
| PLAN-stage-confirm-gate.md | 有效（明天施工） | 阶段间闸是通往本模型的第一步，scope 不变 |

---

## 8. 实施路线（Story 状态机是中期，阶段闸是近期）

| 阶段 | 内容 | 时机 |
|---|---|---|
| 近期（明天 plan） | 阶段间确认闸（StageConfig.confirm）+ PTY 回收 + resume 跳过 | 已规划，PLAN-stage-confirm-gate.md |
| 短期 | `story.lifecycle_state` 字段 + 简单状态机（开发/测试/上线）+ UI 主视图改造 | 阶段闸之后 |
| 中期 | profile `story_states:` 完整定义 + 状态转移 API + UI 闸卡片 | 短期验证后 |
| 长期 | driver 重构成纯 loop（Execute + transition），剥离业务语义 | 中期成熟后 |

**明天的阶段闸不改 Story 状态机**（那个是后续），但它让"阶段间能停下来"——这是 Story 状态机插入裁决的前提。先把闸做出来，再往上盖 Story 状态层。
