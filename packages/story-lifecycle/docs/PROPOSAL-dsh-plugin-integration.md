# PROPOSAL: story-lifecycle 作为 deepseek-harness 插件集成

> **状态**:方案稿,待外部评审
> **日期**:2026-08-14
> **作者意图**:把本项目的核心能力(story/stage 编排 + LLM judge + 知识飞轮)做成 [deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)(下称 **dsh**)的原生插件。
> **本文档自包含**——评审者无需阅读此前的对话,所有事实都带证据来源(§附录)。标 **【已核实】** 的是读了 dsh 源文档确认的;标 **【待核实】** 的是推断或尚未读到原文的,请重点挑这部分。

---

## 0. 给评审者的问题清单(先看这个)

请围绕以下几点给判断:

1. **形态选择**:本项目核心改写成 dsh 原生插件(形态1,TS)是否合理?替代方案是"Python 大脑保留 + 薄 TS 插件走 JSON-RPC"(形态2)。形态1 的主要代价是丢 Python 生态(miner 的转录→SQLite 处理)。这个取舍对吗?
2. **映射健壮性**:§3 的"story-lifecycle 概念 → dsh seam"映射表有没有错配?尤其是 LLM judge 挂到 `agent/turn-stopping`、知识注入挂到 `system-prompt/assemble` 这两处,是否符合 dsh waterfall 的语义?
3. **重定位代价**:§4 列出"外部驱动机制(PTY tap / declare / OrchestratorThread / BaseAdapter)在插件化后变冗余"——这个判断是否成立?有没有被低估的隐性依赖?
4. **flywheel 论文冲突**:本项目的 `REFACTOR-orchestrator-three-layer-positioning.md` §2.1 论断"编排器价值在『信息差』不在『能力差』"。插件化到 dsh 后,dsh 接管了 loop(能力差),本项目只剩信息差层。这个论断是被强化还是被架空?
5. **UI 路线**:§5 选 UI-A(贡献成 dsh `ui-*` slot 插件)。但 dsh 是 developer preview、slot 契约会 churn。这个风险是否高到该走 UI-B(保留自有 frontend + dsh headless)?
6. **待核实项**(§6):dsh 的 slot 系统契约、`subagent/` 是否真能委派 claude/codex 做 coding、miner 如何消费 dsh `session/event` 流——这三项我没读到原文,请评估其可行性与风险。

---

## 1. 背景:两个系统各是什么

### 1.1 story-lifecycle(本项目)

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

**本项目自述的"被动性"痛点**(用户原始抱怨,经代码核实):
- stage 完成判定完全被动(等 declare);
- 质量判定只在 stage 边界,**进行中编排器无任何 LLM 介入**;
- 健康观察是二值(卡/没卡),无进度估计/漂移检测;
- 无"持续观察者"角色(`PtyLogger` 持续落 events.jsonl,但没人 tail)。

### 1.2 deepseek-harness(dsh)

DeepSeek 开源的 AI agent 框架,Cordis 基础,**"Everything is a Plugin"**。TS/Node。【已核实:dsh README + `docs/architecture.md` + `docs/capability-seams.md` + `docs/agent-lifecycle.md` + `docs/web-styling.md` + `packages/client/README.md`】

> ⚠️ dsh 现为 **developer preview**,README 明说"THERE WILL BE COMPATIBILITY-BREAKING CHANGES"。

**架构核心**:
- 运行中的 dsh = "a plugin tree composed at boot from ordered layers"(profile/bundle)。
- "every part of the product is a plugin... including the agent loop itself, so every part is replaceable from configuration"。`core/agent` 拥有 `Agent` 接口;`core/agent-loop` 是默认实现。**step = 一次模型请求 + 它调的 tool;turn = 零或多个 step**。

**扩展模型(seam)**:一个 capability 有三个角色——
- **Service Definition**(owner,声明契约,挂在某个 `ctx.<key>`)
- **Service Provider**(后端实现,可多个,可换)
- **Consumer**(通常是 model-facing tool,只依赖 Service Definition 不依赖具体 provider)

> 原文:"Extension plugins depend on Service Definitions, never concrete providers." 多个 provider 可同背一个 Definition(`ctx.storage`、`ctx.sessionPersistence` 都支持多后端 side-by-side)。

**单 turn 事件序(waterfall,监听者须调 `next()` 委托)**【已核实:agent-lifecycle.md】:

```
turn/start → agent/inbox/claimed(每条消息)
  → step/start → user/message
  → system-prompt/assemble → agent/request → llm/stream → assistant/chunk* → assistant/message
  → tool/call → tool/result(barriers + bounded rolling pool)
  → step/end → agent/turn-stopping(终态检查点)→ turn/end
另:agent/pre-step(权威 steering,返回 authoritative)、agent/request-error(返回 retry 或保留原错)
```

**其他相关 `ctx` 服务**:`ctx.goals`(同 session 目标生命周期)、`ctx.jobs`(后台工作)、`ctx.workflowEngine`(每 context 一个引擎,无命名 provider registry)。
**状态**:durable replay 落在 `session/event` 流;live control/status 落在 `agent/*`。

**与外部 agent 的关系**【已核实:packages 表】:
- `hooks/` = "Hook bridges + the shared Claude Code / Codex wire-protocol library" —— 有 claude/codex 线协议库。
- `subagent/` = 委派(文档:"from a fresh child agent to a delegated turn in another product")。
- `sdk/` = "Out-of-process runtime SDK: JSON-RPC protocol, TypeScript client, and server plugin" —— 可 headless/embedded。
- `acp/` = "Automation-only Agent Client Protocol server"。
- 注:dsh 自带 loop 和自己的 tool,**不默认把 Claude Code 当主 loop 用**;但可经 subagent 委派。【待核实:subagent 委派 claude/codex 做实际 coding 的具体机制与成熟度】

---

## 2. 核心判断:能不能做成 dsh 插件

**结论:技术上能,且本项目核心价值(story/stage 模型 + judge + 知识飞轮)在 dsh 里反而落得更干净。** 代价是大半个"外部驱动机制"变冗余(§4)。

理由:dsh 的扩展模型(seam + waterfall)与本项目概念契合度很高——几乎每个 story-lifecycle 概念都能在 dsh 里找到对等落点,而且"waterfall 事件序"恰好把本项目现在靠"5s 轮询 + side-channel"硬凑出来的感知,变成了一等公民。

---

## 3. 概念映射表(判断"能做"的硬依据)

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

**§1.1 列的"被动性痛点"在 dsh 里直接消失**:dsh 原生给每个 turn 的完整事件序,想在哪个点插感知/判定/纠偏都行(全是 waterfall);不需要 PTY 刮日志、不需要 5s 轮询、不依赖 agent 自报 declare。

---

## 4. 选定形态:形态1(TS 原生重写核心)

三种集成形态对比:

| 形态 | 做法 | 对齐 dsh | 成本 | 代价 |
|---|---|---|---|---|
| **形态1(选定)** | 用 Cordis idiom 把 story-lifecycle 核心重写成原生 dsh 插件(TS) | 最高 | 最高(重写) | 丢 Python 生态(miner 的转录→SQLite 处理、knowledge 的 raw SQL) |
| 形态2 | Python 大脑保留,经 dsh `sdk/`(JSON-RPC)暴露成 `ctx.story`/`ctx.knowledge` provider,dsh 侧写薄 shim | 中 | 低 | 跨语言边界(序列化/调试) |
| 形态3 | 加 `DshAdapter` 把 dsh 当后端接进现有编排器 | 低 | 低 | 把 dsh 当哑 CLI 用 PTY 盯,**丢掉它最大资产(事件 loop),被动性问题不解决**——不推荐 |

**为什么选形态1**:形态3 不解决被动性;形态2 保留 Python 但引入跨语言边界且仍要维护外部驱动思路。形态1 最彻底地吃下 dsh 的"事件即感知"红利,且与下文 UI-A 同栈连贯。**主要顾虑**:`story-miner`(Python,转录归一+挖掘)和 `knowledge`(raw SQL schema)是飞轮的承重墙,纯 TS 重写代价大——建议这两块**保留 Python,经 `sdk/` 当 provider**(形态1+2 混合),核心编排层(story/judge/stage)才用 TS 原生。【待评审:这个"核心 TS + 飞轮 Python"的混合是否自洽】

### 4.1 重定位的代价:哪些现有机制会变冗余

插件化不是"加个插件",是**重新定位项目身份**:从"外部驱动前沿 coding agent 的编排器"变成"dsh loop 之上的 story/judge/知识层"。变冗余的现有机制:

- **PTY tap / `awaiting_detector` / declare 事件完成判定**:dsh 有原生 tool 事件。
- **`OrchestratorThread` 5s 轮询调度**:dsh 事件驱动。
- **`BaseAdapter` / `SessionSpec` / session-id 三模型**(prespecified / output-driven / file-scan):仅当仍要驱动非 dsh agent 时(经 `subagent/`)才部分保留。
- 本仓 `AGENTS.md` 里那一大段 adapter/session-id/artifact-driven 的设计契约,**大部分失效**。

> 请评审重点挑:这些"变冗余"的判断有没有低估的隐性依赖?例如 `worktrees_root`(per-story 工作区)、`seed_context`(接手中途需求)、`task_actions` 约束(stage 语义)——这些不是调度机制而是**业务语义**,在 dsh 里应作为 `ctx.story` 的内容保留,不算冗余。

---

## 5. UI 怎么弄

**好消息:本项目 frontend 已是 React 19 + TS + Vite**,所以 UI 不是框架重写,是"重新装裱"。

### 5.1 栈对照

| 维度 | 本项目现在 | dsh web 【已核实:web-styling.md + client/README】 |
|---|---|---|
| 框架 | React 19 + TS + Vite | React + CSS Modules + clsx |
| 样式 | `.ui-*` 原语 + design tokens | `--dsw-*` 静态层 + `--dsw-alias-*` 语义层(**禁组件库/禁 Tailwind**) |
| 状态 | Zustand + TanStack Query | dsh object services / slot 状态 |
| 数据层 | WebSocket `/ws/story` `/ws/pty` `/ws/stories` + React Query | `connection/` 包(browser-host RPC + event delivery) |
| 终端 | xterm.js(PTY 查看器) | tool 事件流;可复用 xterm 渲染 |
| 路由 | react-router 顶层路由 | **slot 系统**(`ui-*` 包注册进 shell) |

两套都"token 驱动 + 约定式原语"——同一种设计哲学,移植本质是"换 token 命名空间 + 路由换 slot 注册"。

### 5.2 dsh 的 UI 模型

- **slot 系统**(`client/` 包拥有):"Defines how UI features register and compose extension slots"。贡献 UI = 写一个 `ui-*` 包(如 `ui-story-lifecycle`),把组件注册进具名 slot,即出现在 dsh web shell(`npx dsh web`,:3080)。现有例子:`ui-conversation/`、`ui-settings/`(包名 `@deepseek-ai/dsh-client-<name>`)。【待核实:具名 slot 列表 + 注册 API,原文在 dsh `AGENTS.md`,本次未读】
- **样式硬规矩**:feature 组件只用 `--dsw-alias-*` 语义 token;不许写死色值/抄静态调色板;theme 选择器不许进 feature CSS;用 CSS Modules + clsx。

### 5.3 搬过去 = 三件事

1. **重样式**:`.ui-*` → `--dsw-alias-*` + CSS Modules。组件逻辑不动,只换样式层。
2. **重打包**:react-router 顶层路由 → `ui-*` slot 注册。各页面(lifecycle 仪表盘 / intake modal / PTY 查看器 / judge 确认门)变成注册到具名 slot 的 feature 组件。
3. **换数据层**:`/ws/*` WebSocket → dsh `connection/` 的 RPC + event 流。React Query 大部分保留(只换 query fn 数据源)。

### 5.4 两条 UI 路线

- **UI-A(推荐,与形态1 同栈连贯)**:贡献成 dsh `ui-*` slot 插件,进 :3080 壳。story 仪表盘 / judge 门 / 终端都注册成 slot,与 dsh 自带 conversation/chat 同壳。一处 stack、一套事件总线、无 RPC 边界。
- **UI-B(保底)**:保留本项目 frontend,dsh 跑 headless(`sdk/`)当后端。保住 UI 现状,但要自己复刻一部分 dsh 的 chat/tool 渲染。**此路线其实与形态2(Python 核心)更配,与形态1 拧。**

**缓解 churn 的写法**:把 UI 组件写成 framework-agnostic 纯 React + CSS Modules,slot API 只出现在一层薄"注册胶水"里——slot 契约变了只动胶水,不动组件(与本仓现有 `.ui-*` 原语解耦思路一致)。

---

## 6. 风险与未决项

| # | 项 | 状态 | 说明 |
|---|---|---|---|
| R1 | dsh 是 developer preview,会破坏性变更 | 【已核实】 | README 明说。slot 契约 / seam API 都可能 churn。形态1 重写绑死在它上面,风险高。 |
| R2 | slot 系统精确契约(具名 slot 列表 + 注册 API) | 【待核实】 | 原文在 dsh `AGENTS.md`(`client/` README 引:"Authoring rules live in AGENTS.md")。决定你的页面能不能挂进去、挂到哪。**最大未知项**。 |
| R3 | `subagent/` 能否委派 claude/codex 做实际 coding | 【待核实】 | 本项目现状是直接驱动前沿 CLI。若 dsh subagent 委派成熟度不足,要回到"用 dsh 当 loop + 仍直接 spawn CLI"的混搭,部分抵消形态1 的纯净度。 |
| R4 | miner 如何消费 dsh `session/event` 流 | 【待核实】 | 飞轮的输入从"刮 PTY"变"消费 session/event"是净改善,但 miner 是 Python、dsh 是 TS,跨进程取事件的具体通道(`sdk/` JSON-RPC?文件 sink?)要验证。 |
| R5 | "核心 TS + 飞轮 Python"混合是否自洽 | 【待评审】 | §4 提的折中:编排层 TS 原生,miner/knowledge 保留 Python 经 sdk 当 provider。跨语言 + 双运行时,复杂度上升。 |
| R6 | 与 flywheel 论文的关系 | 【待评审】 | `REFACTOR-orchestrator-three-layer-positioning.md` §2.1 论断编排器价值在"信息差"不在"能力差"。插件化后 dsh 吃掉能力差,本项目只剩信息差——是被强化还是被架空? |

---

## 7. 建议的下一步(评审通过后)

1. **抠 dsh `AGENTS.md` 的 slot 契约**(消 R2):产出"具名 slot 清单 + 注册 API + 本项目各页面→slot 映射"。
2. **验证 `subagent/` 委派**(消 R3):确认能否在 dsh loop 顶层委派 claude/codex 做实际 coding。
3. **最小 PoC**:一个 `ui-story-lifecycle` 包骨架(注册一个 slot + 一个组件 + 接 dsh event),跑通"在 :3080 里看到 story 仪表盘"。
4. **决定 R5**:编排层 TS 原生 vs 全 TS(含飞轮重写)vs 形态2 混合——先做这个二选一再进 PoC。

---

## 附录:证据来源

**dsh 侧(均来自 master 分支 raw 文档):**
- 架构/seam/生命周期:`docs/architecture.md`、`docs/capability-seams.md`、`docs/agent-lifecycle.md`
- UI:`docs/web-styling.md`、`packages/client/README.md`、`packages/` 目录说明(`host/`、`sdk/`、`acp/`、`hooks/`、`subagent/`、`web-react/`、`connection/`)
- 状态:developer preview + 破坏性变更警告见 dsh `README.md`

**本项目侧:**
- `AGENTS.md`(本仓根):adapter/SessionSpec 契约、session-id 三模型、per-story worktree、全局编排线程、artifact-driven 完成判定、LLM 判定层、task_actions、offline prompt analysis 等设计契约
- `packages/story-lifecycle/docs/REFACTOR-orchestrator-three-layer-positioning.md` §2.1/§4:信息差 vs 能力差论断
- `packages/story-lifecycle/docs/DESIGN-artifact-driven-stage-completion.md`:评审 B(边界不 agentic)、评审 C(砍打字纠偏)、§4.6(无状态编排)、§4.7/§4.12(verify 靠执行 / headless 优先——本期不做,正是"可变更主动"的演进入口)
- 代码勘察:`orchestrator/scheduler.py`(OrchestratorThread,5s poll)、`executors.py`(`is_artifacts_ready` 只认 declare)、`evaluation/stage_completion.py`(judge 非工具)、`evaluation/stuck_diagnose.py` + `engine/supervisor.py`(二值卡住检测)、`frontend/package.json`(React 19 栈)
