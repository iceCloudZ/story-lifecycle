# Transcript 信号挖掘 — 探索候选清单

> 背景:从 code agent(主 Claude Code)的本地落盘机制里,挖出 story-miner 现在**没采**或**采错**的行为信号。
> 每个点先派子代理做初步探索(真实数据是否存在 + 价值评估),再决定是否立项。
> 生成:2026-06-27。关联:`miner/adapters/claude.py`、`miner/common.py`、`miner/store.py`。

参考:官方 [Checkpointing 文档](https://code.claude.com/docs/en/checkpointing)、[Agent SDK file-checkpointing](https://code.claude.com/docs/en/agent-sdk/file-checkpointing)。

## 候选点

### 1. Compact 摘要采集 + turn 污染修复
- **现状**:`claude.py:48` 把 user 文本计 `turns`;compact 摘要若以 user 角色出现且不以 `SYS_PREFIX` 开头,会被**误计入 turns / 污染 first_ucmd**,直接破坏 I2 绑定与 I3 注入质量。
- **价值假设**:修正计数准确性;摘要文本本身是 agent 自带蒸馏,喂 retrospect/distill 质量高于原始对话。
- **待探索**:真实 transcript 里 compact 事件字段名(`compactMetadata` / `isCompactSummary`?)、出现频次、如何识别。

### 2. Checkpoint / Rewind 作为失败信号
- **现状**:`file-history-snapshot` 顶层事件在 `claude.py:39` 被丢。checkpoint = 每次 Write/Edit/NotebookEdit 前的还原点。
- **价值假设**:`/rewind`(还原代码/对话)是**用户认证的"这条路走错了"**,比 `is_error` 更强的失败负样本;还能算**有效 churn(净保留 vs 毛写入)**。
- **待探索**:transcript 里 rewind 动作是否有记录、能否检测被回溯的 turn;checkpoint 元数据在 transcript 里的形态(内容 blob 在 sidecar,本次只看元数据)。

### 3. Subagent transcript 归因
- **现状**:dispatch 子代理在主 transcript 里只剩 `Task` 工具调用,子代理的活/token/失败**全归零到主会话** → stage_cost/workload 系统性偏低。
- **价值假设**:扫子代理独立会话 + `parent_sid` 关联,补齐成本/工作量。
- **待探索**:子代理 transcript 落盘位置/格式、能否 parent 关联;⚠️ 不能整段灌,需抽 meta+工具+失败并严格截断 mask。

### 4. Todo / Task 结构化
- **现状**:`TaskCreate`/`TaskUpdate` 被拍平成 `kind='tool', name='TaskUpdate'`,无法还原任务状态时间线。
- **价值假设**:解析 input(subject/status)→ 结构化 `todo` 事件/表,retrospect 可重建"agent 当时以为的计划"。
- **待探索**:Task* 工具 input 字段结构、能否稳定还原状态机。

### 5. think 事件补全
- **现状**:`common.py:5` 契约定义了 `think` kind,但 **claude adapter 从未 emit**;Claude 的 `thinking` content block 被静默丢弃。
- **价值假设**:补上后可做"思考占比/推理强度"分析,对 learn 曲线与 failure 分析是免费增益。
- **待探索**:thinking block 在 transcript 里的字段/体积、占比。

### 6. Session friction(会话摩擦指数)
- **现状**:slash command(`/compact` `/rewind` `/clear` `/init` `/fast`)被 `common.py:18` `SYS_PREFIX` + `real_user()` **当噪声过滤扔掉**。
- **价值假设**:这些是用户意图/摩擦金矿 → 新维度 `friction`(compact+rewind+clear+denial 加权),定位高摩擦会话优先复盘。
- **待探索**:command 事件在 transcript 的字段与频次,三端差异。

### 7. 被丢的量化信号(token / 时间间隙 / 工具拒绝)
- **现状**:token usage、turn 间 timestamp gap、工具 denial 全丢 → stage_cost 只能近似。
- **价值假设**:真实 token = 真实成本;时间 gap = 隐藏人工 review/debug 成本;denial = 负信号。
- **待探索**:三端(Claude/Codex/Kimi)transcript 里这些字段是否存在/格式。

### 8. Checkpoint UUID 作 session↔story 关联键
- **现状**:I2 用 cwd+ts 时间窗(~18% 命中)。
- **价值假设**:每条 user message 带 UUID(checkpoint 锚),是 session 内天然稳定键,可补强 `sessions↔stories` 绑定。
- **待探索**:UUID 是否稳定可得、与 `.story/runs/<key>/anchors.jsonl` 能否对齐。

## 探索结果(待子代理回填)

| # | 点 | 信号存在? | 价值 | 工作量 | 结论 |
|---|---|---|---|---|---|
| 1 | Compact + turn 修复 | ✅ 1.7% 文件,单会话可达 7 次 | HIGH | S | 立做:识别续跑摘要修 turn 污染;顺采 `compactMetadata.{preTokens,postTokens}` |
| 2 | Checkpoint/Rewind 失败信号 | ❌ rewind 零留痕;`file-history-snapshot` 多空 | LOW | — | 关掉,等上游给 `/rewind` 加事件 |
| 3 | Subagent 归因 | ✅ 474 文件,`sessionId`+`agentId`+`isSidechain` 现成 | MED | M | 可做:扩 discover glob `subagents/*.jsonl`;需 isSidechain 去重 |
| 4 | Todo/Task 结构化 | ✅ 29.2% 文件,状态机字段完整 | HIGH | M | 立做:还原 `pending→in_progress→completed` + `blockedBy` 依赖图 |
| 5 | think 补全 | ✅ 4163 块/140 文件,96.9% 明文 | HIGH | S | 立做:`claude.py` content 循环加一个 `elif pt=='thinking'` |
| 6 | Session friction | ⚠️ slash 稀疏;真信号是 `<turn_aborted>`(62 次) | MED | M | 可做:采 `compact_boundary` + `<turn_aborted>` 算 friction |
| 7 | 量化信号 token/gap/denial | token✅ / gap✅(被 `ts[:10]` 截断) / denial⚠️ | HIGH | S/S/M | 立做:先修 `claude.py:32` timestamp 截断 + 采 `message.usage` |
| 8 | UUID 关联键 | ⚠️ transcript 有 user-msg UUID,`anchors.jsonl` 不记 | MED | M | 缓:卡在 story-lifecycle 跨包,`inject_prompt` 拿不到将生成的 UUID |

## 优先级建议

**核心结论(反直觉)**:我们花最多力气讨论的 **#2 checkpoint/rewind 是死路**(rewind 在 transcript 无痕)。
真正的问题是相反方向 —— **adapter 正在主动丢弃 6 个已经在盘上的高价值信号**。

### Tier 1 — 立即做(高价值 + S 工作量,一笔过)
它们共享同一根因:`claude.py` 的 content-part 循环只认 3 种 part + `ts[:10]` 截断时间戳。
一次对 `adapters/claude.py` 的集中修改即可解锁大部分:
- **#5 think**:加 `elif pt=='thinking'` → 补 4163 条推理事件(纯增益,契约早有 `think` kind)。
- **#7a timestamp 截断修复**:`claude.py:32` 去掉 `[:10]` → 恢复秒级 ts,所有 turn 间 gap 分析才能跑(现在是 bug)。
- **#1 Compact + turn 修复**:识别 `"This session is being continued"` → 不计 turns、不污染 first_ucmd;顺采 compactMetadata token。
- **#7b token**:采 `message.usage`(Claude)+ `usage.record`(Kimi)。

### Tier 2 — 排期做(高价值 + M 工作量,需扩 schema + 重 ingest)
- **#4 Todo/Task 结构化**:独立 `todos` 表(状态机 + 依赖图),29.2% 会话受益。
- **#7c denial**:区分 permission denied vs 执行错误(MED,可选)。

### Tier 3 — 选做(中价值)
- **#3 Subagent 归因**:路径干净、关联键现成,但去重麻烦、数据量小。
- **#6 friction**:真抓手是 `<turn_aborted>`,不是 slash command。
- **#8 UUID 关联键**:卡在跨包,投入产出比一般,优先优化现有 cwd+ts 时间窗。

### ✅ 验证结果:思路 B(compact→失败聚类)不成立
脚本:`scripts/verify_compact_failure.py`(配对设计:每个 compact 边界前后各 K 个 tool_result 比 `is_error`)。
样本:1027 session 中 22 个含 compact 边界,共 40 个边界。

| K | 前(失败率) | 后(失败率) | 相对变化 | 配对 后>前 / 后<前 / 相等 |
|---|---|---|---|---|
| 10 | 19/400 = 4.8% | 23/379 = 6.1% | +28% | 8 / 6 / 26 |
| 20 | 38/792 = 4.8% | 34/732 = 4.6% | -3% | 12 / 14 / 14 |

**结论:不成立。** K=10 的 +28% 是 23 vs 19 个失败的小样本噪声(配对 8:6 近硬币),K=20 即消失(配对 12:14);40 个边界里 26 个两侧零错误(平局)。`is_error` 基率仅 4-6%、N=40,信号太弱。
**含义**:别为"compact 致错"建预测 feature。但 **#1 compact 采集仍该做** —— 价值在修 turn/first_ucmd 污染 bug + 拿 compactMetadata token,与失败聚类无关。

## 跨端补充探索(Codex / Kimi)

初次 8 点探索偏 Claude-only。补探 Codex/Kimi 原始 transcript 后,矩阵修正:

### 关键修正
- **"Kimi 零失败信号"是错的** —— Kimi `tool.result` 有 `isError:true`(含 "user rejected approval" = 拒绝信号),adapter 全丢了。**Kimi 是最被低估的一端**。
- **#1 compact / #4 todo 是 Claude-only**:Codex/Kimi 都不压缩、无 Claude 式 Task 工具。但 Kimi 有 `tools.update_store`(todo 状态)和 `turn.cancel`(摩擦)。
- **Codex reasoning 是加密的**(`encrypted_content`,`content:null`)—— 当 think 采也无用,跳过。只有 Claude/Kimi 的 think 是明文可用。

### 三端通用(最高杠杆,纯 bug / S 工作量)
| 信号 | Claude | Codex | Kimi | 现状 |
|---|---|---|---|---|
| timestamp 截断 | `ts[:10]` | `str(ts)[:10]` | ms→日期 | 三端都丢时分秒;改 3 个 adapter |
| token usage | `message.usage`(丢) | `token_count`(丢,含 cache/reasoning tokens) | `usage.record`(塞进 think) | 三端都有、三端都错位 |
| 失败/ok | `is_error` ✓ | 启发式 | `isError`(丢!) | Kimi 补 isError 即解锁 failure_mode |

### 各端 bonus 信号(adapter 漏采,原始数据有)
- **Codex**:`patch_apply_end`(文件改动+success —— 补上 Codex 没有 code 事件的缺口)、`session_meta` git(commit/branch/repo)、`turn_context`(每回合 cwd/权限)、`task_started/complete`(`turn_id`)。
- **Kimi**:`permission.record_approval_result`(审批/拒绝)、`turn.cancel`(用户中断 = 摩擦,等价 Claude `<turn_aborted>`)、tool `args`/`display`(补 cmd/path)、性能 `llmFirstTokenLatencyMs`/`finishReason`、`content.part` 的 `think`。

### 修订后的优先级
- **Tier 0(三端通用,S)**:① timestamp 截断修复(3 adapter)② Kimi `isError` 采集 ③ token 采集成独立 kind(三端,从 think 里挪出来)
- **Tier 1**:Claude think(明文)+ Kimi think(`part.type=think`);Codex reasoning 加密跳过。Claude compact+turn 修复(Claude-only)。Claude todo 结构化。
- **Tier 2(bonus)**:Codex `patch_apply_end`+git meta;Kimi denial / `turn.cancel` / tool args / latency。
- **关闭**:#2 checkpoint(Claude-only,dead);Codex reasoning(加密不可用)。

## 实施计划(分阶段)

**分阶段决策(2026-06-27)**:hook 推送架构(无损/实时/跨端)定为**二期**,已在 `hooks/` 起头(README + `emit.py` 骨架,未启用)。一期先**把原生 transcript 数据梳理好**(adapter 路径)—— transcript 是三端通用底座,hook 覆盖尚不齐。

### 一期:native data(adapter 修正)— 按此顺序
⚠️ adapter 一旦改动需**重建 `transcripts.db`**(重 ingest,见 CONTEXT.md)。

**Tier 0 — 三端通用,S(先做)**
1. **timestamp 截断修复**:`adapters/{claude,codex,kimi}.py` 去掉 `ts[:10]` / `%Y-%m-%d` 截断,存完整 ISO/毫秒。→ 所有 turn 间 gap 分析才能跑。schema 不变,只更精确。
2. **Kimi `isError` 采集**:`adapters/kimi.py` 在 tool result 提取 `isError` → `kind='result', ok=not isError`。→ 解锁 Kimi failure_mode(现在对 Kimi 全瞎)。
3. **token 独立 kind**:三端 token 各归位 —— Claude `message.usage`、Codex `token_count`(含 cache/reasoning tokens)、Kimi `usage.record`(从错位的 `think` 挪出)→ 统一 `kind='token'`。需 schema 决策(加列 vs 复用 text)。

**Tier 1 — 单/双端**
4. **think 采集**(Claude 明文 + Kimi `part.type=think`;Codex 加密跳过):`common.py` 已有 `think` kind;`adapters/claude.py` 加 `elif pt=='thinking'`,`adapters/kimi.py` 解析 `content.part`。
5. **Claude compact + turn 修复**:`adapters/claude.py` 识别 `"This session is being continued"` / `compact_boundary` → 不计 turns、单独 `compact` kind,采 `compactMetadata.{preTokens,postTokens}`。Claude-only。
6. **Claude todo 结构化**:`adapters/claude.py` TaskCreate/TaskUpdate → `kind='todo'` + 结构化字段(subject/status/activeForm),或独立 `todos` 表。

**Tier 2 — bonus(选做)**
7. Codex `patch_apply_end`(补 code 事件缺口)+ `session_meta` git;Kimi `permission.record_approval_result` + `turn.cancel`(friction)+ tool `args/display`。

**关闭**:#2 checkpoint(rewind 无痕);Codex reasoning(加密)。

### 二期:hook 推送架构 — 见 `hooks/README.md`
一期把 adapter 路径补齐后,Tier 0 的那些问题在二期 hook 路径上会从源头消失(timestamp/isError/token 实时无损采)。二期不阻塞一期。
