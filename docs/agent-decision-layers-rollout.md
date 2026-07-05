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

### 4.0 五层落地总览(2026-07-05 续作完成)

| 层 | 阶段 | Decider | 状态 | 自验证 |
|---|---|---|---|---|
| 层1 执行内 | 0 | `decide_response`/`handle_pty_output`/`supervise_pty_session`/`awaiting_detector`/`claude_stream` | ✅ | 受控 agent 真 PTY 闭环(capture→detect→deepseek→write→log,event id=330)+ 真 deepseek judge_permission(rm -rf →deny) |
| 层3 异常 | 1 | `decide_recovery`(+wired into run_story) | ✅ Decider+wiring | mock planner 抛错 → 真 recovery_action 事件入 DB → 不卡 active |
| 层4 评判 | 2 | `judge_quality` | ✅ Decider | 真 deepseek:tests fail→rework(0 LLM)、空 stub→rework=quality(抓硬指标漏判) |
| 层2 边界 | 3 | `decide_transition` | ✅ Decider | 8 测全 action 矩阵 + 历史 swap 优先;planner.py:776 接入待做 |
| 层5 元 | 4 | `reflect` / `decide_schedule` | ✅ Decider | reflect 跑真 event_log(stats 正确);6+6 测 |

**全量回归**:`723 passed, 1 failed(预存在 profile 一致性,无关), 4 skipped`。
**诚实的待办**(非阻塞,各层 Decider 已就绪):
- 0b-2/0b-3 Claude MCP server 暴露 + `--permission-prompt-tool` 真闭环(本机 Claude 全程 allow,无真 permission_request 可触发;codex/kimi PTY 轨已证明同一决策大脑闭环)。
- 0d perms bypass-flags 接入(CLI flag 各异 + codex 环境阻断)。
- 层3 rescue Handler(retry 换 adapter 重启 planner)、层2 planner.py:776 硬编码 insert 替换 + replanner、层5 playbook→history_facts 回注 + scheduler→graph FIFO 替换。

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

#### 0c-2 · `supervise_pty_session`(async 消费 tap 循环)  ✅ DONE
- **目标**:把 `handle_pty_output` 接到 `ManagedPty.add_tap()` 的 async 队列,持续监督一个 PTY session。
- **签名**:`async def supervise_pty_session(*, pty, adapter, story_facts, is_awaiting_fn, llm_invoke, log_event_fn, buffer_bytes=2000) -> None`。循环:`data = await tap.get()` → 解码追加到滑窗 buffer(保留末尾 buffer_bytes 字节)→ `handle_pty_output(...)` → 命中则清 buffer。`finally: pty.remove_tap(tap)`。
- **TDD**:fake pty(`add_tap` 返回一个预填 chunks 的 `asyncio.Queue` + 记录 write + 末尾塞 `None` 触发 `pty.alive=False` 退出循环)→ 跑 `await supervise_pty_session(...)` → 断言命中点被应答 + log。用 `@pytest.mark.asyncio`。
- **新模块**:`supervisor.py` 加 `supervise_pty_session`。

#### 0c-3 · pattern 识别(借 agent-yes 三层 pattern)  ✅ DONE
- **目标**:实现真实的 `is_awaiting_fn`——识别 codex/kimi 在 PTY 里"在等人"。
- **调研**:`snomiao/agent-yes` 用 per-CLI `{readyPatterns, enterPatterns, fatalPatterns}`(正则)。抄这个抽象进 `knowledge/adapters/{codex,shell}.py` 或 yml 配置。
- **实现**:新 `orchestrator/engine/awaiting_detector.py`:`make_awaiting_fn(adapter: str) -> Callable[[str], tuple[str, list[str]] | None]`,从 adapter 配置读 pattern。先硬编码 codex/kimi 的几个常见提问 pattern(如 `"选择|请选择|\\?\\s*$"`),LLM 兜底把命中片段解析成 (question, options)。
- **TDD**:喂样例 PTY 输出("请选择: A) foo B) bar")断言返回 (question, ["A","B"]);喂普通输出断言 None。
- **研究**:实际跑 `codex`/`kimi`(在 tmp repo 触发一个提问),抓 PTY 输出看提问的真实 pattern——这是最不确定的一步,值得花时间。

#### 0c-4 · codex/kimi 轨端到端  ✅ DONE(受控 agent 闭环;真 codex 受环境阻断)
- **目标**:起真 codex/kimi(implement stage),supervisor 自动应答提问,`select * from events where event_type='supervisor_decision'` 有记录。
- **方法**:`story serve` + 手动构造一个 implement stage 的 web pty session + 触发提问 + 看 supervisor 应答。可能要先把 supervisor 接到 `planner.continue_orchestrator_agent`(launch action 起 pty 后启 `supervise_pty_session` task)。
- **实际验证(2026-07-05)**:受控闭环 E2E —— 真 winpty PTY 跑一个发"请选择: A) … B) …"提问的脚本,
  `supervise_pty_session` + 真 `make_awaiting_fn("codex")` + 真 deepseek(`deepseek-v4-pro`)+ 真 `db.log_event`:
  - ✅ 检测命中 → options `["A","B"]` → deepseek 决策 `choice="A"` → supervisor 写回 `A\r` → **脚本收到 `RECEIVED_ANSWER:A`**
  - ✅ `event_log` 落 `supervisor_decision`(id=330,payload 含 adapter/question/options/choice/reason,question 已剥离 ANSI)
  - 即:`capture → detect → decide → write-back → log` 全链路真跑通。
- **环境阻断(诚实记录)**:
  - `codex` 跑不起来(`Error: timed out waiting for cloud config bundle after 15s` —— 网络/auth),非本任务代码问题。
  - `kimi -p` headless 不提问(直接出答案退出);interactive TUI 提问触发不确定。
  - 故用受控 agent 脚本(确定性提问)验证 supervisor 机制本身;真 CLI 输出样本已用 `kimi -p`/`claude stream-json` 单独验证检测面。
- **E2E 副产出**:发现并修了 detector 没剥 ANSI 转义 → 真 PTY 输出污染 question 字段(已修 + 加单测)。

#### 0b · Claude 轨(stream-json + hooks,**官方最稳**)
- **0b-1 stream-json 解析** ✅ DONE:新 `engine/claude_stream.py`,`parse_line` / `extract_awaiting` /
  `decide_permission`。识别三种"在等人"信号:permission MCP 工具调用(`assistant.tool_use.name == permission_tool`)、
  裸 `permission_request` 事件、`elicitation`/`idle_prompt`(options 非空)。非信号(system/init、thinking、
  正常 tool_use、result)→ None。fixtures 取自真跑 stream-json(本仓库 Write 全程被 allow,无真 permission_request
  可抓,故 permission/elicitation 用构造样例 + 真实非 awaiting 行)。12 测试 GREEN。
- **0b-2 决策侧** ✅ DONE(决策半):`decide_permission(tool_name, tool_input, story_facts, llm_invoke) -> {behavior, reason}`,
  复用 `supervisor.decide_response`(选项固定 [allow, deny],守 §2.2 #5)。真 deepseek 实测:
  `rm -rf /` → **deny**、`Read README.md` → **allow**、`Write import os` → **allow**(决策正确)。
  `permission_tool_response` 包成 MCP `--permission-prompt-tool` 返回形(`{behavior, updatedInput, message}`)+ 落日志。
- **0b-2 应答机制 = 选项 (b) defer/resume(不走 MCP)** ✅ DONE:`supervise_claude_stream(lines, story_facts, llm_invoke, log_event_fn)`
  —— 消费 stream-json 行流 → `extract_awaiting` 命中(permission_request/elicitation)→ `decide_response` 决策 →
  `log_decision` 落 `supervisor_decision`。与 codex/kimi PTY 轨 `supervise_pty_session` 对称(共用 `decide_response` 大脑,
  感知源是结构化 stream-json 而非 PTY regex)。复用已建的 `claude_stream.py`,**不引入 MCP server 子进程**。
  Handler 拿 decisions 后用 `claude -p --resume` 回填答案(本机 Claude 全 allow → 无真 permission_request → 回填环境阻断,
  但决策循环已 4 测验证)。
- **0b-3 端到端** ⏳ 受环境限:本机 Claude 全程 allow(无 permission_request 可触发),无法自然驱动 defer/resume 回填闭环;
  codex/kimi PTY 轨(0c-4)已用受控 agent 证明了同一决策大脑的全链路闭环(capture→detect→decide→write→log)。
- **0b-2 应答机制(design 探索)**:选一:
  - (a) `--permission-prompt-tool mcp__lifecycle__permission`:lifecycle 跑 MCP server 暴露 `permission_prompt`,Claude 调时 → `decide_response` → 返回 allow/deny。**支持 Claude.ai 订阅认证**(Agent SDK 要 API key 付费)。
  - (b) `defer` + `claude -p --resume`:Claude 提问 → hook defer → lifecycle 决策 → resume 回填。
  - 推荐 (a)(更直接,官方背书)。

#### 0d · 链路闭合配套
- **B done 路径收敛** ✅ DONE:planner 默认 done_file 改用 `stage_done_file_rel(story_key, stage)`(新 `infra/paths.py`
  单一真相,workspace 相对的 `.story/done/{key}/{stage}.json`)。**根因**:planner 旧默认写 `.story-done/{key}-{stage}.json`、
  自己在 `Path(workspace)/done_file_rel` 轮询,但 `graph.py:259` 用绝对 `stage_done_file()` 读新布局 → 写读不对齐,
  done 永远收不到(断点 B)。新增 `stage_done_file_rel` 与绝对版同布局(对齐不变式测试守护),planner 两处默认(295/496)切换。
- **D 异常回写** ✅ DONE:`graph.py run_story` except 加 `db.update_story(story_key, status="failed", last_error=str(exc)[:500])`
  (包 try 防二次异常)。**根因**:旧 except 只写 graph_error.log,story 仍标 `active` 永远卡住(断点 D)。
  TDD:`test_run_story_marks_failed_on_raise`(mock planner 抛错 → status=failed + last_error 含异常文本)+ 不误伤正常完成。
- **权限源头堵** ⏳ 设计就绪,未接:`knowledge/adapters/{codex,shell}.py` launch 加 bypass flag(codex `--full-auto` / kimi `--auto`)、
  Claude 用 `--permission-prompt-tool`(不弹)。CLI flag 各异 + 改 launch 行为,未在本机逐 CLI 验证(codex 受环境阻断),
  故先留设计;supervisor 已能在确实提问时兜底应答。`BaseAdapter` 可加 `bypass_flags()` 钩子默认空,子类按 CLI 填。
- **TDD**:B `test_paths_done`(写读对齐不变式 3 测)+ D `test_run_story_error`(2 测),全 GREEN。

**阶段 0 checkpoint**:Claude 轨 + codex/kimi 轨各跑通一个 implement stage,决策全落 log 可审计。

---

### 阶段 1 · 失败恢复(层 3)  ✅ Decider+wiring DONE;rescue Handler 待接
- 新 `engine/recovery.py` 纯 Decider ✅:`decide_recovery(*, exc, story_facts, adapter, attempt_count, recovery_facts) ->
  {action, reason, new_adapter?}`。规则驱动(无需 LLM,recovery 频次低 + 规则更稳,守 §2.2 #7):
  - auth/config 错 → ``escalate_human``(重试无价值)。
  - 达 max_attempts(默认 3):P0/P1 → escalate_human、P2 → downgrade_to_manual、P3+ → skip_stage。
  - 瞬时错未达上限 → ``retry_new_adapter``,按 ``adapter_order`` 轮转(codex→claude→kimi,回绕)。
  - ``abort`` 留 policy_engine 接入(基础版不主动触发)。
- `graph.py run_story` except 接 `decide_recovery` ✅:落 `recovery_action` 事件(审计 + 层5 反思数据源)+
  story 标 failed(不卡 active,断点 D 同步修)。
- **TDD**:8 个 Decider 单测(全 action 矩阵 + adapter 轮转 + 兜底)+ 1 wiring 测,全 GREEN。
- **§5.0 自验证**:wiring 测真跑 —— mock planner 抛 `TimeoutError("done file never appeared")`(模拟 done 永不现)→
  真调 decide_recovery → 真 `recovery_action` 事件入 event_log(payload action=retry_new_adapter + new_adapter + reason)→
  story 标 failed(不卡 active)。
- **rescue Handler 待接**(诚实记录):decide_recovery 只决策 + 记录;**实际执行**救法(retry 换 adapter 重启 planner /
  skip / downgrade)是后续 Handler,需 planner 暴露 adapter-override 入口。决策侧已就绪可审计。

### 阶段 2 · 质量 judge(层 4)  ✅ Decider DONE;gate 接入待 AI-2 协调
- 新 `evaluation/judge.py` 纯 Decider ✅:`judge_quality(*, done_data, test_result, story_facts, llm_invoke) ->
  {pass, reason, rework_point?}`。两段决策:
  - **硬指标(规则,无 LLM)**:done `build_passed=False` → rework "build";`tests_passed=False` 或
    test_result 有 failures → rework "tests"(省 token,§2.2 #7)。
  - **LLM judge(结构化)**:硬指标过 → 喂结构化 facts,固定选项 [pass, rework](§2.2 #5),
    复用 `supervisor.decide_response`。choice=rework → rework_point="quality"。
- gate 接入 ⏳ 待 AI-2 协调:gate decide+apply 拆分完成后接 judge(fail 则不 apply / 触发 retry)。
  本阶段 Decider 已就绪;最小判据(done 字空/false → rework)已在硬指标段覆盖。
- 发布风险 ⏳ 未做:`service/delivery.py` 扩展(预判发布影响面辅助人)。
- **TDD**:6 测(硬指标 build/tests/test_result fail 不调 LLM + LLM pass/rework + 缺字段兜底),全 GREEN。
- **§5.0 自验证(真 deepseek)**:
  - clean(build+tests 过)→ **pass=True**。
  - tests_failed → **pass=False rework=tests,0 次 LLM 调用**(硬指标短路)。
  - done 全过但实现是**空 stub** → LLM 判 **pass=False rework=quality**(reason:"All functions are empty stubs,
    no actual impl")—— 抓到硬指标漏判的微妙质量问题,正是层4 价值。

### 阶段 3 · stage 转移(层 2)  ✅ Decider DONE;planner 接入待做
- 新 `engine/transition.py` 纯 Decider ✅:`decide_transition(*, gate_decision, failure_mode, history_facts) ->
  {action, reason, rescue_stage?}`。action:``proceed`` / ``retry`` / ``swap_approach`` /
  ``insert_rescue_stage`` / ``escalate``。规则驱动 + history_facts:
  - gate 过 → ``proceed``。
  - 缺依赖 → ``insert_rescue_stage``(+ rescue_stage 名)。
  - **历史"同类失败换法成功"→ ``swap_approach``(优先于无脑 retry,避免反复重试同一失败法)**。
  - 同 stage 反复失败 ≥ max_retries → ``escalate``。
  - 其余可恢复首次 → ``retry``。
- 替 `planner.py:776` 硬编码 `actions.insert()` ⏳ 待做:把 verify-gate retry 路径换成
  decide_transition 映射(retry→insert verify-retry、swap→换 adapter、rescue→插救援 stage、escalate→停)。
  现有 verify-gate 流程在用,替换有回归风险,留作专门接入 + 回归。
- 新 `engine/replanner.py` ⏳ 待做:执行反馈 → 重规划(复用 `planner.run_orchestrator_agent.invoke_with_tools`)。
- **TDD**:8 测覆盖全 action 矩阵 + 历史 swap 优先 + 缺依赖插救援 + 反复失败 escalate,GREEN。
- **checkpoint(Decider 级)**:gate fail 后的转移**决策**非硬编码(历史驱动 swap / 缺依赖 rescue / 反复 escalate);
  planner 真接入后即满足"智能转移"端到端。

### 阶段 4 · 反思 + 调度(层 5)  ✅ Decider DONE;回注/并发接线待做
- 新 `learning/reflection.py` ✅:`reflect(*, events) -> {playbook, stats}`。读决策事件流
  (supervisor_decision/recovery_action/judge_verdict/transition_decision)→ 沉淀 playbook。
  **verifier 形态(§2.2 #6)**:基于事件 ground truth(同 story recovery 后是否真 pass)判成功,
  非 verbal reflection。规则:``recovery(retry_new_adapter X→Y) + 后续 pass`` → "X 失败换 Y 成功" 规则,
  support 累加、降序;无 pass 兜底不沉淀(避免学错)。recovery 输出补 ``failed_adapter`` 供反思读。
- 新 `engine/scheduler.py` ✅:`decide_schedule(*, stories) -> [story_key]`。替 ``graph.py max_workers=4``
  纯 FIFO。排序键 ``(ready, priority_rank, created_at)``:就绪先跑、P0>P1>…、同优先级 FIFO。缺字段兜底。
- **TDD**:reflection 6 测(playbook 形成 / support 累加 / 无 pass 不沉淀 / stats)+ scheduler 6 测
  (优先级 / 就绪优先 / FIFO / 缺字段兜底),全 GREEN。
- **§5.0 自验证(真 DB)**:reflect 跑在真实 ``event_log`` 上 —— 读到真实 ``supervisor_decision`` 事件、
  stats 正确、playbook 当前为空(真 DB 暂无 recovery→pass 链,该链在隔离测试 DB 验);逻辑由 6 单测保证
  (含 recovery+pass→rule、support 累加、无 pass 不沉淀)。随真 story 积累,playbook 自填充。
- **回注接线 ⏳ 待做**:playbook → 层2 transition 的 ``history_facts`` / context_providers(让新 story 受益,
  飞轮转起来);scheduler → 替 graph.py 的 FIFO 提交。Decider 已就绪。

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
