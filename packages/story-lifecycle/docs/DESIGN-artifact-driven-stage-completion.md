# 成果物驱动 + 分层判定编排 —— 设计文档

> 状态:**待实现(v3,经外部评审修订)**。创建:2026-07-26。
> 修订史:v1(纯监工,过度保守)→ v2(agentic 编排器,能力对但切片错)→ **v3(分层判定 + 评审修订)**。
> 范围:`packages/story-lifecycle`(完成判定、supervisor、db 成果物/决策/轨迹表、pty 日志、prompt、story-tool);连带 `packages/story-miner`(link 双写兼容)。
> **本文自包含**:背景、代码现状、决策依据(含业界调研 + 场景区分 + 外部评审修订)、方案、分阶段实现、风险全部内联。
> **v3 修订来源**:外部 AI 评审指出 v2 四处漏洞(self-grading 同族相关性 / agentic 切片错 / 打字纠偏该砍 / verify 自报验证=done 换名)。v3 全盘接受,见 §9 评审回应。

---

## 0. TL;DR(评审者先读)

**现状**:code CLI(claude/opencode/kimi)干完 stage 后按自定义协议写 `done.json`("我完了");planner 轮询它出现就推进。

**问题**:`done.json` 是 code CLI **自称完成**,会撒谎(写了但成果物空/错)、会遗漏(没写 → 永久卡死,真实发生:design 跑 25min 没出 done)。它把编排器锁在被动盲等,且给新 CLI 接入强加"学写 done 协议"负担。

**方案(三层)**:

**① 砍 done,完成靠成果物**。论据:story 本质是产生可落地变更(再简单也有一句话设计+一行代码+测试报告;不改代码也有 SQL)—— 每个 stage **必然有文件成果物**,成果物本身是完成的 ground truth,不需要额外的"完成通知"。

**② 分层判定**(v3 核心修正,采纳评审 B):不是"全 agentic"也不是"全确定性",而是按调度点分层:
- **调度点①(stage 边界)**:纯判定函数 + 预注入上下文(非 agentic)。判完成 + 判质量。
- **调度点②(卡住时)**:supervisor 预处理摘要喂纯判定函数判 5 类卡因;**仅规则触发**(同 stage 第二次卡住 / 摘要检测到循环)才升级 agentic 深读 events.jsonl(只读工具 + 调用上限 ≤5)。agentic 是例外路径不是默认。

**③ 人确认是显式不变量**(v3 核心修正,采纳评审 A):场景区分"LLM 可当调度"成立的**唯一前提**是 `confirm=true` 人确认兜底。关掉它,LLM 的 false approve 无人拦,论证失效。**confirm=true 是硬约束,不是默认配置。**

```
现状(冗余 + 可撒谎):
   code CLI 产出 spec.md → 写 done.json(自称完了) → planner 看 done 推进

v3:
   code CLI 产出 spec.md(原子写) → 纯判定 LLM 判成果物够不够 → 人确认 → 推进
   卡住 → supervisor 规则检测 → 摘要喂纯判定 → (例外)agentic 深读 → 干预
```

**配套**:
1. 成果物版本化进 `story_doc` / `story_doc_version`(已存在,全版本+FTS+confirmed_by)
2. `story-tool` CLI(workspace/declare/todo,原子写+版本化三合一)
3. PTY 两层日志(raw + events.jsonl)
4. **orchestrator_decision 决策审计表**(含 reject 上限防护)
5. story_session 扩展执行轨迹(attempt/outcome/failure_reason)

**明确不做(本期)**:
- **打字纠偏砍掉**(v3 修正,采纳评审 C)—— 用 restart-with-seed 替代。保留表设计,降级 backlog。
- 交付追踪全链路 —— 只预留字段 + 提示词 + 写回端点
- verify 靠执行(编排器跑测试看退出码)—— v3 先按"号称完成"做,列为演进方向(§4.7)

---

## 1. 起因与背景

### 1.1 触发事件
opencode 接入的 UI e2e 中,calculator design 阶段(claude)**跑 25 分钟没产出 done.json**,planner 永久卡在轮询。code CLI 活着、CPU 持续(在干活),但没按协议写 done —— 编排器完全无感知。done 协议两类失效:① 遗漏(本次)② 撒谎(写了 done 但成果物空)。

### 1.2 问题本质
done.json 是 code CLI **自己声明完成**,把"完成"用另一个文件重复声明。成果物落地本身就是证据,不需要额外通知;让 code CLI 自报完成不可信(既得利益方会"为显得成功而优化",Claude Code #1770 社区共识)。

### 1.3 基石约束 = 硬契约(v3 修正,采纳评审 D)
> story 本质是产生可落地变更。再简单的需求都有一句话设计+一行代码+测试报告;不改代码也有 SQL 要执行。纯评审/纯确认 ≠ story。

**v3 提升为 schema 强制契约**:profile 层校验 —— **任何 stage 必须声明至少一个文件类 expected_output,否则拒绝加载**。纯决策 stage 产出 `review_verdict.md`(固定 schema:decision+rationale+checklist);会签靠 `confirmed_by` 人工写(db 已有,确定性不经 LLM)。

约束从"经验碰巧成立"变"架构强制成立",循环依赖消除,verdict 文件还成可检索可版本化审计资产。

---

## 2. 业界调研 + 场景区分

### 2.1 场景区分 —— 成立,但有显式前提(v3 修正,采纳评审 A)
业界反对"LLM 当编排器"针对的是**复杂动态路由**(5 agent 协作网、动态决定调谁/并行否/上下文怎么传)。本场景是**结构固定单线推进**(design→build→verify)+ **标准明确** —— LLM 做的是"对着标准判完成/判卡住",不是动态路由。业界硬伤逐条在本场景检验,大部分不成立(详见 v2 §2.1 表格)。

**但 v3 承认评审指出的、本场景仍触发的硬伤**:
1. **self-grading 同族相关性**:编排 LLM 和 code CLI 大概率同代同家。code CLI 写错的 spec,编排 LLM 用同样先验判会犯同样错。**done 自报换成"同温层互评",相关性失败没消除只换了位置。**
2. **false reject 打回循环**:同一成果物两次无状态唤起可能给不同 verdict。reject→重跑→再 reject 烧 token。**v2 给打字纠偏设了上限却没给 reject 设 —— 不对称防护。**
3. **循环论证**:"标准明确"是方案前提,不是论据。

**显式不变量(v3 核心)**:场景区分成立的**唯一前提**是 `confirm=true` 人确认兜底 false approve。**关掉人确认,§2.1 整个论证失效。** 这点从隐含提升为硬约束。

吸收的业界教训:不全 span 在线(按需唤起)、无状态(避压缩失忆)、Decider/Handler 分层、决策全落库。

### 2.2 PTY 日志格式
业界一致选结构化 JSONL(Claude Code 原生 jsonl / Agent Logging 101 / Log Store Not Terminal Output)。本设计两层:raw 字节(保真回放)+ events.jsonl(喂 LLM/飞轮),且和 miner 现有转录格式对齐。

### 2.3 原子写
业界标准:写临时文件(同目录)→ fsync → rename。POSIX rename 保证原子;Windows MoveFileEx 大部分原子,杀软占用需重试。本设计由 story-tool 内置,code agent 不自己管。

---

## 3. 代码现状(改哪里)

- **3.1 完成判定**:planner poll done.json → 推进。盲等,不看 PTY。
- **3.2 supervisor**:daemon 消费 tap,只答提问(awaiting_detector),不判完成/不判卡住。
- **3.3 story_doc 表(关键发现)**:已存在且成熟 —— db 真相+本地 .md 缓存+全版本历史+FTS+`confirmed_by` 人工确认字段。**几乎为本方案预生**,只缺没接进推进流程 + 没承载代码类 + 没预留交付字段。
- **3.4 expected_outputs**:profile 声明了(design→spec/research、build→代码、verify→test_report),但**只写进 prompt,从不被检查**。
- **3.5 unified_gate**:stage 后 LLM 质量评审。v3 并入调度点①(一次做完完成+质量)。

---

## 4. 方案

### 4.1 总览:分层判定(非全 agentic)

```
   ┌──────────────────────────────────────────────────────────────────┐
   │  调度点① stage 边界 —— 纯判定函数 + 预注入上下文(非 agentic)    │
   │    输入(调用前确定性组装):PRD + 成果物内容 + 决策历史 + 执行轨迹│
   │    判断:approve / reject(打回) / escalate                     │
   │    → approve + confirm=true → 人确认 → 推进                      │
   │    → reject → 回 code CLI 重试(带上次 reject 不同理由当 seed)   │
   └──────────────────────────────────────────────────────────────────┘
   ┌──────────────────────────────────────────────────────────────────┐
   │  调度点② 卡住时 —— 摘要先行,agentic 为例外路径                  │
   │    supervisor 规则检测(超时/僵死/循环报错)→ 唤起:             │
   │      第一步:预处理摘要(最后N条events+错误行+idle时长)          │
   │              喂纯判定函数判 5 类卡因(真卡/提问/跑偏/慢/失败)    │
   │      例外升级(规则触发):                                       │
   │        • 同 stage 第二次卡住 / 摘要检测到循环模式                │
   │        → 升级 agentic 深读 events.jsonl(只读工具,调用≤5)       │
   │    输出决策 → Handler 执行(restart/escalate/wait)              │
   └──────────────────────────────────────────────────────────────────┘

   下方 PTY:
     code CLI 读 prompt(无写done协议;有story-tool用法)
            → 干活 → story-tool declare(原子写+版本化)
     PTY 两层日志(raw + events.jsonl)全量保留
     supervisor 规则监工(daemon)
```

**为什么边界点不 agentic**(评审 B):边界输入完全有界(PRD+成果物+历史),预注入和让它自读信息量一样,后者只多不可复现的工具调用路径。边界点付 agentic 复杂度买不到能力。agentic 只在卡住的长尾诊断(需深挖日志中段)有价值,且为例外路径。

### 4.2 调度点① stage 边界(纯判定)
成果物落地(story-tool declare 触发)→ planner 检测 expected_outputs 全齐 → **纯判定 LLM 唤起**(预注入上下文,非 agentic):
- 判完成 + 判质量(覆盖 PRD 没、质量够不够)
- unified_gate 并入(一次做完,不独立事后跑)
- 输出 approve/reject/escalate → 落 orchestrator_decision

### 4.3 调度点② 卡住(摘要先行 + agentic 例外)
supervisor **规则**检测卡住(不调 LLM):超时无输出/进程僵死/反复报错。检测到 → 唤起:
- **第一步**:预处理摘要(最后 N 条 events + 错误行 + idle 时长)喂纯判定函数,判 5 类卡因。
- **例外升级**(规则触发,不靠 LLM 自评):同 stage 第二次卡住 / 摘要检测到循环模式 → 升级 agentic 深读(只读工具 + 调用 ≤5)。
- 决策:restart(杀+resume/新起,带 seed)/ escalate_human / wait(延长超时)。**无打字纠偏**(§4.8)。

### 4.4 story-tool(code agent 用的 CLI)
普通 CLI(非 MCP),注入 prompt。`workspace` / `declare <doc_type> <path>`(原子写+版本化+触发编排器感知) / `todo`。

### 4.5 PTY 两层日志
`.story/runs/<key>/pty_<stage>/raw.log`(保真)+ `events.jsonl`({ts,dir,type,text,tool_call?},含编排器注入记录)。正常完成也保留(喂飞轮+复盘)。

### 4.6 无状态编排 + 决策全落库
编排 LLM 每次唤起是无状态短调用(不 resume 长会话)。从 db 组装上下文:PRD + 成果物 + 执行轨迹(story_session)+ 决策历史(orchestrator_decision)。决策全落库既是无状态能工作的前提,也是审计载体。

### 4.7 verify 完成判定(v3 新增,采纳评审洞1)
**成果物分三类**(可信度不同):
- ① 客观可复现(git diff/SQL 文件)→ 编排器直接查
- ② 自产证据(test_report.md = code CLI 自称"测过了"= **done.json 换名,谎言通道**)
- ③ 主观判断(spec 设计质量)→ LLM 判 + 人确认

**v3 第一版:先按"号称完成"做** —— test_report.md 仍算 verify 完成信号(承认是暂时谎言通道,标 TODO)。
**演进方向(本期不做,写进文档)**:verify 改编排器执行验证命令集(mvn test/pytest 看退出码),客观部分确定性、主观部分 LLM 判。验证命令集来源(a profile 写死 / b design 产出清单 / c LLM 生成)列开放问题。

### 4.8 打字纠偏(v3 砍掉,采纳评审 C)
**P5 砍掉,降级 backlog。** 用 restart-with-seed 替代(杀掉,带卡因诊断当下一轮 seed 重跑,给 code CLI 干净连贯上下文)。保留 orchestrator_decision 表设计(本就记所有决策)。评审理由:打字纠偏防不住单次带歪 + PTY 注入时序脆弱 + 有更干净替代。将来有遥测证明"长会话小纠偏高频且 restart 成本高"再加,且首次实现就该是"注入前快照+注入后验证"闭环。

### 4.9 决策审计表(orchestrator_decision)+ reject 上限(v3 新增防护)
```sql
CREATE TABLE orchestrator_decision (
  id, story_key, stage, trigger, context_ref,
  decision, reason, action_taken, action_payload, llm_model, decided_at
);
```
**reject 上限(v3,采纳评审 A2)**:同一 stage reject 次数上限 + **每次 reject 必须给与上次不同的具体理由**(否则说明 judge 在抖)+ 超限强制 escalate。防 false reject 打回循环烧 token。

### 4.10 执行轨迹(复用 story_session 扩展)
story_session 加 attempt/outcome/failure_reason/artifacts_prod/pty_log_ref。

### 4.11 交付追踪(预留 + 提示词 + 端点)
story_doc 加 delivery_status/observed_status/verified_payload 预留字段 + 写回端点 + AI 巡检提示词模板。本期不对接外部系统。

### 4.12 headless 优先 PTY 兜底(v3 新增演进方向,采纳评审洞2)
claude -p/stream-json、opencode 非交互模式,结构化事件流 + 进程退出码是内核级完成信号,比 PTY 刮日志高一个可靠级。"单 stage 内无人工介入段落"该用 headless,卡住/需人工才掉回 PTY。**本期不做,列架构演进方向。**

---

## 5. 分阶段实现(v3 重排,采纳评审第4点)

| 阶段 | 内容 | 风险 | 收益 |
|---|---|---|---|
| **P1 砍done+成果物闭环** | 砍done;原子写(同批,防半成品竞态);story-tool骨架;文件存在检查;**miner双写兼容**(新落story_doc+给miner维持旧视图);迁移脚本 | 中 | 砍不可信信号,接入新CLI零协议 |
| **P2 规则卡住检测+人升级** | supervisor加超时/僵死规则+escalate_human(**纯确定性,零LLM**) | 低 | 直接修触发事故(design卡25min),紧跟P1 |
| **P3 版本化+边界纯判定** | story_doc接入推进;边界纯判定LLM(非agentic);unified_gate并入;**reject上限** | 高 | 完成判定升级到质量达标;为AI接管铺路 |
| **P4 卡住LLM诊断** | 卡住点摘要先行纯判定;规则触发升级agentic深读(只读+≤5);story_session扩展 | 高 | 卡住可诊断(长尾深挖) |
| ~~P5 打字纠偏~~ | **砍**,降级backlog。用restart-with-seed | — | — |
| **P6 交付挂钩** | story_doc预留字段;写回端点;提示词模板 | 低 | 上线观测闭环(对接外部留后续) |
| **P7 飞轮切换** | miner从双写切到纯新协议 | 中 | 飞轮不破坏 |

**P1-P2 是确定性核心(低风险,直接堵坑);P3-P4 是 LLM 判定(核心创新);P6-P7 配套。**

---

## 6. 砍掉/反模式

- ✗ done.json / 写done协议 / done撒谎遗漏 / code CLI 自报完成
- ✗ 编排 LLM 全程在线(按需唤起)/ resume 长会话(无状态)/ 直接碰副作用(Decider/Handler 分层)
- ✗ **编排 LLM 判 verify 自报报告为通过**(自报验证=done换名,v3 新增反模式)
- ✗ **打字纠偏(v3 砍)** —— 单次带歪+PTY时序脆弱防不住,用restart-with-seed
- ✗ **边界点 agentic**(v3 修正)—— 输入有界,纯判定够,agentic只多不可复现路径
- ✗ **"无文件成果物走 LLM 判"兜底**(v3 否决)—— 循环依赖,改 schema 强制契约

反模式:重新发明完成信号文件 / 打字纠偏无上限无审计 / 让编排 LLM 记长会话。

---

## 7. 风险与开放问题

- **7.1 self-grading 同族相关性**(v3 新增,评审 A1):编排 LLM 和 code CLI 同代会犯同样错。缓解:confirm=true 人兜底(显式不变量);演进用异构模型(编排用与 code CLI 不同家模型)。
- **7.2 false reject 打回循环**(v3 新增,评审 A2):reject 上限 + 每次理由不同 + 超限 escalate。
- **7.3 verify 自报验证谎言通道**(v3 新增,评审洞1):当前 test_report.md 仍算完成(承认暂时)。演进:编排器执行验证命令集看退出码。**这是方案当前最大残留漏洞,需优先演进。**
- **7.4 迁移期兼容**:双写期(P1),旧 story 跑完为止,不长期双轨。
- **7.5 atomic rename Windows 可靠性**:重试+退避;上限到降级直接写标"非原子",人确认兜底。
- **7.6 code agent 不调 story-tool**:降级自己写文件。planner 兜底扫约定路径(但无原子保证,标 TODO)。
- **7.7 无状态上下文组装成本**:长 story 上下文膨胀。候选:只给当前 stage 相关 + 最近 N 次决策,老摘要化。
- **7.8 headless vs PTY 取舍**(v3 新增):当前选 PTY 全程(因交互/resume/人工)。演进:无人工段落用 headless(§4.12)。

---

## 8. 决策溯源

| 决策 | 选择 | 拒绝 | 依据 |
|---|---|---|---|
| 完成判定 | 成果物+纯判定LLM判质量 | done/纯文件存在 | done不可信;纯文件查不了质量 |
| 编排形态 | **分层判定**(边界纯判定+卡住例外agentic) | 全agentic(v2)/ 全确定性 | 评审B:边界agentic买不到能力,卡住长尾才需 |
| 编排状态 | 无状态 | resume长会话 | 避压缩失忆 |
| 人确认 | **显式不变量**(confirm=true硬约束) | 隐含/可关 | 评审A:关掉则§2.1失效 |
| 卡住检测 | 规则(supervisor) | LLM | 规则省token |
| 卡住干预 | restart-with-seed | ~~打字纠偏~~ | 评审C:打字防不住单次带歪+时序脆弱 |
| 基石约束 | **schema强制契约** | 经验断言+LLM兜底 | 评审D:消除循环依赖 |
| verify | 混合(先号称完成,演进靠执行) | 纯报告 | 评审洞1:报告=done换名 |
| reject防护 | 上限+理由不同+超限escalate | 无上限(v2漏洞) | 评审A2:防打回循环 |
| 代码版本化 | git引用 | 存patch进db | 代码天然在git |
| 文档版本化 | story_doc(已存在) | 新建表 | 复用成熟机制 |
| PTY日志 | 两层(raw+jsonl) | 纯文本 | 业界共识 |
| code工具 | 普通CLI | MCP | 接入成本最低 |
| 执行轨迹 | 复用story_session扩展 | 新建表 | 语义自然 |
| gate | 并入调度点① | 独立事后 | 省调用+共享上下文 |

---

## 9. 外部评审回应(v3 修订来源)

v2 经外部 AI 评审,指出 4 处漏洞,v3 全盘接受:

1. **§2.1 场景区分漏了"概率系统做确定性控制决策"错配**(评审 A)—— self-grading 同族相关性 + false reject 循环 + 循环论证。**v3 修:加显式不变量 confirm=true + reject 上限 + 删循环论证。**
2. **agentic 切片错**(评审 B)—— 边界点不需要 agentic,卡住点 90% 靠摘要。**v3 修:分层判定(边界纯判定 + 卡住例外 agentic)。**
3. **打字纠偏该砍**(评审 C)—— 单次带歪 + PTY 时序脆弱 + 有 restart-with-seed 替代。**v3 修:P5 砍,降级 backlog。**
4. **verify 自报验证 = done 换名**(评审洞1)+ **基石约束循环定义**(评审 D)+ **headless 优先漏了**(评审洞2)+ **分阶段顺序三处硬伤**(评审第4点)。**v3 修:schema 强制契约 + verify 演进方向 + headless 演进方向 + P1-P2 重排。**

评审还指出 v2 没列的真风险(false reject 循环、self-grading),v3 补进 §7。

---

## 附录 A:与现有架构契约的关系

- **AGENTS.md「Driver lifecycle」**:`consume_orphan_done` 失效 → 改"被动消费成果物"(打开详情页扫 expected_outputs + 唤起纯判定)。不变量仍成立,信号源从 done 换成果物。
- **AGENTS.md「Resolver/Decider/Handler 分层」**:严格遵守 —— 纯判定 LLM=Decider,supervisor=Resolver(规则检测),Handler 执行副作用。
- **AGENTS.md「Session-id model」**:不变。resume 看现场,不依赖成果物。
- **DESIGN-task-actions-and-grill-me**:task_actions 不变;完成协议段被本文档替换。
- **DESIGN-session-pty-id-model**:session/resume 不变;PTY 两层日志是新增层。
- **supervisor.py**:v3 扩展为"规则检测卡住 + 摘要喂纯判定 + 例外升级 agentic",但仍不判完成(归调度点①)。
