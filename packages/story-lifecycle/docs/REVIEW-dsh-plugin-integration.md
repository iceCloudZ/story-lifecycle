# REVIEW: `PROPOSAL-dsh-plugin-integration.md` 外部评审

> **评审对象**:`packages/story-lifecycle/docs/PROPOSAL-dsh-plugin-integration.md`(2026-08-14 方案稿)
> **评审基准**:不是 master 分支文档,而是**本机真实安装的 dsh `0.1.0-rc.6`**(`C:\Users\zzh58\AppData\Roaming\npm\node_modules\@deepseek-ai\dsh`,含其 `node_modules/@deepseek-ai/*` 全部包 + 正在运行的 :3080 web GUI)。提案中所有【待核实】项均以安装版源码为准重新核实,标 ✅/❌。
> **日期**:2026-08-14

---

## 0. TL;DR — 六个问题的结论

| # | 问题 | 结论 |
|---|---|---|
| Q1 | 形态选择(形态1 TS 原生重写 vs 形态2 Python+JSON-RPC) | 形态1 方向对,**但有一个必须修正的前提:rc.6 没有任何外部 CLI 委派能力**——story 的 worker 只能是 dsh 自家 agent。形态2 的 `sdk/` JSON-RPC 在 rc.6 **不存在**(master 有)。 |
| Q2 | §3 映射表健壮性 | 基本成立。judge→`agent/turn-stopping`、知识注入→`system-prompt/assemble` 均 ✅ 真实存在;两处都是 `serial`/`waterfall` 而非纯 waterfall,语义按提案理解即可,细节见 §3。 |
| Q3 | 重定位代价判断 | 成立,但漏了**两条隐性依赖**:worker 身份(rc.6 无外部委派,重定位比提案想的更彻底)+ LLM 调用栈(judge 应迁移到 dsh 的 `ctx.llm`,影响 token 计量,而计量是 miner 的成本分析输入)。 |
| Q4 | flywheel 论文冲突 | **被强化,不是被架空**。插件化 = REFACTOR 文档"砍①替模型思考、保留②帮模型执行、加③跨 session 持久化"的机械执行;story 层只剩信息差。但信息差资产隐式编码在 Python 里,不会自动迁移,这是重写工作量的大头。 |
| Q5 | UI 路线 | UI-A 可行且契约已核实,但"重装裱"低估三项成本:**dsh shell 是 React 18**(story 前端是 React 19)、client 包需 tsdown 单独构建(HMR 只覆盖 dsh 仓库内)、具名 slot 清单需 PoC 实测。churn 风险(R1)维持最高。 |
| Q6 | 待核实项 R2/R3/R4 | R2 slot 契约 ✅ 已核实(存在且具体);R3 subagent 委派 claude/codex ❌ **rc.6 不可用**(master 有,见 §7);R4 miner 消费 ✅ 比提案想的还简单——`session.jsonl` 明文落盘,Python 直接读。 |

---

## 1. 评审基准:安装版 rc.6 vs master(提案没有意识到这是第一个决策点)

提案 §1.2 引用的都是 deepseek-harness **master 分支**文档(`docs/architecture.md`、`packages/` 表)。但安装版 rc.6 与 master 的**包面差异巨大**,直接决定形态选择:

| 能力 | master(提案引用) | rc.6(本机安装) |
|---|---|---|
| `hooks/`(Claude Code / Codex 线协议) | ✅ 有(`packages/hooks/hooks-codex`) | ❌ 无 |
| `subagent/` 委派 codex | ✅ 有(`packages/subagent/subagent-codex`) | ❌ 无(只有 in-process 委派) |
| `sdk/`(JSON-RPC 出进程 SDK) | ✅ 有(`packages/subagent/subagent-dsh-sdk`) | ❌ 无 |
| `acp/` | ✅ 有 | ❌ 无 |
| `subagent/` in-process(fork/spawn) | ✅ | ✅(`dsh-subagent-fork-in-process` / `dsh-subagent-spawn-in-process` / `dsh-subagent-in-process-driver`) |

证据:rc.6 完整包清单(`node_modules/@deepseek-ai/` 下 180+ 包)中**不存在**任何 claude/codex/kimi/opencode/sdk/acp/hooks 适配包;master 侧见 [packages/README.md](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/README.md)、[hooks/README.md](https://github.com/deepseek-ai/DeepSeek-Harness/blob/master/packages/hooks/README.md)、[subagent-codex/README.md](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/subagent/subagent-codex/README.md)、[subagent-dsh-sdk/README.md](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/subagent/subagent-dsh-sdk/README.md)。

**含义**:
1. "用 dsh loop 顶层委派 claude/codex 做 coding"(提案 R3 的设想)**在当前安装版不存在**。要么接受"worker = dsh 自家 agent(deepseek 模型 + dsh 工具集)",要么把 master 的 hooks/subagent-codex 发布列为路线硬依赖。
2. 提案 §4 形态2 的"Python 经 `sdk/` 当 provider"**现在无法照搬**,替代通道是 HTTP REST(TS 插件 fetch Python)或共享文件/SQLite。
3. 这本身就是 R1(preview churn)的实证:**rc.6 和 master 的差异不是小修小补,是包面的增删**。

**建议:把"基于 rc.6 还是 master"定为决策 0**,它决定 worker 模型和桥接通道,排在 Q1 之前。

---

## 2. Q1 形态选择:形态1 方向对,但 worker 模型必须修正

提案选形态1(TS 原生重写核心),理由是形态3 不解决被动性、形态2 引入跨语言边界。**同意方向**,但补充三点:

1. **worker 身份修正(最关键)**:形态1 的隐含假设是"重写后,story 仍驱动前沿 CLI 干活,只是编排换成 dsh loop"。rc.6 无外部委派 → 形态1 实际形态是"**dsh agent 自己当 worker**":stage 的 design/implement/test 由 dsh 的 agent loop + subagent + workflow 执行,工具是 dsh 自带的 bash/pwsh/fs/editor。这**不是坏事**(dsh 的 agent 有完整事件流,被动性问题彻底消失),但必须明说,否则重写出来的东西和预期不符。
   - 若业务**必须**用 claude/codex 当 worker:当前版本下形态1 不成立,只能等 master 的 hooks/subagent-codex 发布,或退回形态3(把 dsh headless 当 CLI worker——`dsh --profile headless "job"` 可命令行调用,这是最便宜的先行集成)。
2. **形态2 前提失效但结论不变**:rc.6 没有 `sdk/`,所以"薄 TS shim 走 JSON-RPC"要改成"TS 插件直接 fetch Python REST API"(即形态 A 的薄壳思路)或"共享 session.jsonl + SQLite"。跨语言边界仍然存在,提案对形态2 的批评(序列化/调试)依旧成立。
3. **"核心 TS + 飞轮 Python"混合依然自洽**,只是桥接通道从 JSON-RPC 换成 HTTP + 文件,见 §7 R5。

---

## 3. Q2 映射健壮性:表基本成立,两处语义修正

逐行核实(证据来自 rc.6 安装包源码):

| 提案映射 | 核实结果 |
|---|---|
| stage 推进/advance → `agent/turn-stopping` 或 `agent/pre-step` | ✅ 真实存在。`dsh-agent-loop` 在 step 后跑 `serial("agent/turn-stopping", …)`(`dsh-agent-loop/lib/index.js:565`);`agent/pre-step` 是 `waterfall`、返回 `PreStepDecision`、可拒绝输入(`dsh-agent-loop/lib/index.js:501`)。**语义修正**:`turn-stopping` 是 `serial`(顺序委托,终态检查点),`pre-step` 才是 `waterfall`(权威 steering)。judge 放 `turn-stopping` = "检查 stage 该不该结束",放 `pre-step` = "steer 下一个 stage 做什么"——提案两个都列了,按此分工即可。scope 映射见 `dsh-scope/lib/invariant.js`(`agent/turn-stopping`→agent scope)。 |
| LLM judge → `agent/turn-stopping` | ✅ 语义契合:serial 检查点是天然的 stage 边界判定位。**注意**:`agent/turn-stopping` 载荷只有 `{agent, turn, signal}`,没有消息/工具结果上下文——judge 需要自己从 session 读(有 `ctx.session`/projection),或挂在 `tool/result` 之后,PoC 要实测数据可得性。 |
| 知识注入 → `system-prompt/assemble` | ✅ 正是该 waterfall 的用途:`dsh-system-prompt/lib/index.js:283` 用 `ctx.waterfall(…, "system-prompt/assemble", assembly, context, …)` 组装 prompt;`dsh-agent-presets` 自己就监听它注入 preset 内容(`dsh-agent-presets/lib/invariant.js:1164`)。scope 是 system-prompt scope(`dsh-scope/lib/invariant.js:30`)。 |
| artifact 完成判定 → `tool/result` post-hook | ✅ `tool/result` 是 SessionEventMap 的一等事件(`dsh-commands/lib/typert.host.js:398` 完整声明)。**注意**:它只覆盖 dsh 自家工具;若未来走 master hooks 驱动外部 CLI,事件形态不同。"读文件判完成"的半成品误判问题在 dsh 里同样存在(只是换成 fs 工具结果),1068018 事故的安全纪律要保留:用工具结果 + declare 语义,不裸读文件。 |
| miner → 消费 `session/event` 流 | ✅ 比提案说的还好,见 §7 R4。 |
| 淘汰:PTY tap / declare / OrchestratorThread / BaseAdapter | 成立,但见 Q3 的隐性依赖。 |

**结论**:§3 映射表无错配,两处语义修正(serial vs waterfall、judge 上下文数据可得性)都不推翻设计。

---

## 4. Q3 重定位代价:判断成立,补两条隐性依赖

提案 §4.1 列的冗余项(PTY tap / awaiting_detector / declare 完成判定 / 5s 轮询 / BaseAdapter·SessionSpec / 本仓 AGENTS.md 的 adapter·session-id·artifact-driven 契约)在"worker = dsh agent"前提下**基本全部成立**。但漏了:

1. **LLM 调用栈(提案完全没提)**:story 的 judge/规划现在走 `infra/llm_client.py`(自有 provider/key/model 配置)。迁到 dsh 后应走 dsh 的 `ctx.llm`(`dsh-llm` + `dsh-llm-deepseek` + `dsh-llm-retry` + `dsh-token-meter`,还有 `dsh-settings-file` 热更新 provider)。这不只是换 API——**token 计量会换到 dsh 侧**,而 miner 的成本分析依赖 token 数据。好消息:`session.jsonl` 的 `request/header` 事件带 `EpochHeader`(usage),成本数据反而更全。迁移设计必须把"judge 走 ctx.llm + usage 落 session 流"写进去。
2. **worker 身份(承上)**:rc.6 无外部委派 → "外部驱动机制变冗余"不是渐进收缩,是**整层消失**。隐性依赖:本仓的 `story tool declare` 契约(agent 自报完成 + miner 绑定)没有对等物——dsh 里完成信号来自 tool 事件;`PtyLogger events.jsonl` 由 `session.jsonl` 替代。
3. 提案 §4.1 末尾自己列的"业务语义不算冗余"(worktrees_root / seed_context / task_actions / profiles)——**确认这个判断正确**,并补充:**judge 的 context assembly(`assemble_judge_context`)和 playbook seeding 也是业务语义,而且是信息差核心**,必须作为 `ctx.story` 的内容 1:1 搬运,不是"换语言重写"能自动带过去的。这是重写工作量的大头,建议在评估里单独列项。

---

## 5. Q4 flywheel 论文冲突:被强化,不是被架空

REFACTOR 文档 §2.1 的命题是"编排器持久价值在信息差(看到 worker 看不到的信息),不在能力差(规划更聪明)"。插件化到 dsh 后:

- dsh 接管 loop(能力差层)→ story 层**只剩信息差层**(跨阶段视角、playbook、judge 上下文、跨 session 持久化)。这与 REFACTOR 的三层处置完全同构:形态1 就是"把①替模型思考砍掉交给 dsh、②③搬进 TS 并加强"的机械执行。
- **所以论断被强化**:插件化消灭的恰是 REFACTOR 说要砍的部分,留下的恰是它说要保/加的部分。
- **反面提醒**:信息差资产(judge context assembly、seed pipeline、playbook 写入、quality-flywheel seeding)目前隐式编码在 Python 代码里,不会因"换语言"自动增值。若重写只搬状态机不搬信息差逻辑,那才是真正的架空。评审建议:把"信息差逻辑清单 + 各自在 TS 侧的落点"作为形态1 的第二张映射表,与 §3 概念映射表并列。

---

## 6. Q5 UI 路线:UI-A 可行,但"重装裱"低估三项成本

**核心好消息**:slot 系统在 rc.6 **真实存在且契约具体**(消掉了提案的"最大未知项"):

- 纯注册核心:`dsh-client-ui-slots/lib/index.js` 的 `SlotCore`——四种 slot kind(`single`/`keyed`/`list`/`chain`)、`register(options, component)`、children 声明表、priority shadowing、store seat、错误边界。
- 根声明(实测):`dsh-client-ui-layout/lib/client.js:405-430` — `root` 声明四个子 slot:`sidebar`(single,root)、`conversation`(single,session-maybe)、`details`(single,session)、`shell.overlay`(list,root)。story 仪表盘 / judge 确认门可以挂 `details` 或 `shell.overlay`(模态)。
- 插件形态(实测):npm 包 + `exports["./client"]` + package.json `dsh.client {platform, inject}` 清单(`dsh-client-ui-workflow-run/package.json`);node 半(`dsh-client-modules/lib/index.js`)扫描 loader 条目中声明 `dsh.client` 的包,组装 `window.__DSH_BOOT__`,服务 `/plugins/<id>/client.js`;浏览器半是 `{apply(ctx), inject:[…]}` 的 cordis 风格插件体。

**但"搬过去=换 token 命名空间 + 路由换 slot"低估三项成本**:

1. **React 版本差**:dsh shell 的客户端组件 peerDeps 是 **React 18**(`dsh-client-ui-workflow-run` peerDeps `react: ^18.2.0`),story 前端是 **React 19**。JSX 大体兼容,但 react-router 7 / TanStack Query / React 19 专属 API 要按 React 18 重定,不是纯样式层改动。
2. **构建链**:client 包用 tsdown 单独 bundle(`"bundle": "tsdown"`),且 HMR 只对 **dsh 仓库内**的 dev:web watcher 生效——外部插件(不在 dsh 仓库里)的改动要 rebuild + 重启 dsh 才能看到。开发迭代速度比提案预期慢,建议 PoC 阶段就用真实外部包验证这条链路。
3. **具名 slot 全清单待实测**:`conversation` 内部还声明了子 slot(tool 视图、input trigger 等),story 需要哪个得在 PoC 里逐槽实测;`root` 只给了骨架。

**UI-B 依然成立**且与"worker=dsh agent + 飞轮 Python"的混合形态更配。提案的 churn 缓解写法(framework-agnostic 组件 + 薄注册胶水)完全正确,保留。**结论**:UI-A 可行,但要把"React 18 对齐 + tsdown 构建链 + slot 实测"写进成本;R1(preview churn)维持最高风险——本次评审本身就证明 slot/client 契约在 rc 期会变。

---

## 7. Q6 待核实项结论(R1–R5 逐条)

| # | 项 | 结论 |
|---|---|---|
| R1 | dsh preview 破坏性变更 | ✅ 维持,且**加重**:rc.6 与 master 的包面差异(hooks/sdk/acp 全缺)证明 churn 是剧烈的、真实的。 |
| R2 | slot 系统契约 | ✅ **已核实存在且具体**(§6)。剩余未知只有"conversation 内部具名 slot 清单",属 PoC 实测项,不再是设计阻塞。 |
| R3 | subagent 能否委派 claude/codex | ❌ **rc.6 不可用**:无 hooks/sdk/acp 包,subagent 全部 in-process(`dsh-subagent-*-in-process` 三件套)。✅ **master 已有**(subagent-codex、subagent-dsh-sdk、subagent-acp)。结论:方向可行、当前版本不行;决策 0(§1)决定这条怎么走。 |
| R4 | miner 如何消费 session/event 流 | ✅ **已核实,且比提案简单**:`dsh-session-persistence-jsonl` 按 `$DSH_HOME/sessions/<projectKey>/<sessionId>/session.jsonl` **明文 JSONL 落盘**(默认无压缩,`dsh-session-persistence-jsonl/lib/index.js:145-157` 的路径构造;本机已见 `C:\Users\zzh58\.dsh\sessions\--D-github-story-lifecycle--\session-6018f542-…`)。事件含 `tool/call`/`tool/result`/`assistant/message`/`request/header`(token usage)。**Python 直接读文件即可,不需要 sdk/JSON-RPC**;另有 `dsh-session-query-sqlite` 的 SQLite 全文派生索引(application_id 保护,schema v8)可选。miner 的改造 = 换一个"转录源"(session.jsonl 替代 PTY 刮),富一个量级。 |
| R5 | "核心 TS + 飞轮 Python"混合自洽性 | **自洽,但桥接通道要改**:rc.6 无 sdk/,桥接 = ①miner 读 session.jsonl/SQLite(事件面,单向、简单)②TS 编排层 fetch Python REST 8180(控制面,如知识库查询)。双运行时复杂度不变。建议把这两个通道写成契约文档,别依赖 sdk/ 发布。 |

---

## 8. 额外发现(提案未覆盖,建议纳入)

1. **`dsh-tool-cordis` 提供动态工具定义**(`cordis_define`,browser 侧可定义/启停工具行,见 `dsh-client-ui-cordis` 描述)。story 工具(story_create/plan/judge…)理论上可以动态注册,免重启,PoC 值得验证。
2. **`dsh-schedule` 存在**:stage 超时/卡住检测可以做成事件驱动版本(替代 detect_stuck 的轮询),与 watermark 事件流契合。
3. **`dsh-goal-round-driver` 监听 `agent/pre-step` 做 round 检查**(`dsh-goal-round-driver/lib/index.js:281`)——这就是"进行中编排器介入"的现成范式,story 的"进行中质量感知"可以直接抄这个模式。
4. **`ctx.jobs` / `ctx.workflowEngine` 底座齐全**(`dsh-jobs`、`dsh-workflow`、`dsh-workflow-worker-thread`、`dsh-tool-workflow`),stage=workflow/goal 的映射有现成实现可参考。
5. **当前 story 的 judge 走 `llm_client.py`、dsh 走 `ctx.llm`**——迁移后 token 计量归 dsh(见 §4),miner 成本分析的输入通道要跟着改。

---

## 9. 建议的下一步(修正版)

按依赖顺序,不按提案原顺序:

1. **决策 0(新增)**:定"基于 rc.6 还是 master"。rc.6 = worker 只能是 dsh agent、桥接走 HTTP+文件;master = 未来可委派 codex、有 sdk/,但要自己从源码构建。**这个决策先于一切**。
2. **信息差资产清单(新增,对应 Q4)**:列出 judge context assembly / seed pipeline / playbook 写入 / quality-flywheel 在 TS 侧的落点,与 §3 概念映射表并列。这是重写工作量的真实大头。
3. **最小 PoC(原第 3 步提前)**:一个外部 client 包(`ui-story-*`)+ 一个模型工具(`story_*`,走 HTTP 8180),验证:①React 18 下组件能挂进 slot ②外部包构建链(HMR 不生效 → rebuild+重启)③judge 在 `turn-stopping` 能读到所需上下文。PoC 不重写任何 Python。
4. **subagent 委派验证(原第 2 步,降级)**:只在决策 0 选 master 时做;选 rc.6 则改为"验证 dsh agent 当 worker 的 coding 质量是否达标"——这是形态1 成立与否的实证。
5. **决定 R5**:飞轮通道契约(session.jsonl + HTTP 8180)先定,再进编排层重写。

---

## 附录:证据索引

**安装版 rc.6(`C:\Users\zzh58\AppData\Roaming\npm\node_modules\@deepseek-ai\dsh\node_modules\@deepseek-ai/`)**:
- 事件/waterfall:`dsh-commands/lib/typert.host.js:398`(SessionEventMap 完整声明)、`dsh-agent-loop/lib/index.js:501/565`(pre-step waterfall / turn-stopping serial)、`dsh-system-prompt/lib/index.js:283`(assemble waterfall)、`dsh-scope/lib/invariant.js`(事件→scope 映射)
- slot:`dsh-client-ui-slots/lib/index.js`(SlotCore 全文)、`dsh-client-ui-layout/lib/client.js:405-430`(root 声明)、`dsh-client-ui-workflow-run/package.json`(`dsh.client` 清单 + React 18 peerDeps + tsdown)
- 客户端模块系统:`dsh-client-modules/lib/index.js`(扫描 `dsh.client` 包、组装 `__DSH_BOOT__`、服务 `/plugins/<id>/client.js`)
- 会话持久化:`dsh-session-persistence-jsonl/lib/index.js:145-157`(路径)、`dsh-session-query-sqlite/lib/index.js`(SQLite 派生索引,schema v8)
- 子代理:`dsh-subagent-fork-in-process` / `dsh-subagent-spawn-in-process` / `dsh-subagent-in-process-driver`(仅 in-process)
- 工具/服务底座:`dsh-tool-cordis`、`dsh-schedule`、`dsh-goal-round-driver/lib/index.js:281`、`dsh-workflow`、`dsh-jobs`

**master(提案引用,本次补证)**:
- [packages/README.md](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/README.md)
- [packages/hooks/README.md](https://github.com/deepseek-ai/DeepSeek-Harness/blob/master/packages/hooks/README.md)、[hooks-codex/README.md](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/hooks/hooks-codex/README.md?plain=1)
- [packages/subagent/subagent-codex/README.md](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/subagent/subagent-codex/README.md)、[subagent-dsh-sdk/README.md](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/subagent/subagent-dsh-sdk/README.md)、[subagent-acp/README.md](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/subagent/subagent-acp/README.md)

**本项目侧**:`docs/REFACTOR-orchestrator-three-layer-positioning.md` §2.1(信息差/能力差)、`docs/DESIGN-artifact-driven-stage-completion.md`(declare 纪律)、`src/story_lifecycle/infra/llm_client.py`(judge 调用栈现状)
