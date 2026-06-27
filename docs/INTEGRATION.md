# agent-transcript-miner × story-lifecycle 集成设计

> 把 miner 从静态脚本变"被工作流调用"。实施依据，其他 AI 领卡做，主窗口验收。
> 先读 `CONTEXT.md`（上手）和 `ROADMAP.md`（分析方向）。

## 核心认知（修正版，重要）
1. **定时扫描是兜底主力**：大部分工作（排查/小需求，约 114/323 会话）**不走 story-lifecycle**，靠定时入库 + playbook 反哺。playbook 是**任务类型粒度**（debug/需求开发/SQL…），对所有任务——包括非 story 的——都有效，**且已落地**（4 skill 已接入）。这是 miner 对日常工作的主要价值。
2. **story 绑定要主动，不能让 miner 猜**：当前 miner 用 cwd+ts+id-mention 猜，走 story 的会话只绑 **4%（4/83）**，且有误绑（1065518 一个 story 占 41 会话）。正解：story-lifecycle 在 `inject_prompt` 时主动写 story↔session 锚点，miner 直接读。
3. **story-lifecycle 钩子是增强**（只对走 story 的少数需求）：注入历史 context（⑩b）/ done 复盘。不是主力。

## 当前绑定问题诊断
- 总 323 会话，绑 story_id 60 (18%)；但走 story 迹象（first_ucmd 含 tapd/story/spec）的 83 个**只绑 4 (4%)**
- stories 表只 9 个（大量 story 没 `.story/` 目录）
- link 时间窗误绑：单 story（1065518）占 41 会话，明显不合理

## 端到端数据流（修正版）
```
┌─ 所有对话（排查 / 小需求 / 走story的需求）─────────────────┐
│  agent → 全局 transcript (~/.claude/projects 等)            │
│    ↓ ① 定时扫描【主力兜底，每日 store 增量 + 每周重算】      │
│  transcripts.db                                              │
│    ↓ ② 离线挖掘【任务类型粒度，对所有任务有效，不依赖 story】 │
│  playbooks / 约束库 / 失败模式 → 反哺 hc-all skill【已落地】  │
└──────────────────────────────────────────────────────────────┘
┌─ 走 story 的需求（少数，额外增强）──────────────────────────┐
│  story-lifecycle inject_prompt(stage) 【主动写锚点】         │
│    → .story/runs/<story>/anchors.jsonl                       │
│    ↓ miner.link 读锚点精确绑定（替代猜测，I2）               │
│  ③ inject_prompt 注入该 story 历史 context（⑩b，I3）        │
│  ④ done_cmd 复盘该 story → .story/done/<id>/retrospect.md（I4）│
└──────────────────────────────────────────────────────────────┘
```

---

## 任务卡

### I1 定时扫描兜底 `[miner侧]` `[已完成]`
- **现状**：`store` 全量 discover ~50s，只能手动跑；排查/小需求对话无法自动入库。
- **目标**：定时增量入库 + 定期重生成 playbook，让 db 和 playbook 跟着最新对话走。
- **步骤**：
  1. `miner/store.py` 加 `--since Nd` 参数：discover 时跳过 mtime < now-N 天的文件（增量只 re-parse 近期，<10s）
  2. `scripts/refresh.sh`：每日 `store --since 1`；每周 `store`（全量）+ `generate_playbooks.py`（重算 playbook）
  3. 给出 cron/计划任务配置（Windows 用 schtasks 或 loop skill）
- **验收**：① `store --since 1` <10s 且只处理近期文件；② refresh.sh 跑通，db 行数增长、playbook 时间戳更新；③ 给出可挂 cron 的命令
- **约束**：增量逻辑不能破坏全量；store 的 mtime 增量已存在（sources 表），--since 只加 discover 层过滤

### I2 story↔session 主动绑定 `[跨项目]` `[已完成]`
- **现状**：miner link 用 cwd+ts+id-mention 猜，走 story 的会话只绑 4%，且误绑。
- **目标**：story-lifecycle 主动暴露锚点，miner 精确绑定，绑定率 → >80%、零误绑。
- **接口契约**：
  - story-lifecycle 侧：`adapter.inject_prompt(prompt, story_key, stage)` 启动会话时，追加写 `<ws>/.story/runs/<story_key>/anchors.jsonl`，每行：
    `{"story_key":..., "stage":..., "adapter":..., "cwd":..., "ts":"<iso 精确时刻>", "prompt_hash":...}`
  - miner 侧：`link.py` 优先读 anchors.jsonl——对每条锚点，用 `(cwd 匹配 ws) + (ts 之后该 cwd 的最近 session)` 精确命中一个 session，回填 story_id；anchors 没覆盖的才退回旧的 cwd+ts 宽窗（低置信）
- **步骤**：① story-lifecycle inject_prompt 加写 anchors（几行）；② miner link.py 加 read_anchors + 精确匹配优先；③ 重跑 link 对比绑定率
- **验收**：① 走 story 迹象会话的绑定率从 4% → >80%；② 无单 story 异常多绑（如 1065518 的 41 个要降下来）；③ anchors 命中的标 high-confidence，宽窗兜底的标 low
- **约束**：anchors.jsonl 是 story-lifecycle 写、miner 读（单向）；不改 inject_prompt 核心逻辑，只追加写

### I3 provider 注入（⑩b）`[已完成]`
- **现状**：recommend.py `--package` 已实现任务上下文包（<500 字），是 provider 雏形。
- **目标**：story-lifecycle inject_prompt 调 `miner.context_provider.get_context(story_key, ws, stage)` 填 `{transcript_context}` 占位。
- **依赖**：I2（注入该 story 的会话需要先可靠绑定）。
- **接口契约**：`get_context(story_key, ws, stage) -> str | None`；返回 None 则 prompt 不注入该段。
- **验收**：一个 hc-all story 的 design prompt 里 {transcript_context} 有相关历史上下文（非空、非乱码、和 story 主题相关）。

### I4 done 复盘钩子 `[已完成]`
- **现状**：`retrospect.py` 已模块化（T3 完成：`render_session(sid)` + 单会话/批量模式）。
- **目标**：story done 时自动生成该 story 的复盘，写进 `.story/done/<story_key>/retrospect.md`。
- **已完成**：
  ① `scripts/retrospect.py` 新增 `--story <story_key>` 模式，聚合该 story 所有绑定 session 生成合并复盘；
  ② `story-lifecycle/src/story_lifecycle/cli/list_cmd.py` 的 `done_cmd` 已调用 `scripts/retrospect.py --story <key>`；
  ③ 输出 `.story/done/<id>/retrospect.md`。
- **依赖**：I2（按 story_id 聚合 session 需要可靠绑定）。
- **验收**：done 一个 story 后，`.story/done/<id>/retrospect.md` 自动产出，含该 story 的访问文件/工具/踩坑/关键决策。
- **验证样本**：`D:/hc-all/.story/done/1064837/retrospect.md`

---

## 依赖与并行
- **I1 独立**（miner 侧，不依赖 story）：本窗口可立即做，**覆盖全部对话，排查/小需求立刻受益**
- **I2 是 I3/I4 的前置**（绑定不准，注入/复盘都不准）：优先做
- **I2 跨项目**（story-lifecycle 写 + miner 读）：建议 ⑩b 窗口（它已在改 story-lifecycle）
- I3（⑩b 进行中）、I4 依赖 I2

## 验收约定
- 每卡完成后产出：代码改动 + 一份 `scripts/out/<task>-verify.md`（做法/前后对比数字/未决）
- 改 story-lifecycle 的（I2 写锚点 / I3 / I4 钩子）：遵循 story-lifecycle 的 adapter 模式，不破坏 inject_prompt 核心
- 改 hc-all 的（无，本集成不动 hc-all skill——playbook 接入已-done）
- 主窗口验收：跑各卡 verify + 检查绑定率/增量耗时等硬指标

## 数据红线
- transcripts.db / .story/runs 含真实对话（金融 PII），**不入 git**；anchors.jsonl 含 story_key+ts（非 PII）可入库
- 详见 CONTEXT.md

---

## 派发 prompt（复制到其他窗口启动）

### I1 定时扫描（miner 侧，独立，已完成）
```
实施 agent-transcript-miner 的 I1（定时扫描兜底）。
先读 D:/github/story-lifecycle/packages/story-miner/docs/INTEGRATION.md 的 [I1 卡] + CONTEXT.md。
做：miner/store.py 加 --since Nd（discover 层时间过滤，<10s）；scripts/refresh.sh（每日增量/每周全量+重生成 playbook）；Windows schtasks 定时命令。
运行：source .venv-monorepo-test/Scripts/activate
验收按 I1 卡：store --since 1 <10s、全量回归正常、refresh.sh 可跑、给 schtasks 命令；结果写 packages/story-miner/scripts/out/i1-verify.md。
```

### I2 story↔session 主动绑定（跨项目，已完成）
```
实施 agent-transcript-miner 的 I2（story↔session 主动绑定），跨 story-lifecycle + miner。
先读 D:/github/story-lifecycle/packages/story-miner/docs/INTEGRATION.md 的 [I2 卡]（含接口契约）+ CONTEXT.md。
做：① story-lifecycle adapter.inject_prompt 启动会话时写 <ws>/.story/runs/<story_key>/anchors.jsonl（story_key/stage/adapter/cwd/ts精确/prompt_hash）；
   ② miner/link.py 加 read_anchors，优先用(cwd→ws + ts之后该cwd最近session)精确命中回填 story_id，anchors 没覆盖的退回启发式（id-mention / branch-match）标 low-confidence。
验收按 I2 卡硬指标：走 story 迹象会话(first_ucmd 含 tapd/story/spec)绑定率 >80%（hc-all 已达成 80.4%）；消除单 story 异常多绑(1064837 从 84 降到 5)；anchors 命中标 high-confidence。写 packages/story-miner/scripts/out/i2-i4-verify.md。
```

### I3 / I4 状态 `[已完成]`
- **I3（provider 注入）**：`story-lifecycle` 默认启用 `miner.story_context_provider`，`design/build/verify` prompt 自动注入 `{transcript_context}`。
- **I4（done 复盘钩子）**：`story done <key>` 调用 `retrospect.py --story <key>`，且使用 `sys.executable` 避免 PATH 错乱。
- 验证文档：`packages/story-miner/scripts/out/i2-i4-verify.md`。

### 验收（主窗口）
各卡完成后，主窗口：① 读 `scripts/out/iN-verify.md`；② 跑硬指标复核——I1: `store --since 1` 实测耗时；I2: 重跑 link 看绑定率 4%→? 且 1065518 误绑消除；③ 给验收结论。

