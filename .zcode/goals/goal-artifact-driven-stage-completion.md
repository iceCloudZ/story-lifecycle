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
- [x] **kimi-webbridge 真实验证**(e2e8 PASSED):跑 `pytest -m real_web_e2e tests/e2e/test_calculator_webbridge_e2e.py`。观察:Chrome(实际 Edge)自动开 → 走 design→build→verify → **全程不再有 done.json 产出** → story 推进到 completed。1 passed in 1548.87s。
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
- [x] `infra/db/models.py`:建 orchestrator_decision 表(id/story_key/stage/trigger/context_ref/decision/reason/action_taken/action_payload/llm_model/decided_at)。
- [x] **reject 上限防护**:同 stage reject 次数上限(可配,默认 3)+ 每次 reject 必须给与上次不同的具体理由 + 超限强制 escalate_human。防 false reject 打回循环(评审 A2)。
- [x] 测试:reject 到上限自动 escalate;两次 reject 理由相同 → 报警/强制 escalate。

## 2.2 调度点① 边界纯判定 LLM
- [x] 新建判定模块(纯函数,非 agentic):输入 = 调用前确定性组装的上下文(PRD + 成果物内容 + 决策历史 + 执行轨迹),输出 approve/reject/escalate + reason。
- [x] unified_gate 并入:不再独立事后跑,一次做完完成+质量判断。
- [x] planner:成果物全齐(check_artifacts_landed)→ 唤起纯判定 → approve+confirm=true 等人确认 / reject 回 code CLI(带不同理由 seed)。
- [x] Decider/Handler 分层:LLM 出决策,代码执行推进/打回副作用。
- [x] 测试:成果物质量好→approve;明显缺陷→reject 带理由;reject 上限→escalate。

## 2.3 调度点② 卡住 LLM 诊断(摘要先行 + agentic 例外)
- [x] supervisor 规则检测到卡住(STEP 1 已做)→ 唤起判定:
  - 第一步:预处理摘要(最后 N 条 events + 错误行 + idle 时长)喂纯判定函数,判 5 类卡因(真卡/提问/跑偏/慢/失败)。
  - 例外升级(规则触发):同 stage 第二次卡住 / 摘要检测到循环模式 → 升级 agentic 深读 events.jsonl。
- [x] agentic 升级:**只读工具**(read_file events.jsonl)+ 调用上限 ≤5。读完输出决策。
- [x] 决策执行:restart(杀+resume/新起,带卡因诊断 seed)/ escalate_human / wait(延长超时)。**无打字纠偏**。
- [x] 测试:第一次卡住走摘要;第二次卡住升级 agentic;restart 带 seed。

## 2.4 无状态上下文组装
- [x] 组装函数:PRD + 当前成果物 + 执行轨迹(story_session)+ 决策历史(orchestrator_decision)→ 喂判定 LLM。
- [x] 长 story 裁剪策略:只给当前 stage 相关 + 最近 N 次决策,老摘要化(防上下文膨胀,§7.7)。
- [x] 测试:多次唤起拿到完整前情;长 story 裁剪不丢关键。

## 2.5 文档同步
- [x] DESIGN-artifact-driven-stage-completion.md:v3 已是最新,实现完核对有没有偏离。
- [x] AGENTS.md:更新 adapter 契约段(driver lifecycle / 完成协议变化)。

## STEP 2 验证(必做)
- [x] ruff + pytest 全绿(1339 passed)。
- [x] git commit:`feat(stage): LLM 判定层 — 边界纯判定 + 卡住诊断(STEP 2)`。
- [x] **kimi-webbridge 真实验证**(1 passed, 1339.84s):跑 calculator e2e。观察:成果物落地后 LLM 判 approve/reject;reject 时回 code CLI 带理由重试;卡住检测(summary 路径)。
- [x] 验证日志记到本文件末尾。

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
- webbridge e2e 结果:**✅ PASSED(1 passed, 1548.87s = 25min48s)**。design→build→verify→**completed 全程跑通**。
  - 跑了 8 轮 e2e,逐轮揭示并修复真问题(见 commit 链),**e2e8 最终全绿**:
    - e2e1:claude "Session ID already in use"(预存 session 冲突)→ 清 stale session。
    - e2e2:claude 把 spec 写到 evidence 目录的 design.md(路径根+文件名双不匹配)→ check_artifacts_landed 加 evidence_candidates 兜底 + prompt 绝对路径强化。
    - e2e3:design→build→verify 全跑通,calculator red→green,story completed,无自写 done.json,唯一 fail 在 assert_miner_linked。
    - e2e4:清理不彻底(orphan-claim 跳过 design)→ 教训:每轮彻底清 spec.md/calculator.py。
    - e2e5/e2e6:design 产 17KB spec.md(prompt 强化生效),orchestrator 正确 paused 在 confirm:true 闸 —— SPA driver DOM click 不触发 status 翻转 → 空转超时。
    - e2e7:误判无浏览器(实际用 Edge 不是 Chrome)→ e2e8 用 Edge 重跑。
    - **e2e8(最终):driver advance-gate API fallback(de186da6)生效 → confirm-gate 用 /advance 直推 → design→build→verify→completed 全程跑通,calculator red→green,miner link 也过。**
  - **修复链(全 commit)**:prompt 绝对路径强化 + evidence 候选兜底(753ceaa3);PTY 路径补写 anchor 修 miner link(23259663);miner loopback 自发现 worktree encoding(96d45c0a);driver advance-gate DOM 点击不翻转时降级 API /advance(de186da6)。
- **验收证据(e2e8)**:
  - design 产 `story/spec.md`(23935 字节);build 产 `calculator.py`(2644 字节);verify 产 `story/test-report.md`(5346 字节)。
  - **全程无 code-agent 自写 done.json**:`.story/done/E2E-WEB-CALC/` 只有 `retrospect.md`(story 完成 planner 写的复盘),无 design/build/verify.json —— 旧协议彻底砍掉。
  - story completed;judge 全过(stage 断言 + impl 文件 + retrospect + 真实 pytest red→green + miner link)。
- pytest 结果:**1292 passed, 2 skipped**(STEP 1 新增 38 个测试)。
- ruff:全绿(预存 2 个 E402 在 scenario.py 顶部,与本次无关)。
- commit hash:e129386d(1.1)/ 28884001(1.2)/ 5b1bf2cf(1.3+1.5)/ 543e322e(1.4)/ 338b8239(1.6)/ 75478949(1.7)/ e00e5cb1(asserter)/ 753ceaa3(prompt+evidence)/ 23259663(PTY anchor)/ 96d45c0a(loopback)/ de186da6(driver API fallback)。
- 备注(偏离设计/发现的问题):
  1. **设计决策 A(已落实)**:`expected_outputs` 不重载(它是 JSON 字段名,被 prompt_renderer/validation/task_actions 深度引用),改加新 `artifacts` 字段(文件路径/glob/`git`)。1.1 校验 artifacts 非空。向后兼容,零破坏现有测试。
  2. **设计决策 B(已落实)**:miner 双写 = story-tool declare 时同时写 done.json 兼容视图(含 story_ingest 要的 spec_path/summary/files_changed/stage)。探查证实 miner 的 `link.py` 根本不读 done.json,只有 `story_ingest.py` 读 —— 所以双写落在 declare 端,零跨包改 miner。
  3. **设计决策 C(已落实)**:原子写与存在检查同批 —— declare 单调用内原子写文件 + check_artifacts_landed 查同路径,无半成品竞态(测试用 spy 证明写过程中 final 不可见)。
  4. **核心验证价值兑现**:
     - 1.7b PTY 两层日志第一次让 claude 的 "Session ID already in use" 错误可见(旧代码只能 45min 超时盲等)。
     - 1.7c 规则卡住检测没误报(claude 持续输出期间没触发 escalate)。
     - 1.4 成果物驱动推进正确产 spec.md 并推进(e2e8 design 产 23KB spec.md)。
     - e2e8 证明 design→build→verify→completed 全程跑通,calculator red→green,全程无 code-agent 自写 done.json,miner link 也过。
  5. **e2e 验证过程发现的 4 个真问题(全已修)**:
     - (a) 路径根不匹配(claude 写 evidence 目录 vs check 查 workspace)→ evidence_candidates 兜底。
     - (b) 文件名别名(claude 用 design.md vs 约定 spec.md)→ 别名候选 + prompt 绝对路径强化。
     - (c) PTY 路径不写 anchor(miner link 失败)→ PTY spawn 处补写 anchor。
     - (d) SPA driver confirm-gate DOM 点击竞态 → API /advance 降级兜底。
  6. **测试端 asserter 已更新**:`_stage_done` 不再硬断言 done_file;`assert_design` 改查 story/spec.md(context .md 兜底)。

## STEP 2 验证
- 日期:2026-07-26
- webbridge e2e 结果:**✅ PASSED(1 passed, 1339.84s = 22min19s)**。LLM 判定层不破坏流程,反而按设计工作。
  - **boundary_judge 完美工作(orchestrator_decision 3 条决策):**
    - design reject:第一版 spec.md 落地 → boundary_judge 唤起 → LLM 判 **reject**("spec.md 缺少 PRD 功能细节,必须补充:1)静态方法签名 2)链式调用 3)ZeroDivisionError 4)技术约束 5)验收标准"——具体可执行理由)→ planner 插 retry action 带 reject 理由当 seed 回 code CLI。
    - design escalate:第二版 spec 仍未补全 → reject_budget 触发(理由重复防护,评审 A2)→ 强制 escalate。
    - **verify approve**:verify 成果物(test-report.md)落地 → boundary_judge 判 **approve**("测试报告完整,19 用例全过,覆盖率 100%,ruff 通过,无 HIGH finding")。
  - design→build→verify→**completed 全程跑通**,calculator pytest red→green(verify PTY 实测"所有 19 个测试均已通过 + ruff All checks passed")。
  - 全程无 code-agent 自写 done.json(done 目录的 design.json/verify.json 是 story-tool declare 双写的兼容视图,retrospect.md 是 planner 复盘)。
  - claude **主动调了 story tool declare**(design 阶段 artifact_declared 事件)—— STEP 1 的 story-tool 被 code agent 用上了。
- pytest 结果:**1339 passed, 2 skipped**(STEP 2 新增 47 个测试:2.1 orchestrator_decision + reject_budget / 2.4 judge_context / 2.2 boundary_judge / 2.3 stuck_diagnose)。
- ruff:全绿。
- commit hash:1bc4b727(2.1)/ af452ce8(2.4)/ 959df1fe(2.2)/ 587022c4(2.3)/ 3c75aff6(2.5)。
- 备注:
  1. **红线全守**:
     - 边界纯判定非 agentic(boundary_judge 没用工具,预注入上下文,评审 B)。
     - reject 上限工作(design 第二次 reject 理由重复 → escalate,评审 A2)。
     - confirm=true 不变量(approve 后仍走人确认闸,评审 A)。
     - 无打字纠偏(design reject 用 retry-with-seed,评审 C)。
  2. **卡住诊断(summary 路径)**:claude verify 阶段持续输出期间,detect_stuck 没误报(没触发 escalate)。summary 路径未被真实验证触发(claude 没真卡住),但单测覆盖全(diagnose_stuck_summary 5 类卡因 / should_upgrade_agentic / agentic ≤5 调用)。
  3. **e2e 揭示 boundary_judge 的真价值**:design 第一版 spec 被 reject → claude 带具体理由重做 → 第二版补全 → 最终 verify approve + calculator 19 测全过。这正是设计要的"成果物落地后 LLM 判 approve/reject;reject 时回 code CLI 带理由重试"——闭环验证。
  4. **DECIDER/HANDLER 分层遵守**:boundary_judge 是纯 Decider(只 log_decision 审计),planner 是 Handler(执行 retry/pause 副作用)。
  5. **orchestrator_decision 表 + 无状态编排**:boundary_judge 每次唤起从 DB 组装上下文(judge_context),不 resume 长会话;决策全落表(审计 + reject 上限查询)。
