# 成果物驱动 + agentic 编排器 —— 设计文档

> 状态:**待评审(v2,重大修订)**。创建:2026-07-26,修订:2026-07-26。
> 范围:`packages/story-lifecycle`(尤其 `orchestrator/engine/planner.py` 完成判定、`orchestrator/engine/supervisor.py` 监工、`infra/db/models.py` 成果物/决策/轨迹表、`infra/terminal/pty.py` PTY 日志、prompt 模板);连带 `packages/story-miner`(link 机制)。
> 评审目标:验证"**砍掉 done 文件协议、改用成果物落地 + agentic 编排 LLM 调度**驱动 stage 推进"的方案正确性、可行性、迁移路径;以及编排 LLM 作为 agent(带工具、无状态、读 db 组装上下文)在本场景是否成立。
> **本文自包含**:背景、代码现状、决策依据(含业界调研 + 场景区分)、方案、分阶段实现、风险、开放问题全部内联,评审者无需读本对话历史。
> **v2 修订说明**:v1 把编排 LLM 压成"只在异常被唤起的纯监工",完成判定全归确定性文件检查 —— 这是**过度保守**,误把业界"反对复杂动态路由 LLM 编排"的结论推广到了本场景。v2 修正:编排 LLM 是 **agentic 编排器(带工具、无状态)**,在 stage 边界判完成 + 卡住时判干预。详见 §2.1 场景区分。

---

## 0. TL;DR(评审者先读)

**现状**:code CLI(claude/opencode/kimi)干完一个 stage 后,要按自定义协议**额外写一个 `done.json`**("我完了"的信号文件);编排器(planner)轮询这个文件出现就推进下一 stage。

**问题**:`done.json` 是 code CLI **自称完成** —— 它会**撒谎**(写了 done 但成果物是空的/错的),也会**遗漏**(干完了没按协议写 → 编排器永久卡死,真实发生过:design 跑 25 分钟没出 done.json)。它把编排器锁在**被动盲等**位置,且给新 CLI 接入强加了"学一套写 done 协议"的负担。

**方案(两层)**:

**① 砍掉 `done.json`,完成靠成果物**。核心论据:**story 的本质是产生可落地变更**(再简单的需求都有一句话设计 + 一行代码 + 一个测试报告;不改代码也有 SQL 要执行)—— 所以每个 stage **必然**有文件成果物,成果物本身就是完成的 ground truth,不需要再写一个"完成通知"。

```
现状(冗余 + 可撒谎):
   code CLI 产出 spec.md → 额外写 done.json("我完了") → planner 看 done 推进
                              ↑ 多余的中间人,且不可信

新方案:
   code CLI 产出 spec.md(原子写) → 编排 LLM 判成果物够不够 → 人确认 → 推进
   (成果物落地 = 完成的证据;编排 LLM 判质量;done 这个自称信号整个砍掉)
```

**② 编排 LLM 是 agentic 编排器**(不是纯监工)。它在**两个调度点**做判断:
- **stage 边界**:成果物落地了 → 编排 LLM 判"覆盖 PRD 没/质量够不够"→ 通过/打回
- **卡住时**:supervisor 规则检测到卡住(超时无输出/进程僵死)→ 唤起编排 LLM,它读 PTY 日志判"真卡/提问/跑偏/慢/失败"→ 输出干预决策(打字纠偏/重启/找人/继续等)

**为什么编排 LLM 能当调度(而业界反对 LLM 编排)**:业界证伪的是**复杂动态路由**(5 agent 协作网、动态决定调谁/并行否/上下文怎么传)。本场景是**结构固定 + 标准明确**(design→build→verify 单线、story 通过标准清晰)—— LLM 做的是"对着明确标准判完成/判卡住",不是动态路由。详见 §2.1 场景区分。

**配套**(都是为了让上面成立):
1. 成果物**版本化进 db**(复用现有 `story_doc` / `story_doc_version` 表 —— 全版本历史 + FTS + `confirmed_by` 人工确认字段,几乎为本方案预生)
2. **PTY 全量日志**(两层:raw 字节 + 结构化 JSONL)—— 喂编排 LLM 按需读(它用 read 工具自己读 events.jsonl,不预处理摘要)
3. 编排器**提供一个普通 CLI 工具**(`story-tool`),注入 prompt,code agent 调它完成"原子写 + 版本化 + 编排器感知"三合一
4. **无状态编排 + 决策全落库**:编排 LLM 每次唤起从 db 组装完整前情(决策历史 + code CLI 执行轨迹 + 成果物版本 + PRD),自己不"记得"。决策(含打字纠偏的载荷)**全落库审计**,也是下次唤起的输入
5. **gate 并入编排 LLM 判定**(不再独立事后跑)—— 编排 LLM 在 stage 边界一次做完"完成 + 质量"判断

**明确不做(本期)**:
- 交付追踪全链路(SQL/Nacos 执行、生产上线观测)—— 只在 `story_doc` **预留字段 + 提供 AI 提示词模板 + 写回端点**,不自己对接外部系统
- 编排 LLM 全程在线(它是**按需唤起**:stage 边界 + 卡住时,不是盯着 PTY 流)
3. 编排器**提供一个普通 CLI 工具**(`story-tool`),注入 prompt,code agent 调它完成"原子写 + 版本化 + 编排器感知"三合一
**明确不做(本期)**:
- 交付追踪全链路(SQL/Nacos 执行、生产上线观测)—— 只在 `story_doc` **预留字段 + 提供 AI 提示词模板 + 写回端点**,不自己对接外部系统
- 编排 LLM 全程在线盯着 PTY 流(它是**按需唤起**,不是常驻监工)

---

## 1. 起因与背景

### 1.1 触发事件

接入 opencode CLI 的 UI 驱动 E2E 测试中,calculator 场景的 design 阶段(claude)**跑了 25 分钟没产出 `done.json`**,planner 永久卡在轮询。code CLI 进程活着、CPU 持续(在干活),但没按协议写 done —— 编排器对此**完全无感知**(只看 done 文件,不看 PTY 里 code CLI 在干啥)。

排查发现:done 文件协议有**两类失效模式**,都真实发生过:
1. **遗漏**(本次):code CLI 干完了/卡在某步,没写 done → 编排器死等
2. **撒谎**(历史):code CLI 写了 done 但成果物空/错 → 编排器误推进半成品

### 1.2 问题的本质

`done.json` 是 **code CLI 自己声明完成** 的信号。它把"完成"这个事实**用另一个文件重复声明一遍** —— 而:

- 成果物落地(spec.md 存在、代码改了、测试报告写了)**本身就是完成的证据**,不需要再额外通知
- 让 code CLI 自己声明完成,**本质是不可信的**(它是既得利益方,会"为显得成功而优化")
- 业界对照:Claude Code 社区 [Issue #1770](https://github.com/anthropics/claude-code/issues/1770) 直接指出子 agent 会"为显得成功而优化",父 agent **不能信任自我报告**

`done.json` 是个**本不该存在的中间人协议**。

### 1.3 story 的本质约束 = 必然有产出

砍 done 的前提是"每个 stage 必然有文件成果物可查"。这个前提由 **story 的定义**保证:

> story 的本质是产生可落地变更。再简单的需求都有一句话设计 + 一行代码改动 + 一个测试报告。不改代码也有 SQL 要执行(SQL 脚本本身是文件产出)。纯评审/纯确认 ≠ story(不进编排流程)。

**结论:不存在"无文件产出"的 stage。** 所以"成果物落地 = 完成"是充分的,不需要 done 兜底。这个约束是整个方案成立的基石(评审重点之一:这个约束是否真的对所有 stage 成立?见 §7 开放问题 1)。

---

## 2. 业界调研(决策依据)

本方案的关键选择都有业界对照,不是臆想。

### 2.1 场景区分 —— 业界反对的是"复杂动态路由",不是本场景(v2 核心修正)

业界有大量"反对 LLM 当编排器"的声音,本设计 v1 曾据此把编排 LLM 压成纯监工。**这是误读** —— 业界证伪的场景和本场景根本不是一回事。

**业界证伪的场景(复杂动态路由)**:
```
通用多 agent 编排:LLM 要决定"现在调哪个 agent?并行还是串行?这个失败换哪个?
agent A 输出怎么路由给 B/C?上下文怎么在 5 个 agent 间传递?"
→ 动态路由,LLM 概率系统做确定性调度 = 错配,且 token 烧在"想怎么调度"上
```
- **[Microsoft Conductor](https://opensource.microsoft.com/blog/2026/05/14/conductor-deterministic-orchestration-for-multi-agent-ai-workflows/)** 主张确定性编排、编排层零 token,针对的是"已知结构 workflow 动态路由是多余开销"
- **[Stop Letting LLMs Orchestrate](https://www.abdelaziznotes.com/posts/stop-letting-llms-orchestrate-your-ai-agents)** 列的硬伤(LLM 无视委派自己干 / 长会话压缩失忆 / 无法强制并行 / subagent 后台输出失败率 40% / 无检查点)—— **全部针对的是多 agent 动态协作网**

**本场景(story 推进,结构固定 + 标准明确)**:
```
① 流程结构固定:design → build → verify 单线推进,不是动态路由
② 一个 story 一条线,不是 5 个 agent 协作网
③ story 通过标准明确(成果物落地 + 覆盖 PRD),不是模糊判断
④ "怎么算通过"是有明确答案的问题
```

把业界的硬伤逐条放到本场景检验,**大部分不成立**:

| 业界硬伤 | 在本场景 |
|---|---|
| LLM 无视委派自己干 | 不成立 —— 编排 LLM 不"委派",它判 code CLI 产出够不够 |
| 长会话压缩失忆 | 不成立 —— story 短链路,且本设计选**无状态**(§4.6),每次从 db 重组装上下文,不存在长会话 |
| 无法强制并行 | 不成立 —— story 单线推进,不需并行 |
| subagent 后台输出失败率 40% | 不成立 —— 编排 LLM 读的是**落盘的 events.jsonl**(可靠),不是流式抓子 agent |
| 无阶段级检查点 | 正是本设计要解决的 —— 成果物版本化 + 决策流水就是检查点 |
| 烧 token 想调度 | **唯一真实代价** —— 但本场景调度是"stage 边界 + 卡住时按需唤起",不是全程在线,成本可控 |

**结论:本设计让编排 LLM 当调度是可行的** —— 它做的不是业界反对的"复杂动态路由",而是"对着明确标准判完成/判卡住"。这正是 LLM 擅长的(给它上下文做判断)。

**仍然吸收业界教训的点**:
- **不是全程在线**(避"烧 token + 不可靠")—— 只在 stage 边界 + 卡住时唤起
- **无状态**(避"长会话压缩失忆")—— 每次从 db 重组装上下文(§4.6)
- **Decider/Handler 分层**(避"LLM 直接碰副作用失控")—— LLM 出决策,代码执行副作用(§4.7)
- **决策全落库**(避"无检查点 + 不可审计")—— 含打字纠偏载荷(§4.8)

业界还在追"父 agent 监控/干预子 agent"的方向(**[Claude Code #1770](https://github.com/anthropics/claude-code/issues/1770)**,Anthropic 尚未回应)—— 本设计是走在前面的实现。

### 2.2 PTY 日志格式 —— 业界一致选结构化 JSONL

- **Claude Code** 原生 JSONL(`~/.claude/*.jsonl`,每行一个 typed message record)
- **[Agent Logging 101](https://sidsaladi.substack.com/p/agent-logging-101-the-complete-guide)**:JSONL 是 agent 日志最佳格式,每行独立 JSON,天然支持流式 append
- **[Your AI Coding Agent Needs a Log Store, Not Terminal Output](https://modelpiper.com/blog/ai-agent-feedback-loop-log-store)**:原始终端输出不够,agent 需要 dedicated log store 捕捉完整 write→run→check→fix 反馈环
- 纯 ANSI 文本**只在终端原生回放**有优势;我们的场景(喂编排 LLM / 喂 miner 飞轮 / 人查卡住)全都需要可查询可解析,纯文本要反复 parse 转义码

**最佳实践是两层都留**(§4.3):raw 字节流(保真回放 + 取证)+ 结构化 JSONL(喂 LLM/飞轮)。且 JSONL 和 miner 现有转录格式(wire.jsonl / jsonl)**天然对齐**,飞轮零改动可消费。

### 2.3 原子写 —— 业界标准模式

砍 done 后完成判定全靠"成果物文件存在",必须解决"code CLI 写到一半被当完成"。业界标准:**写临时文件(同目录)→ fsync → rename 成成品名**。
- POSIX `rename()` **保证原子** —— 读者要么看到旧要么看到新,永不见半成品
- Windows `MoveFileEx + MOVEFILE_REPLACE_EXISTING` 大部分原子,但杀软/索引占用时 rename 会失败 → 需重试
- 约束:临时文件必须与成品**同磁盘**(否则 rename 退化 copy+delete,中途崩溃留半成品)

→ 本方案不要求 code CLI 自己实现原子写,而是由编排器提供的 `story-tool` 内置(§4.4),code agent 调工具即得原子保证。

---

## 3. 代码现状(改哪里)

### 3.1 完成判定 —— planner 盲等 done

`orchestrator/engine/planner.py`:poll loop 查 `done.json` 出现 → 推进。`stage_done_file_rel()` 给路径。code CLI 按协议写 done(格式在 prompt 模板里硬编码一大段"完成协议")。

**盲点**:planner 不知道 code CLI 在 PTY 里干了啥/卡没卡/对不对,只看 done 来没来。

### 3.2 supervisor —— 只答提问,90% 时间瞎

`orchestrator/engine/supervisor.py` + `supervise_pty_session`:daemon 线程消费 PTY tap queue,但职责**刻意收窄** —— 只在 `awaiting_detector` 识别到"提问/要选择"时介入(人工通知 或 auto_confirm 时 LLM 答覆)。**不判完成、不判卡住、不主动干预**。

### 3.3 成果物表 —— 已存在但没用进推进流程(关键发现)

`infra/db/models.py` 已有 `story_doc` + `story_doc_version` + `story_doc_fts`:

```sql
story_doc(story_key, doc_type, title, current_version, latest_content,
          local_path, updated_by, updated_at, confirmed_by, confirmed_at)
story_doc_version(id, story_key, doc_type, version, content, change_reason, author, created_at)
```

设计很成熟:**db 是真相 + 本地 .md 是只读缓存**(code agent 读文件不读 db)+ **全版本历史**(每次改 +1)+ **变更原因** + **FTS5 全文检索** + **`confirmed_by`/`confirmed_at` 人工确认字段**(注释明说"AI 不能自我确认,只有 user 点确认才写")。

**这张表几乎是为本方案预生的** —— 砍 done 后,它就是成果物的正式载体。`confirmed_by` 就是人确认闸。只缺:① 没接进 planner 推进流程 ② 没承载代码类成果物(代码走 git,需加 git ref 字段)③ 没预留交付追踪字段。

### 3.4 expected_outputs —— 声明了但从不检查

profile 的 stage 声明 `expected_outputs`(design→research_path/spec_path/complexity、build→plan_path/files_changed/summary、verify→test_report_path/...)。但代码里(`prompt_renderer.py`、`snapshot.py`)**只用来写进 prompt 告诉 code CLI 该产出什么**,**从不被 planner 用来检查是否真产出了**。这正是要补的环节。

### 3.5 unified_gate —— 事后质量评审

`orchestrator/evaluation/unified_gate.py`:stage 完整后跑的 LLM 质量评审。本方案要把它**前置到人确认前**(§4.5)。

---

## 4. 方案

### 4.1 总览:两个调度点 + agentic 编排器

编排 LLM 是 **agentic 编排器**(带工具、无状态)。它在**两个调度点**被唤起做判断,不是全程在线:

```
   ┌──────────────────────────────────────────────────────────────────┐
   │              编排 LLM (agentic 编排器,无状态,按需唤起)         │
   │                                                                  │
   │   调度点 ① stage 边界(成果物落地时唤起):                       │
   │     判完成 + 判质量 → approve / reject(打回)                   │
   │                                                                  │
   │   调度点 ② 卡住时(supervisor 检测到唤起):                      │
   │     读 events.jsonl 判卡因 → intervene / restart / wait / 升级   │
   │                                                                  │
   │   工具集(它自己调,§4.7):read_file / query_db / git_inspect   │
   │                      + 决策类(paste_to_pty / restart / escalate)│
   │                                                                  │
   │   无状态:每次唤起从 db 组装上下文(§4.6),自己不"记得"        │
   └─────────────────────────┬────────────────────────────────────────┘
                             │ 决策(Decider,不直接碰副作用)
                             ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │   Handler 层(代码执行副作用:写 PTY / 杀进程 / 推进 / 落审计) │
   └─────────────────────────┬────────────────────────────────────────┘
                             │
   ┌─────────────────────────▼────────────────────────────────────────┐
   │                              PTY                                 │
   │   ┌──────────────────────────────────────────────────────────┐   │
   │   │                     code CLI (claude/opencode/...)       │   │
   │   │                                                          │   │
   │   │   读 prompt(无"写 done 协议";有 story-tool 用法)      │   │
   │   │      → 干活 → 调 story-tool declare(原子写+版本化)     │   │
   │   │                       │                                  │   │
   │   │   PTY 两层日志(全量,§4.3):                            │   │
   │   │     raw.log + events.jsonl ──┐                           │   │
   │   │                               │                          │   │
   │   │   ┌───────────────────────────▼────────────────────────┐ │   │
   │   │   │  supervisor (规则监工,daemon 线程)                │ │   │
   │   │   │   • 超时无输出 / 进程僵死 / 反复报错 → 疑似卡住    │ │   │
   │   │   │   • 识别提问 → 通知人                              │ │   │
   │   │   │   • 检测到卡住 → 唤起编排 LLM(调度点②)           │ │   │
   │   │   └────────────────────────────────────────────────────┘ │   │
   │   │                                                          │   │
   │   │   ✗ 不再写 done.json(砍掉)                             │   │
   │   └──────────────────────────────────────────────────────────┘   │
   └──────────────────────────────────────────────────────────────────┘
```

### 4.2 调度点 ① —— stage 边界判完成 + 判质量

成果物落地(code CLI 调 `story-tool declare` 触发,§4.4)→ planner 检测到该 stage `expected_outputs` 全齐 → **唤起编排 LLM**:

```
   编排 LLM(stage 边界,无状态唤起):
     输入上下文(db 组装,§4.6):
       • PRD(需求)
       • 当前 stage 的成果物内容(read_file 读 story_doc 指向的 .md / git diff)
       • 该 story 的决策历史 + code CLI 执行轨迹(重试几次、为什么)
       • profile.confirm 配置
     判断:
       成果物覆盖 PRD 没?质量够吗?有无遗漏?
     输出决策:
       approve  → (confirm=true?等人确认:自动推进)→ 推进下一 stage
       reject   → 打回 code CLI 重试(带 reject 理由当下一轮 seed)
       escalate → 找人(成果物存疑,超出 LLM 判断信心)
     决策落 orchestrator_decision 表(§4.8)
```

**关键**:`unified_gate`(LLM 质量评审)不再独立事后跑 —— 它**并入这次编排 LLM 调用**(一次做完"完成 + 质量"判断,不是两次)。省一次 LLM 调用,且完成判定和质量判定共享同一上下文。

### 4.3 调度点 ② —— 卡住时判干预

supervisor 用**规则**检测卡住(不调 LLM,省 token):超时无新输出 / 进程活着但 idle / 反复报错。检测到 → **唤起编排 LLM**:

```
   编排 LLM(卡住时,无状态唤起):
     输入上下文:
       • events.jsonl 路径(它用 read_file 自己读需要的段,不预处理摘要)
       • code CLI 执行轨迹(这次是第几次 attempt、之前判过啥)
       • 成果物当前状态
     读 PTY 日志,判卡因 5 类:
       ① 真卡死(进程僵死/死锁)     → restart(杀 + resume/新起)
       ② 卡在提问/要选择            → escalate_human(或 auto_confirm 时自己答)
       ③ 跑偏了(方向不对/陷入循环) → intervene(打字纠偏,带新指令)
       ④ 只是慢(还在正常跑)        → wait(延长该 stage 超时,避免再误报)
       ⑤ 失败了(报错但进程退了)    → restart / 换 adapter / escalate
     输出决策 → Handler 执行 → 决策落 orchestrator_decision 表
```

### 4.4 story-tool(code agent 用的 CLI 工具,产出落地)

编排器提供一个**普通 CLI 可执行**(非 MCP,任何能跑 bash 的 code agent 都能用)。注入 prompt 告诉 code agent 怎么用:

```
story-tool workspace
  → 输出产出路径约定(spec 放哪、代码改哪、test_report 放哪)

story-tool declare <doc_type> <path>
  → 三合一:原子写(tmp→rename,§2.3)+ 写 story_doc 版本化 + 触发编排器感知
  → code agent 调它即完成"产出落地",不自己管原子写/db
  → 触发后 planner 检测 expected_outputs 全齐 → 唤起编排 LLM(调度点①)

story-tool todo
  → 查当前 stage 还缺哪些 expected_outputs(code agent 自检)
```

**为什么普通 CLI 而非 MCP**:MCP 要实现 server + code agent 得支持协议(claude/opencode 支持,更土的 CLI 不一定);普通 CLI 任何能 Bash 的 agent 都能用,接入成本最低(契合"支持任意 CLI")。

### 4.5 PTY 全量日志(两层)

```
   .story/runs/<story_key>/pty_<stage>/
     ├── raw.log      PTY 原始字节流(含 ANSI 转义码)
     │                • 100% 保真回放终端 / 取证 / PTY 协议调试
     └── events.jsonl 结构化事件(每行一个 JSON)
                      • {ts, dir(in/out), type, text(已剥ANSI), tool_call?}
                      • 也记录编排器自己注入 PTY 的内容(supervisor 打字纠偏)
                        —— 否则看到 code CLI 答非所问却不知是编排器戳的
                      • 喂编排 LLM 按需读 / 喂 miner 飞轮 / 卡住诊断
```

**正常完成也保留**(重要资产 —— 喂飞轮 + 复盘)。编排 LLM 只在被唤起时用 read_file 读 events.jsonl(不是流式喂它)。

### 4.6 无状态编排 —— 上下文从 db 组装

编排 LLM **每次唤起是无状态短调用**(不是 resume 长会话)。它不"记得"前情,靠 db 组装完整上下文。这避开了业界"长会话压缩失忆"的坑(§2.1)。

唤起时组装的上下文(编排 LLM 自己用工具查,或调用前预注入):

```
   组装给编排 LLM 的上下文:
   ─────────────────────────────────────────────
   • PRD(需求原文)
   • 当前 stage 的成果物(指针 + 关键内容)
   • code CLI 执行轨迹(story_session 扩展,§4.9):
       第几次 attempt、每次 outcome、failure_reason、产出哪些 artifacts
   • 编排决策历史(orchestrator_decision 表,§4.8):
       之前判过 approve/reject 吗、为什么、打字纠偏过几次
   • events.jsonl 路径(它按需 read,不全量喂)
   ─────────────────────────────────────────────
```

**重点**:决策历史 + 执行轨迹全落库 —— 这既是"无状态编排能工作"的前提(下次唤起能拿到前情),也是"可审计"的载体(每次决策、每次纠偏、每次重试都留痕)。

### 4.7 编排 LLM 的工具集

agentic 编排器带的工具(它自己调):

```
   查(只读,无副作用):
   • read_file        读 events.jsonl / 成果物 / PRD(调度点②读 PTY 的核心)
   • query_story_db   查执行轨迹 / 决策历史 / 成果物版本
   • git_inspect      查 git status/diff/log(代码类成果物检查)
   • list_artifacts   列当前 stage expected_outputs 落地情况

   决策(返回意图,Handler 执行副作用 —— Decider/Handler 分层):
   • paste_to_pty     打字纠偏(次数上限内,载荷落审计,§4.8)
   • restart_cli      杀 + resume/新起
   • escalate_human   找人(落 awaiting_confirm + 通知)
   • advance_stage    推进下一 stage(成果物 + 决策通过后)
   • mark_reject       打回 code CLI 重试(带 reject 理由)
```

**Decider/Handler 分层(AGENTS.md 红线)**:决策类工具,LLM 调它们时是**返回决策意图**,真正的 PTY/进程/db 副作用由 Handler 层代码执行(并记审计)。LLM 永远不直接碰副作用,但又有"工具"可调(从它视角是 agentic 的)。

### 4.8 决策审计表(orchestrator_decision)

编排 LLM 的每次决策(含打字纠偏)**全落库**,既是审计证据,也是下次无状态唤起的输入:

```sql
CREATE TABLE orchestrator_decision (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  story_key      TEXT NOT NULL,
  stage          TEXT NOT NULL,
  trigger        TEXT NOT NULL,   -- stage_boundary / stuck / question / error
  context_ref    TEXT,            -- 喂 LLM 的上下文指针(events.jsonl 路径 + 成果物版本号)
  decision       TEXT NOT NULL,   -- approve / reject / intervene / wait / restart / escalate
  reason         TEXT,            -- LLM 给的理由
  action_taken   TEXT,            -- Handler 实际执行的动作
  action_payload TEXT,            -- 动作载荷(打字纠偏时 = 塞进 PTY 的具体文本)← 审计关键
  llm_model      TEXT,            -- 哪个模型判的(可复现)
  decided_at     TEXT NOT NULL
);
```

**打字纠偏可审计** = `action_taken='paste_to_pty'` + `action_payload='具体塞了什么'` 全留痕,事后能查"编排器何时、基于什么判断、往 code CLI 塞了什么"。

**打字纠偏安全闸**(防 LLM 与 code CLI 陷入"纠偏→更乱→再纠偏"死循环):同一 stage 编排 LLM 打字纠偏**次数上限 ≤ 2 次**。超过 → Handler 强制升级到 `escalate_human`(找人),不再让 LLM 自己戳。(次数 = `SELECT COUNT(*) WHERE decision='intervene' AND story_key=? AND stage=?`)

### 4.9 code CLI 执行轨迹(复用 story_session 扩展)

无状态编排要"知道 code CLI 干到哪了、崩过几次、每次产出啥"。**复用现有 `story_session` 表加字段**(不新建表,语义自然 —— 一个 session 就是一次 attempt):

```sql
ALTER TABLE story_session ADD COLUMN attempt INTEGER DEFAULT 1;
ALTER TABLE story_session ADD COLUMN outcome TEXT;        -- success / failed / stuck_killed / running
ALTER TABLE story_session ADD COLUMN failure_reason TEXT; -- 失败原因(错误摘要 / LLM 判的卡因)
ALTER TABLE story_session ADD COLUMN artifacts_prod TEXT; -- 本次产出的成果物(story_doc 版本引用 JSON)
ALTER TABLE story_session ADD COLUMN pty_log_ref TEXT;    -- events.jsonl 路径(LLM 按需读)
```

编排 LLM 唤起时查这张表 + orchestrator_decision 表 + story_doc 成果物 + PRD,组成完整上下文(§4.6)。例如:

```
   "build 阶段第 3 次 attempt(session_abc),前 2 次都因 X 失败,
    本次产出了 spec.md v3 但测试还没跑 → 该让它继续 / 还是该干预"
```

### 4.10 交付追踪(本期只预留 + 提示词 + 端点)

`story_doc` 加预留字段(本期不对接外部系统,只留挂钩):

```sql
ALTER TABLE story_doc ADD COLUMN delivery_status TEXT;   -- 预留:未上线/部分/已上线
ALTER TABLE story_doc ADD COLUMN observed_status TEXT;   -- 预留:线上正常/异常/未观测
ALTER TABLE story_doc ADD COLUMN verified_payload TEXT;  -- 预留:AI 写回的验证结论+证据
```

story-lifecycle 提供两个东西(很轻,本期可做):
1. **提示词模板**(给 AI 巡检):"验证 story X 是否真上线:查 <release_system> 看 commit 是否在生产版本、查 <db_exec_log> 看 SQL 是否执行、查 <config_center> 看 Nacos 是否落地;结论写回。"
2. **写回端点**:`POST /api/story/{key}/delivery/verify` → 更新预留字段

AI(独立巡检 agent)按提示词查外部系统,查完调端点写回。**story-lifecycle 自己不碰外部系统。**

---

## 5. 分阶段实现(按风险/收益排序)

| 阶段 | 内容 | 风险 | 收益 |
|---|---|---|---|
| **P1 砍 done + 成果物判定** | 砍 done;planner 改查 expected_outputs 文件存在;prompt 删"写 done 协议";迁移脚本(存量 done → story_doc) | 中(改完成判定核心) | 砍掉不可信信号,接入新 CLI 零协议成本 |
| **P2 版本化 + story-tool** | story_doc 承载成果物(version+1);`story-tool` CLI(workspace/declare/todo);原子写内置 | 中 | 成果物可回溯;code agent 有统一产出入口 |
| **P3 编排 LLM 调度点①** | 实现 stage 边界的 agentic 编排器(判完成+质量);unified_gate 并入;orchestrator_decision 表 | 高(agentic 编排器核心) | 完成判定从"文件存在"升级到"质量达标";为 AI 接管确认铺路 |
| **P4 PTY 日志 + 调度点②** | pty.py 落两层日志;supervisor 规则检测卡住 + 唤起编排 LLM 读 events.jsonl 判干预;story_session 扩展执行轨迹 | 高(改 supervisor + 卡住判定) | 卡住可诊断可干预(解决本次 design 卡 25min 场景) |
| **P5 打字纠偏 + 审计** | paste_to_pty 工具(次数上限 ≤2);action_payload 落审计;升级机制 | 中(有副作用,需谨慎) | 编排 LLM 能纠偏跑偏的 code CLI,且可审计可控 |
| **P6 交付追踪挂钩** | story_doc 预留字段;写回端点;提示词模板 | 低 | 上线观测闭环(对接外部系统留后续) |
| **P7 飞轮同步** | miner link 从 done+anchors 改成 story_doc + 时间窗;消费 events.jsonl | 中 | 飞轮不破坏 |

P1-P2 是成果物闭环(确定性,低风险);P3-P5 是 agentic 编排器(核心创新,风险集中在这);P6-P7 是配套。**P1 单独就能跑通**(裸文件检查代替 done),后续逐步把编排 LLM 的判断能力叠上去。

---

## 6. 砍掉的东西 / 反模式

- **✗ done.json 文件**(整个完成协议从 prompt 删掉)
- **✗ code CLI 学"怎么写 done"的自定义协议负担**
- **✗ done 撒谎/遗漏导致的卡死和误推进**
- **✗ code CLI 自己声明完成**(不可信,改成果物证明)
- **✗ 编排 LLM 全程在线盯着 PTY 流**(按需唤起,不是常驻)
- **✗ 编排 LLM resume 长会话**(无状态,避压缩失忆)
- **✗ 编排 LLM 直接碰副作用**(Decider/Handler 分层,只出决策)

**反模式**(评审重点 —— 这些不该出现在新设计里):
- 重新发明一个"完成信号文件"(等于 done 换名)
- 编排 LLM 全程在线指挥(把业界"复杂动态路由 LLM 编排"的坑全踩一遍,§2.1)
- 打字纠偏无次数上限 + 无审计(失控且不可追溯)
- 让编排 LLM 记长会话上下文(压缩失忆,§2.1)

---

## 7. 风险与开放问题(评审重点)

### 7.1 "每个 stage 必然有文件产出"这个约束真的成立吗?

方案基石(§1.3)。需评审确认:
- 是否存在**纯决策类 stage**(只给结论不产文件)?—— 当前 profile 没有,但未来可能有(如"评审/会签")
- 若有,该 stage 完成机制是什么?**候选**:让它也产文件(`review_verdict.md`)?还是允许"无文件成果物"走编排 LLM 判断?(注意:有编排 LLM 后,纯决策 stage 的完成可由 LLM 判,不再需要文件兜底 —— 这是 v2 比 v1 更灵活之处)

### 7.2 agentic 编排器的复杂度(v2 最大风险,评审重点)

编排 LLM 是 agent(带工具、能 read 文件/查 db),这等于在 code agent 上面又套一个 agent。风险:
- **agent 管 agent 的协调复杂度**(业界 Conductor/Vincent 都没让编排器是 agentic 的,他们用纯判定函数 + 预处理摘要,§2.1)
- **可复现性差**(agentic 的每次调用路径不同,难调试)
- **token 成本**(编排 LLM 自己 read 文件 + 查 db + 推理,比纯判定函数贵)
- **评审问题**:这个复杂度值得吗?替代是"编排 LLM 用纯判定函数(i)+ supervisor 预处理摘要喂它"—— 更可控可复现,但能力弱(不能按需深挖 events.jsonl)。**本设计选 ii 是为了卡住诊断的能力**(调度点② 要 LLM 能读 PTY 判断卡因,纯判定函数做不到)。评审者需权衡:这个能力溢价是否值得付 agentic 复杂度?

### 7.3 打字纠偏的安全性

往进行中的 code CLI PTY 塞指令有风险(把对的带歪)。本设计的防护:次数上限 ≤2(超了升级找人)+ action_payload 全审计。评审:
- 上限 2 够吗?(候选:可配,profile 级)
- 审计能追责,但伤害已造成 —— 要不要加"打字纠偏前先找人确认"的强约束?(会降低自动化程度,违背 agentic 初衷)

### 7.4 迁移期兼容

存量 story 还依赖 done。方案:迁移脚本(done.json → story_doc)+ 新 story 走新协议、旧 story 跑完为止。**不双轨**。评审:迁移脚本能覆盖所有存量形态吗?

### 7.5 atomic rename 在 Windows 的可靠性

杀软/索引占用文件时 rename 失败。方案:重试 + 指数退避。评审:重试上限到仍失败怎么办?(候选:降级直接写 + 标记"非原子",编排 LLM 判完成时兜底)

### 7.6 code agent 真会调 story-tool 吗?

依赖 code agent(claude/opencode)理解 prompt 工具用法并主动 Bash 调。agentic 的通常能;较弱的 CLI 可能退化自己写文件(绕过版本化)。评审:降级可接受吗?(候选:planner 兜底扫描约定路径,即便没调 tool 也发现成果物)

### 7.7 无状态编排的上下文组装成本

每次唤起从 db 重组装完整上下文(PRDL + 执行轨迹 + 决策历史 + 成果物)。长 story(多 stage 多次重试)上下文可能膨胀。评审:上下文要全量还是裁剪?(候选:只给当前 stage 相关 + 最近 N 次决策,老的摘要化)

---

## 8. 决策溯源(为什么是这个形状)

| 决策 | 选择 | 拒绝的替代 | 依据 |
|---|---|---|---|
| 完成判定 | 成果物落地 + 编排 LLM 判质量 | done 文件 / 纯文件存在检查 | done 不可信(§1.2);纯文件存在查不了质量 |
| 编排形态 | **agentic 编排器(ii)**,按需唤起 | 纯判定函数(i) / 全程在线指挥(A) | 场景区分(§2.1);ii 能力够卡住诊断,避 A 的坑 |
| 编排状态 | **无状态**,db 组装上下文 | resume 长会话 | 避压缩失忆(§2.1 §4.6) |
| 调度点 | stage 边界 + 卡住时 | 仅边界 / 全程 | 边界判完成,卡住判干预(解决 design 卡 25min) |
| 卡住检测 | 规则(supervisor) | LLM | 规则省 token,LLM 只在检测到后判怎么办 |
| 打字纠偏 | 要,次数上限 + 审计 | 不要 / 无限制 | 要纠偏能力,但防失控(§4.8) |
| Decider/Handler | LLM 出决策,代码执行副作用 | LLM 直接碰副作用 | AGENTS.md 红线;可控可审计 |
| 代码版本化 | git 引用 | 存 patch 进 db | 代码天然在 git;db 存 patch 膨胀 |
| 文档版本化 | story_doc 表(已存在) | 新建表 | 复用成熟机制(§3.3) |
| PTY 日志 | 两层(raw + JSONL) | 纯文本 / 纯 JSONL | 业界最佳实践(§2.2) |
| code 工具 | 普通 CLI(story-tool) | MCP server | 接入成本最低,支持任意 CLI(§4.4) |
| 执行轨迹 | 复用 story_session 扩展 | 新建表 | 语义自然(一 session = 一 attempt) |
| gate | 并入编排 LLM(stage 边界一次做完) | 独立事后跑 | 省一次调用,共享上下文(§4.2) |
| 交付追踪 | 预留字段 + 提示词 + 端点 | 本期对接外部系统 | 范围控制(§4.10) |

---

## 附录 A:与现有架构契约的关系

- **AGENTS.md「Driver lifecycle」**:driver 假设"CLI lifecycle ⊆ driver lifecycle"。砍 done 后,`consume_orphan_done`(被动消费 done)失去意义 —— 改成"被动消费成果物"(打开 story 详情页扫 expected_outputs + 唤起编排 LLM 判完成)。不变量仍成立,信号源从 done 换成成果物 + 编排 LLM。
- **AGENTS.md「Resolver/Decider/Handler 分层」**:本设计严格遵守 —— 编排 LLM 是 Decider(出决策,纯),supervisor 是 Resolver(规则检测卡住),Handler 执行副作用(写 PTY/杀进程/推进/落审计)。
- **AGENTS.md「Session-id model」**:不变。resume 仍看 session 现场,不依赖成果物。
- **DESIGN-task-actions-and-grill-me.md**:task_actions 驱动 stage 语义不变;完成协议部分(prompt "写 done" 段)被本文档替换。
- **DESIGN-session-pty-id-model.md**:session/resume 机制不变;PTY 两层日志是新增层,不改 session id 模型。
- **supervisor.py 现有职责**(只答提问):v2 扩展为"规则检测卡住 + 唤起编排 LLM",但仍不判完成(完成归编排 LLM 调度点①)。
