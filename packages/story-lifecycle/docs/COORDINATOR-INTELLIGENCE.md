# 协调 Agent 智能化:现状核查与改造 RFC

> **取证日期**:2026-07-06
> **方法**:三路并行只读子代理(Explore),逐条对照"协调 agent 六块智能"的说法 × 真实代码,每条给 True/False/Partial + file:line 证据。
> **目标**:在动工"补强协调层智能"之前,先把**现状描述**校准到代码事实——避免团队按错误前提排期。
> **结论**:战略地图(跨阶段记忆 + 跨 story 调度 + 事前门 + skill 调度 + 自愈 + 可观测)成立,六块方向都成立;但**有 3 处现状描述是硬伤**,会直接改变优先级。修正后真正"从零要写"的只有异常自愈一块。
>
> ⚠️ 行号基于 2026-07-06 代码,随重构会漂移;动工前按**符号名**复核。

---

## TL;DR

| # | 说法 | 判定 | 一句话 |
|---|---|---|---|
| ① | playbook 飞轮"接了一半,差从 event_log 提取规律" | 🟡 **Partial** | 读路径已全链路通到 transition,"提取规律"这步**已经写了**;真缺口是**不持久化 + judge 没接** |
| ② | "多 story 是 FIFO/优先级" | 🟢 **True** | `scheduler.py` 排序键 `(ready, priority, created_at)`,无依赖/冲突/资源感知——真空白 |
| ③ | "judge 是纯 LLM,最大可靠性缺口" | 🔴 **False** | `judge.py` 已是**硬指标在前、LLM 在后**的两段式;硬伤是"missing 不拦 + 只在 verify 跑" |
| ④ | "skill 从'agent 自己想起来调'升级为'协调层确定性调度'" | 🟢 **True(确是空白)** | `prompt_renderer.py` 只在 prompt 里写"请调用 Skill",无确定性 hook |
| ⑤ | "recovery 只在 run_story 抛异常时触发" | 🟢 **True** | 且 `recovery.py:3` docstring 自称覆盖 poll-timeout——**文档与实现矛盾** |
| ⑥ | "event_log + PTY tap → SSE 推进度" | 🟡 **Partial** | 实时推送以 **WebSocket** 为主,只有 1 个 SSE 端点;能力在,只是"SSE"措辞不准 |

**3 处硬伤**(直接影响排期,详见 §2):

1. ③ "judge 纯 LLM" 是错的——已有两段式 judge,扩展而非新建。
2. ① "飞轮接了一半"低估了——读路径已通到 transition,且 reflection 规律挖掘已写,只差持久化 + 接 judge 两段。
3. ⑤ recovery 触发面 + done 兜底**完全空白**——是唯一"从零要写"且直接决定"流程卡不卡死"的项。

---

## §0 背景与范围

协调 agent(story-lifecycle)的价值,集中在**单个 code agent 在 PTY 里看不见全局、做不了**的事:跨阶段记忆、跨 story 调度、事前风险门、按需调度 skill 链、异常自愈、可观测。

本文先对"当前代码做到哪一步"做严格核查(§1),再据此重排优先级(§2),最后给出每块的 file/函数级改造点(§3)。**核查依据是 2026-07-06 代码本身**,不是文档宣称——`ARCHITECTURE.md` 与 `recovery.py` docstring 各有一处与实现不符,已在对应条目标注。

范围限定在 `packages/story-lifecycle/` 协调层;`story-miner` 的挖掘产物如何回流是跨包话题,仅在 §3.1 边界处提及。

---

## §1 现状核查(六块 × 代码对账)

### ① playbook 飞轮 —— Partial(描述偏保守)

**说法**:"judge/transition 没有把跨 story 的成败规律喂回去…… `_build_verify_history_facts` 接了一半,差自动从 event_log 提取规律这步。"

**事实**:读路径已经全链路通了,而且"从 event_log 提取规律"这步**已经实现并接线**,不是"差的这步":

- `engine/planner.py:427-464` `_build_verify_history_facts` 已在 `planner.py:910-922` 真实调用,读**全局** `event_log`(`models.py:1258` `get_recent_events_by_type(["recovery_action","judge_verdict","transition_decision"], limit=100)`)。该函数 docstring(`models.py:1261`)明写"供层5 reflection 的全局 playbook 用(飞轮知识是跨 story 的)"。
- `learning/reflection.py:25-118` 的 `reflect()` 真的在做规律挖掘:取 `recovery_action(retry_new_adapter)` 事件,按 `story_key` 分组,要求该 story 后续有 pass 事件才沉淀("没 pass 兜底 → 不沉淀",`reflection.py:46-65`),产出"adapter X 失败 → 换 Y 成功"规则带 support 数。
- 产出的 `same_failure_swap_succeeded` 标志喂回 `decide_transition`(`transition.py:69-73`),**影响"换不换 adapter"的决策**——这是真实的跨 story 反馈。

**真缺口(说法漏掉的两段):**

1. **judge 完全没接**。`judge.py` 全文 grep 不到 knowledge/playbook/history;LLM prompt(`judge.py:130-142` `_build_judge_prompt`)只喂当前 story 的结构化 facts。所以是"transition 喂了、judge 没喂",不是"都没喂"。
2. **playbook 不持久化**。`reflect()` 每次在 verify-gate 里**现算**(`planner.py:453`),从不写进 `KnowledgeIndex`;repo 内 grep `reflect` 的调用点只有 `reflection.py` 自身,无批量/调度 caller。所以是"算了但不积累",而非"没接"。

➡️ 这块的实质工作量 = **持久化 + 接进 judge**,不是"建飞轮"。

---

### ② 跨 story 调度 —— True(确是真空白)

**说法**:"现在多 story 是 FIFO/优先级。"

**事实**:准确。

- `engine/scheduler.py:26-44` `decide_schedule` 排序键 `(ready, priority_rank, created_at)`:`ready`(line 38)→ `priority_rank` P0>…>P5(line 39-40,默认 P2)→ `created_at` FIFO 兜底(line 41)。整个文件 45 行。
- `graph.py:369-395` `order_ready_stories` 喂 ready story 过 `decide_schedule` 后提交;`graph.py:22` 实际 dispatcher 是 `ThreadPoolExecutor(max_workers=4)`——按提交顺序并发到 4。
- **没有**:依赖感知排序、两个 story 改同一文件的冲突检测、agent 配额/资源调度。唯一"冲突"感知在 `shadow_router.py:35/290-293`(`CONSTRAINT_CONFLICT`),但那是 shadow-run router,不是多 story 调度器。

➡️ 你提的三个增强(依赖/资源/冲突)都成立,是从零写。

---

### ③ 风险门 / 硬指标门 —— 🔴 False("judge 纯 LLM"是硬伤)

**说法**:"现在 judge 是纯 LLM,这是最大可靠性缺口。"

**事实**:**这一句不符合代码**,而它正是你排的第 ② 优先级,所以必须纠正。`evaluation/judge.py:46-60` 已经是**硬指标在前、LLM 在后**的两段式:

```
judge.py:46-51   if done_data.get("build_passed") is False:  → rework, rework_point="build"  [不调 LLM]
judge.py:52-60   if tests_passed is False / failures 非空 / failed>0:                       → rework, rework_point="tests"  [不调 LLM]
judge.py:62-77   只有硬指标过了才进 decide_response(LLM),且喂结构化 facts,不喂裸日志
```

模块 docstring(`judge.py:5-8`)自己写着"硬指标(规则,无 LLM)…… LLM judge(结构化):硬指标过 → 喂结构化 facts"。所以"补硬指标门"**已经做了一半**,不是新建。

**真缺口(更细,且更可操作):**

1. **只认 `is False`,不认 missing/空**。`judge.py:32-33` 注释明写:缺 `build_passed` 当成"还没失败",落到 LLM。kimi 那种"自报没编译"如果 `build_passed` 是缺而非 `False`,门就不拦——这正是你 §7 跑下来的 kimi done-fumble 形态。
2. **只在 verify 阶段跑**。`planner.py:875` `if stage == "verify":` 才进 `run_verify_gate`;build/compile 阶段没有 `compile_passed` 早停(repo 全局 grep `compile_passed` = **0 命中**)。
3. 你提的"early stop:build_passed 空 → 直接 rework"**目前不成立**——见上,空值落到 LLM。

➡️ 实质工作 = **把现有门从 verify 推到 build/compile + 让 missing 也算不过**,不是"补硬指标门"。

---

### ④ skill 链当工具调度 —— True(确是空白)

**说法**:"skill 从'agent 内部自己想起来调'升级为'协调层按 stage 确定性调度',覆盖率 100%。"

**事实**:准确,这正是当前状态。

- `engine/profile_loader.py:24` `StageConfig.skill: str = ""`(line 137 读 `stage_raw.get("skill","")`)。
- 唯一使用点 `prompt_renderer.py:339` 读 `skill`,在 line 372-377 / 404-409 把 `{skill_instruction}` 替换成字面提示词:**"在开始本阶段任务前,请先使用 Skill 工具调用 `{skill}`"**——即请 agent 自己调。
- `context/pack.py:36-37` 同样只是 append "## 建议调用 /{skill} 处理" 文本。
- `planner.py:875-958` 的 gate 只插**另一个 agent 重跑 action**(`build_repair_action` → 新 launch),没有任何确定性外部 skill/hook 调度。grep `subprocess/Popen/run_skill/invoke_skill/tool_gate` 在 `orchestrator/engine/` 仅命中 headless spawn + PTY supervisor。

➡️ 这条判断准,是从零写确定性 hook 层。

---

### ⑤ 异常自愈 —— True(且文档与实现矛盾)

**说法**:"recovery 只在 run_story 抛异常时触发,poll-timeout / escalate 不进 recovery。"

**事实**:准确,且发现一处 docstring 撒谎。

- `recovery.py:46-53` `decide_recovery` 签名强制 `exc: BaseException`——无 exception 对象无法调用。
- 唯一调用点 `graph.py:198-244`,在 `except Exception as exc:`(line 202)里包 `planner.continue_orchestrator_agent(story_key)`(line 200)。recovery 只在该调用 raise 时触发。
- planner 内所有失败路径**全用 `return status=failed`,不 raise**,因此都进不了 recovery:
  - poll-timeout:`planner.py:860-872`
  - headless 退出无 done:`planner.py:823-835`
  - headless spawn 失败:`planner.py:657-663`
  - CLI 启动失败:`planner.py:748-757`
  - verify-gate fail(escalate):`planner.py:951-957`
  - transition escalate/proceed/skip → `repair_action is None`:`planner.py:929-936`

**文档矛盾**:`recovery.py:3` docstring 自称覆盖"planner 轮询超时",但超时走 `return` 不 `raise`,根本到不了 `except`。docstring 是 aspirational,实现没兑现——动工时记一笔。

**done 兜底**:repo 全局 grep `fumble` = **0 命中**;`handshake` 只在 docstring 出现(`infra/paths.py:7`、`knowledge_store/scaffold.py:50`),无合成逻辑。现有的 `git diff` 兜底(`infra/benchmarks/artifacts.py:28-76` `extract_model_patch`)只给 swebench `finalize` 抽 patch(`validation.py:80,87-104`),跟 done 握手无关。**完全空白。**

➡️ 这是六块里唯一"完全不存在、需从零写"且直接决定"流程卡不卡死"的项。

---

### ⑥ 可观测 —— Partial(措辞修正)

**说法**:"event_log + PTY tap → SSE 推进度(哪个 agent 在哪个 stage、token 消耗、卡在哪)。"

**事实**:能力都在,但"SSE"措辞不准。

- **event_log**:表存在(`models.py:133-140`,列 `id/story_key/stage/event_type/payload/created_at`),写点 `models.py:739-750` `log_event` + ~10 个 typed writer(`observability/events.py:26-121`)+ 直接 `db.log_event` 调用(`policy_engine.py:360`、`shadow_router.py:359`、`auto_discovery.py:367`、`planner.py:923`)。跨 run 读取见 ①。
- **PTY tap**:`pty.py:147` `ManagedPty`,`add_tap()`(line 264)是 supervisor 侧信道;`supervisor.py:135-179` 消费(`await asyncio.wait_for(tap.get(), ...)`)。
- **实时推送 = WebSocket 为主,不是 SSE**:per-story `/ws/story/{story_key}`(`api.py:703`)、全局 `/ws/stories`(`api.py:151`)、PTY→浏览器 `/ws/pty/{story_id}`(`api.py:281,287`)。**只有 1 个真 SSE 端点** `/api/story/{story_key}/plan/stream`(`api.py:2627`,`StreamingResponse media_type=text/event-stream`,line 2647-2693)。
- **token 成本视图**:`models.py:885-941` `get_story_token_usage` 按 `by_stage` + `by_model` 拆,带 `cost_cny`;暴露于 `/api/story/{story_key}/stats`(`api.py:482-513`)。注意是 per-stage/per-model 聚合,非 per-PTY-session。

➡️ 推送基础设施齐(WS + 1 SSE),token 视图也有;缺的是把这些拼成"实时看板 + 智能打断点",不是从零建传输层。

---

## §2 优先级重排

原排序(基于描述):① playbook 飞轮 > ② 硬指标门 > ③ 异常自愈。

核查后,按"代码事实"重排:

| 优先级 | 块 | 核查后真实状态 | 为什么这个序 |
|---|---|---|---|
| **P0** | ⑤ 异常自愈 | 完全空白 + docstring 撒谎 | 唯一"从零写"且直接决定流程卡不卡死;poll-timeout/escalate 全走 `return failed`,无人值守必卡 |
| **P0** | ① playbook 飞轮 | 读路径已通到 transition,差持久化 + 接 judge | 性价比最高——补最后两段即闭环,且接 judge 后 ③ 的 LLM 段才有依据 |
| **P1** | ③ 硬指标门 | 已有两段式 judge,差 missing 也拦 + 推到 build/compile | 是**扩展**不是新建;且依赖 ① 把知识喂进 judge 后才算真补强,故排在 ① 后 |
| **P2** | ④ skill 确定性调度 | 完全空白 | 价值高但非卡点;hook 层可独立迭代 |
| **P2** | ② 跨 story 调度 | FIFO,真空白 | 价值高但需多 story 并发场景才兑现;当前并发上限 4,边际收益靠后 |
| **P3** | ⑥ 可观测增强 | WS+SSE+token 视图都在 | 拼装成看板 + 智能打断点;基础设施已就绪,非新建 |

**关键纠正**:原排序把 ③ 当成"最大可靠性缺口、从零补",核查后它是"扩展已有 judge";而原排序低估的 ⑤ 才是真正的"从零 + 卡点"。两者的序应互换。

---

## §3 下一步(每块的 file/函数级改造点)

> 仅列锚点,不展开实现;每块动工前先按符号名复核行号。

### §3.1 P0 · 异常自愈(从零写)

**问题**:`planner.py` 所有失败路径 `return status=failed` 不 raise → `graph.py:198-244` 的 `except` 永远不触发 → `recovery.py:decide_recovery` 是死代码。

**改造锚点:**

- `recovery.py:46-53` —— 放宽 `decide_recovery` 入参,接受"非异常失败信号"(enum:`exception | poll_timeout | escalate | no_done`)。
- `planner.py:860-872 / 823-835 / 951-957 / 929-936` —— 把"直接 `return failed`"改成"构造失败信号 → 走 recovery 决策"。先抽一个 `_terminal_failure(reason, ...)` 统一出口,避免散点改。
- `recovery.py:3` docstring —— 修正"覆盖 poll-timeout"的假宣称,或干脆让实现兑现它。
- **done 兜底**:新增 `done` 合成路径——agent 退出无 done 文件时,用 `git diff`(参考 `infra/benchmarks/artifacts.py:28-76` 现有 patch 抽取)兜底合成最小 done payload,标记 `synthesized=True`,让 judge 显式判而非直接 fail。

**验收**:构造 poll-timeout / escalate / no-done 三类用例,各跑一遍,确认都进 `decide_recovery` 而非直接 `failed`;done 兜底产出的 synthesized done 能被 judge 正常裁决。

**回归测试**(AGENTS.md 硬规则:历史 bug 必须有回归测试):为每类失败信号加 `tests/` 用例。

### §3.2 P0 · playbook 持久化 + 接 judge(补最后两段)

**问题**:`reflection.py:reflect()` 现算不积累;`judge.py` 完全不消费跨 story 知识。

**改造锚点:**

- 持久化:`reflection.py:25-118` 产出的规则,写进 `KnowledgeIndex`(scenario/playbook schema 已在 `knowledge` 包定义)。新增一个批量/调度 caller——目前 `reflect` 只在 verify-gate 现算,需要一个离线/定时累积入口。
- 接 judge:`judge.py:130-142` `_build_judge_prompt` 增加一个 `knowledge_section`(仿 `prompt_sections.py:125-136` `build_knowledge_section` 已有的 executor 侧注入),把"同类 story 常翻车点"喂给 LLM 段。注意只读 facts,judge 本身保持 pure(AGENTS.md:Decider 规则)。
- 边界:`KnowledgeIndex` 是 `knowledge` 包契约;story-lifecycle 通过 `knowledge/context_providers/` 消费(SOFT import)。持久化写入若跨包,确认方向不破坏"miner→knowledge→lifecycle"的飞轮单向。

**验收**:跑两个同类 story,第一个的失败规律在第二个的 judge prompt 里可见(而非只在 transition)。

### §3.3 P1 · 硬指标门扩展(扩已有 judge)

**问题**:`judge.py:32-33` 只认 `is False` 不认 missing;硬门只在 verify 跑。

**改造锚点:**

- `judge.py:46-51` —— 把 `is False` 收紧为"非真即不过"(显式区分 `None` 未跑 vs `False` 跑了没过,按需;但"missing → 不过"是安全默认)。
- build/compile 阶段早停:在 `planner.py` 对应 stage 出口(类比 `planner.py:875` 的 verify gate)加一个轻量硬指标检查;若 kimi 自报"没编译"且 `build_passed` 缺失,直接 rework 不浪费一轮 verify。
- 复用 §3.2 的 `compile_passed`(目前 repo 0 命中)——在 build 阶段 done 解析时抽取。

**验收**:kimi done-fumble 形态(build_passed 缺失)在 build 阶段即被判 rework,不进 verify。

### §3.4 P2 · skill 确定性调度 + 跨 story 调度

- **skill hook**:在 `planner.py:875-958` gate 之外,加一个 stage-transition hook 层(stage 完成 → 确定性触发 `prd-review-plan`/`build-check`/`code-standards-check`/`pre-release-review`),结果喂 judge。注意 AGENTS.md 触发清单第 3 条:先定义 `state x user_action -> action` 再写 handler 副作用。
- **跨 story 调度**:`scheduler.py:26-44` 扩展排序键——依赖感知(读 story 间声明依赖)、冲突检测(两个 story 改同文件 → 预警/合并)、资源(agent 配额 + 并发上限,目前硬编码 `max_workers=4`)。

### §3.5 P3 · 可观测拼装

- 把现有 WS(`/ws/story`、`/ws/pty`、`/ws/stories`)+ SSE(`/plan/stream`)+ token 视图(`/stats`)拼成统一看板。
- 加"智能打断点":协调层在"风险高 / 判断不确定"节点主动 `pause` 等人确认(对齐 mandatory flow),而非全跑完 review。

---

## 附录:file:line 证据索引

> 按块归类,符号名优先,行号辅助。漂移以符号名为准。

**① playbook 飞轮**
- `orchestrator/engine/planner.py:427-464` `_build_verify_history_facts`
- `orchestrator/engine/planner.py:910-922` 接线进 verify-gate
- `orchestrator/learning/reflection.py:25-118` `reflect` / `build_transition_history_facts`
- `orchestrator/engine/transition.py:69-73` `same_failure_swap_succeeded` 反馈点
- `infra/db/models.py:1258-1261` 全局 event 查询 + docstring
- `orchestrator/evaluation/judge.py:130-142` `_build_judge_prompt`(无 knowledge 段——缺口)

**② 跨 story 调度**
- `orchestrator/engine/scheduler.py:26-44` `decide_schedule`
- `orchestrator/engine/graph.py:22` `ThreadPoolExecutor(max_workers=4)`
- `orchestrator/engine/graph.py:369-395` `order_ready_stories`
- `orchestrator/engine/shadow_router.py:35,290-293` `CONSTRAINT_CONFLICT`(shadow-run,非调度器)

**③ 硬指标门**
- `orchestrator/evaluation/judge.py:46-60` 硬指标两段式
- `orchestrator/evaluation/judge.py:32-33` "missing 当未失败"注释(缺口)
- `orchestrator/evaluation/judge.py:62-77` LLM 段(硬指标过后)
- `orchestrator/evaluation/gate.py:215-240` `run_verify_gate`
- `orchestrator/engine/planner.py:875` `if stage == "verify":`(只在 verify 跑——缺口)

**④ skill 调度**
- `orchestrator/engine/profile_loader.py:24,137` `StageConfig.skill`
- `orchestrator/engine/prompt_renderer.py:339,372-377,404-409` prompt-only 注入
- `orchestrator/context/pack.py:36-37` 文本建议
- `orchestrator/engine/planner.py:875-958` gate(只插 agent 重跑,无确定性 hook)

**⑤ 异常自愈**
- `orchestrator/engine/recovery.py:46-53` `decide_recovery` 签名(强制 exc)
- `orchestrator/engine/recovery.py:3` docstring(与实现矛盾)
- `orchestrator/engine/graph.py:198-244` 唯一调用点(except 内)
- `orchestrator/engine/planner.py:823-835 / 860-872 / 951-957 / 929-936` return-failed 不 raise 的各失败路径
- `infra/benchmarks/artifacts.py:28-76` `extract_model_patch`(现有 git diff 兜底,swebench 专用,非 done 握手)

**⑥ 可观测**
- `infra/db/models.py:133-140` `event_log` schema
- `infra/db/models.py:739-750` `log_event` writer
- `infra/terminal/pty.py:147,264,163` `ManagedPty` / `add_tap` / `_taps`
- `orchestrator/engine/supervisor.py:135-179` tap 消费
- `orchestrator/service/api.py:703,151,281,287` WebSocket 端点
- `orchestrator/service/api.py:2627,2647-2693` 唯一 SSE 端点 `/plan/stream`
- `infra/db/models.py:885-941` `get_story_token_usage`(`by_stage`/`by_model`/`cost_cny`)
- `orchestrator/service/api.py:482-513` `/stats`

---

## 附:与现有文档的出入(动工时一并修正)

| 文档 | 位置 | 宣称 | 实际 |
|---|---|---|---|
| `recovery.py` docstring | `:3` | recovery 覆盖"planner 轮询超时" | 超时走 `return` 不 `raise`,进不了 recovery |
| (本 RFC §③ 纠正) | — | "judge 是纯 LLM" | 已是硬指标 + LLM 两段式 |

— 完。后续按 P0 → P1 → P2 → P3 推进,每块动工前先按符号名复核行号。
