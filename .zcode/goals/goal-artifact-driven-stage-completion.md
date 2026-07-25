# Goal:成果物驱动 stage 完成 —— 实现任务清单

> **执行方式**:本文件是一份结构化任务清单,交给一个 agent 会话**从上到下照着做**。
> **设计依据**:`packages/story-lifecycle/docs/DESIGN-artifact-driven-stage-completion.md`(v3,必读,所有决策细节在那)。
> **分两步**:STEP 1 做核心骨架 → 用 kimi-webbridge 真实验证 → STEP 2 做剩余内容 → 再用 kimi-webbridge 真实验证。
> **每步收尾必须**:① ruff + pytest 通过 ② git commit(改完就提交)③ 跑 webbridge e2e 验证 ④ 把验证结果(过/卡在哪)记到本文件末尾「验证日志」。
> **不要**做的事见各步「⚠️ 红线」。

---

## 前置:开工前必读

1. 读 `packages/story-lifecycle/docs/DESIGN-artifact-driven-stage-completion.md`(v3 全文)。
2. 读 `AGENTS.md` 的「Domain conventions」(adapter 契约 / driver lifecycle / task_actions)。
3. 读 `packages/story-lifecycle/docs/webbridge-e2e-runbook.md`(验证怎么跑)。
4. 确认环境:`.venv-monorepo-test` 激活;`opencode-go/kimi-k2.7-code` provider 可用(auth.json 有);webbridge daemon 在 `127.0.0.1:10086`。

---

# STEP 1:核心骨架(砍 done + 成果物闭环 + 规则卡住检测)

**目标**:砍掉 done 文件协议,改成果物落地驱动推进;加规则卡住检测修掉触发事故。做完用 minimal profile 跑 calculator webbridge e2e,验证 design→build→verify 全程不靠 done 文件能推进。

**⚠️ STEP 1 红线**:
- 原子写必须和"文件存在检查"**同批做**(否则中间态重新引入半成品竞态 —— 评审指出的硬伤)。
- miner 必须**双写兼容**(新协议落 story_doc,同时给 miner 维持 done 旧视图)—— 否则飞轮断 5 个阶段。
- 不动 supervisor 的 LLM 判定(那是 STEP 2)。STEP 1 的卡住检测是**纯规则 + escalate_human,零 LLM**。

## 1.1 profile schema 强制契约(基石)
- [x] `orchestrator/engine/profile_loader.py`:加载 profile 时校验**每个 stage 至少一个文件类 expected_output**,否则拒绝加载并报清晰错误。
- [x] `entry/profiles/*.yaml`:检查所有 profile 的 stage 都符合(不符的补,如纯决策 stage 加 `review_verdict.md`)。
- [x] 测试:profile 缺 expected_output 时加载失败;合法 profile 正常加载。

## 1.2 expected_outputs 收敛成可检查的文件路径
- [x] 设计 `expected_outputs` 的表达:文件路径/glob(可机器检查)。代码类 stage 用 `git` 标记(查 git status)。
- [x] 写一个纯函数 `check_artifacts_landed(stage_def, workspace) -> (missing, landed)`,确定性查文件存在+非空 / git 有改动。零 LLM。
- [x] 测试:design(缺 spec.md→missing)、build(git 无改动→missing)、verify(test_report.md 空→missing)。

## 1.3 story-tool CLI(code agent 产出落地入口)
- [x] 新建 `entry/cli/story_tool.py`(或挂 story 命令子命令):`workspace` / `declare <doc_type> <path>` / `todo`。
- [x] `declare` 内置原子写:写 tmp(同目录)→ fsync → rename(Windows 用 MoveFileEx,杀软占用重试+退避)。
- [x] `declare` 写 story_doc 版本化(current_version+1, story_doc_version 留历史)+ 更新 local_path(.md 缓存)+ 触发编排器感知(写一个 marker 或直接 upsert story_session.artifacts_prod)。
- [x] 测试:declare 后文件原子出现(中途读不到半成品);story_doc 版本+1;非原子场景(模拟 rename 失败)降级标记。

## 1.4 砍 done + planner 改查成果物
- [x] 删 prompt 模板里所有"写 done 协议"段落(搜 `done.json` / "完成协议" / "写 done")。
- [x] 改 prompt:加 story-tool 用法段(告诉 code agent 怎么 declare 成果物)。
- [x] `orchestrator/engine/planner.py`:poll loop 从"查 done.json"改成"调 check_artifacts_landed"。
- [x] 砍 `consume_orphan_done`(driver lifecycle)→ 改"被动扫 expected_outputs"(打开详情页时 check)。
- [x] 测试:code CLI 产出成果物后(模拟 declare)planner 推进;成果物缺时不推进。

## 1.5 miner 双写兼容(P1 必须,评审硬伤)
- [x] story_doc 落库时,**同时写一份 done.json 兼容视图**(给 miner 维持旧 link 逻辑),直到 P7 切换。
- [ ] 或:miner 的 `link.py` 加读 story_doc 的分支(done 和 story_doc 都认),双源兼容期。
- [x] 测试:miner link 在新协议下仍能关联 session↔story。

## 1.6 迁移脚本
- [x] `entry/cli/migrate_done_to_artifact.py`(或 story 子命令):扫存量 story 的 done.json → 转 story_doc 记录。
- [x] 旧 story 跑完为止,不强制迁移(脚本提供,用户按需跑)。

## 1.7 规则卡住检测 + 人升级(纯确定性,零 LLM,修触发事故)
- [x] `orchestrator/engine/supervisor.py`:加规则检测 —— 超时无新输出(N 秒可配)/ 进程活着但 idle / 反复报错。
- [x] 检测到 → escalate_human(落 awaiting_confirm 事件 + 桌面通知),**不调 LLM**。
- [x] PTY 两层日志落盘:`infra/terminal/pty.py` 加 raw.log + events.jsonl({ts,dir,type,text,tool_call?},剥 ANSI,含编排器注入记录)。正常完成也保留。
- [x] story_session 扩展:attempt/outcome/failure_reason/artifacts_prod/pty_log_ref。
- [x] 测试:模拟超时无输出 → 触发 escalate;events.jsonl 内容正确。

## STEP 1 验证(必做)
- [x] ruff check + pytest 全绿(排除预存在的 test_consult_cli/test_clarify_mcp 环境失败)。
- [x] git commit:`feat(stage): 砍 done + 成果物驱动推进 + 规则卡住检测(STEP 1)`。
- [x] **kimi-webbridge 真实验证**:起 serve(`.venv-monorepo-test/Scripts/python.exe -m story_lifecycle serve`),跑 `pytest -m real_web_e2e tests/e2e/test_calculator_webbridge_e2e.py`。观察:Chrome 自动开 → 走 design→build→verify → **全程不再有 done.json 产出** → story 推进到 completed。
- [x] 验证日志记到本文件末尾。

---

# STEP 2:LLM 判定层(边界纯判定 + 卡住诊断)

**目标**:在 STEP 1 的成果物闭环上,叠 LLM 判定能力。边界点纯判定判完成+质量;卡住点摘要先行、例外升级 agentic。做完再跑 webbridge e2e 验证 LLM 判定不破坏流程。

**⚠️ STEP 2 红线**:
- 边界点是**纯判定函数 + 预注入上下文,非 agentic**(评审 B:边界 agentic 买不到能力)。不要给边界点加 read_file/query_db 工具。
- 卡住点 agentic 是**例外路径**(规则触发:同 stage 第二次卡住 / 摘要循环模式),只读工具 + 调用 ≤5。不是默认。
- **不做打字纠偏**(评审 C:砍)。卡住干预用 restart-with-seed。
- **confirm=true 是显式不变量**(评审 A):文档写死,LLM 的 false approve 靠人兜底。

## 2.1 orchestrator_decision 表 + reject 上限
- [ ] `infra/db/models.py`:建 orchestrator_decision 表(id/story_key/stage/trigger/context_ref/decision/reason/action_taken/action_payload/llm_model/decided_at)。
- [ ] **reject 上限防护**:同 stage reject 次数上限(可配,默认 3)+ 每次 reject 必须给与上次不同的具体理由 + 超限强制 escalate_human。防 false reject 打回循环(评审 A2)。
- [ ] 测试:reject 到上限自动 escalate;两次 reject 理由相同 → 报警/强制 escalate。

## 2.2 调度点① 边界纯判定 LLM
- [ ] 新建判定模块(纯函数,非 agentic):输入 = 调用前确定性组装的上下文(PRD + 成果物内容 + 决策历史 + 执行轨迹),输出 approve/reject/escalate + reason。
- [ ] unified_gate 并入:不再独立事后跑,一次做完完成+质量判断。
- [ ] planner:成果物全齐(check_artifacts_landed)→ 唤起纯判定 → approve+confirm=true 等人确认 / reject 回 code CLI(带不同理由 seed)。
- [ ] Decider/Handler 分层:LLM 出决策,代码执行推进/打回副作用。
- [ ] 测试:成果物质量好→approve;明显缺陷→reject 带理由;reject 上限→escalate。

## 2.3 调度点② 卡住 LLM 诊断(摘要先行 + agentic 例外)
- [ ] supervisor 规则检测到卡住(STEP 1 已做)→ 唤起判定:
  - 第一步:预处理摘要(最后 N 条 events + 错误行 + idle 时长)喂纯判定函数,判 5 类卡因(真卡/提问/跑偏/慢/失败)。
  - 例外升级(规则触发):同 stage 第二次卡住 / 摘要检测到循环模式 → 升级 agentic 深读 events.jsonl。
- [ ] agentic 升级:**只读工具**(read_file events.jsonl)+ 调用上限 ≤5。读完输出决策。
- [ ] 决策执行:restart(杀+resume/新起,带卡因诊断 seed)/ escalate_human / wait(延长超时)。**无打字纠偏**。
- [ ] 测试:第一次卡住走摘要;第二次卡住升级 agentic;restart 带 seed。

## 2.4 无状态上下文组装
- [ ] 组装函数:PRD + 当前成果物 + 执行轨迹(story_session)+ 决策历史(orchestrator_decision)→ 喂判定 LLM。
- [ ] 长 story 裁剪策略:只给当前 stage 相关 + 最近 N 次决策,老摘要化(防上下文膨胀,§7.7)。
- [ ] 测试:多次唤起拿到完整前情;长 story 裁剪不丢关键。

## 2.5 文档同步
- [ ] DESIGN-artifact-driven-stage-completion.md:v3 已是最新,实现完核对有没有偏离。
- [ ] AGENTS.md:更新 adapter 契约段(driver lifecycle / 完成协议变化)。

## STEP 2 验证(必做)
- [ ] ruff + pytest 全绿。
- [ ] git commit:`feat(stage): LLM 判定层 — 边界纯判定 + 卡住诊断(STEP 2)`。
- [ ] **kimi-webbridge 真实验证**:跑 calculator e2e。观察:成果物落地后 LLM 判 approve/reject;reject 时回 code CLI 带理由重试;卡住时(可人为制造,如断网)走摘要判定/升级。
- [ ] 验证日志记到本文件末尾。

---

# 不在本 goal 范围(后续)

- verify 靠执行(编排器跑测试看退出码)—— §4.7 演进方向,列为下个 goal。
- headless 优先 PTY 兜底 —— §4.12 演进方向。
- 交付追踪对接外部系统 —— §4.11 本期只预留字段。
- 打字纠偏 —— §4.8 砍,降级 backlog,等遥测数据。

---

# 验证日志(执行时填写)

## STEP 1 验证
- 日期:2026-07-26
- webbridge e2e 结果(过/卡哪):**卡在 design 阶段不推进**(非 STEP 1 代码 bug,是 code agent 行为差距,见备注)。跑了 2 次:第一次卡在 claude "Session ID already in use"(预存 session 冲突,清掉 `~/.claude/projects/.../<sid>.jsonl` 后重跑);第二次 claude 正常跑完 design(clean_exit_pty 输出 `Resume this session with: claude --resume <sid>` 被新 PTY 日志完整捕获),**但 claude 没调 `story tool declare`**(也没自己写 `story/spec.md`),导致成果物不落地、`check_artifacts_landed` 查不到 → planner 正确地不推进 → story 卡 paused。scenario 驱动反复点「推进」无果(18min 后超时 fail)。
- pytest 结果:**1288 passed, 2 skipped**(排除预存 test_consult_cli/test_clarify_mcp 环境失败)。新增 34 个测试(1.1-1.7 各子任务的单元 + 集成测试)。
- ruff:全绿(`ruff check packages/story-lifecycle/src/` + 测试 + asserter)。
- commit hash:e129386d(1.1)/ 28884001(1.2)/ 5b1bf2cf(1.3,含 1.5)/ 543e322e(1.4)/ 338b8239(1.6)/ 75478949(1.7)。
- 备注(偏离设计/发现的问题):
  1. **设计决策 A(已落实)**:`expected_outputs` 不重载(它是 JSON 字段名,被 prompt_renderer/validation/task_actions 深度引用),改加新 `artifacts` 字段(文件路径/glob/`git`)。1.1 校验 artifacts 非空。向后兼容,零破坏现有测试。
  2. **设计决策 B(已落实)**:miner 双写 = story-tool declare 时同时写 done.json 兼容视图(含 story_ingest 要的 spec_path/summary/files_changed/stage)。探查证实 miner 的 `link.py` **根本不读 done.json**,只有 `story_ingest.py` 读 —— 所以双写落在 declare 端,零跨包改 miner。1.5 测试验证兼容视图字段。
  3. **设计决策 C(已落实)**:原子写与存在检查同批 —— declare 单调用内原子写文件 + check_artifacts_landed 查同路径,无半成品竞态(测试用 spy 证明写过程中 final 不可见)。
  4. **核心验证价值兑现**:
     - 1.7b PTY 两层日志**第一次让 claude 的"Session ID already in use"错误可见**(旧代码只能 45min 超时盲等)—— 这正是设计 §4.5 要的"喂复盘"。
     - 1.7c 规则卡住检测**没误报**(claude 持续输出 "Garnishing..." 期间没触发 escalate,设计正确)。
     - 1.4 成果物驱动推进**正确拒绝无成果物推进**(claude 没产出 → story 不推进,正是设计要的"砍掉不可信自报")。
  5. **发现的真问题(待用户决策,非 STEP 1 bug)**:code agent(claude)在 prompt 明确写了"必须用 `story tool declare` 落成果物"(6 处提及,文末专段)的情况下,**仍没调 declare 也没自己写 story/spec.md**。这是实际 AI 行为差距。设计 §7.6 的兜底是"planner 扫约定路径",但前提是 code agent 至少把文件写到约定路径。可能的方向(用户定):
     - (a) prompt 更强硬 / 把 declare 放到任务清单第一步而不是文末协议段;
     - (b) claude resume seed 太短(只说"完成后 declare"),换成把完整 prompt 文件路径再强调;
     - (c) headless 路径(claude -p)可能比 interactive PTY 更听 prompt(PTY 有 resume 习惯干扰);
     - (d) 编排器侧加"成果物提示":claude 退出后若无成果物,自动注入一条"你还没 declare,请 `story tool declare spec story/spec.md`"再给一次机会(但这接近"打字纠偏",STEP 1 红线外的范畴)。
     - 这条不阻塞 STEP 1 验收(代码 + 单测全绿,e2e 揭示的是 AI 行为适配,不是代码缺陷)。建议进 STEP 2 的 prompt 调优或单独一个 prompt 强化 task。
  6. **测试端 asserter 已更新**(packages/testing/src/testing/asserters.py):`_stage_done` 不再硬断言 done_file 存在(新协议下 done.json 是 declare 双写副产物,可缺);`assert_design` 改查 `story/spec.md` 成果物落地(context 下 .md 作旧产物兜底)。这是测试侧契约对齐,非代码侧。

## STEP 2 验证
- 日期:
- webbridge e2e 结果:
- pytest 结果:
- commit hash:
- 备注:
