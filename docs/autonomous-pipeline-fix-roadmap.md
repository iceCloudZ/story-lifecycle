# 全自动 AI Coding 流水线:断点审计与修复路线图

> **取证日期**:2026-07-03
> **方法**:6 段并行只读子代理(systematic-debugging Phase 1 取证)+ curl 实跑验证
> **目标**:让 `story-lifecycle` 的全自动 FC 流水线真正无人值守跑通 `design → implement → test`
> **结论**:LLM 配置层已 curl 验证通过;**全自动跑不通全是代码层**,归到 5 个根因 A–E,按 `A → (B∥D) → C → E` 修。
>
> ⚠️ 行号基于 2026-07-03 代码,随重构会漂移;动工前按符号名复核。

---

## TL;DR

| 根因 | 一句话 | 严重度 | 成本 |
|---|---|---|---|
| **A** headless 死代码 | 生产 `graph.py:187` 永远走 PTY,无头正确逻辑不可达 | 🔴 致命 | 低(接线) |
| **B** done 路径三套 | resume poller 找 `.story/done/`,planner 默认 `.story-done/` | 🔴 致命 | 中 |
| **C** verify 硬闸常开 | FC 路径零 `create_finding` → gate 恒 advance | 🔴 质量 | 中(有分叉) |
| **D** 异常不收敛 | `run_story` 吞异常不回写 `failed` → 永卡 active | 🔴 运维 | 低 |
| **E** 飞轮回注全断 | knowledge 未装 + `D:/hc-all` 不存在 + `out/` 无产物 | 🟡 暖启动 | 高 |
| ~~F~~ LLM 配置 | ~~model 名可能无效~~ → ✅ curl 验证全通,**排除** | — | — |

**最关键洞察**:`ARCHITECTURE.md` 宣称"全自动 FC 能跑",但代码里 headless 那套正确逻辑(`planner.py:593-616` + `_kill_headless`)是**死代码**,生产走的是 PTY 半成品。calculator real-AI E2E 绿很可能走的是测试里直接传 `headless=True` 的路径,**与生产无人值守不是同一条路径**——别被它误导。

---

## 设计意图(全自动 FC 链路)

```
规划  api.py POST /plan/stream → planner.py:170 run_orchestrator_agent
      (FC 循环 llm.invoke_with_tools) 产出 _agent_actions,写 _plan_confirmed=False 暂停
        ↓ 前端 confirm
确认  api.py POST /plan/confirm → graph.py:203 start_story_async(线程池)
        ↓
执行  planner.py:426 continue_orchestrator_agent while 循环:
        launch adapter(claude/codex)经 pty.py 启 CLI → 轮询 .done
        → gate.py:190 run_verify_gate 硬闸 → advance / retry / fail
        ↓
飞轮  adapter 写 anchors.jsonl → miner 回读 → knowledge 契约包 → context_providers 回注入
```

---

## 断点全景(逐段)

| 段 | 状态 | 关键断点 |
|---|---|---|
| 规划+确认+LLM | ✅ 代码通 / ⚠️ F 已排除 | 仅 LLM 偶发空 tool_calls 静默产出空 plan(`planner.py:273-275`) |
| 执行调度+resume | 🔴 | done 路径不一致致 poller 命中率为零;`run_story` 吞异常不回写 failed;重启一律改 paused |
| 执行循环+retry | 🔴 | retry 是硬编码 `actions.insert()`(非 LLM 重新规划);PTY 超时分支不 kill 进程;marker 不清除 |
| adapter+PTY | 🔴 | 生产永远 `headless=False`;PTY 路径无超时/无 kill/无 done 兜底;done 纯靠 CLI 自写 |
| gate+verify | 🔴 | FC 路径零 `create_finding` → gate 恒 advance;不跑测试不读 done 字段;evaluator_loop 事实不可达 |
| 飞轮+knowledge | 🔴 | knowledge 包未装 + `_KNOWLEDGE_ROOT` 硬编码 `D:/hc-all` 不存在;kb.py 读 `out/*.json` 无产物;全静默 |

---

## 根因详解 + 修法

### 根因 A:headless 路径写对了,生产没接(字面破口)

**证据**
- `orchestrator/engine/graph.py:187` `planner.continue_orchestrator_agent(story_key)` 不传 `headless` → 默认 `False`(`planner.py:426`)。
- 全包 grep `headless=True` 零命中(除测试)。
- headless 正确逻辑在 `planner.py:593-616` + `_kill_headless:355-380`(done 一出现即 `taskkill /F /T` 杀进程树),但**不可达**。
- PTY 路径(`planner.py:617-624` `ensure_agent_pty`)无超时/无 kill:所有 kill 逻辑挂 `headless_proc`,PTY 分支它恒 `None`。

**影响**
- 生产跑的是裸交互 `claude`(PTY),要 TTY、弹权限确认,无法无人值守。
- claude 不退出 → 僵尸进程泄漏(只有 `atexit` `cleanup_all` 兜底)。

**修法**
1. `graph.py:187` 让全自动模式传 `headless=True`(或加 profile 配置 `headless: true`):
   ```python
   planner.continue_orchestrator_agent(story_key, headless=True)
   ```
2. `knowledge/adapters/claude.py:30-38` `headless_launch_cmd` 补 `--model`:
   ```python
   return [resolve_executable("claude"), "-p", "--model", model,
           "--allowedTools", ..., "--permission-mode", "acceptEdits"]
   ```
3. codex adapter 没有 `headless_launch_cmd`(`base.py:100` 返回 None)→ 决策:
   - 补 codex headless(研究 codex CLI 的无头模式),或
   - 文档标注 codex 仅 PTY,全自动限定 claude。
4. headless 路径 anchors 写入已在 `planner.py:582-591` 补好,无需动。

**验证**
- 单测:mock adapter,断言 `graph.py` 传 `headless=True`、launch_cmd 含 `--model`。
- 集成:headless 跑 minimal profile story,`tasklist | grep claude` 确认 done 后进程被 kill、无残留。

---

### 根因 B:done-file 路径三套不收敛 + 无兜底

**证据**
- poller 入口:`service/api.py:254` → `infra/paths.py:stage_done_file` = `.story/done/{key}/{stage}.json`。
- planner 默认:`planner.py:495-498` `f".story-done/{key}-{stage}.json"`(旧扁平)。
- plan_step tool 默认:`planner.py:295` 同款旧布局。
- verify retry:`planner.py:771` `f".story/done/{key}/verify-roundN.json"`(新)。
- done 文件纯靠 prompt:`planner.py:903-906` 文字要求 CLI 写,lifecycle 零兜底。
- `story_service.py:56` 还在告警 legacy `.story-done/`。

**影响**
- LLM 填的 done_file 路径若不正好等于 `stage_done_file()` → resume poller(`find_ready_interactive_stories`)永远找不到 → `resume_ready_interactive_stories` 永远空。
- CLI 忘写 done → 干等 30min 超时 fail。

**修法**
1. 统一默认:`planner.py:495-498` 和 `planner.py:295` 改成调用 `stage_done_file(workspace, story_key, stage)`(从 paths.py 取单一真相)。
2. prompt 里给 CLI 的 done 路径也用 `stage_done_file()` 渲染(检查 `_build_cli_prompt` / prompt_renderer 一致)。
3. (强化)lifecycle 侧加 done 兜底:poll 超时且 CLI 进程已退出、产物存在时代写 done 标记;否则超时即 fail,不无限等。

**验证**
- 单测:LLM 不传 done_file 时,planner 产出 action 的 done_file == `stage_done_file()`。
- 集成:跑 story,done 写出后 `find_ready_interactive_stories` 能命中。

---

### 根因 C:verify gate 硬闸常开

**证据**
- gate 判据:`evaluation/gate.py:213` `db.get_open_findings(story_key, min_severity="high")`;`gate.py:214-215` 空则 advance。
- FC 自动路径**零 `create_finding` 调用**(grep 全包:只有 `review_feedback.py:import_review` 被 CLI/HTTP 手动触发、`seed_pipeline.py:775` 离线、测试)。
- gate 不跑测试、不读 done 的 `build_passed`/`tests_passed`(done schema `infra/prompts/verify.md:33-34` 定义了这俩字段,全代码非测试无读取)。
- 连锁:`planner.py:769-797` retry 是硬编码 `actions.insert()`(不重新规划);`evaluator_loop.py` 被 gate import(`gate.py:205`)但 findings 恒空 → 事实不可达。

**影响**
- verify"通过"= LLM 在 done.json 写一行 summary,质量门形同虚设。
- retry 机制永不触发(无 findings)。

**修法(有设计分叉,推荐组合)**
- **选项 1(最小,推荐先做)**:gate 读 done 文件的 `build_passed`/`tests_passed` 字段,空/false → retry。改 `gate.py:213` 附近,加从 `last_done_data` 读布尔的方法。
- **选项 2(强)**:gate 真跑 profile 的 `verification_commands`(`strict.yaml:59` `[pytest, ruff]`),读退出码写 finding。需在 gate 里加 `subprocess` 执行。
- **选项 3(接现有)**:FC verify stage 后自动调 `review_feedback.import_review`(LLM judge 生成 finding),让 gate 查 finding 有数据。LLM judge 调 deepseek(已 curl 验证可用)。

推荐 **1+2 组合**:done 字段快速判定 + 关键命令真跑。

**验证**
- 单测:done `build_passed=false` 时 gate 返回 retry;`round_count > max_retries` 时 fail。
- 集成:故意提交测试失败的 implement,确认 gate retry 到 `max_retries` 后 fail。

---

### 根因 D:异常/状态不收敛

**证据**
- `graph.py:188-200` `run_story` except 块只 `log.error` + 写 `graph_error.log`,**无 `db.update_story(status="failed")`**。
- `recover_orphan_stories`(`graph.py:279-286`)重启时把所有 active **无条件改 paused**。
- `_active_execution` marker(`planner.py:637-646`)写入后从不清除(grep 无 del/pop/None)。

**影响**
- planner 未捕获异常 → story 永卡 active,done file 不出现 → `find_ready_interactive_stories` 也救不了 → 只能重启 → 重启又改 paused → 人工。

**修法**
1. `graph.py:188-200` except 加:
   ```python
   db.update_story(story_key, status="failed", last_error=str(e)[:500])
   ```
2. `recover_orphan_stories`:保留 paused 默认(避免重启即重 spawn,注释说是刻意的),但加日志 + 可选配置 `auto_resume_on_restart`。
3. (可选)`continue_orchestrator_agent` 完成/失败时清 `_active_execution` marker。

**验证**
- 单测:注入 raise,断言 story status 变 failed。
- 集成:重启后 active story 被标 paused 且 UI 有"继续执行"入口。

---

### 根因 E:飞轮回注端三处全断(静默)

**证据**
- `knowledge/context_providers/knowledge_provider.py:198-201` try import knowledge → ImportError 返回 `""`。本机 knowledge 包未装。
- `_KNOWLEDGE_ROOT` 硬编码 `D:/hc-all/.story/knowledge`(`knowledge_provider.py:51-53`),目录不存在。
- 全自动实际走 `build_kb_tool_section`(`planner.py:861`)→ `kb.py`(`packages/story-miner/scripts/kb.py:104`)读 `out/result_axis_phase2.json` / `bug_story_graph.json` / `story_task_types.json` —— `out/` 不存在 → kb.py fallback 空。
- transcript provider(`context_providers/__init__.py:59-68`)依赖 `miner.config`,本机 miner 未装 → 返回 None → `transcript_section=""`。
- 三条全被 try/except 静默吞,prompt 照渲染无告警。
- INDEX.json disjoint:key 归一化不对齐(miner `task_type_playbooks.py:185-192` 用 `11<ws>00` 前缀,lifecycle 用 db story_key)。

**影响**
- LLM 零经验盲跑,操作者无感。anchor 写入实装,回注全断。

**修法**
1. `pip install -e packages/knowledge`(补 AGENTS.md setup 步骤)。
2. 准备知识目录,或改 `_KNOWLEDGE_ROOT` 为 config/workspace 驱动(去掉硬编码 `D:/hc-all`)。
3. 跑通 miner 产物链生成 `out/*.json`:
   ```bash
   python -m miner.store --since-days N
   python -m miner.story_ingest
   python -m miner.link
   # 然后 bug_story_graph / classify_story_task_type / result_axis_phase2
   ```
4. key 归一化:统一 miner 写入与 lifecycle 查询的 story_key(去 `11<ws>00` 前缀的 normalize 抽到共享处)。
5. 去静默:knowledge_provider 返回空时 `log.warning`,并在 story context 标注"飞轮未转"。

**验证**
- 装 knowledge 后 `KnowledgeIndex.retrieve` 返回非空。
- 跑 story,prompt 里 `knowledge_section` 非空。

---

### 根因 F:LLM 配置 — ✅ 已 curl 验证排除(2026-07-03)

```
GET  /v1/models                       → 200  (deepseek-v4-flash / deepseek-v4-pro)
POST /v1/chat/completions             → 200  ✓ 正确返回 tool_calls(function calling 工作)
     model=deepseek-v4-pro, tools=[ls]
POST /v1/chat/completions             → 200  ✓
     model=deepseek-chat
```

key 有效、`deepseek-v4-pro` 是有效 model 名、function calling 正常。**LLM 层非断点**,原"model 名无效"假设被推翻。唯一残留:LLM 偶发返回空 tool_calls 时静默产出空 plan(`planner.py:273-275`)——建议加显式报错/重试,非阻塞。

---

## 修复路线图(依赖与顺序)

```
A (接 headless)  ──┐
                   ├──→ 全自动能"无头跑起来"
B (done 路径)    ──┤
D (异常回写)     ──┘

C (verify 证据)  ──── 依赖 A/B(跑起来才有 verify)→ "质量可信"

E (飞轮产物)    ──── 独立,非阻塞全自动,阻塞暖启动 → "越跑越聪明"
```

推荐顺序:**A → (B ∥ D) → C → E**。F 已排除。

---

## 端到端验证清单(怎么知道全自动真跑通了)

1. **配置**:`story doctor` 全绿;`pip install -e packages/knowledge`。
2. **headless smoke**:`story serve` + POST `/plan/stream`(minimal profile,claude adapter)→ `/plan/confirm` → **不碰 UI** → story 状态走 `planning → active → completed`。
3. **进程卫生**:跑完后 `tasklist | grep claude` 无残留(根因 A 验证)。
4. **resume**:中途重启 `story serve`,active story 标 paused,UI 点"继续"能推进(根因 D 验证)。
5. **质量闸**:故意提交失败测试的 implement,gate retry 到 `max_retries` 后 fail(根因 C 验证)。
6. **飞轮**:跑完 N 个 story 后 `out/*.json` 非空,新 story prompt 含 `knowledge_section`(根因 E 验证)。
7. **回归测试**:每个根因配一个回归测试(AGENTS.md 硬规则:TUI/CLI/workflow/orchestration 历史bug必须有回归测试)。

---

## 相关

- `packages/story-lifecycle/docs/ARCHITECTURE.md` — 设计意图(与本文"现状"对照,看差距)
- memory:`dev-flywheel-reality`(成熟度全景)、`tapd-deepseek-config-gaps`(F 已排除)、`real-e2e-flywheel-green`(calculator E2E 绿,但需对照是否生产路径)
- 取证产物:6 段子代理报告(2026-07-03 会话,本文件即汇总)
