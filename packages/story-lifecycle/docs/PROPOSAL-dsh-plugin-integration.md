# PROPOSAL: story-lifecycle 作为 deepseek-harness 插件集成

> ## ⛔ 决策:不采用(为本项目 4 目标)
> **结论(2026-08-14)**:不为 story-lifecycle 的 4 个初衷(AI 开发/成果物/上线流程 · 反复迭代+resume · 多 story 并行+worktree 隔离 · 挖知识反哺)迁移到 dsh。理由:这 4 项**全是 story-lifecycle 自有层**,dsh(agent loop 框架)一项都不加分;用户最初感到的"不智能/被动",根因是**没有每日主动跑 TAPD 同步 + 跟日程挂钩**(不是 loop 被动),已由 in-place 的 `PLAN-proactive-cadence.md`(已落地 `story daily`)解决,不用换框架。
> **保留本文作决策记录**:§0.2「护城河 vs 日用品」拆分仍有概念价值(判断"该 ride 哪个平台"的通用框架);§5 概念映射表说明"技术上能做"。下次再起"dsh 会不会更好"的念头时,**先读 §0.2 再论**——大概率结论不变。

> **状态**:已决策——不采用(见上方决策块)
> **日期**:2026-08-14
> **作者意图**:把本项目的核心能力(story/stage 编排 + LLM judge + 知识飞轮)做成 [deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)(下称 **dsh**)的原生插件,**ride dsh 的平台迭代,而非自建全套**。
> **本文档自包含**——评审者无需阅读此前的对话,所有事实都带证据来源(§附录)。标 **【已核实】** 的是读了 dsh 源文档确认的;标 **【待核实】** 的是推断或尚未读到原文的,请重点挑这部分。
> **阅读顺序**:先读 §0(战略前提,决定"该不该 ride"),再读 §1(评审问题),§2 以后才是"怎么做"。

---

## 0. 战略前提:ride dsh,还是继续自建?

### 0.1 核心论点:这是"持续性成本",不是"一次性成本"

loop / 感知 / 流式 / tool 执行 / UI 壳这一层,是 agent 平台每年都在飞快演进的**日用品**。story-lifecycle 现在手搓了一套(OrchestratorThread 5s 轮询 + PTY tap + declare 自报 + detect_stuck 二值检测),这正是用户抱怨"agent 被动"的根——**手搓了平台该提供的东西**。

正确的成本对比不是"一次性迁移成本 vs 一次性修补成本",而是:

> **一次性迁移成本 vs 永续的跑步机成本。**

每出一个新 CLI 行为 / 新模型 / 新边界情况,自建方手动补一次;dsh 有团队 + 社区 + DeepSeek 在推(高关注度、快迭代),补一次所有人受益。**时间拉长,跑步机成本必赢。** ride 的本质:把日用品层上交给一个会持续演进的平台,自己只留没人替你做的那层。

### 0.2 ride 成立的决定性条件:护城河与日用品几乎不重叠

只有当"自己独特价值"和"平台提供的东西"重叠小时,ride 才划算。本项目满足:

| | 提供方 | dsh 会不会也做 |
|---|---|---|
| **护城河(keep)** | | |
| story/stage 工作流语义(design→test、task_actions 约束、handoff/seed_context、lifecycle) | story-lifecycle | dsh 只有 `ctx.goals`/`ctx.workflowEngine`,**没有"编码任务穿多阶段 + artifact gate + LLM judge"这套** |
| 知识飞轮(miner→knowledge schema→context_providers,跨项目跨 session) | story-lifecycle | dsh 有 session-query(查自己 session),**没有跨项目挖掘的 playbook/failure** |
| stage 边界 LLM judge(对 PRD 判累积产出 quality/lifecycle_target) | story-lifecycle | dsh 有 turn-stopping hook,**没有这个具体判定** |
| **日用品(offload 给 dsh)** | | |
| agent loop / step / turn | 手搓 → dsh | dsh 原生,持续演进 |
| 感知(完成/卡住/健康) | 手搓 → dsh | dsh 原生事件流 |
| tool 执行管线 / 流式 / context 压缩 | 无/手搓 → dsh | dsh 原生 |
| UI 壳(终端/chat/tool 渲染) | 手搓 → dsh `ui-*` slot | dsh 原生 |

**重叠近乎为零**——这是"建在平台上"最理想的形态。本项目专注没人替你做的语义 + 飞轮,日用品全部上交。

### 0.3 ride 的正确姿势:最小化自建,最大化上交

不是"全量 TS 重写",而是**紧贴 dsh 事件系统的部分薄、平台无关的部分厚**:

- **必须紧贴事件系统的**(story/stage 状态、judge 挂 waterfall、知识注入挂 `system-prompt/assemble`)→ 写成**薄 TS 原生 dsh 插件**(seam provider + waterfall listener)。薄,是为了 dsh 改 API 时只动胶水。
- **框架无关的数据基建**(miner 转录→SQLite、knowledge schema、context_providers 查询)→ **保留 Python**,经 `sdk/` 当 provider 喂给 dsh。飞轮平台无关,不该绑死 dsh。

→ 这落到 §5 的"混合形态",在 ride 论点下**它是正解,不是折中**。

### 0.4 ride 的税,与退出逻辑

1. **churn 税**:dsh 是 developer preview,明说破坏性变更,上游更新会打断你。**缓解**:护城河保持干净解耦,胶水层薄。
2. **schema 耦合**:miner 要消费 dsh `session/event` 流(比刮 PTY 富,但绑它的 schema)。
3. **平台/路线图风险**:dsh 路线不由本项目定。DeepSeek 是真公司(存活风险低),但 roadmap 对不齐是可能的。

**退出逻辑(最关键)**:把护城河做成**平台无关**——飞轮 + story 语义不依赖 dsh 内部细节。万一 dsh 停滞/转向,story-lifecycle 的价值活着,能换下一个平台。一句话:**ride dsh,但别把灵魂焊死在 dsh 上。**

### 0.5 诚实的保留

"高关注度 + 快迭代"是现在时态,现在热 ≠ 一定成。但只要护城河干净,ride 的下行有限(最多浪费胶水功夫),上行是省掉整条跑步机。**风险/收益不对称地偏向 ride**——前提是 §0.4 的解耦做对。

---

## 1. 给评审者的问题清单

请围绕以下几点给判断:

1. **【战略层,先评这条】ride dsh 的判断是否成立?** §0.2 的护城河/日用品拆分是否真的不重叠?持续性跑步机成本是否高过一次性迁移成本?有没有被低估的"自建反而更好"的理由?
2. **形态选择**:§5 的"混合形态"(薄 TS 事件耦合 + Python 飞轮经 provider)是否自洽?有没有更好的切法?
3. **映射健壮性**:§4 的"story-lifecycle 概念 → dsh seam"映射表有没有错配?尤其 LLM judge 挂 `agent/turn-stopping`、知识注入挂 `system-prompt/assemble` 这两处,是否符合 dsh waterfall 语义?
4. **重定位代价**:§5.1 列出"外部驱动机制(PTY tap / declare / OrchestratorThread / BaseAdapter)插件化后变冗余"——这个判断是否成立?有没有被低估的隐性依赖?
5. **flywheel 论文关系**:本项目 `REFACTOR-orchestrator-three-layer-positioning.md` §2.1 论断"编排器价值在『信息差』不在『能力差』"。ride 到 dsh 后,dsh 吃掉能力差(loop),本项目只剩信息差——是被强化还是被架空?
6. **UI 路线**:§6 选 UI-A(贡献成 dsh `ui-*` slot 插件)。但 dsh 是 preview、slot 契约会 churn。这个风险是否高到该走 UI-B(保留自有 frontend + dsh headless)?
7. **待核实项**(§7):dsh 的 slot 系统契约、`subagent/` 是否真能委派 claude/codex 做 coding、miner 如何消费 dsh `session/event` 流——这三项没读到原文,请评估可行性与风险。

---

## 2. 背景:两个系统各是什么

### 2.1 story-lifecycle(本项目)

Python 编排器,把 AI coding agent 跑过 story 工作流(design → implement → test)。单仓 `story-lifecycle`,工作区 `D:/github/story-lifecycle`,四个包:

| 包 | 角色 |
|---|---|
| `story-lifecycle` | 核心编排器:驱动 AI coding agent 走 story 工作流(FC 驱动,Python) |
| `story-miner` | 把 coding-agent 转录归一进 SQLite,挖行为/失败/成本知识 |
| `knowledge` | 统一知识 schema(scenario/playbook/failure),被上下两个包消费 |
| `testing` | 真实 AI E2E 测试 harness |

**飞轮**:`story-miner` 挖经验 → `knowledge` 定义共享 schema → `story-lifecycle` 经由 `knowledge/context_providers/` 消费。

**关键架构事实**(来源:本仓 `AGENTS.md` + 代码勘察):

- **外部驱动模型**:编排器 spawn 外部 CLI(claude/codex/kimi/opencode),从外面盯。agent 是黑盒。
- **spawn 契约**:`BaseAdapter.start_session(...) -> SessionSpec`(`command` + `pty_prompt` + `readiness_marker`),spawner 不按 adapter 类型分支。
- **完成判定**:100% 依赖 agent 自报 `story tool declare`(`executors.py` `is_artifacts_ready` 只查 DB 的 `artifact_declared` 事件,不读文件)。这是 **1068018 事故**后的安全纪律(agent 边写边存,读文件会撞半成品 → 误 reject)。
- **调度**:全局编排线程 `OrchestratorThread`(`orchestrator/scheduler.py`),**固定 5s 轮询**,无 backoff。
- **LLM 判定层**(两处,均"边界纯判定"):
  - `stage_completion.judge_stage_completion`:artifact landed 后被唤起,**无 tool**,上下文经 `assemble_judge_context` 预注入。一次 LLM 出 `quality`/`lifecycle_target`/`summary`。
  - `stuck_diagnose`:`detect_stuck`(二值规则:300s 无输出 / 连续 5 条 error)命中后,先 summary 纯判定,例外才 agentic(只 `read_file`,≤5 次)。
- **frontend**:React 19 + TS + Vite + Zustand + TanStack Query + react-router;`.ui-*` 原语 + design tokens;xterm.js 做 PTY 查看器。

**本项目自述的"被动性"痛点**(用户原始抱怨,经代码核实):stage 完成判定完全被动(等 declare);质量判定只在 stage 边界,**进行中编排器无任何 LLM 介入**;健康观察是二值(卡/没卡),无进度估计/漂移检测;无"持续观察者"角色(`PtyLogger` 持续落 events.jsonl,但没人 tail)。**这正是 §0.1 所说的"手搓了平台该提供的东西"。**

### 2.2 deepseek-harness(dsh)

DeepSeek 开源的 AI agent 框架,Cordis 基础,**"Everything is a Plugin"**。TS/Node。【已核实:dsh README + `docs/architecture.md` + `docs/capability-seams.md` + `docs/agent-lifecycle.md` + `docs/web-styling.md` + `docs/config-catalog.md` + `packages/client/README.md` + `packages/` 目录】

> ⚠️ dsh 现为 **developer preview**,README 明说"THERE WILL BE COMPATIBILITY-BREAKING CHANGES"。

**模型/provider 支持【已核实,非 DeepSeek 锁定】**:
- `llm/` 包 = "the abstract service + provider adapters",**provider-agnostic**(seam 模式)。
- 具体三类 provider 插件:`dsh-llm-deepseek`(默认吃 `DEEPSEEK_API_KEY`/`DEEPSEEK_BASE_URL`)、**`dsh-llm-pi-ai`(OpenAI 兼容,BYOK:`apiKeyEnv`+`baseURL` 每路由独立)**、`dsh-llm-replay`(回放/测试)。
- **Claude Code / Codex 的 subagent 桥**:`dsh-subagent-claude-code`、`dsh-subagent-codex`——能把实际 coding 委派给 Claude/Codex。
- 默认 model catalog 是 DeepSeek(V4 Flash/Pro),**但非强制**。→ "用最好的前沿 agent"这个立身之本不丢。

**架构核心**:
- 运行中的 dsh = "a plugin tree composed at boot from ordered layers"(profile/bundle)。
- "every part of the product is a plugin... including the agent loop itself, so every part is replaceable"。`core/agent` 拥有 `Agent` 接口;`core/agent-loop` 默认实现。**step = 一次模型请求 + 它调的 tool;turn = 零或多个 step**。

**扩展模型(seam)**:一个 capability 三角色——**Service Definition**(owner,挂 `ctx.<key>`) / **Service Provider**(后端,可多个可换) / **Consumer**(通常是 model-facing tool,只依赖 Definition 不依赖具体 provider)。原文:"Extension plugins depend on Service Definitions, never concrete providers."

**单 turn 事件序(waterfall,监听者须调 `next()` 委托)**【已核实:agent-lifecycle.md】:

```
turn/start → agent/inbox/claimed(每条消息)
  → step/start → user/message
  → system-prompt/assemble → agent/request → llm/stream → assistant/chunk* → assistant/message
  → tool/call → tool/result(barriers + bounded rolling pool)
  → step/end → agent/turn-stopping(终态检查点)→ turn/end
另:agent/pre-step(权威 steering,返回 authoritative)、agent/request-error(返回 retry 或保留原错)
```

**其他 `ctx` 服务**:`ctx.goals`(同 session 目标)、`ctx.jobs`(后台工作)、`ctx.workflowEngine`(每 context 一个)。**状态**:durable replay 落 `session/event`;live control 落 `agent/*`。
**headless/embedded**:`sdk/`(JSON-RPC out-of-process)、`acp/`(自动化协议)可不带 web UI 跑。

---

## 3. 核心判断:能不能做成 dsh 插件

**结论:技术上能,且本项目核心价值(story/stage 模型 + judge + 知识飞轮)在 dsh 里反而落得更干净。** 代价是大半个"外部驱动机制"变冗余(§5.1)。

理由:dsh 的扩展模型(seam + waterfall)与本项目概念契合度很高——几乎每个 story-lifecycle 概念都能在 dsh 找到对等落点,而"waterfall 事件序"恰好把本项目现在靠"5s 轮询 + side-channel"硬凑出来的感知变成一等公民。结合 §0 的战略前提:**不只是"能做",而是"该 ride"。**

---

## 4. 概念映射表(判断"能做"的硬依据)

| story-lifecycle 现有概念 | dsh 里的落点 | 命运 |
|---|---|---|
| story + stage 状态机 | 新建 `ctx.story` Service Definition + 复用 `ctx.goals` | ✅ 原生落地 |
| stage 推进 / advance | `agent/turn-stopping` waterfall(authoritative,判 stage 该不该结束)或 `agent/pre-step`(steer 下一个 stage) | ✅ 原生落地 |
| LLM judge(stage 边界) | `agent/turn-stopping` 权威检查点 / 或独立 judge tool | ✅ 落得更干净 |
| 知识注入(`context_providers`) | `system-prompt/assemble` waterfall(把 playbook/failure 塞 prompt) | ✅ 这正是 waterfall 用途 |
| miner(转录→SQLite) | 消费 dsh `session/event` 流(**比刮 PTY 富一个量级**) | ✅ 飞轮反而变好 |
| 知识库查询 | `ctx.knowledge` Service Definition,provider 读 SQLite | ✅ 原生落地 |
| artifact 完成判定 | `tool/result` post-hook + goal 检查 | ✅ 原生落地 |
| **PTY tap / declare 事件** | dsh 原生 `tool/call`→`tool/result` 事件 | ❌ **淘汰** |
| **OrchestratorThread 5s 轮询** | dsh 事件驱动(`ctx.on`/waterfall) | ❌ **淘汰** |
| **BaseAdapter / SessionSpec** | dsh 自己就是 loop;仅驱动非 dsh agent 时(经 `subagent/`)才相关 | ❌ **大幅收缩** |

**§2.1 列的"被动性痛点"在 dsh 里直接消失**:dsh 原生给每个 turn 的完整事件序,想在哪个点插感知/判定/纠偏都行(全是 waterfall);不需要 PTY 刮日志、不需要 5s 轮询、不依赖 agent 自报 declare。

---

## 5. 选定形态:ride 混合形态(薄 TS 事件耦合 + Python 飞轮)

按 §0.3,ride 的正确姿势是"紧贴事件系统的薄 + 平台无关的厚"。先把三种候选形态摊开:

| 形态 | 做法 | 对齐 dsh | 成本 | 代价 |
|---|---|---|---|---|
| 形态1(全 TS) | 用 Cordis idiom 全量重写(含飞轮) | 最高 | 最高 | 丢 Python 生态(miner 转录→SQLite、knowledge raw SQL) |
| 形态2(全薄桥) | Python 全保留,经 `sdk/`(JSON-RPC)暴露成 provider,dsh 侧只写 shim | 中 | 低 | 编排逻辑跨语言边界(事件 waterfall 也走 RPC,抵消 dsh 事件红利) |
| 形态3(反向 adapter) | 加 `DshAdapter` 把 dsh 当后端接进现有编排器 | 低 | 低 | 把 dsh 当哑 CLI 用 PTY 盯,**丢掉事件 loop,被动性不解决**——不推荐 |

**选定:形态1 + 形态2 的混合**(ride 形态):
- **编排逻辑(必须紧贴 dsh 事件系统)** → TS 原生 dsh 插件:`ctx.story` 状态机、judge 挂 `agent/turn-stopping`、知识注入挂 `system-prompt/assemble`、artifact 判定挂 `tool/result` post-hook。**吃满 dsh 事件红利,且层薄。**
- **飞轮(miner / knowledge,框架无关数据基建)** → 保留 Python,经 `sdk/` 当 `ctx.knowledge` provider 喂给 dsh。不绑 dsh 内部,符合 §0.4 退出逻辑。

> 在 ride 论点下,这个混合**不是折中,是正解**:事件红利最大化 + 飞轮平台无关。它取代了早期"形态1 全量重写"的设想。

### 5.1 重定位的代价:哪些现有机制会变冗余

插件化不是"加个插件",是**重新定位项目身份**:从"外部驱动前沿 coding agent 的编排器"变成"dsh loop 之上的 story/judge/知识层"。变冗余的现有机制:

- **PTY tap / `awaiting_detector` / declare 事件完成判定**:dsh 有原生 tool 事件。
- **`OrchestratorThread` 5s 轮询调度**:dsh 事件驱动。
- **`BaseAdapter` / `SessionSpec` / session-id 三模型**(prespecified / output-driven / file-scan):仅当仍要驱动非 dsh agent 时(经 `subagent/`)才部分保留。
- 本仓 `AGENTS.md` 里那一大段 adapter/session-id/artifact-driven 的设计契约,**大部分失效**。

> 请评审重点挑:这些"变冗余"有没有低估的隐性依赖?注意区分**调度机制**(该淘汰)与**业务语义**(该保留进 `ctx.story`)——如 `worktrees_root`(per-story 工作区)、`seed_context`(接手中途需求)、`task_actions` 约束(stage 语义),这些是语义不是调度,在 dsh 里作为 `ctx.story` 内容保留,不算冗余。

---

## 6. UI 怎么弄

**好消息:本项目 frontend 已是 React 19 + TS + Vite**,所以 UI 不是框架重写,是"重新装裱"。

### 6.1 栈对照

| 维度 | 本项目现在 | dsh web 【已核实:web-styling.md + client/README】 |
|---|---|---|
| 框架 | React 19 + TS + Vite | React + CSS Modules + clsx |
| 样式 | `.ui-*` 原语 + design tokens | `--dsw-*` 静态层 + `--dsw-alias-*` 语义层(**禁组件库/禁 Tailwind**) |
| 状态 | Zustand + TanStack Query | dsh object services / slot 状态 |
| 数据层 | WebSocket `/ws/story` `/ws/pty` `/ws/stories` + React Query | `connection/` 包(browser-host RPC + event delivery) |
| 终端 | xterm.js(PTY 查看器) | tool 事件流;可复用 xterm 渲染 |
| 路由 | react-router 顶层路由 | **slot 系统**(`ui-*` 包注册进 shell) |

两套都"token 驱动 + 约定式原语"——同一种设计哲学,移植本质是"换 token 命名空间 + 路由换 slot 注册"。

### 6.2 dsh 的 UI 模型

- **slot 系统**(`client/` 包拥有):"Defines how UI features register and compose extension slots"。贡献 UI = 写一个 `ui-*` 包(如 `ui-story-lifecycle`),把组件注册进具名 slot,即出现在 dsh web shell(`npx dsh web`,:3080)。现有例子:`ui-conversation/`、`ui-settings/`(包名 `@deepseek-ai/dsh-client-<name>`)。【待核实:具名 slot 列表 + 注册 API,原文在 dsh `AGENTS.md`,本次未读】
- **样式硬规矩**:feature 组件只用 `--dsw-alias-*` 语义 token;不许写死色值/抄静态调色板;theme 选择器不许进 feature CSS;用 CSS Modules + clsx。

### 6.3 搬过去 = 三件事

1. **重样式**:`.ui-*` → `--dsw-alias-*` + CSS Modules。组件逻辑不动,只换样式层。
2. **重打包**:react-router 顶层路由 → `ui-*` slot 注册。各页面(lifecycle 仪表盘 / intake modal / PTY 查看器 / judge 确认门)变成注册到具名 slot 的 feature 组件。
3. **换数据层**:`/ws/*` WebSocket → dsh `connection/` 的 RPC + event 流。React Query 大部分保留(只换 query fn 数据源)。

### 6.4 两条 UI 路线

- **UI-A(推荐,与 ride 形态同栈连贯)**:贡献成 dsh `ui-*` slot 插件,进 :3080 壳。story 仪表盘 / judge 门 / 终端都注册成 slot,与 dsh 自带 conversation/chat 同壳。一处 stack、一套事件总线、无 RPC 边界。
- **UI-B(保底)**:保留本项目 frontend,dsh 跑 headless(`sdk/`)当后端。保住 UI 现状,但要自己复刻一部分 dsh 的 chat/tool 渲染。**此路线与"全薄桥"形态2 更配,与选定 ride 形态拧。**

**缓解 churn 的写法**:把 UI 组件写成 framework-agnostic 纯 React + CSS Modules,slot API 只出现在一层薄"注册胶水"里——slot 契约变了只动胶水,不动组件(与本仓现有 `.ui-*` 原语解耦思路一致,也呼应 §0.4 退出逻辑)。

---

## 7. 风险与未决项

| # | 项 | 状态 | 说明 |
|---|---|---|---|
| R1 | dsh 是 developer preview,会破坏性变更 | 【已核实】 | README 明说。slot 契约 / seam API / event schema 都可能 churn。这是 ride 的税(§0.4),缓解靠护城河解耦。 |
| R2 | slot 系统精确契约(具名 slot 列表 + 注册 API) | 【待核实】 | 原文在 dsh `AGENTS.md`(`client/` README 引:"Authoring rules live in AGENTS.md")。决定你的页面能不能挂进去、挂到哪。**UI 层最大未知项**。 |
| R3 | `subagent/` 能否委派 claude/codex 做实际 coding | 【待核实】 | `dsh-subagent-claude-code`/`dsh-subagent-codex` 包**存在**(已核实),但委派成熟度、能否承载本项目现在的 stage 工作流语义,未验证。若不足,要回到"dsh 当 loop + 仍直接 spawn CLI"混搭,部分抵消纯净度。 |
| R4 | miner 如何消费 dsh `session/event` 流 | 【待核实】 | 输入从"刮 PTY"变"消费 session/event"是净改善,但 miner 是 Python、dsh 是 TS,跨进程取事件的具体通道(`sdk/` JSON-RPC?文件 sink?)要验证。 |
| R5 | "薄 TS 编排 + Python 飞轮"混合是否自洽 | 【待评审】 | §5 选定的 ride 形态。跨语言 + 双运行时,但飞轮解耦是 §0.4 退出逻辑的前提——请评这个取舍。 |
| R6 | 与 flywheel 论文的关系 | 【待评审】 | `REFACTOR-orchestrator-three-layer-positioning.md` §2.1 论断编排器价值在"信息差"不在"能力差"。ride 到 dsh 后 dsh 吃掉能力差,本项目只剩信息差——强化还是架空? |

---

## 8. 建议的下一步(评审通过后)

1. **抠 dsh `AGENTS.md` 的 slot 契约**(消 R2):产出"具名 slot 清单 + 注册 API + 本项目各页面→slot 映射"。
2. **验证 `subagent/` 委派**(消 R3):确认能否在 dsh loop 顶层委派 claude/codex 做实际 coding,以及能否承载 stage 工作流语义。
3. **最小 PoC(两层各一个)**:
   - TS 侧:一个 dsh 插件骨架——`ctx.story` 最小状态 + judge 挂 `agent/turn-stopping` + 一个 `ui-*` slot 组件,跑通"在 :3080 里看到 story 仪表盘"。
   - Python 侧:miner 经 `sdk/` 暴露成 `ctx.knowledge` provider,跑通"dsh 知识注入读到飞轮数据"。
4. **确认 R5**:PoC 后回头定"编排层 TS 原生 + 飞轮 Python"的边界画在哪。

---

## 附录:证据来源

**dsh 侧(均来自 master 分支 raw 文档):**
- 架构/seam/生命周期/配置:`docs/architecture.md`、`docs/capability-seams.md`、`docs/agent-lifecycle.md`、`docs/config-catalog.md`
- UI:`docs/web-styling.md`、`packages/client/README.md`、`packages/` 目录说明(`host/`、`sdk/`、`acp/`、`hooks/`、`subagent/`、`llm/`、`web-react/`、`connection/`)
- provider 非锁定:`dsh-llm-deepseek` / `dsh-llm-pi-ai`(OpenAI 兼容 BYOK)/ `dsh-llm-replay` + `dsh-subagent-claude-code` / `dsh-subagent-codex`(均见 config-catalog + packages)
- 状态:developer preview + 破坏性变更警告见 dsh `README.md`

**本项目侧:**
- `AGENTS.md`(本仓根):adapter/SessionSpec 契约、session-id 三模型、per-story worktree、全局编排线程、artifact-driven 完成判定、LLM 判定层、task_actions、offline prompt analysis 等设计契约
- `packages/story-lifecycle/docs/REFACTOR-orchestrator-three-layer-positioning.md` §2.1/§4:信息差 vs 能力差论断
- `packages/story-lifecycle/docs/DESIGN-artifact-driven-stage-completion.md`:评审 B(边界不 agentic)、评审 C(砍打字纠偏)、§4.6(无状态编排)、§4.7/§4.12(verify 靠执行 / headless 优先——本期不做,正是"被动性"的根)
- 代码勘察:`orchestrator/scheduler.py`(OrchestratorThread,5s poll)、`executors.py`(`is_artifacts_ready` 只认 declare)、`evaluation/stage_completion.py`(judge 非工具)、`evaluation/stuck_diagnose.py` + `engine/supervisor.py`(二值卡住检测)、`frontend/package.json`(React 19 栈)
