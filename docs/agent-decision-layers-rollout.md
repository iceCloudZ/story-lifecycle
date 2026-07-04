# Agent 决策层落地 · 长程执行手册

> **目标**:把 story-lifecycle 从"开环 prompt 调度器"升级成**真 agent(感知-决策-行动-记忆闭环)**。
> **自包含**:另一个 Claude 会话(或你)读完本文件,即可从「§4 当前进度」继续执行,不需要其他上下文。
> **起点日期**:2026-07-05。**做法**:TDD(RED→GREEN→REFACTOR),每步看测试失败再实现。

---

## 1. 背景与脉络

- 前序:`docs/autonomous-pipeline-fix-roadmap.md`(全自动流水线断点 A–F 修复)——那是"让它能跑通"。
- 本文档:**让它成为真 agent**——给 lifecycle 装上"感知-决策-行动-记忆"闭环(supervisor),再延伸到五层决策。
- 关键转折:全自动跑不通的最深层原因不是 done 路径/gate,而是**没有 supervisor**(planner 只注入 prompt + 轮询 done,完全不监听 AI 输出 → AI 一提问就卡死)。supervisor 是 agent 的定义性结构。
- 工作流确认(用户):design 阶段 web pty 人接手;implement/verify 自动驱动 + supervisor 自动应答 AI 提问;release 人确认。权限类用户都选最大权限(源头 flag 堵)。

---

## 2. 架构(动代码前必读)

### 2.1 五层决策 + 双轨 supervisor

```
层5 元        反思学习 · 调度优先级           (阶段4)
层4 评判      质量 judge · 发布风险           (阶段2)
层3 异常      失败恢复 · 终止/升级            (阶段1)
层2 边界      stage 转移 · 计划迭代           (阶段3)
层1 执行内    supervisor(AI 提问自动答)      (阶段0,地基,手工做不能自举)
              ├─ Claude 轨: claude -p --output-format stream-json + hooks + defer/resume
              └─ codex/kimi 轨: PTY + agent-yes 三层 pattern + deepseek 决策
```

两轨**共用一个决策大脑**(deepseek Decider + 决策事件流),只"感知+应答"层不同。

### 2.2 核心原则(违反就重蹈 gate.py 覆辙)

1. **Decider 纯函数**:`(facts, 注入的 llm) -> decision`,零副作用(不读 DB、不写文件、不起进程)。副作用归 Handler。
2. **每个决策 `log_event`**:可审计 + 是反思层(阶段4)的数据源。
3. **复用骨架**:感知 → 纯 Decider → Handler → log。supervisor(阶段0)立骨架,层 2–5 换感知源 + 决策 prompt 复用。
4. **决策上下文喂结构化 facts**(LangGraph 范式),**不喂原始 PTY/stream 文本**(Anthropic game-of-telephone 降噪)。
5. **LLM judge 用结构化输出 + 固定选项顺序**(Anthropic 实证最稳)。
6. **反思用 verifier subagent 查 ground truth**(Claude Code `type:"agent"` hook),**不用 verbal reflection**(LLM 自我对话易自欺)。
7. **监督层 LLM 限频限深**:每次决策最多 1 次 LLM 调用 + 短 prompt(CrewAI manager-worker 烧 token 是反面教材;Anthropic 说 multi-agent 多 15× token)。
8. **不分新 package**:全落 `orchestrator/`(engine/evaluation/learning)。

### 2.3 关键调研发现(2026-07-05 开源调研,详 `.claude/plans/snappy-puzzling-moonbeam-agent-abd58d7b7e4afedaf.md`)

- **Claude 轨官方非-PTY**:`claude -p --output-format stream-json` 发结构化事件(`permission_request`/`idle_prompt`/`elicitation_dialog`),`PreToolUse`/`PermissionRequest`/`Elicitation` hooks 程序化应答,`defer`+`--resume` 是"外部 LLM 决策后回填"的官方 round-trip。**Claude 不走 PTY**(那是重新发明 Anthropic 已解决的轮子)。
- **codex/kimi 轨借 `snomiao/agent-yes`**:三层 pattern 配置 `{readyPatterns, enterPatterns, fatalPatterns}`(per-CLI)+ node-pty plumbing。决策层换成 deepseek。
- **统一**:两轨产统一"决策事件流" `{agent_id, adapter, ts, question, options, context_digest, choice, reason}`。

---

## 3. 环境前置(2026-07-05 scout 已确认)

- ✅ `claude` / `codex` / `kimi` CLI 全在 PATH(`which` 确认);`zellij` 也在
- ✅ `winpty` 装了 → PTY 走 winpty 模式(Windows)
- ✅ Web Board `story serve` 跑在 `127.0.0.1:8180`(`curl /api/session/health` → 200)
- ✅ deepseek key 已 curl 验证(规划 + 决策都可用,model `deepseek-v4-pro` 有效,FC 工作)
- ✅ pytest 用 **PATH 上的 hermes venv**(`C:\Users\zzh58\AppData\Local\hermes\hermes-agent\venv`),项目 `.venv` 没装——直接 `pytest ...` 即可
- ✅ `pty.py._queue` + WebSocket 广播(`api.py:47 _ws_clients`)基础设施在

**跑测试**(从 repo root `D:\github\story-lifecycle`):
```bash
pytest packages/story-lifecycle/tests/test_supervisor.py -v       # 单文件
pytest packages/story-lifecycle/tests/                            # 全包
```

**TDD 铁律**:NO PRODUCTION CODE WITHOUT A FAILING TEST FIRST。写测试 → 跑看 RED(失败原因要正确)→ 最小实现 → 跑看 GREEN → 重构。

---

## 4. 当前进度(已完成 · 2026-07-05)

### 4.1 新增/修改文件

| 文件 | 状态 | 内容 |
|---|---|---|
| `packages/story-lifecycle/src/story_lifecycle/orchestrator/engine/supervisor.py` | 新增 | `decide_response` / `log_decision` / `handle_pty_output` |
| `packages/story-lifecycle/src/story_lifecycle/infra/terminal/pty.py` | 修改 | `_distribute` / `add_tap` / `remove_tap`;`_read_loop` 改用 `_distribute` |
| `packages/story-lifecycle/tests/test_supervisor.py` | 新增 | 5 测试 |
| `packages/story-lifecycle/tests/test_pty_tap.py` | 新增 | 2 测试 |

### 4.2 已实现函数签名(供续作者对齐)

```python
# supervisor.py —— 纯 Decider
def decide_response(*, question: str, options: list[str], story_facts: dict,
                    llm_invoke: Callable[[str], str]) -> dict:
    """Returns {"choice": str, "reason": str}. choice 必须在 options 里,否则 ValueError。
    剥离 ```json``` 代码块后 json.loads。"""

def log_decision(*, story_key: str, stage: str, adapter: str, question: str,
                 options: list[str], decision: dict, log_event_fn: Callable) -> None:
    """Handler: 写 log_event(event_type="supervisor_decision", payload={adapter,question,options,choice,reason})。注入 log_event_fn 可测。"""

def handle_pty_output(*, buffer: str, pty, adapter: str, story_facts: dict,
                      is_awaiting_fn: Callable, llm_invoke: Callable[[str], str],
                      log_event_fn: Callable) -> bool:
    """codex/kimi 轨同步核心:is_awaiting_fn(buffer) 命中 → decide_response → pty.write(choice+'\r') → log_decision。返回是否应答。"""

# pty.py —— ManagedPty 方法
def add_tap(self, maxsize=512) -> asyncio.Queue:  # 旁路 queue,每条输出复制一份;Web Board 主 _queue 不变
def remove_tap(self, tap: asyncio.Queue) -> None
def _distribute(self, data: bytes) -> None  # put 到 _queue + 所有 taps,QueueFull drop oldest
```

### 4.3 测试状态

- **7 新测试全 GREEN**(test_supervisor 5 + test_pty_tap 2)
- **全量 657 passed, 1 failed, 4 skipped**
- 唯一 fail:`test_smoke::test_packaged_and_root_profiles_consistent`——**预存在**,与本任务无关(我没碰 profiles,git status 可证),归 AI-3/4 窗口或 profile 维护者。**不要花时间修它**。

### 4.4 supervisor 骨架已立

感知(`is_awaiting_fn`)→ 纯 Decider(`decide_response`)→ Handler(`pty.write` + `log_decision`)→ 记忆(`log_event`)。**codex/kimi 轨同步闭环已通**(handle_pty_output 单元测试)。

---

## 5. 剩余执行步骤(从这里继续)

> 每步:TDD(写测试→RED→实现→GREEN)+ 跑全量确认无回归。每完成一个阶段建分支 commit。

### 5.0 完成判据(每一层 done 必须满足,缺一不可)

每一层(阶段 0–4)的"完成"**不是**"代码写完 + unit test GREEN",而是必须**自己跑通端到端并记录证据**。未满足 4 条 = 该层未完成,**不许进入下一层**(verification-before-completion:证据先于断言)。

1. **测试 GREEN**:TDD 写的测试 + 该层相关全量测试通过
   ```bash
   pytest packages/story-lifecycle/tests/   # 657+ passed(唯一允许 fail:预存在 profile 一致性)
   ```
2. **端到端自验证(必须自己跑,不许只靠 unit test)**——用 `verify` skill(run the app and observe behavior):
   - 起真的 code agent(claude/codex/kimi)在真实 workspace 跑该层涉及的 stage
   - **主动触发**该层决策点:
     - 层1 supervisor:让 AI 真的提出一个澄清/选择问题(给个模糊任务)
     - 层3 recovery:故意制造失败(kill adapter 进程 / done 永不写)
     - 层4 judge:故意提交一个测试失败的 implement
     - 层2 transition:让 gate 产 fail 决策
     - 层5 reflection:连跑 N 个 story 产生决策 log
   - **observe 该层真的工作**:supervisor 自动答进去 / recovery 真换了 adapter 或降级 / judge 真拦下 retry / transition 真转移非硬编码 / reflection 真产出 playbook
3. **记录证据**(可追溯,落到本文件该层 checkpoint 处):
   - 决策事件 SQL:`select * from events where event_type in ('supervisor_decision','recovery_action','judge_verdict','transition_decision') and story_key='...'`
   - Web Board 截图(supervisor 应答的 PTY 画面)/ 关键日志片段
   - 在该层 checkpoint 勾选 ✅ + 贴证据(事件 id / 截图路径 / log 摘要)
4. **全量回归**:`pytest packages/story-lifecycle/tests/` 仍 657+ passed,无新 fail。

> 反面:如果只跑了 unit test 就标 done,后续会爆(supervisor 在真 PTY 输出下识别失败、MCP 没接通、deepseek 实际返回格式不同等,只有端到端能抓)。**这一步省不得。**

### 阶段 0 剩余(codex/kimi 轨收尾 + Claude 轨 + 配套)

#### 0c-1 · `handle_pty_output` 未命中测试  ✅ DONE
- **目标**:验证 is_awaiting 未命中时**不调 LLM、不写 PTY、不 log**(短路,省 token)。
- **TDD**:在 `test_supervisor.py::TestHandlePtyOutput` 加 `test_no_answer_no_llm_no_log_on_miss`:fake_awaiting 返回 None,断言 `answered is False`、`writes == []`、`logs == []`,且 `fake_llm` 调用计数为 0(用计数器断言)。
- **预期**:当前实现已短路(`if not hit: return False`),测试应直接 GREEN(验证现有行为)。若 GREEN 则继续;若意外 RED,说明实现有 bug,修实现。

#### 0c-2 · `supervise_pty_session`(async 消费 tap 循环)
- **目标**:把 `handle_pty_output` 接到 `ManagedPty.add_tap()` 的 async 队列,持续监督一个 PTY session。
- **签名**:`async def supervise_pty_session(*, pty, adapter, story_facts, is_awaiting_fn, llm_invoke, log_event_fn, buffer_bytes=2000) -> None`。循环:`data = await tap.get()` → 解码追加到滑窗 buffer(保留末尾 buffer_bytes 字节)→ `handle_pty_output(...)` → 命中则清 buffer。`finally: pty.remove_tap(tap)`。
- **TDD**:fake pty(`add_tap` 返回一个预填 chunks 的 `asyncio.Queue` + 记录 write + 末尾塞 `None` 触发 `pty.alive=False` 退出循环)→ 跑 `await supervise_pty_session(...)` → 断言命中点被应答 + log。用 `@pytest.mark.asyncio`。
- **新模块**:`supervisor.py` 加 `supervise_pty_session`。

#### 0c-3 · pattern 识别(借 agent-yes 三层 pattern)
- **目标**:实现真实的 `is_awaiting_fn`——识别 codex/kimi 在 PTY 里"在等人"。
- **调研**:`snomiao/agent-yes` 用 per-CLI `{readyPatterns, enterPatterns, fatalPatterns}`(正则)。抄这个抽象进 `knowledge/adapters/{codex,shell}.py` 或 yml 配置。
- **实现**:新 `orchestrator/engine/awaiting_detector.py`:`make_awaiting_fn(adapter: str) -> Callable[[str], tuple[str, list[str]] | None]`,从 adapter 配置读 pattern。先硬编码 codex/kimi 的几个常见提问 pattern(如 `"选择|请选择|\\?\\s*$"`),LLM 兜底把命中片段解析成 (question, options)。
- **TDD**:喂样例 PTY 输出("请选择: A) foo B) bar")断言返回 (question, ["A","B"]);喂普通输出断言 None。
- **研究**:实际跑 `codex`/`kimi`(在 tmp repo 触发一个提问),抓 PTY 输出看提问的真实 pattern——这是最不确定的一步,值得花时间。

#### 0c-4 · codex/kimi 轨端到端
- **目标**:起真 codex/kimi(implement stage),supervisor 自动应答提问,`select * from events where event_type='supervisor_decision'` 有记录。
- **方法**:`story serve` + 手动构造一个 implement stage 的 web pty session + 触发提问 + 看 supervisor 应答。可能要先把 supervisor 接到 `planner.continue_orchestrator_agent`(launch action 起 pty 后启 `supervise_pty_session` task)。

#### 0b · Claude 轨(stream-json + hooks,**官方最稳**)
- **0b-1 stream-json 解析**:新 `engine/claude_stream.py`,解析 `claude -p --output-format stream-json` 的事件流,识别 `permission_request`/`idle_prompt`/`elicitation_dialog` → 抽 (question, options)。TDD:喂样例 stream-json 断言。
- **0b-2 应答机制(design 探索)**:选一:
  - (a) `--permission-prompt-tool mcp__lifecycle__permission`:lifecycle 跑 MCP server 暴露 `permission_prompt`,Claude 调时 → `decide_response` → 返回 allow/deny。**支持 Claude.ai 订阅认证**(Agent SDK 要 API key 付费)。
  - (b) `defer` + `claude -p --resume`:Claude 提问 → hook defer → lifecycle 决策 → resume 回填。
  - 推荐 (a)(更直接,官方背书)。
- **0b-3 端到端**:起 `claude -p --output-format stream-json --permission-prompt-tool ...`,supervisor 通过 MCP 决策,验证 implement stage 跑通。

#### 0d · 链路闭合配套
- **B done 路径收敛**:`planner.py:495-498` + `planner.py:295` 默认值改用 `stage_done_file()`(`infra/paths.py`,单一真相 `.story/done/{key}/{stage}.json`)。
- **D 异常回写**:`graph.py:188-200` `run_story` except 加 `db.update_story(story_key, status="failed", last_error=str(e)[:500])`。
- **权限源头堵**:`knowledge/adapters/{codex,shell}.py` launch 加 bypass flag;Claude 用 `--permission-prompt-tool`(不弹)。
- **TDD**:每个改都先写测试(B:`test_default_done_file_matches_stage_done_file`;D:`test_run_story_marks_failed_on_raise` mock planner 抛错)。

**阶段 0 checkpoint**:Claude 轨 + codex/kimi 轨各跑通一个 implement stage,决策全落 log 可审计。

---

### 阶段 1 · 失败恢复(层 3)
- 新 `engine/recovery.py` 纯 Decider:`decide_recovery(*, exc, story_facts, adapter, attempt_count, recovery_facts) -> {"action": "retry_new_adapter"|"skip_stage"|"downgrade_to_manual"|"escalate_human"|"abort", "reason": str, "new_adapter": str?}`。
- `graph.py run_story` except 接 `decide_recovery`(替当前吞异常),Handler 执行救法。
- 终止判断接 `policy_engine`(投入产出,扩展 L0–L5 矩阵)。
- **TDD**:喂异常类型断言救法;mock planner 抛错断言 story 状态 + adapter 切换。
- **checkpoint**:故意制造失败(adapter 崩/done 永不现)→ recovery 自动救,不卡 active。

### 阶段 2 · 质量 judge(层 4)
- 新 `evaluation/judge.py` 纯 Decider:`judge_quality(*, done_data, test_result, story_facts, llm_invoke) -> {"pass": bool, "rework_point": str?, "reason": str}`。读 done 的 `build_passed`/`tests_passed` + 真跑 profile `verification_commands` + LLM judge(结构化输出 + 固定选项)。
- gate 接 judge(**协调 AI-2 窗口** gate decide+apply 拆分——它的 `gate.py:190` 拆完后接 judge;或本阶段先加最小判据 done 字段空/false → retry)。
- 发布风险:`service/delivery.py` 扩展(预判发布影响面辅助人)。
- **TDD**:done `build_passed=false` → judge 返回 rework;LLM judge 结构化输出。
- **checkpoint**:故意提交失败测试 → judge 拦下 retry,不靠人判质量。

### 阶段 3 · stage 转移(层 2)
- 新 `engine/transition.py` 纯 Decider:`decide_transition(*, gate_decision, failure_mode, history_facts) -> {"action": "retry"|"skip"|"swap_approach"|"insert_rescue_stage"|"escalate", ...}`。
- 新 `engine/replanner.py`:执行反馈 → 重规划(复用 `planner.run_orchestrator_agent` 的 `invoke_with_tools`)。
- 替 `planner.py:769-797` 硬编码 `actions.insert()`。
- **TDD**:gate fail + 历史"同类失败换 adapter 成功" → decide_transition 返回 swap_approach。
- **checkpoint**:gate fail 后智能转移,非硬编码 insert。

### 阶段 4 · 反思 + 调度(层 5)
- 新 `learning/reflection.py`:读 `events` 表(supervisor_decision/recovery/judge 决策 log)→ 调整决策规则 / 沉淀 playbook(打通飞轮 miner→knowledge→context_providers 回注)。用 verifier subagent 形态(查 ground truth),非 verbal reflection。
- 新 `engine/scheduler.py`:多 story 优先级/并发(替 `graph.py max_workers=4` FIFO)。
- **TDD**:喂历史决策 log 断言沉淀的 playbook;喂多 story 状态断言调度顺序。
- **checkpoint**:跑 N story 后 reflection 产出可复用知识,新 story 受益(飞轮转起来)。

---

## 6. 关键参考

- **Claude hooks 官方(必读)**:https://code.claude.com/docs/en/hooks
- Claude Agent SDK hooks:https://code.claude.com/docs/en/agent-sdk/hooks
- Headless `claude -p`:https://code.claude.com/docs/en/headless
- `--permission-prompt-tool` 深挖:https://lobehub.com/nl/mcp/user-claude-code-permission-prompt-tool
- `snomiao/agent-yes`(codex/kimi 轨检测层):https://github.com/snomiao/agent-yes
- LangGraph supervisor(决策上下文范式):https://github.com/langchain-ai/langgraph-supervisor-py
- Anthropic 多 agent research(orchestrator-worker + LLM judge):https://www.anthropic.com/engineering/built-multi-agent-research-system
- OpenHands `AWAITING_USER_INPUT` 状态机:https://github.com/OpenHands/OpenHands/issues/5535
- 完整调研报告(本地):`.claude/plans/snappy-puzzling-moonbeam-agent-abd58d7b7e4afedaf.md`

---

## 7. 执行约定

- **每阶段一个 feature 分支**:`fix/agent-stageN-...`(阶段 0 = `fix/agent-supervisor-stage0`)
- **TDD**:写测试→看 RED→最小实现→看 GREEN→重构。不跳。
- **Decider 纯函数**:零副作用,LLM/DB/PTY 全注入。守 AI-2 角色分离(不重蹈 `gate.py` 焊死覆辙)。
- **不破坏 Web Board**:pty.py 改动要保 `_pty_ws_handler`(`api.py:241 pty._queue.get()`)兼容——已用 `_distribute` + `add_tap` 旁路解决,后续动 pty.py 前重读 `test_pty_tap.py`。
- **每层必须自验证**:unit test GREEN 不算 done——必须起真 code agent 跑端到端 + observe + 记录证据(§5.0 完成判据)。用 `verify` skill。未自验证不许进下一层。
- **每 1-2 步 commit**,commit message 末尾加 `Co-Authored-By: Claude <noreply@anthropic.com>`。
- **预存在 fail 不归本任务**:`test_smoke::test_packaged_and_root_profiles_consistent`(profile 一致性)。

---

## 8. 续作者快速入口

1. 读本文件 §2(架构原则)+ §4(当前进度)。
2. `pytest packages/story-lifecycle/tests/test_supervisor.py packages/story-lifecycle/tests/test_pty_tap.py -v` 确认 7 测试 GREEN(基线)。
3. 从 §5「阶段 0 剩余」的 **0c-1** 开始 TDD。
4. 卡住时读 §6 调研报告 + §2 原则。
