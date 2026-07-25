# 成果物驱动的 stage 完成 —— 设计文档

> 状态:**待评审**。创建:2026-07-26。
> 范围:`packages/story-lifecycle`(尤其 `orchestrator/engine/planner.py` 完成判定、`infra/db/models.py` 成果物表、`infra/terminal/pty.py` PTY 日志、prompt 模板);连带 `packages/story-miner`(link 机制)。
> 评审目标:验证"**砍掉 done 文件协议、改用成果物落地 + 人确认驱动 stage 推进**"的方案正确性、可行性、迁移路径;以及 PTY 全量日志 + 编排 LLM 按需读的"监工"模型是否成立。
> **本文自包含**:背景、代码现状、决策依据(含业界调研)、方案、分阶段实现、风险、开放问题全部内联,评审者无需读本对话历史。

---

## 0. TL;DR(评审者先读)

**现状**:code CLI(claude/opencode/kimi)干完一个 stage 后,要按自定义协议**额外写一个 `done.json`**("我完了"的信号文件);编排器(planner)轮询这个文件出现就推进下一 stage。

**问题**:`done.json` 是 code CLI **自称完成** —— 它会**撒谎**(写了 done 但成果物是空的/错的),也会**遗漏**(干完了没按协议写 → 编排器永久卡死,真实发生过:design 跑 25 分钟没出 done.json)。它把编排器锁在**被动盲等**位置,且给新 CLI 接入强加了"学一套写 done 协议"的负担。

**方案**:砍掉 `done.json`,改用 **stage 声明的成果物(`expected_outputs`)落地 + 人确认**驱动推进。核心论据:**story 的本质是产生可落地变更**(再简单的需求都有一句话设计 + 一行代码 + 一个测试报告;不改代码也有 SQL 要执行)—— 所以每个 stage **必然**有文件成果物,成果物本身就是完成的 ground truth,不需要再写一个"完成通知"。

```
现状(冗余 + 可撒谎):
   code CLI 产出 spec.md → 额外写 done.json("我完了") → planner 看 done 推进
                              ↑ 多余的中间人,且不可信

新方案(干净):
   code CLI 产出 spec.md(原子写) → planner 查 spec.md 存在 → 人确认 → 推进
   (成果物落地 = 完成的唯一证据;done 这个自称信号整个砍掉)
```

**配套**(都是为了让上面成立):
1. 成果物**版本化进 db**(复用现有 `story_doc` / `story_doc_version` 表 —— 它已经支持全版本历史 + FTS + `confirmed_by` 人工确认字段,几乎是为本方案预生的)
2. **PTY 全量日志**(两层:raw 字节 + 结构化 JSONL)—— code CLI 卡住时,supervisor 把 JSONL 路径喂编排 LLM 按需读,获取上下文判断怎么干预(平时编排 LLM 零 token)
3. 编排器**提供一个普通 CLI 工具**(`story-tool`),注入 prompt,code agent 调它完成"原子写 + 版本化 + 编排器感知"三合一
4. **gate 前置**:现有 `unified_gate`(LLM 质量评审)从事后移到"人确认前",把评审结论作为"建议确认/建议打回"的参考显示给人

**明确不做(本期)**:
- 交付追踪全链路(SQL/Nacos 执行、生产上线观测)—— 只在 `story_doc` **预留字段 + 提供 AI 提示词模板 + 写回端点**,不自己对接外部系统
- 编排 LLM 全程在线指挥 code CLI(业界已证伪不可靠,见 §2.3)

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

### 2.1 "编排 LLM 监工" vs "编排 LLM 指挥" —— 业界一边倒选前者

最初设想两种形态:
- **形态 A**:编排 LLM 全程在线,读 PTY 输出,主动打字指挥 code CLI(它说完了才算完)
- **形态 B**:确定性编排骨架(planner) + LLM 只在异常时被唤起监工

业界资料一致证伪 A、验证 B:

- **[Microsoft Conductor](https://opensource.microsoft.com/blog/2026/05/14/conductor-deterministic-orchestration-for-multi-agent-ai-workflows/)**(2026-05):主张**确定性编排**(YAML/代码定义 workflow),编排层**零 token**。让 LLM 动态路由 = 多花钱、更慢、更不可预测。
- **[Stop Letting LLMs Orchestrate Your AI Agents](https://www.abdelaziznotes.com/posts/stop-letting-llms-orchestrate-your-ai-agents)**:列 LLM 编排硬伤 —— LLM 无视委派自己干、长会话压缩后失去对子 agent 感知、无法强制并行、subagent 后台输出获取失败率 ~40%、无阶段级检查点失败只能从头重启。核心论点:**"orchestration is a code problem, not a prompting problem"**。
- **[Claude Code Issue #1770](https://github.com/anthropics/claude-code/issues/1770)**:社区强烈要求"父 agent 监控子 agent + 偏离时干预",方案是 `spawn_monitored`(拿句柄流式监听)+ `.pause()`/`.halt()` + `event_callback`。**Anthropic 尚未回应**(底层 `parent_tool_use_id` 已存在但没开放)—— 说明这是行业在追的方向,且**还没人有现成答案**,我们做 B 是走在前面。

**结论:形态 B 不是"退而求其次",是正确架构。** 编排骨架保持确定性(planner),LLM 只做监工(supervisor),且**只在异常时按需唤起**(不是全程在线)。

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

### 4.1 总览流程图

```
   ┌──────────────────────────────────────────────────────────────────┐
   │                    planner (确定性编排,零 token)                 │
   │                                                                  │
   │   stage 完成判据(唯一):                                       │
   │        该 stage 的 expected_outputs 文件全部落地(非空)?      │
   │              ├── 是 → 跑 gate(建议)→ 人确认 → 推进            │
   │              └── 否 → 继续等(supervisor 监工,见下)           │
   └──────────────────────────────┬───────────────────────────────────┘
                                  │
   ┌──────────────────────────────┼──────────────────────────────────┐
   │                              PTY                                 │
   │   ┌───────────────────────────┼──────────────────────────────┐  │
   │   │                           │     code CLI                 │  │
   │   │                                                                  │
   │   │   读 prompt(不再有"写 done 协议"段落;有 story-tool 用法)   │
   │   │      → 干活: 读文件/写代码/跑测试/写 SQL                    │
   │   │      → 调 story-tool declare <doc_type> <path>               │
   │   │           (工具内置:原子写 tmp→rename + 写 story_doc 版本化)│
   │   │                           │                                  │
   │   │                           ├─► PTY 两层日志(全量,见 §4.3):│
   │   │                           │     • raw 字节流(保真回放)    │
   │   │                           │     • events.jsonl(结构化)    │
   │   │                           │           │                      │
   │   │                           │   ┌───────▼──────────┐           │
   │   │                           │   │  supervisor      │           │
   │   │                           │   │  (监工,非裁决)  │           │
   │   │                           │   │                  │           │
   │   │                           │   │ 识别提问 → 通知  │           │
   │   │                           │   │ 卡住/异常 →      │           │
   │   │                           │   │   喂编排 LLM     │           │
   │   │                           │   │   (按需读 JSONL)│           │
   │   │                           │   │   → 判断怎么干预│           │
   │   │                           │   │                  │           │
   │   │                           │   │ ✗ 不判完成       │           │
   │   │                           │   │  (那是成果物事) │           │
   │   │                           │   └──────────────────┘           │
   │   │                           │                                  │
   │   │   ✗ 不再写 done.json(砍掉)                                 │
   │   └────────────────────────────────────────────────────────────┘  │
   └──────────────────────────────────────────────────────────────────┘

   成果物落地后:
        story_doc 表记录(version+1,latest_content 更新,local_path 指向 .md 缓存)
              ↓
        planner 查 expected_outputs 全落地 → 跑 gate(质量建议)
              ↓
        profile.confirm=true? → 人确认(confirmed_by 写入)→ 推进下一 stage
                       false? → 自动推进
```

### 4.2 stage 完成判定(精确语义)

**完成 = 该 stage 的 `expected_outputs` 声明的所有成果物文件存在且非空。**

`expected_outputs` 收敛成**纯文件路径/glob**(可机器检查,零 token):

| stage | expected_outputs(示例) | 检查方式 |
|---|---|---|
| design | `spec.md`(设计文档)、`research.md`(调研) | 文件存在 + 非空 |
| build | git 改动 | `git status --porcelain` 非空(代码天然在 git,不需 code CLI 额外产文件) |
| verify | `test_report.md` | 文件存在 + 非空 |

**版本号**:同一 stage 每次产出/重试 `current_version + 1`(`story_doc_version` 记录全历史,含 `change_reason`)。

**半成品防护**(原子写):成果物文件由 `story-tool declare` 产生(§4.4),工具内置"写 tmp(同目录)→ fsync → rename",code agent 不自己管原子细节。

### 4.3 PTY 全量日志(两层)

```
   .story/runs/<story_key>/pty_<stage>/
     ├── raw.log      PTY 原始字节流(含 ANSI 转义码)
     │                • 唯一 100% 保真回放终端(颜色/光标/清屏)
     │                • 零解析成本 append
     │                • 用途:终端回放 / PTY 协议调试 / 取证
     └── events.jsonl 结构化事件(每行一个 JSON)
                      • {ts, dir(in/out), type, text(已剥ANSI), tool_call?}
                      • 也记录编排器自己注入 PTY 的内容(supervisor 打字干预)
                        —— 否则会看到 code CLI 答非所问却不知是 supervisor 戳的
                      • 用途:喂编排 LLM 按需读 / 喂 miner 飞轮 / 卡住诊断
```

**正常完成也保留**(重要资产 —— 喂飞轮 + 事后复盘)。**只在 supervisor 报"疑似卡住"时**,编排 LLM 才被唤起,读 `events.jsonl` 获取上下文判断怎么干预(平时零 token)。

### 4.4 story-tool(code agent 用的 CLI 工具)

编排器提供一个**普通 CLI 可执行**(非 MCP,任何能跑 bash 的 code agent 都能用,包括最土的)。注入 prompt 告诉 code agent 怎么用。命令集:

```
story-tool workspace
  → 输出产出路径约定(spec 放哪、代码改哪、test_report 放哪)
  → code agent 据此知道去哪写、写什么

story-tool declare <doc_type> <path>
  → 三合一:原子写(tmp→rename)+ 写 story_doc 版本化 + 通知编排器感知
  → code agent 调它就完成了"产出落地",不用自己管原子写/db

story-tool todo
  → 查当前 stage 还缺哪些 expected_outputs(给 code agent 自检用)
```

**为什么是普通 CLI 而非 MCP**:MCP 要实现 server + code agent 要支持 MCP 协议(claude/opencode 支持,但更土的 CLI 不一定);普通 CLI 任何能 Bash 的 agent 都能用,接入成本最低(契合"支持任意 CLI"目标)。代价:code agent 用 Bash 调,多一层 shell。

### 4.5 gate 前置(人确认前跑,做建议)

现有 `unified_gate`(LLM 质量评审)从事后移到**人确认前**:

```
   成果物落地 → planner 查 expected_outputs 全齐
              → 跑 unified_gate(质量评审,产出 approve/打回 + 理由)
              → 把 gate 结论 + 成果物内容 展示给人
              → 人确认(confirmed_by 写入)/ 人打回(回 build 重试)
              → 推进
```

gate **不阻塞**(它给建议,人最终拍板)。后续 AI 接管确认时,gate 结论就是 AI 的输入。

### 4.6 交付追踪(本期只预留 + 提示词 + 端点)

`story_doc` 加预留字段(本期不实现对接,只留挂钩):

```sql
ALTER TABLE story_doc ADD COLUMN delivery_status TEXT;   -- 预留:未上线/部分/已上线
ALTER TABLE story_doc ADD COLUMN observed_status TEXT;   -- 预留:线上正常/异常/未观测
ALTER TABLE story_doc ADD COLUMN verified_payload TEXT;  -- 预留:AI 写回的验证结论+证据
```

story-lifecycle 提供两个东西(很轻,本期可做):
1. **提示词模板**(给 AI 巡检用):"验证 story X 是否真上线:查 <release_system> 看 commit 是否在生产版本、查 <db_exec_log> 看 SQL 是否执行、查 <config_center> 看 Nacos 是否落地;把结论写回。"
2. **写回端点**:`POST /api/story/{key}/delivery/verify` → 更新预留字段

AI(独立巡检 agent 或 code agent)按提示词去查外部系统(用它们各自 API/CLI),查完调端点写回。**story-lifecycle 自己不碰外部系统。**

---

## 5. 分阶段实现(按风险/收益排序)

| 阶段 | 内容 | 风险 | 收益 |
|---|---|---|---|
| **P1 最小闭环** | 砍 done;planner 改查 expected_outputs 文件存在;prompt 删"写 done 协议"段;迁移脚本(存量 done → story_doc) | 中(改完成判定核心) | 砍掉不可信信号,接入新 CLI 零协议成本 |
| **P2 版本化 + 工具** | story_doc 承载成果物(version+1);`story-tool` CLI(workspace/declare/todo);原子写内置 | 中 | 成果物可回溯;code agent 有统一产出入口 |
| **P3 PTY 日志 + 监工** | pty.py 落两层日志;supervisor 升级识别卡住 + 喂编排 LLM 按需读 | 高(改 supervisor 核心循环) | 卡住可诊断可干预(解决本次 design 卡 25min 场景) |
| **P4 gate 前置** | unified_gate 移到人确认前;UI 展示 gate 建议 + 成果物 | 中 | 人确认有依据;为 AI 接管铺路 |
| **P5 交付追踪挂钩** | story_doc 预留字段;写回端点;提示词模板 | 低 | 上线观测闭环(对接外部系统留后续) |
| **P6 飞轮同步** | miner link 机制从 done+anchors 改成 story_doc + 时间窗;消费 events.jsonl | 中 | 飞轮不破坏 |

P1-P2 是核心,P3-P6 是配套。**P1 单独就能跑通**(用裸文件检查代替 done),P2 让它变干净,后续逐步加。

---

## 6. 砍掉的东西 / 反模式

- **✗ done.json 文件**(整个完成协议从 prompt 删掉)
- **✗ code CLI 学"怎么写 done"的自定义协议负担**
- **✗ done 撒谎/遗漏导致的卡死和误推进**
- **✗ 编排 LLM 全程在线指挥**(业界证伪,见 §2.1)
- **✗ code CLI 自己声明完成**(不可信,改成果物证明)

**反模式**(评审重点 —— 这些不该出现在新设计里):
- 重新发明一个"完成信号文件"(等于 done 换名)
- 让 supervisor 判完成(它的职责是监工,完成判定归成果物)
- 让编排 LLM 全程读 PTY(贵且不可靠,只在异常按需读)

---

## 7. 风险与开放问题(评审重点)

### 7.1 "每个 stage 必然有文件产出"这个约束真的成立吗?

方案基石(§1.3)。需评审确认:
- 是否存在**纯决策类 stage**(只给结论不产文件)?—— 当前 profile 没有,但未来可能有(如"评审/会签")
- 若有,该 stage 的完成机制是什么?**候选**:让它也产文件(`review_verdict.md`)?还是允许"无文件成果物"走 supervisor 判断?(后者会把完成判定从确定性变成需判断,尽量避免)

### 7.2 迁移期兼容

存量 story 还依赖 done。方案:迁移脚本(把 done.json 转成 story_doc 记录)+ 新 story 走新协议、旧 story 跑完为止。**不双轨**(双轨=两套完成逻辑长期并存,维护噩梦)。评审:迁移脚本能覆盖所有存量形态吗?

### 7.3 atomic rename 在 Windows 的可靠性

杀软/索引服务占用文件时 rename 失败。方案:重试 + 指数退避(几 ms 内通常释放)。评审:重试上限到了仍失败怎么办?(候选:降级直接写 + 标记"非原子",让人确认时兜底)

### 7.4 PTY 日志体积

全量落盘可能很大(长 stage 几 MB)。方案:events.jsonl 剥 ANSI 后较小;raw.log 可设上限/轮转。评审:是否需要按 stage 完成后归档/压缩?

### 7.5 code agent 真会调 story-tool 吗?

依赖 code agent(claude/opencode)理解 prompt 里的工具用法并主动 Bash 调。claude/opencode 这种 agentic 的通常能;但"任意 CLI"里较弱的可能不会调,退化为自己写文件(绕过版本化)。评审:这种降级可接受吗?(候选:planner 兜底扫描约定路径,即便 code agent 没调 tool 也能发现成果物)

### 7.6 gate 前置的成本

每个 stage 人确认前都跑 LLM gate = token 成本上升。评审:gate 跑全程还是抽样?轻量 gate(查文件大小/覆盖关键点) vs 重 gate(LLM 评质量)的边界?

---

## 8. 决策溯源(为什么是这个形状)

| 决策 | 选择 | 拒绝的替代 | 依据 |
|---|---|---|---|
| 完成判定 | 成果物文件存在 + 人确认 | done 文件 / LLM 判完成 | done 不可信;LLM 判完成贵且误判(§1.2) |
| 编排形态 | 确定性骨架 + LLM 异常监工 | LLM 全程指挥(形态 A) | 业界证伪 A(§2.1) |
| 代码版本化 | git 引用 | 存 patch 进 db | 代码天然在 git;db 存 patch 膨胀 |
| 文档版本化 | story_doc 表(已存在) | 新建表 | 复用成熟机制(§3.3) |
| PTY 日志 | 两层(raw + JSONL) | 纯文本 / 纯 JSONL | 业界最佳实践(§2.2) |
| code 工具 | 普通 CLI | MCP server | 接入成本最低,支持任意 CLI(§4.4) |
| 半成品防护 | story-tool 内置原子写 | 让 code agent 自己原子写 | 减轻 code agent 协议负担 |
| 交付追踪 | 预留字段 + 提示词 + 端点 | 本期对接外部系统 | 范围控制;外部系统是独立域(§4.6) |
| gate 位置 | 人确认前(建议) | 事后评审 / 阻塞推进 | 给人依据,不阻塞(§4.5) |

---

## 附录 A:与现有架构契约的关系

- **AGENTS.md「Driver lifecycle」**:driver 假设"CLI lifecycle ⊆ driver lifecycle"。砍 done 后,`consume_orphan_done`(被动消费 done 文件)失去意义 —— 需改成"被动消费成果物文件"(打开 story 详情页时扫 expected_outputs 是否落地)。该不变量仍成立,只是信号源从 done 换成成果物。
- **AGENTS.md「Session-id model」**:不变。resume 仍看 session 现场,不依赖成果物(§D12)。
- **DESIGN-task-actions-and-grill-me.md**:task_actions 驱动 stage 语义不变;完成协议部分(prompt 里的"写 done"段)被本文档替换。
- **DESIGN-session-pty-id-model.md**:session/resume 机制不变;PTY 日志是新增层,不改 session id 模型。
