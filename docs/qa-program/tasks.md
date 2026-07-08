# 任务卡(Task Cards)

> 体系化测试的 atomic 任务卡。每张卡**自包含**:现状/目标/代码落点/步骤/验收/约束。
> 取卡顺序 = README.md §进度表 里最早的 `[待办]`。做完一张改进度表 + 写报告。

## 卡的格式(每张卡都有这 6 段)

```text
### T?.? · <任务名> `[状态]`
现状:      现在代码/测试是什么情况(量化)
目标:      做完应该达到什么(可验收)
代码落点:  改/测哪些文件(带函数名+行号)
步骤:      1. 2. 3.(atomic,编号步骤)
验收:      必须跑的命令 + 期望结果(全绿)
约束:      不能破坏什么(架构不变量 / AGENTS.md 规则)
```

---

## 阶段一 · 质量闸(模块④ — 最高优先级,现在最薄)

> 现状:`orchestrator/evaluation/` 只有 `test_verify_gate_plan_summary.py` 1 个测试文件。
> 质量闸是系统心脏,且最近 PTY/HITL bug 链的根因正是 gate 行为边界没覆盖。

### T1.1 · gate 硬闸不可绕(max_retries 强制 fail) `[待办]`

**现状**:`gate.py:247` `if round_count > max_retries` 是质量不变量(架构红线 #2),但**无测试断言它不可绕过**。

**目标**:证明 `round_count > max_retries` 时代码路径**必然**走到 fail 分支,无论 finding 数量/质量如何。

**代码落点**:
- 测:`packages/story-lifecycle/tests/test_gate_hard_fail.py`(新建)
- 被测:`orchestrator/evaluation/gate.py:192 run_verify_gate` + `gate.py:247` 强制 fail 分支
- 关键函数:`increment_review_round_count(context, stage)`(gate.py:39)

**步骤**:
1. 构造 `gate_ctx` fixture:`review_round_count_<stage>` 已 = `max_retries`(用 `increment_review_round_count` 手动推到上限)。
2. 再调一次 `run_verify_gate(...)`,断言返回的 `GateDecision.decision == "fail"`。
3. 断言 `reason` 含 "HIGH findings persist after ... repair rounds"。
4. 反向断言:即使 finding 列表为空(质量 OK),只要 round 超限,仍 fail(证「硬」)。

**验收**:
```bash
./.venv-monorepo-test/Scripts/python.exe -m pytest \
  packages/story-lifecycle/tests/test_gate_hard_fail.py -v
```
全绿。且断言覆盖:正常超限 fail、空 finding 仍 fail(两条都过才算「不可绕」)。

**约束**:不修改 `gate.py`(只测,不改)。若发现可绕过 → 标 `[阻塞]`,报告里写复现,**这是 P0 bug**。

---

### T1.2 · gate 三判定分支覆盖(advance/retry/fail) `[待办]`

**现状**:`run_verify_gate` 有三个出口(advance/retry/fail),但只测了 retry 的子场景。

**目标**:三个判定分支各有**至少一个**正向测试。

**代码落点**:
- 测:`packages/story-lifecycle/tests/test_gate_branches.py`(新建)
- 被测:`gate.py:192 run_verify_gate` 全部分支

**步骤**:
1. **advance**:0 HIGH finding + round 未超限 → `decision == "advance"`。
2. **retry**:有 HIGH finding + round < max_retries → `decision == "retry"`,且 `human_message` 含 round 计数。
3. **fail**:有 HIGH finding + round = max_retries(边界) → `decision == "fail"`(与 T1.1 互补,这里聚焦「刚到上限」)。

**验收**:
```bash
./.venv-monorepo-test/Scripts/python.exe -m pytest \
  packages/story-lifecycle/tests/test_gate_branches.py -v
```
三个分支各 ≥1 测试全绿。

**约束**:mock `build_repair_packet`(它是 packet 构造,T1.4 单独测),本卡只测判定逻辑。

---

### T1.3 · no_progress 终止(防死循环) `[阻塞]` · 已查证(文档/实现不符)

**现状**(2026-07-08 已查证):README 提「连续无改善自动终止」(收敛条件),**但代码未实现**。
`detect_no_progress` 已被 ISS-008 删(见 `ARCHITECTURE.md:143` + `evaluator_loop.py:1-7`)。
实际收敛兜底靠 `gate.py:247 if round_count > max_retries` 计数(T1.1 已验证有效)。
残留 3 处 `no_progress` 引用全是消费方(shadow_router/debug_packet),等的是已不存在的信号 = 死代码。
详见 [原报告](reports/T1.3-gate-no-progress.md) + [复核补充](reports/T1.3-no-progress-revisit.md)。
**阻塞在产品决策**:owner 走 A(更新 README 删过期描述,T1.3 关闭)还是 B(补实现,不推荐)。

**目标**:若存在 no_progress 检测逻辑,测它;若不存在(README 宣称但未实现),报告里标 `[阻塞]` 记录现状。

**代码落点**:
- 先查:`grep -rn "no_progress\|no-progress\|consecutive" orchestrator/evaluation/`
- 测:`packages/story-lifecycle/tests/test_gate_no_progress.py`(新建,若逻辑存在)

**步骤**:
1. grep 找 no_progress 实现。若在 gate.py → 构造连续 N 轮相同 finding 的场景,断言 fail。
2. 若 no_progress 是 FC 模式 LLM 内化(无 Python 逻辑)→ 报告记录「设计如此」,本卡转 `[已完成]`(已澄清)。
3. 若 README 宣称但代码无 → 报告标 `[阻塞]`,这是文档/实现不符(参考 `COORDINATOR-INTELLIGENCE.md` 已发现类似问题)。

**验收**:
```bash
./.venv-monorepo-test/Scripts/python.exe -m pytest \
  packages/story-lifecycle/tests/test_gate_no_progress.py -v  # 若存在
```
或:报告写清「no_progress 是 LLM 内化,无 Python 逻辑」,附 grep 证据。

**约束**:诚实——找不到就报找不到,不许编。

---

### T1.4 · evaluator_loop repair-packet 构造 `[待办]`

**现状**:`evaluator_loop.py:29 build_repair_packet` 是 FC 模式下唯一的 packet 构造器,只有间接测试。

**目标**:对 `build_repair_packet` 做输入→输出契约测试。

**代码落点**:
- 测:`packages/story-lifecycle/tests/test_repair_packet.py`(新建)
- 被测:`orchestrator/evaluation/evaluator_loop.py:29 build_repair_packet`

**步骤**:
1. 给定 fixture:`plan_summary`、`high_findings`(list)、`stage`、`round_num`。
2. 调 `build_repair_packet(...)`,断言返回的 packet 字符串:
   - 含 stage 名
   - 含每条 finding 的描述
   - 含 round_num
3. 边界:空 findings list → packet 不崩(可能空 section,但不异常)。
4. (可选)若 `write_to_disk=True`,断言 `.story/context/<key>/repair_<stage>_round<N>.md` 被写。

**验收**:
```bash
./.venv-monorepo-test/Scripts/python.exe -m pytest \
  packages/story-lifecycle/tests/test_repair_packet.py -v
```
全绿。

**约束**:packet 内容断言用子串包含(不锁死格式,格式会演进)。

---

### T1.5 · Finding 生命周期(quality 飞轮) `[待办]`

**现状**:`quality.py` 有完整 Finding 状态机(record/update_status/record_verification)+ learned pattern 提案,但无端到端生命周期测试。

**目标**:测 Finding 从 open → accepted → fixed → verified → **learned Pattern** 全链路。

**代码落点**:
- 测:`packages/story-lifecycle/tests/test_finding_lifecycle.py`(新建)
- 被测:`orchestrator/evaluation/quality.py`:`record_finding`(51)、`update_finding_status`(70)、`record_verification`(105)、`propose_learned_pattern`(261)、`approve_pattern`(291)、`activate_pattern`(296)

**步骤**:
1. `record_finding(...)` → 断言 status=open,DB 有记录。
2. `update_finding_status(..., "accepted")` → 断言状态变 + 写了 audit event(`finding_status_changed`)。
3. 依序走 fixed → verified。
4. verified 后 `propose_learned_pattern(...)` → 断言 pattern status=proposed。
5. `approve_pattern` → `activate_pattern` → 断言 status=active。

**验收**:
```bash
./.venv-monorepo-test/Scripts/python.exe -m pytest \
  packages/story-lifecycle/tests/test_finding_lifecycle.py -v
```
全链路绿。

**约束**:用临时 DB fixture(参考现有 `conftest.py` 的 db fixture 模式),不污染真实库。

---

## 阶段二 · 执行编排(模块③ — 第二大风险区)

### T2.1 · FC 规划循环(mock LLM 生成 actions) `[待办]`

**现状**:`planner.py:172 run_orchestrator_agent` 是 FC 核心,已有 `test_agent_planner.py` 但聚焦局部。

**目标**:用 mock LLM 驱动完整 FC 规划循环,断言产出的 `_agent_actions` 正确。

**代码落点**:
- 测:`packages/story-lifecycle/tests/test_fc_planning_loop.py`(新建)
- 被测:`orchestrator/engine/planner.py:172 run_orchestrator_agent`、`planner.py:333` 写 actions、`planner.py:334` 写 `_plan_confirmed=False`

**步骤**:
1. mock `llm.invoke_with_tools`(planner.py:243)返回预设 tool_calls(launch design 等)。
2. 调 `run_orchestrator_agent(...)`,断言:
   - `ctx["_agent_actions"]` 非空,含期望的 launch action。
   - `ctx["_plan_confirmed"] == False`(暂停语义)。

**验收**:
```bash
./.venv-monorepo-test/Scripts/python.exe -m pytest \
  packages/story-lifecycle/tests/test_fc_planning_loop.py -v
```
全绿。

**约束**:mock LLM(确定性),绝不调真 LLM。

---

### T2.2 · plan_confirm 暂停语义 `[待办]`

**现状**:`planner.py:468 continue_orchestrator_agent` 在 confirm 后执行,但暂停→恢复的边界未专门测。

**目标**:证明 `_plan_confirmed=False` 时真暂停,confirm 后才执行 actions。

**代码落点**:
- 测:`packages/story-lifecycle/tests/test_plan_confirm_pause.py`(新建)
- 被测:`planner.py:504 ctx["_plan_confirmed"] = True`

**步骤**:
1. 规划完 → 断言 `_plan_confirmed=False`,actions 未执行(mock adapter 计数器=0)。
2. 模拟 confirm → 调 continue 路径 → 断言 actions 执行(mock adapter 计数器>0)。

**验收**:
```bash
./.venv-monorepo-test/Scripts/python.exe -m pytest \
  packages/story-lifecycle/tests/test_plan_confirm_pause.py -v
```
全绿。

**约束**:mock adapter 的 launch,不真起 CLI。

---

### T2.3 · .done 握手轮询(超时/成功) `[待办]`

**现状**:`pty.py:481 ensure_agent_pty` + `_wait_ready`(443)管 readiness,done 握手在执行层轮询。无专门测试。

**目标**:测 done 握手的成功路径 + 超时路径。

**代码落点**:
- 先查:`grep -rn "\.done\|done.json\|done handshake" orchestrator/engine/ infra/terminal/`
- 测:`packages/story-lifecycle/tests/test_done_handshake.py`(新建)

**步骤**:
1. mock PTY + mock 文件系统:模拟 claude 写 `.story/done/<key>/<stage>.json`。
2. 成功路径:轮询在 timeout 内发现 done 文件 → 返回 done。
3. 超时路径:timeout 内无 done 文件 → 返回 timeout/错误,不无限挂。

**验收**:
```bash
./.venv-monorepo-test/Scripts/python.exe -m pytest \
  packages/story-lifecycle/tests/test_done_handshake.py -v
```
全绿。

**约束**:**铁律 pty.py 不动**(只测外部行为,不改 pty.py 内部)。mock 时间加速轮询。

---

### T2.4 · 三启动模式一致性(-p / query / release) `[待办]`

**现状**:三种 claude 启动方式(`headless_launch_cmd` / `interactive_launch_cmd` / release_prompt)并存,产出应一致但无对比测试。这是最近 PTY 那条线的核心风险。

**目标**:证明三模式对同一 prompt 产出**可验证的等价启动意图**。

**代码落点**:
- 测:`packages/story-lifecycle/tests/test_launch_modes.py`(新建)
- 被测:`knowledge/adapters/claude.py:33 interactive_launch_cmd`、`claude.py:60 headless_launch_cmd`、`service/api.py` release_prompt 路径

**步骤**:
1. 同一 prompt 经三模式生成启动 argv / 渲染文本。
2. 断言三者都含该 prompt(或其文件引用)。
3. 断言 headless 用 `-p`、interactive 用 `claude "query"`、release 是纯文本。

**验收**:
```bash
./.venv-monorepo-test/Scripts/python.exe -m pytest \
  packages/story-lifecycle/tests/test_launch_modes.py -v
```
全绿。

**约束**:这是回归基线——以后改启动方式,此测试要能及时发现行为漂移。

---

### T2.5 · PTY/HITL bug 回归(最近 5 个 fix(pty)) `[待办]`

**现状**:最近 5 个 commit(`522d26a8`→`5e65535c` + `3fefbd65`)是 PTY 注入的试错链,AGENTS.md 要求「每个历史 bug 必须有回归测试」,但这批没有。

**目标**:为最近 PTY bug 链配回归测试。

**代码落点**:
- 参考:`docs/handoff-design-hitl.md`(bug 全貌)
- 测:`packages/story-lifecycle/tests/test_pty_injection_regression.py`(新建)

**步骤**:
1. 读 `handoff-design-hitl.md` §4(哪些验证过/没成)、§10、§11。
2. 核心回归点:`claude "query"` 模式(`interactive_launch_cmd` 带 prompt)正确生成 argv(取代 PTY 注入)。
3. 断言:`interactive_launch_cmd(model, prompt="X")` 返回 `["claude", "X"]`(或 resolve_executable 等价)。
4. 断言:prompt="" 时不带 arg(空白 claude)。

**验收**:
```bash
./.venv-monorepo-test/Scripts/python.exe -m pytest \
  packages/story-lifecycle/tests/test_pty_injection_regression.py -v
```
全绿。

**约束**:这批 bug 的「注入失败」部分(bracketed paste 等)已被 `claude query` 取代,回归测的是**当前正确行为**,不是复现已废弃的注入逻辑。

---

## 阶段三 · HITL(模块⑦)

### T3.1 · clarify MCP 阻塞 + 交互式双路径 `[待办]`

**现状**:`clarify_server.py:76 handle_clarify_call`(MCP 阻塞路径)+ `prompt_sections.build_design_dimensions_section(interactive=True)`(交互终端路径),两路径需对比测。

**目标**:证 MCP clarify 真阻塞到人答;交互式分支走「终端问人」文案。

**代码落点**:
- 测:`packages/story-lifecycle/tests/test_clarify_dual_path.py`(新建)
- 被测:`orchestrator/mcp/clarify_server.py:76 handle_clarify_call`、`poll_clarify_answer`(130)、`engine/prompt_sections.py build_design_dimensions_section`

**步骤**:
1. MCP 路径:`handle_clarify_call` 落 request 事件 → 用 fake poll 立即返回 answer → 断言返回 MCP result 含 answer。
2. MCP 超时:fake poll 超时 → 断言返回「conservative fallback」文案(clarify_server.py:121 附近)。
3. 交互式路径:`build_design_dimensions_section(interactive=True)` → 断言文案含「在终端问人」而非「调 mcp__lifecycle__clarify」。

**验收**:
```bash
./.venv-monorepo-test/Scripts/python.exe -m pytest \
  packages/story-lifecycle/tests/test_clarify_dual_path.py -v
```
全绿。

**约束**:MCP 路径用 fake poll(不真阻塞),交互路径纯文案断言。

---

### T3.2 · supervisor HITL 决策 `[待办]`

**现状**:`supervisor.py:27 decide_response`(注入 LLM 决策)有 `test_supervisor.py`,但 `handle_pty_output`(84)的 HITL 触发边界未深测。

**目标**:补 supervisor 决策边界测试。

**代码落点**:
- 测:扩充 `test_supervisor.py` 或新建 `test_supervisor_boundary.py`
- 被测:`supervisor.py:84 handle_pty_output`、`decide_response`(27)

**步骤**:
1. mock LLM 返回 `{choice, reason}`,断言 `decide_response` 正确解析。
2. PTY 输出含特定 marker(如提问信号)→ 断言触发 HITL 决策。
3. 边界:LLM 返回非法格式 → 降级行为(不崩)。

**验收**:
```bash
./.venv-monorepo-test/Scripts/python.exe -m pytest \
  packages/story-lifecycle/tests/test_supervisor_boundary.py -v
```
全绿。

**约束**:mock LLM。

---

### T3.3 · approval_queue 阻塞部署 `[待办]`

**现状**:`/api/approvals` 端点存在,deploy stage `requires_human=True`(stage_library.py:171),但「部署前必须审批」无测试。

**目标**:证 deploy stage 在无 approval 时阻塞。

**代码落点**:
- 测:`packages/story-lifecycle/tests/test_approval_blocks_deploy.py`(新建)
- 被测:`stage_library.py` deploy 定义(`requires_human=True`)+ approval 端点

**步骤**:
1. 构造 story current_stage=deploy。
2. 无 approval → 断言执行被阻塞(返回 wait_human 或类似)。
3. 有 approval → 断言可推进。

**验收**:
```bash
./.venv-monorepo-test/Scripts/python.exe -m pytest \
  packages/story-lifecycle/tests/test_approval_blocks_deploy.py -v
```
全绿。

**约束**:若实际实现是「deploy 在 profile 里默认不启用」→ 报告记录现状,本卡转已完成(已澄清)。

---

## 阶段四 · 补齐其余模块(①②⑤⑥)

### T4.1 · ① 接入:tapid 幂等 + prd 边界 `[待办]`

**现状**:`test_sync*.py` 有同步测试,但「重复同步同一 TAPD id 不重复建 story」(幂等)未明确测。

**目标**:tapd sync 幂等 + prd_generator 空标题/超长边界。

**代码落点**:
- 测:扩充 `test_sync_by_id.py` + `test_prd_generator.py`
- 被测:`sourcing/sources/tapd_*`、`service/prd_generator.py`

**步骤**:
1. 同一 TAPD id 连续 sync 两次 → 断言只建一个 story。
2. prd_generator 空标题 → 降级行为(不崩)。
3. prd_generator 超长标题 → 截断或正常处理。

**验收**:`pytest test_sync_by_id.py test_prd_generator.py -v` 全绿。

**约束**:mock TAPD API,不打真实接口。

---

### T4.2 · ② 上下文:resolver 零副作用不变量 `[待办]`

**现状**:`resolver.py:3` 注释「Pure read operations. No writes, no side effects」是架构不变量(#1),但无测试守护。

**目标**:用测试断言 resolver 调用前后,story 状态/文件系统零变化。

**代码落点**:
- 测:`packages/story-lifecycle/tests/test_resolver_pure.py`(新建)
- 被测:`orchestrator/context/resolver.py:35 resolve`

**步骤**:
1. 快照 story DB 状态 + `.story/` 目录 mtime。
2. 调 `resolver.resolve(story_key)` 多次。
3. 断言 DB 状态不变、文件 mtime 不变、无新文件产生。

**验收**:`pytest test_resolver_pure.py -v` 全绿。

**约束**:这是架构不变量测试,归入阶段五的 `tests/invariants/` 也可。

---

### T4.3 · ② 上下文:SOFT 缝降级 `[待办]`

**现状**:`context_providers/__init__.py` try/except miner 是 SOFT 缝(不变量 #4),但「卸 miner 后 lifecycle 照跑」无测试。

**目标**:模拟 miner/knowledge 包不可 import,断言 lifecycle 不崩。

**代码落点**:
- 测:`packages/story-lifecycle/tests/test_soft_seam_degradation.py`(新建)
- 被测:`knowledge/context_providers/__init__.py:get_transcript_context`、`knowledge_provider.py`

**步骤**:
1. monkeypatch `sys.modules` 让 `import miner` 抛 ImportError。
2. 调 `get_transcript_context(...)` → 断言返回 None(不抛异常)。
3. 同理 mock `import knowledge` ImportError → `knowledge_provider.get_context()` 优雅跳过。

**验收**:`pytest test_soft_seam_degradation.py -v` 全绿。

**约束**:用 monkeypatch,不真卸载包。

---

### T4.4 · ⑤ 交付:worktree 清理无残留 `[待办]`

**现状**:`test_worktree.py` 存在,但 cleanup 后的「无孤儿进程/无残留目录」未测。

**目标**:worktree cleanup 后断言目录删除、PTY 关闭。

**代码落点**:
- 测:扩充 `test_worktree.py`
- 被测:`orchestrator/workspace/worktree/` cleanup 路径

**步骤**:
1. prepare worktree → cleanup → 断言目录不存在。
2. 若有关联 PTY → 断言 PTY 进程已 kill。

**验收**:`pytest test_worktree.py -v` 全绿。

**约束**:Windows 下注意进程 kill 的平台差异。

---

### T4.5 · ⑥ 飞轮:anchors round-trip + 卸包照跑 `[待办]`

**现状**:跨包 `tests/contracts/test_anchors_contract.py` 已有,补 round-trip(lifecycle 写→miner 读→回填 story_id)。

**目标**:anchors.jsonl 写入 → miner link 读取 → 正确回填 story_id 全链路。

**代码落点**:
- 测:扩充 `tests/contracts/test_anchors_contract.py`
- 参考:`docs/INTEGRATION.md` I2 锚点契约

**步骤**:
1. lifecycle 侧 `inject_prompt` → 写 anchors.jsonl。
2. miner 侧 `link.read_anchors` → 精确匹配 session → 回填 story_id。
3. 断言 high-confidence 绑定。

**验收**:`pytest tests/contracts/test_anchors_contract.py -v` 全绿。

**约束**:跨包契约测试,放 `tests/contracts/`。

---

## 阶段五 · 机制建设(长期可维护)

### T5.1 · coverage 度量 + 按模块报告 `[待办]`

**现状**:无 coverage 工具接入,不知覆盖率盲区。

**目标**:接入 coverage,出按业务模块的覆盖率报告。

**代码落点**:
- 配置:`pyproject.toml`(加 `[tool.coverage]`)
- 脚本:`scripts/qa-coverage.sh`(新建,可选)

**步骤**:
1. `pip install coverage`(装进 venv)。
2. `pyproject.toml` 加 coverage 配置(按 `orchestrator/evaluation`、`orchestrator/engine` 等分组)。
3. 跑全量测试出报告,记录到 `reports/T5.1-coverage-baseline.md`。

**验收**:
```bash
./.venv-monorepo-test/Scripts/python.exe -m pytest packages/story-lifecycle/tests/ \
  --cov=packages/story-lifecycle/src/story_lifecycle --cov-report=term-missing
```
报告产出,记录各模块覆盖率%(基线,后续卡提升它)。

**约束**:基线报告,不改代码。

---

### T5.2 · tests/invariants/ 目录 + 架构不变量集 `[待办]`

**现状**:架构不变量散落在各测试,无集中守护。

**目标**:建 `tests/invariants/` 目录,集中放 6 条架构不变量测试。

**代码落点**:
- 新建:`packages/story-lifecycle/tests/invariants/`
- 收纳:T4.2(resolver 零副作用)、T1.1(gate 硬闸)、T4.3(SOFT 缝)等不变量测试

**步骤**:
1. 建 `tests/invariants/__init__.py` + `conftest.py`。
2. 把 T1.1/T4.2/T4.3 等不变量测试移入(或加 re-export 保持原位置兼容)。
3. 文档化每条不变量对应架构规则。

**验收**:`pytest tests/invariants/ -v` 全绿,且每条不变量有 docstring 说明。

**约束**:移动测试不改变其行为(re-export 保兼容)。

---

## 执行备忘

- **取卡顺序**:严格按 README 进度表,不跳级(阶段一未完不做阶段二——阶段一堵的洞最大)。
- **卡做完的标志**:验收全绿 + 报告 + 进度表更新 + commit,四者缺一不可。
- **卡住时**:报告标 `[阻塞]`,写清卡在哪、试过什么、下一窗口建议。**不许跳过写报告**。
- **上下文将满**:立即停当前卡,写交接 NOTE 到报告末尾。
