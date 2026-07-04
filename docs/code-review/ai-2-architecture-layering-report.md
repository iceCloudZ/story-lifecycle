# AI-2 · 架构 & 分层审计报告

> 扫描器：AI-2（architecture-and-layering） · 范围：`main` @ `57a6c142` · 日期：2026-07-03
> 扫描 4 包共 **139** 个非测试源码 `.py`（story-lifecycle 121 / story-miner 13 / knowledge 5 / testing 4，已排除 `.venv`、`tests`、`examples`、`frontend`）
> 结论：**7 findings = 5 blocking + 1 warning + 1 nit**。全部 5 个 blocking 集中在 `orchestrator/`，其余三片（knowledge+infra、跨包、entry+sourcing）分层干净。

---

## 0. TL;DR

- **story-lifecycle 的 5 层依赖方向整体合规**：没有 infra→entry、sourcing→entry 的反向 import；跨包飞轮方向正确（knowledge/miner 零引用 story_lifecycle，testing 作为顶层消费者合法引用）；story-miner 仍是 flat `miner/` layout。✅
- **唯一的系统性架构病在 `orchestrator/`：Resolver/Decider/Handler 三角色边界被侵蚀**。Decider 里内联 DB 读（shadow_router、policy_engine），Decider+Handler 融合在同一个函数（`run_verify_gate`），运行态被一个 `is_running: bool` 压扁（6 个真实状态），gate-wait 状态标记只读不写导致状态机分支不可达。
- **这正好结构性印证了 `dev-flywheel-reality` 记忆里的 A/C/D/E 根因**：不是零散接线 bug，是角色契约失守。→ 修复顺序要从"补 wiring"改成"先恢复角色分离"（见 §4）。

---

## 1. Findings（7 条）

| # | severity | category | file | line | title |
|---|---|---|---|---|---|
| 1 | blocking | side-effect | orchestrator/evaluation/gate.py | 190 | `run_verify_gate` 把决策与 DB/文件副作用焊在一起 |
| 2 | blocking | state-model | orchestrator/entry.py | 184 | 运行态被单个 `is_running` bool 压扁（藏 6 个真实状态） |
| 3 | blocking | state-model | orchestrator/entry.py | 172 | `last_gate_decision_id` 只读不写 → `_is_in_gate_wait` 死分支 |
| 4 | blocking | side-effect | orchestrator/engine/shadow_router.py | 70 | `detect_triggers`（Decider）内联 DB 读 |
| 5 | blocking | side-effect | orchestrator/engine/policy_engine.py | 218 | `evaluate_policy`（Decider）内联 `_count_rejections` DB 读 |
| 6 | warning | dead-branch | orchestrator/workspace/worktree/resolver.py | 55 | `resolve_worktrees` 把所有 git 失败静默吞成空 `{}` |
| 7 | nit | architecture-trigger | entry/cli/review_feedback.py | 277 | CLI Handler 命名 `decide_approval` 撞了纯 Decider 角色名 |

> 路径前缀均为 `packages/story-lifecycle/src/story_lifecycle/`。

### #1 blocking · side-effect · `evaluation/gate.py:190`
- **违反规则**：AGENTS.md 硬规则——"Decider 必须是纯函数；Handler 是唯一允许更新 DB / 起线程 / 开终端 / 删 session / 显示 UI 的层"。
- **事实**：`run_verify_gate` 以 `run_*` 命名、返回 `{decision: advance|retry|fail}` 决策，按契约属 Decider；但同一函数体内做了 5 类副作用：① DB 读 `db.get_open_findings` (`:213`)；② 改调用方 `context` dict（`increment_review_round_count` `:217`）；③ 写文件 `build_repair_packet(..., write_file=True)` (`:241`)；④ 写 markdown 报告 `write_gate_report` (`:274`)；⑤ DB 写 `db.log_event` (`:273`)。调用方 `engine/planner.py:761` 在 FC 循环里把它当纯决策用，副作用透明发生。
- **建议**：拆成纯 `decide_verify_gate(findings, round_count, max_retries, quality_cfg) -> {decision, reason}`（Decider，facts 全部入参）+ Handler `apply_verify_gate_outcome(...)` 拥有 build_repair_packet / write_gate_report / db.log_event / context 变更；open_findings 由独立 Resolver 读出后传入。

### #2 blocking · state-model · `entry.py:184`
- **违反规则**：AGENTS.md——"跨系统状态必须用 enum/tagged union 建模，不能用 boolean 压扁" + "同一状态在不同入口判断必须一致"。
- **事实**：`decide_enter_action` / `decide_resume_action` 只拿 `is_running: bool` 一个信号，再从其它字段反推真实相位（gate-wait 从 `last_gate_decision_id`、planning 从 `status=='planning'`/`_plan_confirmed`、active 从 `_active_execution`@`engine/planner.py:637`）。**同一问"这 story 在跑吗？"被不同入口答得不一样**：`engine/graph.py:114 is_story_running` 看 in-process `_running_stories` 线程 dict；`service/api.py:2586` 读 `ctx._active_execution`；`engine/graph.py:244-260 find_ready_interactive_stories` 用 `_active_execution.mode=='interactive_pty'`+stage+done-file 反推；`engine/graph.py:271-286 recover_orphan_stories` 把孤儿 active 一律改 `status='paused'`。真实状态（not_started / planning_pending_confirm / executing / headless_polling / gate_blocked / session_dead）在决策边界被压成一个 bool。
- **建议**：定义 `StoryRunState` enum（上述 6 态），由一个 Resolver 统一计算，所有入口（entry.py / service/api.py / graph.py）读同一个值替换裸 `is_running`；先定义 state×action 映射再动 Handler。

### #3 blocking · state-model · `entry.py:172`
- **违反规则**：AGENTS.md——"每个不可执行分支必须产生用户可见反馈 + 诊断日志" + 状态×动作契约。
- **事实**：`_is_in_gate_wait(story)` (`:162-172`) 读 `ctx.get('last_gate_decision_id')` 判 gate 阻塞，`decide_enter_action`/`decide_resume_action` 据此分支（`:207`→SHOW_GATE_STATUS，`:253`→RETRY_REVIEW），`observability/debug_packet.py:108` 也读同一标记。但全仓 grep 显示 `last_gate_decision_id` **只被读、从未被写**（gate.py / planner.py / service/api.py 都没写）。结果 `_is_in_gate_wait` 恒 False，SHOW_GATE_STATUS / RETRY_REVIEW 分支不可达，gate 阻塞的 story 掉进通用 idle/session 分支——同一真实状态被多入口不一致决策。
- **建议**：要么在 gate Handler 产出非 advance 的 GateDecision 时把 `last_gate_decision_id` 写进 context_json；要么删掉死标记、把 gate-wait 建成独立 `status` 值（如 `'gate_blocked'`），让 entry.py / debug_packet.py / service/api.py 一致可查。

### #4 blocking · side-effect · `engine/shadow_router.py:70`
- **违反规则**：AGENTS.md——"Decider 必须是纯函数"。
- **事实**：`detect_triggers(state, stage_config)` 名义/用法是 Decider（其输出驱动纯 `generate_shadow_proposal` `:560`），但函数体内调三个 Resolver 式 DB 读：`_is_repeated_error` (`:98`，查 `db.get_story_events`)、`_detect_provider_degradation` (`:106`，裸 `conn.execute` on llm_trace)、`_detect_budget_burn` (`:110`，裸 `conn.execute` SUM on llm_trace)。决策对 DB 状态非确定性、无 DB 不可测。（附带：`:132/155/178` 三处 `except: pass` 把 DB 错误静默吞成 False——见 §3 dead-branch 同型。）
- **建议**：把三处 DB 读提升为 `resolve_router_facts(state) -> RouterFacts`（只读 facts struct），`detect_triggers(state, stage_config, facts)` 变纯。shadow 的价值是 counterfactual 评估，必须可从入参复现。

### #5 blocking · side-effect · `engine/policy_engine.py:218`
- **违反规则**：AGENTS.md——"Decider 必须是纯函数"。
- **事实**：`evaluate_policy(action, risk, story_key)` (`:218`) 与 `evaluate_guarded(...)` (`:255`) 按契约是纯 policy decider（静态 `GUARDED_RULES` 矩阵把 (action,category,autonomy)→AutonomyLevel），但两者都在决策体内调 `_count_rejections(story_key, action)` (`:461`，DB 读 rejection 历史）。决策依赖可变 DB 状态、无持久层不可演练，Resolver 角色（读 rejection 历史）与 Decider 角色（套矩阵）被混在一起。
- **建议**：把 `rejection_count: int` 作为显式入参（调用方经 Resolver 解析），恢复 Decider 为 (action,risk,category,autonomy,rejection_count,budget) 的纯函数，L0–L5 矩阵查表可脱离 DB 测试。

### #6 warning · dead-branch · `workspace/worktree/resolver.py:55`
- **违反规则**：AGENTS.md——"每个不可执行分支必须产生用户可见反馈 + 诊断日志" + "用户是否需要手动解释下一步该干嘛"。
- **事实**：`resolve_worktrees` (`:47-73`) 捕获 FileNotFoundError / TimeoutExpired / OSError / 非零 returncode，四类异常一律返 `{}` 且零日志。空 dict 与"合法空仓（还没建 worktree）"返回值**完全相同**。下游 `decide_prepare` (`decider.py:86-97`) 把空 `worktree_map` 当"没注册、可 CREATE"，并驱动 `resolve_story_worktree` 判 UNPREPARED——瞬态 git 失败（超时/缺二进制/仓库锁）与"干净初始态"无法区分，静默产出用户无法解释的 CREATE/UNPREPARED。同型 silent-return 还在 `shadow_router.py:132/155/178`。
- **建议**：每条失败路径 warning 级日志 + 区分"git 不可用"与"无 worktree"——返回 tagged 结果 (ok/empty/error) 或抛 `WorktreeProbeError`，让 Decider/Handler 能 surface 区别而不是伪装成 fresh-slate CREATE。

### #7 nit · architecture-trigger · `entry/cli/review_feedback.py:277`
- **事实**：`decide_approval` 是 CLI 命令 Handler（`:296-336` 读 db.get_finding、调 update_finding_status/db.update_finding/db.log_event、console.print/sys.exit）。entry Handler 有副作用是合规的，**不是契约违规**——但 `decide_` 前缀撞了 AGENTS.md 里纯 Decider 角色名，会误导其它 AI/审计。sourcing 层唯一另一个 `resolve_*`（`sources/base.py:97 resolve_bug_parent`）是真纯函数。entry/sourcing 分层与角色契约其余全部干净。
- **建议**：改名 `decide_approval → handle_approval`（或 `approvals_decide`），把 `decide_` 命名空间留给纯 Decider；CLI 调用串 `story approvals decide` 可保留。

---

## 2. 干净区域（下次别动这些）

- **knowledge + infra 分层**（38 文件）：infra 零引用 entry/sourcing/orchestrator/knowledge；knowledge 只向下 import infra + 自身兄弟。`resolve_session_state` 返回粒度 enum（LIVE/EXITED/MISSING/UNKNOWN），无 bool 压扁。infra 里的副作用（DB/线程/终端）属合规 Handler 行为。
- **跨包飞轮**（18 文件）：knowledge 包零引用 story_lifecycle/miner；miner 零引用 story_lifecycle；testing 作为顶层消费者合法引用。story-miner 仍是 flat `miner/` layout（无 `src/`）。
- **entry + sourcing 分层**（32 文件）：sourcing 零向上 import entry；entry 全部向下 import。entry/ 是纯 CLI（web 在 frontend，非 src）。
- ⚠️ 范围外观察（不计入 finding）：`packages/story-miner/scripts/{bug_iteration_links,bug_story_graph,infer_bug_magnet_commits}.py` import `story_lifecycle.TapdSource`。scripts/ 是开发脚手架非 miner 包本体，按审计范围（`miner/`）不计；但严格说 miner→lifecycle 引用方向要注意别回流进包内。

---

## 3. 全自动飞轮映射（为何"全自动没真跑过"——架构视角）

本审计**独立印证** `dev-flywheel-reality` 记忆里 2026-07-03 定位的 A/C/D/E 根因，并给出结构性解释：**不是零散 wiring bug，是 orchestrator 的 Resolver/Decider/Handler 角色契约系统性失守**。

| 记忆根因 | 代码位置 | 本审计对应 finding | 结构性解释 |
|---|---|---|---|
| **C. verify 硬闸常开** | `gate.py:213/215` findings 恒空→恒 advance | **#1** gate.py:190 | gate 不是纯决策，"findings 空→advance"和全部 I/O 焊在同一函数，无法单独加严判据（跑测试、读 done 的 build_passed）而不重写 |
| **C.（续）** gate-wait 无状态 | —— | **#3** entry.py:172 | 即便 gate 想暂停，`last_gate_decision_id` 只读不写，gate_blocked 分支不可达，状态机根本表达不出"卡在 gate" |
| **D. 异常/状态不收敛** | `graph.py:188-200` except 不回写 failed；`graph.py:279-286` 重启一律 active→paused | **#2** entry.py:184 | 运行态被 `is_running` bool 压扁，headless_polling/executing/session_dead 无法区分，多入口各推各的 → except 不回写、recover 一刀切都是同一 state-model 债的症状 |
| **E. 飞轮回注三处全断且静默** | knowledge_provider/kb/transcript try/except 静默空 | **#4/#5** shadow_router:70 / policy_engine:218 | 同型病在 autonomy 层：facts 内联进 Decider + bare except 静默吞 → 决策不可复现、静默降级。回注要可信，Decider 必须先纯化 |
| **A. headless 是死代码** | `graph.py:187` 不传 headless | **#2**（前置） | headless vs interactive 没建成 enum 状态，所以接线能漂移且无人察觉。**#2 的 StoryRunState enum 是 A 的前置**——先把 headless_polling 建成可查询状态，A 的接线才有地方落 |

**关键结论**：记忆里"按 A→F 顺序补 wiring"是对的优先级，但本审计表明 **A/C/D/E 会反复复发，除非先恢复角色分离**。建议把"角色分离"作为 A→F 的结构性前置（见 §4）。

---

## 4. 推荐修复顺序（以"真正跑通全自动"为目标）

> 原则：先恢复角色契约（让决策可测、可复现），再补 wiring（A→F）。否则 C/D/E 修了还会回来。

1. **【地基】建模 `StoryRunState` enum**（解 #2 + #3）——把 not_started / planning_pending / executing / headless_polling / gate_blocked / session_dead 建成 enum，由单一 Resolver 计算，entry.py / service/api.py / graph.py 全部读同一值，替换裸 `is_running` + 死标记 `last_gate_decision_id`。**这一步同时解锁记忆里的 A（headless 接线有地方落）和 D（状态可收敛）**。先定 state×action 映射表再动代码。
2. **【质量可信】拆 `run_verify_gate` → decide + apply**（解 #1）——纯 `decide_verify_gate(facts)` + Handler `apply_verify_gate_outcome`（拥有 DB/文件/报告副作用）。**这一步解锁记忆里的 C**：拆完 gate 才能加严判据（跑测试、读 done 的 build_passed/tests_passed）、retry/evaluator_loop 才事实可达。依赖步骤 1 的 gate_blocked 状态来表达"卡 gate"。
3. **【回注可信】把 DB 读从 Decider 抽出**（解 #4 + #5）——`resolve_router_facts` / `resolve_rejection_count` 只读 facts，`detect_triggers` / `evaluate_policy` 变纯。**这一步解锁记忆里的 E**：回注决策可复现、bare except 不再静默降级。
4. **【防静默】worktree resolver 区分 git 失败 vs 空**（解 #6）——返回 tagged 结果 (ok/empty/error)，防瞬态失败伪装成 fresh-slate CREATE。低优先，可与任一触碰 worktree 的改动顺手做。
5. **【清理】rename `decide_approval → handle_approval`**（解 #7）——nit，任一次触碰 review_feedback 时顺手。

**预期收益**：步骤 1+2 让 verify gate 从"恒 advance 的橡皮章"变成"可测、可暂停、可 retry 的真闸"；步骤 1 让 headless 路径有状态可挂（配合记忆 A 的接线）；步骤 3 让 autonomy/shadow 决策可信，回注闭环才能成立。三者合起来正是"全自动无人值守"缺的结构性地基。

---

## 5. 严格 JSON（§4 交付物，机器可读）

```json
{
  "reviewer": "AI-2",
  "focus": "architecture-and-layering",
  "scan_scope": "packages/story-lifecycle/src/story_lifecycle/**, packages/story-miner/miner/**, packages/knowledge/src/knowledge/**, packages/testing/src/testing/**",
  "stats": {"files_scanned": 139, "findings_count": 7},
  "findings": [
    {
      "severity": "blocking",
      "file": "packages/story-lifecycle/src/story_lifecycle/orchestrator/evaluation/gate.py",
      "line": 190,
      "category": "side-effect",
      "title": "run_verify_gate 把决策与 DB/文件副作用焊在一起",
      "detail": "违反 AGENTS.md 硬规则『Decider 必须纯；Handler 是唯一允许更新 DB/起线程/开终端/显示 UI 的层』。run_verify_gate 以 run_* 命名、返回 {decision:advance|retry|fail}，按契约属 Decider，但同函数体内：① DB 读 db.get_open_findings(:213) ② 改调用方 context dict increment_review_round_count(:217) ③ 写文件 build_repair_packet(write_file=True)(:241) ④ 写报告 write_gate_report(:274) ⑤ DB 写 db.log_event(:273)。调用方 engine/planner.py:761 在 FC 循环当纯决策用，副作用透明发生。",
      "suggestion": "拆成纯 decide_verify_gate(findings,round_count,max_retries,quality_cfg)->decision + Handler apply_verify_gate_outcome(拥有 build_repair_packet/write_gate_report/db.log_event/context 变更)；open_findings 由独立 Resolver 读出后传入。"
    },
    {
      "severity": "blocking",
      "file": "packages/story-lifecycle/src/story_lifecycle/orchestrator/entry.py",
      "line": 184,
      "category": "state-model",
      "title": "运行态被单个 is_running bool 压扁（藏 6 个真实状态）",
      "detail": "违反 AGENTS.md『跨系统状态必须 enum 建模，不能 bool 压扁』+『同一状态跨入口判断须一致』。decide_enter_action/decide_resume_action 只拿 is_running:bool，再从 last_gate_decision_id/status=='planning'/_plan_confirmed/_active_execution(engine/planner.py:637)反推相位。同一问被答得不一样：engine/graph.py:114 is_story_running 看 in-process _running_stories 线程 dict；service/api.py:2586 读 ctx._active_execution；engine/graph.py:244-260 find_ready_interactive_stories 用 _active_execution.mode=='interactive_pty'+stage+done-file 反推；engine/graph.py:271-286 recover_orphan_stories 把孤儿 active 一律改 status='paused'。真实状态(not_started/planning_pending_confirm/executing/headless_polling/gate_blocked/session_dead)在决策边界被压成一个 bool。",
      "suggestion": "定义 StoryRunState enum(上述6态)，由单一 Resolver 计算并替换裸 is_running，所有入口(entry.py/service/api.py/graph.py)读同一值；先定 state×action 映射表再动 Handler。"
    },
    {
      "severity": "blocking",
      "file": "packages/story-lifecycle/src/story_lifecycle/orchestrator/entry.py",
      "line": 172,
      "category": "state-model",
      "title": "last_gate_decision_id 只读不写 → _is_in_gate_wait 死分支",
      "detail": "违反 AGENTS.md『每个不可执行分支必须产生用户可见反馈+诊断日志』+状态×动作契约。_is_in_gate_wait(story)(:162-172)读 ctx.get('last_gate_decision_id') 判 gate 阻塞，decide_enter_action/decide_resume_action 据此分支(:207→SHOW_GATE_STATUS,:253→RETRY_REVIEW)，observability/debug_packet.py:108 也读同标记。但全仓 grep 该标记只读不写(gate.py/planner.py/service/api.py 都没写)。结果 _is_in_gate_wait 恒 False，SHOW_GATE_STATUS/RETRY_REVIEW 分支不可达，gate 阻塞 story 掉进通用 idle/session 分支，多入口对同一真实状态不一致决策。",
      "suggestion": "在 gate Handler 产出非 advance GateDecision 时把 last_gate_decision_id 写进 context_json；或删死标记、把 gate-wait 建成独立 status 值(如 'gate_blocked')，让 entry.py/debug_packet.py/service/api.py 一致可查。"
    },
    {
      "severity": "blocking",
      "file": "packages/story-lifecycle/src/story_lifecycle/orchestrator/engine/shadow_router.py",
      "line": 70,
      "category": "side-effect",
      "title": "detect_triggers（Decider）内联 DB 读",
      "detail": "违反 AGENTS.md『Decider 必须纯』。detect_triggers(state,stage_config) 名义/用法是 Decider(输出驱动纯 generate_shadow_proposal:560)，但函数体调三个 Resolver 式 DB 读：_is_repeated_error(:98,查 db.get_story_events)、_detect_provider_degradation(:106,裸 conn.execute on llm_trace)、_detect_budget_burn(:110,裸 conn.execute SUM on llm_trace)。决策对 DB 状态非确定性、无 DB 不可测。附带 :132/155/178 三处 except:pass 把 DB 错误静默吞成 False(见 dead-branch 同型)。",
      "suggestion": "把三处 DB 读提升为 resolve_router_facts(state)->RouterFacts(只读 struct)，detect_triggers(state,stage_config,facts) 变纯。shadow 价值是 counterfactual 评估，必须可从入参复现。"
    },
    {
      "severity": "blocking",
      "file": "packages/story-lifecycle/src/story_lifecycle/orchestrator/engine/policy_engine.py",
      "line": 218,
      "category": "side-effect",
      "title": "evaluate_policy（Decider）内联 _count_rejections DB 读",
      "detail": "违反 AGENTS.md『Decider 必须纯』。evaluate_policy(action,risk,story_key)(:218) 与 evaluate_guarded(...)(:255) 按契约是纯 policy decider(静态 GUARDED_RULES 矩阵把 (action,category,autonomy)→AutonomyLevel)，但都在决策体内调 _count_rejections(story_key,action)(:461,DB 读 rejection 历史)。决策依赖可变 DB 状态、无持久层不可演练，Resolver(读 rejection 历史)与 Decider(套矩阵)角色混淆。",
      "suggestion": "把 rejection_count:int 作显式入参(调用方经 Resolver 解析)，恢复 Decider 为 (action,risk,category,autonomy,rejection_count,budget) 纯函数，L0–L5 矩阵查表可脱离 DB 测试。"
    },
    {
      "severity": "warning",
      "file": "packages/story-lifecycle/src/story_lifecycle/orchestrator/workspace/worktree/resolver.py",
      "line": 55,
      "category": "dead-branch",
      "title": "resolve_worktrees 把所有 git 失败静默吞成空 {}",
      "detail": "违反 AGENTS.md『每个不可执行分支必须产生用户可见反馈+诊断日志』+『用户是否需手动解释下一步』。resolve_worktrees(:47-73) 捕获 FileNotFoundError/TimeoutExpired/OSError/非零 returncode，四类异常一律返 {} 且零日志，与『合法空仓』返回值完全相同。下游 decide_prepare(decider.py:86-97) 把空 worktree_map 当『没注册、可 CREATE』，resolve_story_worktree 判 UNPREPARED——瞬态 git 失败(超时/缺二进制/仓库锁)与干净初始态不可区分，静默产出用户无法解释的 CREATE/UNPREPARED。同型 silent-return 还在 shadow_router.py:132/155/178。",
      "suggestion": "每条失败路径 warning 级日志+区分『git 不可用』与『无 worktree』——返回 tagged 结果(ok/empty/error)或抛 WorktreeProbeError，让 Decider/Handler 能 surface 区别而非伪装成 fresh-slate CREATE。"
    },
    {
      "severity": "nit",
      "file": "packages/story-lifecycle/src/story_lifecycle/entry/cli/review_feedback.py",
      "line": 277,
      "category": "architecture-trigger",
      "title": "CLI Handler 命名 decide_approval 撞了纯 Decider 角色名",
      "detail": "decide_approval 是 CLI 命令 Handler(:296-336 读 db.get_finding、调 update_finding_status/db.update_finding/db.log_event、console.print/sys.exit)。entry Handler 有副作用合规，非契约违规——但 decide_ 前缀撞 AGENTS.md 纯 Decider 角色名，会误导其它 AI/审计。sourcing 唯一另一个 resolve_*(sources/base.py:97 resolve_bug_parent)是真纯函数。entry/sourcing 分层与角色契约其余全部干净。",
      "suggestion": "改名 decide_approval→handle_approval(或 approvals_decide)，把 decide_ 命名空间留给纯 Decider；CLI 调用串 story approvals decide 可保留。"
    }
  ],
  "summary": "5 blocking, 1 warning, 1 nit"
}
```
