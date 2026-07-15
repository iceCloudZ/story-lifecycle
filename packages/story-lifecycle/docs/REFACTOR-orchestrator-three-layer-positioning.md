# 编排器三层定位重构 — 设计文档

> 状态:已过一轮评审并采纳修订(2026-07-15)。创建:2026-07-14。
> 范围:`packages/story-lifecycle`(尤其 `orchestrator/` 层)。
> 评审目标:验证"为什么这么改"的理论依据 + 改动方案的正确性/风险。
> 本文自包含:理论依据、代码现状、改动方案全部内联,评审者无需读对话历史。

---

## 0. TL;DR(评审者先读这段)

本文档论证 story-lifecycle 编排器在"GPT-5.6 时代模型原生能力变强"的背景下,**哪些编排逻辑该砍、哪些该保留、哪些该加强**,并给出具体代码改动方案。

核心论点:**编排器的持久价值不在"我比 worker 模型更聪明"(能力差),而在"我看到 worker 看不到的信息"(信息差)**。前者会被模型对称升级吃掉,后者不会。

基于这个论点,把编排器代码切成三层:

| 层 | 当前代码 | 处置 | 理由 |
|---|---|---|---|
| ① 替模型思考 | FC 规划循环 / if/else 决策机 / 分步 verify-gate | **砍** | 模型原生能力已覆盖,且不随模型升级 |
| ② 帮模型执行 | 接力拓扑 / done-gate / per-stage adapter 路由 | **保留** | 模型做不了上下文隔离 + 模型分工 |
| ③ 跨 session 持久化 | reflect(当前是死代码) | **加** | 模型无状态,经验沉淀是独立护城河 |

**文档结构**:§1 起因与外部压力 → §2 理论依据(三篇 arxiv + GitHub 实证)→ §3 代码现状(带 file:line + 代码片段)→ §4 三层定位论证 → §5 改动方案(分阶段)→ §6 风险与开放问题。

---

## 1. 起因与外部压力

### 1.1 触发事件

知乎有一篇流传文章 [《为什么 Codex 搭载 GPT-5.6 后,越来越多用户开始弃用 Skills?》](https://www.zhihu.com/question/2060378207459566963/answer/2060386633493504141),论点是:GPT-5.6 原生规划/工具调用/子 Agent 调度能力变强后,"常驻通用流程型 Skills"(以 Superpowers 为代表)变成冗余开销。

这引发一个合理的担忧:**story-lifecycle 这种"编排器"会不会也是同类的冗余中间层?**

### 1.2 必须区分的两个概念

文章批评的是 **"常驻、通用、强制注入流程的 Skill"**,特征是:

1. 常驻:挂进每个 session,无差别占上下文
2. 通用:不挑任务,任何活都先走它那套流程
3. 强制注入:把"先规划→再拆→再验证"的脚手架硬塞给模型

而 story-lifecycle 是 **FC 编排器**——通过 function-calling 调用模型,不是注入进模型上下文的 skill。两者在架构栈的不同层。但 story-lifecycle **内部确实有一部分逻辑**(`plan_step`/`skip_stage` FC 工具、`decide_transition` if/else 决策机)在干"替模型思考"的事——**这部分落在文章批评的类别里,是本重构要砍的**。

### 1.3 GPT-5.6 的事实核实

- GPT-5.6 于 2026-07-09 全面开放(Sol/Terra/Luna 三档),上下文窗口约 150 万 tokens,优先喂给 Codex。([OpenAI 官方](https://openai.com/zh-Hans-CN/index/gpt-5-6/))
- 被 sunset 的是 GPT-5.2/5.3-Codex 等**旧模型版本**([GitHub Changelog](https://github.blog/changelog/2026-06-05-gpt-5-2-and-gpt-5-2-codex-deprecated/)),**不是 Skills 机制或 Codex agent 能力本身**。
- 全网 Skills 生态在涨(Anthropic 官方 17 个核心 + 社区 8 万+ skill 包)。被弃用的是"常驻通用流程型"子类,不是整个 Skills 机制。

---

## 2. 理论依据(评审重点)

本节给出支撑"三层定位"的硬证据。**如果这些依据被推翻,整个重构方向就要重新审视。**

### 2.1 核心命题:能力差 vs 信息差

**命题**:编排器-模型相对 worker 模型的持久优势,是**信息差**(看到 worker 看不到的信息),不是**能力差**(规划/推理更聪明)。

**为什么能力差站不住**——对称升级问题:

- 模型升级让**编排器-模型**规划能力 +30%
- 但同时让 **worker 模型**(claude/codex/kimi)规划能力也 +30%
- 两者之间的**相对差距不变**,甚至缩小(worker 自己越来越会规划)

所以光靠"我规划得比 worker 好",长期会被对称升级吃掉。

**为什么信息差站得住**——以下信息只有编排器持有,worker 无论多强都看不到:

| 编排器持有 | worker 为什么看不到 |
|---|---|
| 跨阶段视角(design 决定了 X,build 撞上 Y) | 任何单个 worker 只在自己的 stage 上下文里,不持有其他 stage 的因果链 |
| 历史 story 的 playbook(经验沉淀) | 模型无状态,每个 session 是 fresh context |
| adapter 路由决策("build 该用 kimi") | worker 无法对自己做模型切换决策 |
| 持久化能力(写 DB / 写 done / 跨 session) | 模型无持久状态,天生没有 |

**回应"上下文管理也是一种能力差"的反论**:有人会说接力机制的上下文切割(防 Lost in the middle)是编排器的"能力差"优势,也会被模型吃掉(GPT-5.6 自己也能学会切)。这个反论要拆开看——"切割"有两个动作:

| 动作 | 性质 | 被对称升级吃掉? |
|---|---|---|
| **A. 会切**(把任务分成 design/build/verify) | 通用能力 | ✅ 被吃。worker 自己也能判断"先设计再实现" |
| **B. 切在哪 + 各阶段交接什么** | 信息差 | ❌ 吃不掉。需要跨阶段视角 |

B 才是接力的核心价值:决定"切在哪、done.json 交接什么字段"需要知道**下游需要什么**,而下游还没跑,它的需求只有编排器(持有 profile + 跨阶段视角)能预判。单个 worker 不知道自己产出的东西里哪些对下游有用。所以"上下文管理"表面是能力,实质还是信息差——二分法不破。

### 2.2 arxiv 硬证据

#### 2.2.1 [Single-Agent LLMs Outperform MAS on Multi-Hop Reasoning (arXiv:2604.02460)](https://arxiv.org/abs/2604.02460)

> 用信息论(Data Processing Inequality)证明:Multi-agent 系统变得有竞争力,**当且仅当单 agent 的 effective context utilization 退化**;如果单 agent 能完美利用上下文,单 agent 信息论上更高效。

**翻译到本项目**:如果编排器-模型和 worker 看到的信息一样多,信息论上单 agent 更优,编排器冗余。编排器有存在理由,**仅当它消费 worker 看不到的信息**(跨阶段 + 历史)。

**DPI 对"多 CLI 接力"形态的适用性论证**:有人可能质疑——论文讨论的是"单 agent vs 多 agent 实时协同",而接力介于两者之间(多 agent 但不实时协同,只交接落盘产物)。这个适用性恰恰是接力的护城河:接力通过 `done.json` **硬交接**,跨阶段信息必然损耗(下游 worker 拿到的不是上游完整对话,是编排器挑出来写进 done 的摘要)。因此**单 agent 无法完美利用跨阶段上下文**(DPI 的前提条件"perfect context utilization"不满足),接力形态才有存在价值。这反过来证明:编排器持有"跨阶段视角"的信息差是**必要的**,不是可选优化。

**对评审的意义**:这两条把"信息差"论点从直觉升级为信息论证明。如果评审者要推翻本重构,需要先推翻 DPI 论证,或证明接力机制能让单 worker 完美利用跨阶段上下文(当前 done.json 硬交接决定了这不可能)。

#### 2.2.2 [When Single-Agent with Skills Replace MAS (arXiv:2601.04748)](https://arxiv.org/abs/2601.04748)

> Multi-agent 系统可被等价编译成单 agent + skills(skill selection 替代 agent 间通信)。但 LLM 的 skill selection 有**有界容量**——不是渐变退化,是到临界库规模后**急剧崩溃(相变)**。原因是语义混淆(semantic confusability)。解法是**分层路由(hierarchical routing)**。

**翻译到本项目**:用户预期的"模板字典/playbook 越跑越厚"——如果**平铺**给模型选,厚到一定程度会相变崩溃。必须做**分层路由**(按 task_type 分目录等),让模型永远只在自己 task_type 的子集里选。

**对评审的意义**:这条决定了 §5 改动方案里 playbook 的组织方式——必须分层,不能平铺。

#### 2.2.3 [A Self-Improving Coding Agent (SICA, arXiv:2504.15228, 56 引用)](https://arxiv.org/abs/2504.15228) + [Survey of Self-Evolving Agents (arXiv:2507.21046)](https://arxiv.org/html/2507.21046v4)

> Agent 系统可以通过经验积累自我改进,"飞轮"模式(execute → coach → distill → improve)能在跨 session 复利。

**翻译到本项目**:`reflect()` 沉淀 playbook → 落库 → 下一个 story 读回,正是这种飞轮。当前 `reflect()` 是纯内存函数,跑完即丢,飞轮没转起来。

### 2.3 GitHub 实证:"通用 LLM 路由"已红海,但"经验路由"空白

- [ypollak2/llm-router](https://github.com/ypollak2/llm-router):universal LLM router,支持 Claude/Codex/Gemini,free-first fallback
- GitHub topic [llm-orchestration](https://github.com/topics/llm-orchestration):一堆同类
- Tyler Folkman 实验:2415 个 agent turn 路由到 6 个模型,$76.77([substack](https://tylerfolkman.substack.com/p/i-tested-6-ai-models-across-3-providers))
- LiteLLM/Bifrost/Cloudflare 等通用 gateway

**关键区分**:

| 路由类型 | 是否稀缺 |
|---|---|
| 按 token/成本/可用性路由(通用 LLM router 做的) | ❌ 红海,不稀缺 |
| 按"历史经验里哪个模型在哪个 stage 哪类任务上成功率高"路由 | ✅ 无人做,`reflect()` 的独有空间 |

story-lifecycle 的 `reflect()` 已经在沉淀"adapter X 失败→换 Y 成功"——这是把不值钱的通用路由变成值钱的经验路由的关键原料,只是当前没落库。

### 2.4 编排器重心转移的业界共识

[quant67 Agent 框架工程](https://quant67.com/post/llm-infra/19-agent-framework/19-agent-framework.html)总结 2026 趋势:

> "单 agent + 强推理模型 + MCP 工具,胜过多 agent + 弱模型 + 复杂编排。框架的重心已从'帮模型思考'转向'帮模型执行':工具路由、沙箱、可观测、记忆。"

**这条直接命中本重构的三层划分**:要砍的是"帮模型思考"(§4 第①层),要保留加强的是"帮模型执行"(§4 第②层)。

---

## 3. 代码现状(评审者据此判断改动方案可行性)

> 本节所有 `file:line` 基于 2026-07-14 代码。`src/story_lifecycle/` 简写为 `SL/`。

### 3.1 第①层现状:替模型思考(待砍)

#### (a) FC 规划循环 — `planner.py:174-357`

`run_orchestrator_agent` 用 10 轮 `invoke_with_tools` 循环收集 `plan_step`/`skip_stage` tool calls,翻译成 action list:

```python
# planner.py:174-357 (节选核心循环)
actions = []
llm = get_llm()
max_rounds = 10
for round_idx in range(max_rounds):
    resp = llm.invoke_with_tools(messages, ORCHESTRATOR_TOOLS, tool_choice="auto", ...)
    ...
    for tc in tool_calls:
        if name == "plan_step":
            action = {"action": "launch", "adapter": args.get("adapter","claude"),
                      "stage": args.get("stage",""), "focus": args.get("focus",""), ...}
            actions.append(action)
        elif name == "skip_stage":
            action = {"action": "skip", "stage": args.get("stage",""), ...}
            actions.append(action)
```

配套的 system prompt(`planner.py:128`)自己都承认:"CLI(claude/codex/kimi)会自己理解需求并设计方案,**你不需要代劳**"——那这层 FC 收集就是冗余的中间人。

#### (b) if/else 决策机 — `transition.py:30-92`

verify-gate 失败后,`decide_transition` 用纯 if/else 选修复动作:

```python
# transition.py:30-92 (节选)
def decide_transition(*, gate_decision, failure_mode, history_facts=None) -> dict:
    if _gate_passed(gate_decision):
        return {"action": "proceed", ...}
    if failure_mode == "missing_dependency":
        return {"action": "insert_rescue_stage", "rescue_stage": "setup_dependency", ...}
    if history_facts.get("same_failure_swap_succeeded"):
        return {"action": "swap_approach", ...}
    if repeat >= max_retries:
        return {"action": "escalate", ...}
    return {"action": "retry", ...}
```

`failure_mode` 的判定尤其糙——靠 reason 文本关键字嗅探(`planner.py:1366-1374`):

```python
# planner.py:1366-1374
reason_text = str(gate_result.get("reason", "")).lower()
failure_mode = ("missing_dependency"
                if any(k in reason_text for k in ("depend","import","no module","no such file"))
                else "quality")
```

**这套 if/else 不随模型升级**。GPT-5.6 再强,它还是那四个硬编码规则,且看不到 verify gate 的完整证据(findings/judge_verdict 都没喂给它)。

#### (c) 分步 verify-gate — `gate.py:192-325` + `transition.py`

当前 verify 阶段质量验证是**三次往返**:

```
verify done.json → gate.py(judge 复核,LLM 调用 #1)
                 → gate.py(HIGH finding 检查,规则查 DB)
                 → 合并算出 decision ∈ {advance, retry, fail}
                 →[retry 时]→ transition.py decide_transition(if/else,无 LLM)
```

judge 复核和 decide_transition 都是质量决策,却分两次处理。judge 看"合不合格"时已经在看失败原因,让它同时判"怎么救"几乎零成本,且能让"怎么救"看到完整证据。

### 3.2 第②层现状:帮模型执行(待保留)

#### (a) 接力拓扑 — 每 stage 独立 PTY + done 后 kill

```python
# pty.py:500-519 — 每 stage 新建 PTY,只注入 cli_prompt
session_id, pty = spawn_pty(story_id, command, cwd, env=env, purpose="agent")
if prompt:
    pty.write(b"\x1b[200~" + prompt.encode("utf-8") + b"\x1b[201~")
    pty.write(b"\r")

# planner.py:1160-1180 — done 后 kill 进程,不保留给下一 stage
if _agent_pty is not None:
    clean_exit_pty(_agent_pty)
    _agent_pty.kill()
```

价值:design 阶段的发散讨论/试错**不污染** build 阶段。下个 CLI 启动是 fresh context。

#### (b) done-gate 强制 checkpoint

```python
# planner.py:1018-1133 — 轮询 done file,出现才视为 stage 完成
done_path = Path(workspace) / done_file_rel
while elapsed < poll_timeout:
    if done_path.exists():
        done_data = robust_json_parse(done_path)
        db.log_event(story_key, stage, "completed", done_data)
        break
```

done.json 是阶段间唯一硬握手——没写 done 就进不了下一阶段。这是编排层定义的协议,模型不会主动写。

#### (c) per-stage adapter 路由 — `realtest.yaml`

```yaml
# entry/profiles/realtest.yaml — 不同 stage 用不同模型强项
design:
  cli: claude   # 原生加载 skills + brainstorming 澄清需求
build:
  cli: kimi     # 用户确认:编码用 kimi(确定可跑通)
verify:
  cli: claude   # judge 复核
```

profile 兜底覆盖逻辑(`planner.py:680-697`)保证 profile 的 stage→cli 映射优先级最高,LLM 选的 adapter 被覆盖。

**这三件事模型吃不掉**:长 context 必然漂移(隔离有价值),done-gate 是协议不是能力,模型无法对自己做 adapter 切换。

### 3.3 第③层现状:跨 session 持久化(待加,当前是断的)

#### (a) reflect() — 能沉淀但跑完即丢

```python
# learning/reflection.py:25-76 — 从 event_log 沉淀 playbook
def reflect(*, events: list[dict]) -> dict:
    # 只沉淀一种规则:adapter swap
    # recovery_action(retry_new_adapter) + 后续 pass → "adapter X 失败 → 换 Y 成功"
    ...
    return {"playbook": [{"rule": ..., "support": cnt, "evidence": ...}], "stats": ...}
```

**问题**:返回 dict,不落 DB、不落文件。唯一 caller `build_transition_history_facts`(reflection.py:86-118)只取 playbook 喂给 `decide_transition`,跑完即丢。**没有任何地方把 reflect 的 playbook 落盘。**

#### (b) 死代码:知识加载函数从未被调用

```python
# planner.py:25-42 — 读团队级 + story 级知识
def _load_team_knowledge() -> str:
    knowledge_dir = STORY_HOME / "knowledge"
    ...  # grep 确认:在 planner.py 内零调用

def _load_story_knowledge(workspace, story_key) -> str:
    ...  # grep 确认:在 planner.py 内零调用
```

编排器-模型本该用上"跨阶段 + 历史经验"的特权视角,但这两个函数是死代码,它在用和 worker 差不多的信息重新推导 plan。

#### (c) playbook 消费端 — 已存在但要求冲突

两条消费路径,文件名规则冲突:

| 路径 | 代码 | 目录 | 文件名规则 | 读内容? |
|---|---|---|---|---|
| design 维度引导 | `prompt_sections.py:266-298` | `.story/knowledge/playbooks/` | 写死 `security-parameter-trust.md`/`degradation-fallback.md` | ❌ 只检查 exists,塞路径让模型自查 |
| kb.py 查询 | `kb.py:147-170` | `$HC_STORY_KNOWLEDGE/playbooks/` | `<task_type>.md`,task_type ∈ 12 类白名单 | ✅ 读全文,截 `## 高频`/`## 常见失败`/`## 常用操作` 段 |

reflect 产出的经验要进哪条路、怎么组织,直接决定分层路由设计(见 §5)。

---

## 4. 三层定位论证

### 4.1 分层总览

```
┌── 第①层:替模型思考(GPT-5.6 原生能力吃掉)──────────────┐
│  plan_step/skip_stage FC 工具循环(agent_tools.py / planner.py)│
│  decide_transition if/else 决策机(transition.py)            │
│  分步 verify-gate(gate.py judge + transition.py 分两次)      │
│  → 砍:FC 循环改单次 invoke_structured;gate 重写为一次 LLM   │
└──────────────────────────────────────────────────────────┘
┌── 第②层:帮模型执行(模型做不了,保留)──────────────────┐
│  接力拓扑:per-stage PTY + done 后 kill(pty.py / planner.py) │
│  done-gate:done.json 硬握手 + confirm 闸 + verify gate       │
│  per-stage adapter 路由:profile stage→cli(realtest.yaml 等)│
│  → 一个不动,这是编排器不被模型吃掉的核心                     │
└──────────────────────────────────────────────────────────┘
┌── 第③层:跨 session 持久化(模型永远做不了,加)──────────┐
│  reflect() → 落库 → 下个 story 读回(当前断了,要接通)        │
│  task_type 分层路由(防 skill 库相变崩溃)                    │
│  → 加:飞轮闭环是编排器-模型保住信息差优势的唯一手段          │
└──────────────────────────────────────────────────────────┘
```

### 4.2 为什么第②层一个不动(回应"误伤接力机制"的担忧)

最容易被误读的点:砍第①层的 FC 规划,会不会连带砍掉多 CLI 接力?

**不会,因为两者在不同层。** 对比:

| 第①层(砍) | 第②层(保留) |
|---|---|
| 替模型决定"要几个阶段、各用什么 CLI" | 拓扑上怎么 spawn 多个 CLI 接力 |
| 中间表示(action list 的 FC 收集) | done-gate checkpoint 协议 |
| if/else 决定"失败后干嘛" | per-stage adapter 路由(profile 定义) |

砍第①层 = 减"替模型动脑的代码"。保留第②层 = 管"几个 CLI 怎么接力"。**砍的是决策逻辑,不是执行拓扑。**

尤其 per-stage adapter 分工(design=claude/build=kimi/verify=claude)是接力机制的独有价值——GPT-5.6 再强,它不会在 build 阶段"把自己换成 kimi"。这是基于"哪个模型在哪件事上强"的实战判断,不是通用能力。

### 4.3 为什么第③层是命根子

回到 §2.1 的核心命题:编排器-模型要保住对 worker 的相对优势,必须手里有 worker 没有的牌。那些牌只能来自跨 session 的经验沉淀。

当前代码的洞(`§3.3`):`reflect()` 跑完即丢,`_load_team_knowledge` 是死代码。编排器-模型手里握着"跨阶段视角 + 历史经验"的王牌,但没打出来。

**所以第③层不是锦上添花,是编排器-模型长期不被对称升级吃掉的唯一手段。**

---

## 5. 改动方案

### 5.0 总览与实施顺序

```
阶段1 reflect 落库 ──独立──→ 可先做,验证"越跑越厚"链路通
    ↓
阶段2 打通消费端 ──依赖阶段1──→ playbook 有东西可读
    ↓
阶段3-4 重写 verify-gate ──依赖阶段1──→ 读 playbook 作 context
    ↓ (独立)
阶段5 轻量砍 FC ──不依赖──→ 可与 1-4 并行
```

**阶段 1 是试金石**——做完看飞轮是否真转。如果飞轮不转(经验沉淀没有实际收益),整个第③层方向要重新评估,连带影响阶段 3-4。

#### 5.0.1 阶段 1 的量化验证标准(KPI)

没有量化指标,阶段 1 的验证会流于主观感受。KPI 分两层:

| 层次 | 指标 | 何时能看 | 判定标准 |
|---|---|---|---|
| **过程指标**(冷启动期) | playbook 是否生成、是否被下个 story 读取、分层目录结构是否正确 | 单 story 跑完即可 | 链路通:文件生成在正确 task_type 子目录 + 下个 story 的 prompt 里能找到引导行 |
| **效果指标**(需积累样本) | 经验利用率(playbook 命中率)、止损效率(同 failure-pattern 平均 retry 下降)、Swap 成功率(基于 playbook 的模型切换 vs 盲切) | 同 task_type 积累 3-5 个 story 后 | 对比有无 playbook 的 story,retry 次数/Swap 成功率有正向变化 |

**两层不能混**:冷启动期(前几个 story)只能看过程指标,这时 task_type 子目录还是空的,效果指标无意义。强行看效果指标会误判"飞轮没转"。

#### 5.0.2 冷启动过渡

初期没有历史经验,第③层信息差不存在。过渡策略:

- **全局手写 playbook 兜底**:`security-parameter-trust.md` / `degradation-fallback.md` 等手写 spec 蒸馏的维度 playbook 在根目录,所有 task_type 共享。冷启动期靠这些兜底。
- **task_type 子目录是增量层**:reflect 产出的经验按 task_type 落子目录,前期是空目录(消费端 fallback 到全局),积累后才显出分层价值。
- **经验估计**:同 task_type 积累 **3-5 个 story** 后,该子目录开始有参考价值(这是经验值,非精确数——取决于 story 复杂度和失败模式重复度)。在此之前,飞轮的"转"体现在过程指标(链路通),不是效果指标(复利)。

### 5.1 阶段 1:reflect 落库(飞轮写端)

#### 5.1.1 扩展 reflect() 沉淀规则

`SL/orchestrator/learning/reflection.py:25-76` 当前只沉淀 adapter-swap。扩展识别更多事件模式:

```python
def reflect(*, events: list[dict]) -> dict:
    # 现有规则(保留):
    #   adapter-swap: recovery_action(retry_new_adapter) + 后续 pass
    #     → "adapter X 失败 → 换 Y 成功"
    #
    # 新增规则:
    #   failure-pattern: 同 stage 反复失败(transition_decision.action=retry 连续 N 次)
    #     → "stage X 在 task_type Y 上高频失败,原因 Z"
    #   rescue-success: insert_rescue_stage 后 pass
    #     → "缺依赖 D 时,插 setup_dependency 能解"
```

每条 rule 加 `dimension` 字段(adapter-routing / failure-pattern / rescue),供落库时分类到不同文件。

**Q3 回答(原因提取)**:直接存 reason 原文 + 当前 adapter,不做结构化抽取。理由:后续 playbook 是 LLM 消费(playbook 文件读进 prompt 或被 verify-gate 引用),LLM 自己能理解自然语言,不需要在落库阶段强行结构化。强行抽取反而可能丢失关键上下文(比如 reason 里"因为 X 服务还没上线 mock"这种条件信息,抽取成"缺 mock"就丢了"X 服务"的指向)。

#### 5.1.2 新增 write_playbook_file()

`SL/orchestrator/learning/reflection.py` 新增:

```python
def write_playbook_file(
    *, workspace: str, task_type: str, dimension: str, playbook: list[dict]
) -> str | None:
    """把 reflect 的 playbook 按 task_type 分层落盘。
    路径: <workspace>/.story/knowledge/playbooks/<task_type>/<dimension>.md
    去重: 按结构化 key(task_type, stage, failure_mode, adapter)合并,
          support 累加;reason 存原文(取最新)。
    best-effort: 写失败只 warning,不影响 story 完成。
    """
    from ...infra.story_paths import safe_story_path
    path = safe_story_path(
        workspace, ".story", "knowledge", "playbooks", task_type, f"{dimension}.md"
    )
    # 读现有 → 按 key 合并(support 累加,reason 取最新)→ 写回
```

**去重策略:结构化 key 计数(不是文本匹配)**

反复跑同类 story 会累积相同模式的条目(比如 credit-limit 的 build 阶段 codex 失败换 claude,跑 5 个 story 产生 5 条)。纯文本去重不稳——两次跑同一个 bug,reason 措辞不会完全一样。改用结构化 key:

```
key = (task_type, stage, failure_mode, adapter)   # 全是固定枚举,匹配成本极低
value = { rule 文本, support(累加), evidence }
```

同 key 的条目合并成一条,`support` 累加。LLM 读到 `support: 5` 比看 5 条措辞略有不同的 rule 更省 context、信息密度更高。**分层和计数是两件事**:分层(按 task_type 分目录)是路由用的(防相变),计数(同 key 累加 support)是同目录内合并用的(防膨胀)。

**分层设计(§2.2.2 的工程落地)**:

```
.story/knowledge/playbooks/
├── security-parameter-trust.md      ← 全局维度(手写 spec 蒸馏,现有)
├── degradation-fallback.md          ← 全局维度(现有)
├── credit-limit/                    ← task_type 子目录(新增,冷启动期为空)
│   ├── adapter-routing.md           ← reflect 产出(support 累加)
│   ├── failure-patterns.md          ← reflect 产出
│   └── rescue.md                    ← reflect 产出
├── fund-flow/
│   └── ...
```

模型查询时只看"当前 task_type 的子集 + 全局维度",永远不面对全量 playbook——这是防相变崩溃(arxiv 2601.04748)的工程手段。金融 app 的 task_type 是有限的(当前 12 类白名单),分层天然成立。

#### 5.1.3 挂触发点

在 `_write_retrospect` 的两个 completed 触发点旁加 reflect 落库:

```python
# planner.py:1272(终态分支)和 planner.py:1433(主循环完成)
_write_retrospect(workspace, story_key, actions)
# 新增:
_persist_playbook(workspace, story_key)
```

`_persist_playbook` 复用 `_build_verify_history_facts`(planner.py:488-525)已有的事件查询逻辑,调 reflect + write_playbook_file。best-effort,只在 completed 路径触发(failed 不触发,与 retrospect 语义一致)。

### 5.2 阶段 2:打通消费端 + 分层路由

#### 5.2.1 接通死代码

`_load_team_knowledge` / `_load_story_knowledge`(planner.py:25-42)在 `_build_agent_system_prompt`(planner.py:106-144)接通:

```python
def _build_agent_system_prompt(*, profile_stages=None, story_title="", story_key="") -> str:
    team_kb = _load_team_knowledge()  # 接通:全局维度 playbook
    # 拼进 system prompt 的"团队记忆"段
```

当前截断逻辑(前 500 字)太短,改成按 task_type 过滤 + 按维度分段。

#### 5.2.2 build_design_dimensions_section 支持分层

`prompt_sections.py:266-298` 当前 `_DIMENSION_PLAYBOOKS` 写死两个全局文件名。改成读全局 + task_type 特定:

```python
def build_design_dimensions_section(story_key, workspace, stage, *, interactive=False):
    task_type = _get_task_type(story_key)  # 复用 build_kb_tool_section:111 的查询逻辑
    _DIMENSIONS = [("安全", "security-parameter-trust.md"),
                   ("降级兼容", "degradation-fallback.md")]
    _guides = []
    for dim, fname in _DIMENSIONS:
        p_global = playbooks_dir / fname               # 全局(手写 spec)
        p_task = playbooks_dir / task_type / fname if task_type else None  # reflect 产出
        if p_global.exists():
            _guides.append(f"- **{dim}**(通用):先读 `{p_global}`")
        if p_task and p_task.exists():
            _guides.append(f"- **{dim}**(本任务类型历史经验):读 `{p_task}`")
    # reflect 产出的新维度(adapter-routing / failure-patterns)也引导
    ...
```

向后兼容:全局 playbook(根目录)保留,task_type 子目录是新增层。

### 5.3 阶段 3-4:重写 verify-gate 为一次 LLM

#### 5.3.1 当前分步问题(评审重点)

现在 verify-gate 是三次往返(§3.1c):judge 复核(LLM #1) → finding 检查(规则) → decide_transition(if/else)。

**洞察**:judge 看的是"合不合格",decide_transition 看的是"不合格怎么救"——但判断"合不合格"时模型本来就在看失败原因和证据,让它同时判"怎么救"几乎零成本,且能让"怎么救"看到完整证据。

#### 5.3.2 新增 unified gate

新文件 `SL/orchestrator/evaluation/unified_gate.py`:

```python
from pydantic import BaseModel
from typing import Literal, Optional

class RepairAction(BaseModel):
    kind: Literal["retry", "swap_approach", "insert_rescue_stage", "escalate"]
    reason: str
    new_adapter: Optional[str] = None      # swap 时模型指定(替硬编码 _SWAP_ADAPTER_ORDER 轮转)
    rescue_stage: Optional[str] = None

class VerifyGateDecision(BaseModel):
    verdict: Literal["pass", "rework"]
    decision: Literal["advance", "retry", "fail"]
    findings: list[dict] = []
    reason: str
    repair_action: Optional[RepairAction] = None

def run_unified_verify_gate(
    *, story_key, stage, workspace, context, quality_cfg, max_retries,
    done_data, adapter_name,
) -> dict:
    """一次 LLM:质量判断 + finding 识别 + decision + repair_action。"""
    # 1. 组装完整证据包
    task_type = context.get("task_type", "")
    open_findings = db.find_open_findings(story_key, severity="HIGH")
    history_playbook = _load_playbook_for_verify(workspace, task_type)  # 阶段1产出

    evidence = {
        "done_summary": done_data.get("summary", ""),
        "files_changed": done_data.get("files_changed", []),
        "open_high_findings": open_findings,
        "history_playbook": history_playbook,
        "retry_count": context.get("_verify_round", 1),
        "max_retries": max_retries,
        "current_adapter": adapter_name,
        "available_adapters": ["claude", "codex", "kimi"],
    }

    # 2. 一次 LLM
    prompt = _build_unified_gate_prompt(evidence)
    try:
        decision = get_llm().invoke_structured(prompt, VerifyGateDecision, temperature=0.1)
        return decision.model_dump()
    except Exception:
        return _fallback_gate_decision(evidence)  # 见 5.3.3
```

#### 5.3.3 保留 if/else 作 fallback(明确策略)

`transition.py:decide_transition` 不删,改名 `_fallback_transition`。LLM 失败时降级。**Q4 已定:fallback 必须保留 HIGH finding 存在性检查,不能盲目 retry。**

```python
def _fallback_gate_decision(evidence: dict) -> dict:
    """LLM 不可用时降级。区分两种失败:
    - 检测到 HIGH finding 存在(查一次 DB)→ escalate 转人(不掩盖质量问题)
    - 纯 LLM 基础设施抖动(超时/坏JSON)→ retry(不打扰人)
    """
    # 1. HIGH finding 存在性检查(必须保留,防止把真实质量问题当抖动)
    open_high = db.find_open_findings(evidence["story_key"], severity="HIGH")
    if open_high:
        return {"decision": "fail", "reason": f"HIGH finding 存在({len(open_high)}条),转人",
                "repair_action": {"kind": "escalate", "reason": ...}}
    # 2. 无 HIGH finding → 默认 retry,超限才 escalate
    if evidence["retry_count"] >= evidence["max_retries"]:
        return {"decision": "fail", "reason": "retry 超限", ...}
    return {"decision": "retry",
            "repair_action": {"kind": "retry", "reason": "LLM 抖动,默认重试"}}
```

**为什么是这个策略(不是纯极简 retry)**:review 指出一个关键风险——如果遇到真实 HIGH finding(比如安全漏洞)还盲目 retry,等于把质量问题当基础设施抖动处理,**会掩盖 bug**。所以 fallback 必须先查一次 DB 确认有无 HIGH finding:有就 escalate 转人(质量问题不该自动 retry),没有才按 LLM 抖动处理(retry)。

**为什么保留 fallback 而非直接 escalate**:verify-gate 触发点在 story 尾部(design+build 已跑完),LLM 一次抖动不该废掉前面所有工作。代码库已有先例:`_build_verify_history_facts`(planner.py:488-525)本身就是 `try/except → 安全兜底`。

#### 5.3.4 无灰度 + planner.py 调用点切换 + 字段映射

**无灰度**:直接替换,不做 A/B 对比。理由:完整 A/B 需要两套 gate 并存 + 跑足够样本统计准确率,对个人项目工程量过大。替代保障是 `gate_result` 字段严格对齐(下方映射表)+ fallback 路径兜底(§5.3.3)。出问题能靠 fallback 降级,不需要双跑对比。

`planner.py:1342-1425` 的 verify-gate 块改成调 `run_unified_verify_gate`,直接读 `decision["repair_action"]` 转 action dict insert。

**字段映射表(实现时对照)** — 旧 `transition_decision` → 新 `VerifyGateDecision.repair_action`:

| 旧 (transition_decision) | 新 (RepairAction) | 备注 |
|---|---|---|
| `action` | `kind` | 改名(action→kind);值域去掉 proceed/skip(那两个在 gate 外层处理) |
| `reason` | `reason` | 不变 |
| `rescue_stage` | `rescue_stage` | 不变(仅 insert_rescue_stage 带) |
| (无,靠 `_SWAP_ADAPTER_ORDER` 硬编码轮转) | `new_adapter` | **新增**:swap 时模型基于 playbook 指定,替硬编码环形轮转 |

**关键差异——swap 的 adapter 选择**:旧版用 `transition.py:114` 的 `_SWAP_ADAPTER_ORDER = ("codex","claude","kimi")` 环形轮转(不看上下文),新版让模型基于历史 playbook 指定 `new_adapter`(这正是 §2.3 经验路由的落地点)。这是 unified gate 相对旧 decide_transition 最大的能力提升点。

### 5.4 阶段 5:轻量砍 FC 规划

#### 5.4.1 run_orchestrator_agent 从 FC 循环 → 单次 invoke_structured

`planner.py:174-357` 的 10 轮 FC 循环改成单次结构化调用:

```python
class StagePlan(BaseModel):
    stage: str
    skip: bool
    focus: str
    # adapter 不让模型选——由 profile 路由(护城河不动)

class PlanResult(BaseModel):
    stages: list[StagePlan]

def run_orchestrator_agent(story_key, *, on_action=None) -> dict:
    profile_stages = _load_profile_stages(...)
    team_kb = _load_team_knowledge()  # 接通死代码(阶段2)
    prompt = _build_planning_prompt(story_title, requirement, profile_stages, team_kb)
    try:
        result = get_llm().invoke_structured(prompt, PlanResult)
        actions = _plan_result_to_actions(result, profile_stages, story_key)
    except Exception:
        actions = _default_actions(profile_stages, story_key)  # fallback:全跑默认阶段
    ctx["_agent_actions"] = actions
    ctx["_plan_confirmed"] = False
    db.update_story(story_key, context_json=json.dumps(ctx), status="planning")
    return {"status": "planning", "actions": actions}
```

#### 5.4.2 关键边界:接力机制不动(评审重点)

| 保留(不动) | 代码位置 |
|---|---|
| 阶段序列由 profile 定义(design→build→verify) | `entry/profiles/*.yaml` |
| adapter 仍由 profile 的 stage→cli 决定 | `planner.py:680-697` profile 兜底覆盖 |
| 人确认闸 | `api.py:3163-3227` api_confirm_plan |
| 接力拓扑(per-stage PTY + done-gate) | `pty.py` + `planner.py:1018-1180` |

模型只决定"skip 哪些阶段 + 每阶段 focus 要点"——**不让模型选 adapter**(那是 profile 的护城河,§2.3 的经验路由原料)。

**Q5 回答(focus 注入边界)**:模型产出的 `focus`(自然语言)只作为参考 prompt 注入 cli_prompt,真正的执行流程和工具调用仍由 profile 严格控制。当前 focus 就是拼进 `_build_cli_prompt` 的字符串(`planner.py:762-781`),不参与 adapter 决策——"绕过路由"风险本来就不存在。明确写出来是为防止未来误改:不要让 focus 里的内容(比如模型在 focus 里写"建议用 codex")影响 adapter 选择。

#### 5.4.3 agent_tools.py 瘦身

`agent_tools.py` 的 `ORCHESTRATOR_TOOLS`:

- **删**:`plan_step` / `skip_stage`(不再用 FC 收集计划)
- **保留**:`launch_cli` / `check_done_file` / `mark_complete` / `mark_failed`(如果执行阶段仍用 FC;实现时需确认 `ORCHESTRATOR_TOOLS` 是否在执行阶段也传给模型)

**开放问题(评审点)**:`ORCHESTRATOR_TOOLS` 在执行阶段是否还用?如果 `continue_orchestrator_agent` 不靠 FC 启动 CLI(而是直接 spawn),则整个 `ORCHESTRATOR_TOOLS` 可删。实现时需 grep 确认。

---

## 6. 风险与开放问题(评审重点)

### 6.1 已识别风险

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| R1 | invoke_structured 的 Pydantic 校验失败时 `model_construct` 兜底产出残缺对象 | 阶段 3-4/5 的 LLM 决策可能拿到结构不全的返回 | 单元测试覆盖坏 JSON 边界;fallback 路径兜底 |
| R2 | playbook 追加合并去重失败 → 重复 rule 堆积 | 阶段 1 的 playbook 文件膨胀,反噬阶段 2 的引导质量 | 按 rule 文本严格去重,support 累加;加单元测试 |
| R3 | task_type 为空时分层路由崩溃 | 阶段 2 的 build_design_dimensions_section 在 task_type 缺失时崩 | fallback 到全局 playbook,不能崩(§5.2.2 已考虑) |
| R4 | gate 重写回归面大 | 阶段 3-4 改动后 `gate_result` 字段对不齐下游消费者 | **实现前必须 grep `gate_result` 的所有消费点**,尤其 `repair_packet_path`/`report_path` |
| R5 | 砍 FC 时 `ORCHESTRATOR_TOOLS` 在执行阶段也用 | 阶段 5 删过头,执行阶段崩 | 实现时 grep 确认 `ORCHESTRATOR_TOOLS` 的所有使用点 |
| R6 | reflect 扩展规则的"原因提取"质量低 | 阶段 1 的 playbook 价值低,飞轮转不出复利 | Q3 已定:存 reason 原文不抽取,LLM 消费时自己理解;若积累后效果指标差再考虑抽取 |

### 6.2 开放问题(已附作者倾向,仍请评审者回应)

> 以下问题在评审过程中已与作者讨论并给出倾向,正文相应章节已落地。保留问题本身供评审者挑战倾向。

**Q1(理论)**:§2.1 的"信息差 vs 能力差"命题,是否成立?有没有反例——编排器靠"能力差"也能长期存活的场景?
**作者倾向**:命题成立。"上下文管理能力"是最可能的反例,但拆开后真正有价值的是"切在哪、交接什么"——那是信息差不是能力(§2.1 已补论证)。评审者若能找到纯靠通用能力存活且不被对称升级吃掉的反例,可推翻。

**Q2(理论)**:§2.2.1 的 DPI 论证是否真的适用于"多 CLI 接力"这种形态?
**作者倾向**:适用,且接力形态的 done.json 硬交接导致信息必然损耗,单 worker 无法完美利用跨阶段上下文(DPI 前提不满足),这恰好反证接力必要(§2.2.1 已补论证)。

**Q3(设计)**:§5.1.1 的 reflect 扩展规则,"原因 Z"用 reason 原文 vs LLM 抽取?
**作者倾向**:存 reason 原文,不抽取。playbook 是 LLM 消费,LLM 自己理解自然语言,强行结构化会丢上下文(§5.1.1 Q3 已回答)。

**Q4(设计)**:§5.3.3 的 fallback 策略——保留完整原 gate 逻辑 vs 极简"默认 retry"?
**作者倾向**:都不是。fallback 保留 HIGH finding 存在性检查(查一次 DB),有 HIGH finding 就 escalate 转人,无才默认 retry。防止把真实质量问题当基础设施抖动(§5.3.3 已明确)。

**Q5(范围)**:阶段 5 砍 FC 的边界(§5.4.2)是否正确?把 adapter 选择权留给 profile、不让模型选,会不会限制编排器-模型的规划能力?
**作者倾向**:边界正确。focus 只作参考 prompt 注入,不参与 adapter 决策,绕过路由的风险本来就不存在(§5.4.2 Q5 已回答)。adapter 选择权留给 profile 是护城河。

**Q6(顺序)**:§5.0 的实施顺序(阶段 1 先做作试金石)是否合理?
**作者倾向**:合理。阶段 1 做完看过程指标(链路通)+ 积累后看效果指标(复利)。两层 KPI 分开判定,冷启动期只看过程指标不误判(§5.0.1/§5.0.2 已补)。

### 6.3 不在本重构范围内的事

- 接力拓扑本身的设计(per-stage PTY + done-gate)——**不改**,只保留
- per-stage adapter 路由的策略(design=claude/build=kimi)——**不改**,只保留
- story-miner 的 transcript 挖矿逻辑——不在本包
- 半自动模式(`/context/release-prompt`)——完全独立路径

---

## 附:关键代码位置速查

| 关注点 | 位置 |
|---|---|
| FC 规划循环(砍) | `SL/orchestrator/engine/planner.py:174-357` |
| FC 工具定义(砍) | `SL/orchestrator/engine/agent_tools.py:6-103` |
| decide_transition(改 fallback) | `SL/orchestrator/engine/transition.py:30-92` |
| build_repair_action(小改) | `SL/orchestrator/engine/transition.py:95-154` |
| verify-gate 调用点(切换) | `SL/orchestrator/engine/planner.py:1342-1425` |
| gate.py(保留旧函数作 fallback) | `SL/orchestrator/evaluation/gate.py:192-325` |
| reflect(扩展+落库) | `SL/orchestrator/learning/reflection.py:25-76` |
| _write_retrospect 触发点(挂 reflect) | `SL/orchestrator/engine/planner.py:1272, 1433` |
| _build_verify_history_facts(复用事件查询) | `SL/orchestrator/engine/planner.py:488-525` |
| 死代码 _load_team_knowledge(接通) | `SL/orchestrator/engine/planner.py:25-42` |
| build_design_dimensions_section(分层) | `SL/orchestrator/engine/prompt_sections.py:214-300` |
| build_kb_tool_section(参考) | `SL/orchestrator/engine/prompt_sections.py:110-164` |
| 接力拓扑(不动) | `SL/infra/terminal/pty.py:500-519` |
| done-gate(不动) | `SL/orchestrator/engine/planner.py:1018-1133` |
| profile adapter 路由(不动) | `SL/entry/profiles/realtest.yaml` + `SL/orchestrator/engine/planner.py:680-697` |
| LLM 客户端 | `SL/infra/llm_client.py:564-568`(get_llm)、`:272-304`(invoke_structured) |
| safe_story_path | `SL/infra/story_paths.py:91-121` |

---

## 参考文献

1. [Single-Agent LLMs Outperform MAS on Multi-Hop Reasoning (arXiv:2604.02460)](https://arxiv.org/abs/2604.02460) — DPI 信息论论证
2. [When Single-Agent with Skills Replace MAS (arXiv:2601.04748)](https://arxiv.org/abs/2601.04748) — skill 库相变崩溃 + 分层路由
3. [A Self-Improving Coding Agent SICA (arXiv:2504.15228)](https://arxiv.org/abs/2504.15228) — 经验沉淀
4. [Survey of Self-Evolving Agents (arXiv:2507.21046)](https://arxiv.org/html/2507.21046v4) — flywheel 分类
5. [Towards a Science of Scaling Agent Systems (arXiv:2512.08296)](https://arxiv.org/abs/2512.08296) — 模型升级边际收益 > 加 agent
6. [Cross-Task Experiential Learning MAEL (arXiv:2505.23187)](https://arxiv.org/html/2505.23187v1) — 跨任务经验学习
7. [quant67: Agent 框架工程](https://quant67.com/post/llm-infra/19-agent-framework/19-agent-framework.html) — 重心从帮模型思考转向帮模型执行
8. [ypollak2/llm-router (GitHub)](https://github.com/ypollak2/llm-router) — 通用 LLM 路由红海
9. [Tyler Folkman: 6 模型路由实验](https://tylerfolkman.substack.com/p/i-tested-6-ai-models-across-3-providers)
10. [知乎:为什么 Codex 搭载 GPT-5.6 后越来越多用户弃用 Skills](https://www.zhihu.com/question/2060378207459566963/answer/2060386633493504141) — 起因
