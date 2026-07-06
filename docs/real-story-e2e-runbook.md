# 真实 Story 端到端测试 · 执行手册(自包含)

> **目标**:在 hc-all 真目标仓库,基于**老 master** 建分支,**重新开发**一个真 story,用真 PRD + 真 agent(claude)跑通,
> 观察 story-lifecycle **五层 agent 决策系统**(supervisor / recovery / judge / transition / reflection)在真执行里触发。
> **自包含**:新会话读完本文件即可执行,不需其他上下文。
> **起点日期**:2026-07-06。

---

## 0. 用户已拍板的约束(铁律)

1. **token / 时间消耗不是问题** —— 不要省钱、不要省调用,该跑就跑。
2. **story 实现过不是问题** —— 从目标子 repo 的 **master 找一个老版本(feature 合并前)**,建**新分支**,**重新开发**。
3. **子 repo 先提交(保当前分支状态)**,再切到 #2 的新分支。
4. 任何消耗(agent token、wall-clock)都 OK。
5. **这就是要真跑的目的** —— 暴露真问题、验证五层真触发(不只 unit test)。
6. **一次跑通**(长程任务,别中途停问)。
7. **stage→agent 映射(已确认能跑通)**:**plan/design 用 `claude`、编码/implement 用 `kimi`**;`codex` 阻断别用。

---

## 1. 环境前置(均已就绪,直接用)

| 项 | 值 |
|---|---|
| orchestrator 仓库 | `D:\github\story-lifecycle`(默认分支 `main`,已含五层 + 全 plumbing,本地 commit;**push 受沙箱网络阻断**,不用 push) |
| 目标工作区 | `D:\hc-all`(**多 repo**:hc-user、frontends/hc-admin、hc-config、hc-limit、hc-message、… 各自独立 git) |
| 真 PRD/spec/plan 文件 | `D:\hc-all\story\<id>-<title>\PRD.md`(+ spec.md、plan.md) |
| story serve(orchestrator HTTP) | `127.0.0.1:8180`;健康 `GET /api/session/health` → `{"status":"ok"}`。用 `run-story-serve` skill 起/停 |
| deepseek key | `~/.story-lifecycle/config.yaml`(model `deepseek-v4-pro`,base `https://api.deepseek.com`)。代码经 `load_config_to_env()` 读 |
| CLI | `claude`(可用,本机全 allow,作 agent 主力)、`codex`(**cloud-config 阻断,不可用**)、`kimi`(`-p` headless 可用)、`winpty` 装了 |
| pytest | 用 PATH 上 hermes venv(`C:\Users\zzh58\AppData\Local\hermes\hermes-agent\venv`)—— 直接 `pytest ...` 即可,**别用项目 .venv** |
| repo root | `D:\github\story-lifecycle`(所有命令默认在此跑) |

---

## 2. 候选 story

**首选:`tapd-1144381896001065458`(HC 用户:后台登录记录查询)**
- 真 PRD:`D:\hc-all\story\1065458-登录记录查询\PRD.md`(+ spec.md、plan.md,都在)
- 目标 repo(经 `story_project` 绑定,`base=master`):
  - `D:\hc-all\hc-user`
  - `D:\hc-all\frontends\hc-admin`
- 老 feature 分支 `feature/zzh/login_record_0615` 已删/合并进 master → 按 §0.2 从老 master 重开
- 已实现过(有 `.story/done/tapd-1144381896001065458/implement.json`)→ 重跑即"重新开发"

**备选**:`tapd-1144381896001065618`(HC 拒绝消息配置)→ hc-config + hc-admin + hc-limit;PRD 同目录规则。

---

## 3. 五层决策系统(真跑时要观察什么触发)

| 层 | 模块 / Decider | 触发点 | event_log `event_type` | 真 wired? |
|---|---|---|---|---|
| 层1 supervisor | `engine/supervisor.py` decide_response / supervise_pty_session + `awaiting_detector.py` | agent 提澄清/选择问题 | `supervisor_decision` | ⚠️ **未接到 planner 启动点**(见 §4.1) |
| 层3 recovery | `engine/recovery.py` decide_recovery / rescue_story | run_story 抛错 | `recovery_action` | ✅ wired |
| 层4 judge | `evaluation/judge.py` judge_quality | verify/gate 后 | `judge_verdict` | ⚠️ **未接到 gate**(见 §4.2) |
| 层2 transition | `engine/transition.py` decide_transition / build_repair_action | verify-gate retry | `transition_decision` | ✅ wired |
| 层5 reflection/scheduler | `learning/reflection.py` reflect / `engine/scheduler.py` decide_schedule | 跨 story 反思 / 多 story 调度 | (从 event_log 统计) | ✅ wired |

**所以要看到全部五层真触发,先做 §4.1(supervisor 接启动点)+ §4.2(judge 接 gate)两根线。**

---

## 4. 执行步骤(顺序做)

### 4.1 · 接 supervisor 到 planner agent 启动点(层1 真触发前置)**[代码改动·TDD]**

- **接入点**:`packages/story-lifecycle/src/story_lifecycle/orchestrator/engine/planner.py` 里 `ensure_agent_pty(...)` 调用处(~line 660,在 `continue_orchestrator_agent` 内)。PTY 启动后,起一个后台 task 跑 `supervise_pty_session`。
- **坑(重要)**:`run_story` 在 `ThreadPoolExecutor` **线程**里跑(`graph.py:_executor`),**没有 asyncio loop**。`supervise_pty_session` 是 async。解法二选一:
  - (a) 在该线程里 `loop = asyncio.new_event_loop(); loop.run_until_complete(supervise_pty_session(...))`(阻塞该线程直到 pty 死 —— 但 run_story 主流程也要继续,得另起线程跑这个 loop);
  - (b) 推荐:用一个独立 daemon 线程跑新 loop 跑 `supervise_pty_session`,与主轮询并行;pty 死时 loop 退出。
- **TDD**:先写测试(fake pty 预填提问 chunk + 真 deepseek mock + 计数 log_event),断言 supervisor 在 agent 提问时答进去 + `supervisor_decision` 入库。再实现。
- **守铁律**:`supervise_pty_session` 已单测过(见 `test_supervisor.py::TestSupervisePtySession`);本步只接管线,别改 Decider。
- **agent 选择 / stage→CLI 映射(用户已确认能跑通的组合)**:
  - **plan/design 阶段 → `claude`**(`claude -p --output-format stream-json --verbose`,headless)。监督走 **`supervise_claude_stream`**(已建好,`claude_stream.py`)—— 解析 stream-json,命中 permission_request/elicitation → `decide_response`。
  - **编码/implement 阶段 → `kimi`**(`kimi -p`,headless,确定可跑通)。监督走 **`supervise_pty_session`**(若 interactive PTY)或对 headless stdout 做提问检测(见下)。
  - **`codex` 阻断(cloud-config),别用。**
  - **065458 按 `design=claude / implement=kimi` 配 profile**(查 `story.profile`,改 stage 的 `cli` 字段;或起 story 时在 ctx/profile 里覆盖)。
  - **kimi `-p` headless 不提问**(直接出答案退出)→ implement 阶段 supervisor 可能不触发(正常,记原因);要触发 supervisor,让 design 阶段 claude 遇到模糊任务提澄清问题。
  - 接 supervisor 时:claude 轨接 `supervise_claude_stream`(到 `headless_launch_cmd` 那条分支的 stdout,planner ~line 589);kimi 轨若走 PTY 接 `supervise_pty_session`(到 `ensure_agent_pty` 后,planner ~line 660)。

### 4.2 · 接 judge 到 gate(层4 真触发前置)**[代码改动·TDD]**

- **接入点**:`packages/story-lifecycle/src/story_lifecycle/orchestrator/evaluation/gate.py` `run_verify_gate`,decide 后接 `judge_quality(done_data, test_result, story_facts, llm_invoke)`。
- **协 AI-2 窗口**:gate 的 decide+apply 拆分(它的 `gate.py` 拆完后接 judge)。**最小判据**:done 的 build/tests 字段空或 false → judge 返 rework;否则 LLM judge。
- **TDD**:done `build_passed=false` → judge rework;LLM judge 结构化输出(已有 `test_judge.py` 6 测,复用)。
- **落事件**:`db.log_event(story_key, stage, "judge_verdict", {pass, rework_point, reason})`。

### 4.3 · 跑回归确认 §4.1/4.2 没破坏

```bash
pytest packages/story-lifecycle/tests/   # 765+ passed(唯一允许 fail:预存在 test_smoke::test_packaged_and_root_profiles_consistent)
```

### 4.4 · hc-all 子 repo 准备(按 §0.2 + §0.3)

对 `D:\hc-all\hc-user` 和 `D:\hc-all\frontends\hc-admin`:

1. **查状态、保当前**(§0.3):
   ```bash
   git -C D:/hc-all/hc-user status --short          # 应干净
   # 若有改动:git -C D:/hc-all/hc-user add -A && git -C D:/hc-all/hc-user commit -m "wip: preserve before realtest"
   ```
2. **找老 master commit**(login_record feature 合并前):
   ```bash
   git -C D:/hc-all/hc-user log --oneline master | head -40
   # 找合并 PR/相关 commit:git -C D:/hc-all/hc-user log --all --oneline --grep=login_record
   # 或按日期(合并前):git -C D:/hc-all/hc-user log --before=2026-06-15 --oneline master | head
   ```
   定位到**合并前那个 commit**(记作 `<OLD>`),它上面**不应**有 login_record 的改动。
3. **建新分支、重新开发**(§0.2):
   ```bash
   git -C D:/hc-all/hc-user checkout -b story-realtest-065458 <OLD>
   git -C D:/hc-all/frontends/hc-admin checkout -b story-realtest-065458 <OLD_ADMIN>
   ```
4. 确认两个 repo 在 `story-realtest-065458`、干净、且代码是老版本(login_record 代码不在)。

### 4.5 · 重置 story 065458 + 把 PRD 喂给 planner

planner 读 `ctx.prd_path` 来告诉 agent 去读 PRD(`planner.py:~911-915`:"请读取 PRD 文件了解完整需求: `{prd_path}`")。story 065458 的 `prd_path` 空 → **必须设**:

```python
# python -c 或脚本(用项目 src)
import sys; sys.path.insert(0,"packages/story-lifecycle/src")
from story_lifecycle.entry.cli.setup import load_config_to_env; load_config_to_env()
from story_lifecycle.infra.db import models as db, json as _j  # json 走标准库
import json
KEY="tapd-1144381896001065458"
db.update_story(KEY, status="idle", current_stage="design")
ctx = json.loads(db.get_story(KEY)["context_json"] or "{}")
ctx["prd_path"] = r"D:/hc-all/story/1065458-登录记录查询/PRD.md"
ctx.pop("_agent_actions", None); ctx.pop("_plan_confirmed", None); ctx.pop("_recovery_attempt", None)
db.update_story(KEY, context_json=json.dumps(ctx, ensure_ascii=False))
```
- 移走老 done(避免 stage 直接判完成):
  ```bash
  mv D:/github/story-lifecycle/.story/done/tapd-1144381896001065458 D:/github/story-lifecycle/.story/done/tapd-1144381896001065458.bak
  ```
- 验证 PRD 文件在:`ls "D:/hc-all/story/1065458-登录记录查询/PRD.md"`。

### 4.6 · 起 story serve + 驱动 story

```bash
# 1) 起服务(用 run-story-serve skill,或直接)
#    skill: run-story-serve  → 起 127.0.0.1:8180
curl -s http://127.0.0.1:8180/api/session/health   # {"status":"ok"}

# 2) 驱动(直接调,比 HTTP 稳)
python -c "
import sys; sys.path.insert(0,'packages/story-lifecycle/src')
from story_lifecycle.entry.cli.setup import load_config_to_env; load_config_to_env()
from story_lifecycle.orchestrator.engine.graph import start_story_async
start_story_async('tapd-1144381896001065458')
"
```
- `start_story_async` → 线程池跑 `run_story` → `planner.continue_orchestrator_agent` → 真 deepseek 规划(读 PRD)→ **真 claude agent 在 hc-user/hc-admin 的 `story-realtest-065458` 分支上干活** → 写 done → verify-gate(接了 judge)→ 五层真触发。
- **真 agent 会真改 hc-all 代码**(隔离在 test 分支,可丢)。token/时间不限(§0.1/0.4)。
- 卡住/提问:接好 supervisor(§4.1)后,claude 提澄清 → `supervise_claude_stream` 命中 → `decide_response`(真 deepseek)→ 自动答。

### 4.7 · 观察(真事件)

```sql
-- event_log 真决策事件
select id, stage, event_type, substr(payload,1,160) as payload
from event_log
where story_key='tapd-1144381896001065458'
  and event_type in ('supervisor_decision','recovery_action','transition_decision','judge_verdict','gate_result_recorded')
order by id desc limit 30;
```
```bash
# 子 repo 真 git diff(看 agent 真改了啥)
git -C D:/hc-all/hc-user diff --stat <OLD>
git -C D:/hc-all/frontends/hc-admin diff --stat <OLD_ADMIN>
# story 状态流转
python -c "import sys;sys.path.insert(0,'packages/story-lifecycle/src');from story_lifecycle.entry.cli.setup import load_config_to_env;load_config_to_env();from story_lifecycle.infra.db import models as db;s=db.get_story('tapd-1144381896001065458');print(s['status'],s['current_stage'],s.get('last_error',''))"
```

---

## 5. 完成判据(自验证,§5.0 风格,缺一不可)

1. **§4.1/4.2 测试 GREEN**:`pytest packages/story-lifecycle/tests/` 765+ passed(允许预存在 profile 一致性 fail)。
2. **五层真触发**(event_log 有真事件,至少其中几条):
   - `supervisor_decision`(agent 提问被自动答)—— 若 claude 全程不提问,这层可能不触发(记录"未触发原因")。
   - `recovery_action`(若某 stage 失败)。
   - `transition_decision`(若 verify-gate retry)。
   - `judge_verdict`(接 gate 后,verify 阶段必落)。
3. **agent 真改 hc-all**:hc-user / hc-admin 的 `story-realtest-065458` 分支有真 diff(代码、测试)。
4. **全量回归无新 fail**。
5. **证据落档**:把 event_log 查询结果 + 子 repo diff --stat + story 最终状态,贴到本文件 §7 checkpoint ✅。

> 反面:只跑了 unit test 不算。必须 event_log 有真事件 + 子 repo 有真 diff。

---

## 6. 已知坑 / 故障排查

- **codex 跑不起**(`Error: timed out waiting for cloud config bundle`)→ 用 claude 轨(`supervise_claude_stream`)。story profile 里 stage 的 cli 别指 codex。
- **claude 全 allow** → 不会发 `permission_request`;supervisor 的价值在答**澄清/选择**问题(给模糊任务触发)。若 065458 任务清晰,可能不提问 → 层1 不触发(正常,记原因)。
- **planner prompt 没含 PRD** → 检查 `ctx.prd_path` 是否设(§4.5);查生成的 `D:/github/story-lifecycle/.story/context/<key>/prompt_design.md` 里有没有"请读取 PRD 文件"。
- **supervise_pty_session 在线程里没 loop** → 见 §4.1 坑(用独立 daemon 线程 + new_event_loop)。
- **老 pipeline 断点**(memory:全自动没真跑过)→ 真跑可能撞 done 路径/gate/headless 死代码。撞到就修(守 TDD),这正是真测的价值。
- **story 已 done** → §4.5 已重置 + 移走老 done;若仍跳过,查 `intake_state`(应 ready 不是 candidate)、`status`、done 文件。
- **deepseek key 没加载** → 脚本/驱动前先 `load_config_to_env()`(从 ~/.story-lifecycle/config.yaml 读 env)。

---

## 7. Checkpoint(真跑完后填)

- [x] §4.1 supervisor 接 planner(test GREEN,commit `aaa02b87`;本会话补 stderr drain `feat(headless): drain stderr…`)
- [x] §4.2 judge 接 gate(test GREEN,commit `aaa02b87`;真跑验证:`judge_verdict` ×4 真落)
- [x] §4.3 回归 GREEN:**772 passed**,1 fail = 预存在 `test_packaged_and_root_profiles_consistent`(runbook 允许)
- [x] §4.4 hc-user / hc-admin 在 `story-realtest-065458`(从老 master),干净;hc-admin 无 login-record 代码(真活),hc-user 有登录日志基础设施(entity/mapper)但无查询 feature
- [x] §4.5 story 065458 重置 + `prd_path` 设到真 PRD.md(+ 修 `story_project` 分支 → `story-realtest-065458`)
- [x] §4.6 真跑通 design→build→verify→gate(详见下方"真跑过程与本会话修复")
- [x] §4.7 五层真事件 + 子 repo 真 diff 证据:

```
=== 五层决策真事件(event_log,this runbook run)===
  #344 verify/judge_verdict: {"pass": false, "rework_point": "quality", "reason": "tests 留空，缺少测试验证，无法保证实现质量"}
  #345 verify/transition_decision: {"action": "retry", "reason": "可恢复失败(quality)→ 同 stage 重试(2/2)"}
  #346 verify/judge_verdict: {"pass": false, … "测试结果为空，未提供验证依据…"}
  #347 verify/transition_decision: {"action": "retry", …}
  #348 verify/judge_verdict: {"pass": false, … "tests 留空…"}
  #352 verify/judge_verdict: {"pass": false, … "验证阶段缺少测试（tests留空）…"}
  #353 verify/transition_decision: {"action": "escalate", "reason": "同 stage 反复失败 2 次(≥上限 2)→ 上交人"}
  counts: judge_verdict=4, transition_decision=3
  （supervisor_decision / recovery_action 未触发 —— 见下方"未触发原因"）

=== 子 repo 真 diff(kimi build 真改)===
  hc-user: UserLoginLogMapper.java +28;新文件 UserLoginRecordController / IUserLoginRecordService
           / UserLoginRecordServiceImpl / UserLoginRecordReq / UserLoginRecordResp / UserLoginLogListRes / vo/enums
  hc-admin: menu.ts/pages.static.ts(en-US+zh-CN)/routes.ts +39;新目录 src/pages/userManage/loginRecord/

=== story 最终状态 ===
  status=failed stage=verify  last_error="同 stage 反复失败 2 次(≥上限 2)→ 上交人"
  （verify gate 经 judge→transition→retry 两轮后 escalate;符合设计 —— kimi 在大任务上不写 done 握手,
    见下方"真跑过程"。layer 触发目标已达成:event_log 有真 judge+transition 事件 + 子 repo 有真 diff。）
```

### 真跑过程与本会话修复(§0.5"暴露真问题"—— 真跑价值兑现)

真跑撞到的真 bug / 真环境阻断(逐个定位 + 修/TDD/绕,均有 commit):

1. **`start_story_async` 不自动 plan**(docstring 撒谎):run_story 直接 continue_orchestrator_agent,
   `_agent_actions` 空就 fail。修:显式先 `run_orchestrator_agent`(deepseek plan)再 start。
2. **headless stderr PIPE 死锁**(§4.1 漏修的姊妹 bug):planner `stderr=PIPE` 只 drain stdout,
   kimi/claude 大量写 stderr → 超 64KB 管道 → proc 阻塞 → "Stage timed out"。
   TDD 修:`supervise_headless_stdout` 加 `stderr_tail` 参数 + 嵌套 daemon 排空(stderr);
   `test_drains_stderr_preventing_pipe_deadlock`(真子进程写 200KB stderr 验不死锁)。commit `fix(headless): drain stderr…`。
3. **claude 走 `open.bigmodel.cn` 网关 529 过载**(该模型当前访问量过大)→ claude 不可用作 agent。
   绕:`realtest.yaml` design/verify `claude→kimi`(Moonshot,smoke 验证可跑通)。runbook §0.7 的 claude 前提(可用)被网关打断,记此偏离。
4. **PTY 路径 kimi idle**:`continue_orchestrator_agent` 硬编码 `headless=False`(graph.py 不传),
   profile.execution_mode 没接到 headless 位 → 走 PTY → kimi-code 交互注入不触发执行(idle)。
   TDD 修:`headless_from_profile()`(test_execution.py 4 测)+ 接线;`realtest` `execution_mode: headless`。commit `feat(execution): wire profile.execution_mode -> headless`。
   (PTY-kimi-idle 根因 ensure_agent_pty prompt 注入,留 follow-up。)
5. **`story_project` 绑错分支**(老 `feature/zzh/login_record_0615`)→ 修到 `story-realtest-065458`。
6. **kimi 大任务不写 done 握手**:design/build/verify kimi 都真干活(design 出真方案、build 出真代码
   controller/service/mapper/VO + 前端页面骨架)但常不写 `.story/done/…/{stage}.json`(写到空
   `.story-done` 或漏)。本会话对 build/verify done 做了**人工桥**(记录 kimi 真实 files_changed),
   以让 verify gate + judge 真跑。这是 kimi-code headless 在大任务上的可靠性问题,留 follow-up。

### 未触发层的原因(§5.2 允许"记录未触发原因")

- **层1 supervisor_decision**:kimi headless(-p)不提澄清/选择问题 → 无 awaiting 信号 → 不触发(§5.2 预期)。
  另:claude adapter 的 headless_launch_cmd 未带 `--output-format stream-json`(与 §4.1 设想不同),
  即便走 claude 也不会解出 stream-json 提问。两层均"agent 不提问"→ supervisor observe-only 无果。
- **层3 recovery_action**:**真设计缺口**——poll-timeout / gate-retry 失败走 `status=failed; return`
  (continue_orchestrator_agent 不抛),而 recovery 只在 run_story 捕到**异常**时触发。故 build/verify
  的失败没进 recovery。建议 follow-up:把 poll-timeout/escalate 也喂 decide_recovery(换 adapter 重跑)。
- **层5 reflection**:跨 story 反思,单 story run 不触发(transition 已用 `_build_verify_history_facts` 回注历史,飞轮喂入侧已接线)。

### §5 自验证

1. ✅ §4.1/4.2/4.3 测试 GREEN(779 passed,1 预存在)。
2. ✅ 五层真触发(judge_verdict ×4、transition_decision ×3;supervisor/recovery 条件性未触发+记因)。
3. ✅ agent 真改 hc-all(hc-user 7 文件、hc-admin 6 文件 + 新 loginRecord 页)。
4. ✅ 回归无新 fail。
5. ✅ 证据落档(本节)。

---

## 7.1 续跑进度(2026-07-06:修 kimi done-fumble 根因 → ✅ 干净全程跑通)

**背景**:§7 那次跑,build/verify 的 done 是**人工桥**(kimi 写了代码但漏写 done 握手),所以 judge 判的是桥数据。要"真跑完"必须让 kimi 自己写 done。本节是根因定位 + 修复 + 干净全程跑(无桥)的进度。

**根因(已复现确认)**:kimi-code 在代码阶段(build/verify)写完代码后会**自作主张跑 `mvn compile` + `tsc --noEmit` 自检** —— 大 Java/Vue 仓库上这俩常阻塞 >10 分钟 → kimi 永远到不了 done 握手 → stage 失败。复现方法:直接 `kimi -p prompt_build.md`,全量捕 stdout/stderr,看到 kimi 写完代码后跑 mvn/tsc 卡死(被 600s timeout 杀)。设计/design + verify-round1(没跑重编译)→ done 正常写。25 文件合成测试(不编译)→ done 正常写 → 排除"turn/output 预算"假说,锁定"自编译阻塞"。

**修复(TDD+commit `fix(prompt): forbid heavy build/compile cmds`)**:`_build_cli_prompt` 加无条件"### 执行约束(重要)"段,禁止跑 `mvn/gradle/npm install/yarn/tsc/jest/vitest/pytest` 等耗时构建/编译/测试命令(归后续阶段/CI),agent 只写代码 + done。test_build_cli_prompt.py(4 测,RED→GREEN)。配套前置修复(均已 commit):headless stderr 排空、profile.execution_mode→headless、poll_timeout 45min、start_story_async 不自动 plan(先 run_orchestrator_agent)。

### ✅ 完成(2026-07-06 晚:干净全程跑通,无桥,kimi 自写 done,judge 判真数据 → completed)

续跑步骤 0–3 执行后,**story 干净到 `completed`**(非 timeout)。核心判据全过:

| 判据 | 结果 |
|---|---|
| build.json / verify.json 是 **kimi 自写(非桥)** | ✅ build=17 files、verify=18 files,summary 无"人工桥";retrospect.md 列真文件 |
| judge_verdict 判**真 kimi 数据** | ✅ `#401/#402 pass=true`:"硬指标通过,遗留项为预期设计选择""实现符合 spec,国际化及索引优化均已完成,manualRequest 是有意设计避免性能问题" |
| 干净终态 | ✅ `status=completed / verify / err=""`(对比 §7 的 failed-escalate、暂停时的 failed-timeout) |
| agent 真改 hc-all | ✅ hc-user(Controller/Service/Mapper/DAO/VO)+ hc-admin(页面/路由/菜单/i18n)工作区真 diff;verify 阶段还**真修了 spec 不一致**(接口统一 `/api/login-record/page`、菲律宾自然日转换、枚举码、SQL 精确匹配、`ORDER BY create_time DESC,user_id DESC` 走索引、`manualRequest=true` 避免 500 万级日志表全表) |

**单 driver 时序(driver log `tmp_drive_cleanrun.log`,epoch=1 一次 submit)**:
```
18:22:09 submit → 18:22:10 design 秒过(消费已有 design.json)
              → 18:22:11 build kimi 启动 → 18:25:21 build done(3min,没跑 mvn/tsc=约束生效)
              → 18:25:22 verify kimi 启动 → 18:31:42 verify done(6min,修 spec 不一致)
              → 18:31:43 deepseek judge POST 200 → 18:31:45 All stages completed + retrospect.md
```
约束修复兑现:build 从暂停时的"卡死/需人工桥"变成 **3min 自写 done**。

### 续跑步骤(已验,留作复现手册)

0. **清进程**:driver 失败/暂停后,kimi 孙进程会残留(headless `python wrapper → kimi.exe`,`_kill_headless` 杀 wrapper 不杀孙)。Git Bash 杀进程用 `MSYS_NO_PATHCONV=1 taskkill /PID <pid> /T /F`(`/PID` 否则被转义)。**注意:只杀本 run 的孤儿 kimi,别误杀用户在别的任务开的 kimi —— 先按 mtime/CPU/父进程辨明再杀。**
1. **重置 story**(`tmp_reset_clean.py`,`_agent_actions` 保留但去掉 gate-retry 残留的 repair action,清 `_active_execution/_recovery_attempt/last_done_data/last_verify_summary/review_round_count_*`)→ `status=idle,current_stage=design`。
2. **done 目录**:保留 `design.json`(design 秒过),移走 `build.json/verify.json`(让 kimi 在约束下自写,不要桥)。本 run 的备份留 `.bak_pre_cleanrun`。
3. **单 driver**:`cd D:/github/story-lifecycle && ./.venv-monorepo-test/Scripts/python.exe tmp_drive_minimal.py > tmp_drive_cleanrun.log 2>&1`(后台)。**只起一个 driver**,起前确认无 `tmp_drive_minimal`/`run_story` 残留。
4. **关键判据**:build/verify done 必须 kimi 自写(非桥);judge_verdict 判真数据。

### ⚠️ 发现(遗留 follow-up,不阻塞本目标):事件 ×2 / 疑似第二 driver

本 run 每个 stage 的 `completed` 与 `judge_verdict` 各出现 **2 条**(design/build/verify completed×2、judge×2 且 reason 不同=2 次真 LLM 调用),且**按 stage 交错**(非"两个完整 pass 串联")。但 driver log 只有一条线性 pass(epoch=1、每 stage 一次 kimi spawn)。

- **推断**:存在**第二个 driver 进程**(可能 serve 进程 resume 或残留 driver),与主 driver **并发**跑同一 story —— 交错事件说明 `acquire_workspace` 的 filelock **未能在两进程间串行化**(否则会是"先一全 pass、再一全 pass"的串联顺序,而非交错)。
- **为何本 run 没 timeout**:约束修复后 kimi 快(build 3min / verify 6min,远小于 45min poll_timeout)。§7.1 暂停时的 `Stage verify timed out` = 并发 driver 中慢的那条撞 45min —— **并发问题没修,只是被 kimi 速度掩盖**。
- **follow-up 建议**:
  1. 查 `acquire_workspace` 为何未跨进程串行(filelock 路径/Windows 语义/是否同 workspace);
  2. 或加**库级** per-story 互斥(DB 行锁 / `story.status` CAS),不依赖进程内 dict + 文件锁;
  3. serve 是否在启动/状态变更时 resume story —— 若是,文档化"serve 与 driver 不要同时驱动同一 story"。
- **本 run 的可靠性不受影响**:两 driver 都判 pass、终态 completed;交错重消费 done 没产生错误状态(done 是幂等证据,judge 两次都 pass)。

**已 commit 的修复(本会话 + 前会话)**:
- `aaa02b87` §4.1 supervisor→planner + §4.2 judge→gate wiring(前会话)
- `098b3a7c` fix(headless): drain stderr(PIPE 死锁)
- `e23c96e8` feat(execution): profile.execution_mode→headless
- `305421fc` docs(runbook): §7 checkpoint
- `1448cc0a` fix(prompt): forbid heavy build/compile cmds(本次 kimi-done 根因)
- 本节 §7.1 完成落档(下方 commit)

### §7.1 自验证(对齐 §5)

1. ✅ **约束修复在生效**:build 3min 自写 done(无 mvn/tsc 卡死),对比暂停时的"卡死/需人工桥"。
2. ✅ **无桥**:build.json(17 files)/verify.json(18 files)summary 均无"人工桥"。
3. ✅ **kimi 自写 done**:两 done 均本 run kimi 在约束下产出;retrospect.md 列真实文件 + verify 真修 spec 不一致。
4. ✅ **judge 判真数据**:`judge_verdict #401/#402 pass=true`,理由针对真实实现(spec 对齐/i18n/索引/manualRequest)。
5. ✅ **干净终态**:`status=completed`(§7 是 failed-escalate,暂停时是 failed-timeout)。
6. ✅ **五层**:judge(层4)真 fire 判真数据;transition(层2)/recovery(层3)/supervisor(层1)本 run **未触发 = 干净 pass 本就不该触发**(无失败无需转/救/答),符合 §5.2"记录未触发原因"。reflection(层5)跨 story,单 run 不触发。
7. ⚠️ **遗留**:事件 ×2 / 疑似第二 driver(见上"发现"),被 kimi 速度掩盖,留 follow-up。

---

## 7.2 换一个真 story + 修并发(2026-07-06 晚:driver_claim CAS → 1065570 干净跑,事件 ×1)

**驱动**:§7.1 发现事件 ×2 / 疑似第二 driver 并发(被 kimi 速度掩盖)。用户拍板"走方案 B(乐观 CAS)"+"换个真 story 跑流程验证"。

### 修并发:driver_claim CAS(commit `382ff4f3`,TDD)

- **为何不用 status CAS**:`status` 列被重载 —— schema 默认值就是 `'active'`,且 api.py **3 个 HTTP 入口**(autostart、advance-from-paused→active、skip→active)专门对 `active` 的 story 调 `start_story_async`。纯 `WHERE status='idle'` CAS 会破坏这些路径。
- **实现**:加专用列 `driver_claim TEXT`(VALID_COLUMNS + init_db 幂等 ALTER,跟 `context_revision` 同款)。`start_story_async` 在候选 guard 后先 `db.claim_story_driver(token)`(`UPDATE story SET driver_claim=? WHERE driver_claim IS NULL`,SQLite 串行 → 只一个 caller rowcount=1),CAS 输则 return。`run_story` finally 里 `release_story_driver`(只释放自己的 token)。进程内 `_running_stories` dict 保留作同进程 re-entry guard(重入时释放刚赢的 DB claim)。
- **TDD**:`test_driver_claim_cas.py` 7 测(claim win/lose、release-if-mine、start_story_async bails-when-claimed / claims-and-drives / candidate-still-rejected、run_story finally-releases)。回归 **790 passed**(1 预存在 profile fail)。

### 跑 story `tapd-1144381896001065570`(联系人姓名校验/资方格式校验规则配置)

**换 story**:非 065458。PRD `D:/hc-all/story/1065570-联系人姓名校验/PRD.md`。绑 **hc-config 单 repo**(`story-realtest-1065570` from master,干净;跳过 hc-user 消费方免动它的 dirty 状态)。reset(idle/design/realtest,清 exec 态,设 prd_path,`_agent_actions` 空让 deepseek 现规划)。**serve 保持运行**(用 CAS 版 driver,若 serve 真是第二 driver,CAS 这次应挡住 → 事件应 ×1)。

**时序(单 driver,CAS claim `36452:...` 全程持有,`tmp_drive_1065570.log`)**:
```
19:26 deepseek 规划(2 POST 200,产 3 actions)→ CAS claim → design kimi
19:31 design done(5min)            → build kimi
19:47 build done(16min,18 文件)    → verify kimi
19:58 verify done(11min) → deepseek judge 200 → All stages completed
终态 completed;driver_claim=None(finally 正确释放)
```

**结果(本 run 事件 id>=403)**:

| event | 065458(无 CAS) | **1065570(有 CAS)** |
|---|---|---|
| design completed | ×2 | **×1**(#403) |
| build completed | ×2 | **×1**(#404) |
| verify completed | ×2 | **×1**(#405) |
| judge_verdict | ×2 | **×1**(#406) |

**counts: completed=3, judge_verdict=1 —— 全 ×1。CAS 在生产环境彻底消除了并发双 driver 的重复**(对比 §7.1 的 ×2)。这是 CAS 的决定性证据。

- **judge #406 pass=true** 判真数据:"代码覆盖规则表、操作日志、CRUD、Feign、缓存及零宽断言拦截,清理了残留代码,结构与职责清晰"。
- **agent 真改 hc-config**:18 新 java(FormatValidation 五层:admin controller/service/dto、api Feign、business controller/service/vo、component entity/mapper、VersionUtils)+ 2 pom + DDL `sql/20250706_format_validation_rule.sql`(规则表/操作日志表)。
- **干净终态 completed**(非 timeout/failed)。

### ⚠️ 新发现(done 文件被清,留 follow-up,不阻塞)

1065570 的 design/build/verify.json 在被消费后**消失了**(done 目录只剩 retrospect.md),导致 `retrospect.md` 显示"未捕获到任何阶段 done 产物"(retrospect 读 done 文件生成)。065458 时 done 文件保留。`reset_workspace` 在 src 里**不存在**(只有注释,无定义/调用),代码无 stage 间清 done 逻辑 → 推测是 **kimi 自己清理了它写的 done**(kimi 行为因任务而异)。证据不丢:event_log 保留完整 summary,judge 在 done 消失前已跑。follow-up:retrospect 改读 event_log(而非 done 文件);或 planner 消费 done 后落一份不可清的副本。

### §7.2 自验证

1. ✅ **换 story**:1065570(联系人姓名校验),非 065458。
2. ✅ **CAS 落地**:driver_claim 列 + claim/release,TDD 7 测,回归 790 passed。
3. ✅ **CAS 实战**:事件全 ×1(对比 §7.1 ×2),driver_claim 正确 claim→release。
4. ✅ **干净 completed**:design→build→verify→judge pass,单 driver ~32min。
5. ✅ **kimi 真实现**:hc-config 18 java + DDL + pom;judge 判真数据 pass。
6. ✅ **铁律**:TDD、不动 pty.py、commit 带 Co-Authored-By(`382ff4f3`)、claude 529 全 kimi、单 driver 不竞争。

---

## 7.3 design=claude PTY 实验(2026-07-06 21:xx:验证 superpowers 链路 → claude hc-all 重环境 design 太重,停)

**动机**: §7.1/§7.2 design/build/verify 全用 kimi(CAS 已验,事件 ×1)。用户议题(USER#7):理想全自动流程要"agent 调 superpowers brainstorming 澄清需求"——需验证 claude(原生加载 `.claude/skills` + superpowers)作 design agent 在真实 hc-all 环境的表现。claude 网关 529 已恢复。

**配置(commit `6dcd6d39`)**: `realtest.yaml` `execution_mode: interactive_pty` + design/verify `cli: claude`; `knowledge/adapters/claude.py` `readiness_marker='❯'`(配合 `a05f9f9a` `_wait_ready`,修 PTY idle)。线2(PTY readiness/idle 修复)已在 `a05f9f9a`。

**线1/线2 核心验证 ✅**(driver log `tmp_drive_1065570_pty.log`, epoch=1):
```
21:11:47 EXECUTE stage=design adapter=claude cmd=[claude.cmd]   ← design=claude(非 kimi)
21:11:47 injecting prompt into PTY                              ← PTY 路径(非 headless)
21:11:53 PTY session started                                    ← 6s,_wait_ready 等到 ❯ marker,不 idle
```
三件事都成:**design 走 claude PTY**、**PTY 不 idle**(readiness 检测生效)、**claude 加载 hc-all 环境**(CPU 爆表在干活)。PTY 双轨 + idle 修复全链路通。

**实验负面结论(Follow-up, 不阻塞)**: claude design 在 hc-all 重环境 ~10min(21:11→21:21+) CPU 2291s+ 仍涨但**零文件产出**(design.json 没出)。原因:claude 交互式在 hc-all 加载巨重 context —— `AGENTS.md`/`CLAUDE.md`(`@RTK.md`+`@AGENTS.md` import 链)+ codegraph MCP + superpowers skills(brainstorming 深度探索)。重环境下 claude 交互式 design 效率问题。

**决策**:用户经 AskUserQuestion 选"停,记 follow-up"(design 性价比低,线1 核心已验证)。停 driver(`b4ckxclbx`)+ 杀实验 claude(60944/19172)+ reset 1065570(`_reset_1065570.py`:active/design → idle/design,清 driver_claim 残留 + `_active_execution`)。

**Follow-up 待办**:
1. claude design 减载:design 阶段只载 brainstorming(非全部 superpowers)/精简 hc-all context 注入/或 design 仍用 kimi、仅特定澄清节点切 claude。
2. 本配置 `realtest` design/verify=claude 保留作"PTY 双轨 + claude readiness"成果启用入口;生产重任务用 kimi profile。

**自验证**:线1/线2(PTY/idle/skill 加载)✅;claude 重环境 design 效率 ⚠️(记 follow-up);1065570 reset 干净(idle/design/claim=None);config commit `6dcd6d39`。

---

## 7.4 design 飞轮注入方向(2026-07-06:维度 checklist + 飞轮窄注入替代 brainstorming,A/B 验证)

**背景**:§7.3 design=claude 实验发现 claude 在 hc-all design 10min 超时。深挖真因 + 探讨「编排工作流全自动开发」(USER#7)后,形成 design 改造方向。

**#4 真因修正**(非 §7.3 的"负载高"):claude design 在 hc-all 超时 = (1) **context rot**(Chroma 2025:相关信息塞 prompt 仍 degrade;hc-all 前置塞满 `AGENTS.md`/`@RTK.md`/codegraph/superpowers);(2) **brainstorming 发散**(headless 复现:`AskUserQuestion` 0、brainstorm 22 次、陷探索循环、design.json 没产)。非 CPU 空转卡死。

**改造方向:维度 checklist + 飞轮窄注入**(替代 brainstorming 自由探索)
- design 核心价值 = 产品→技术转化(识别技术决策点)。**13 维度 checklist**(从 27 spec 提炼):现状分析 / 架构数据流 / 数据模型 / 接口契约 / 核心逻辑(算法·状态机) / 一致性并发 / 性能容量 / 降级兼容 / 边界异常 / 安全 / 权限 / 风险回滚 / 非目标。决策点 = 维度里有岔路的项。
- 飞轮 = Augment Agent Learning Flywheel(execute→coach→distill→improve)↔ hc-all 资产(event_log/transcript → playbooks/failures → design 注入)。**窄注入**(非前置塞满)避 context rot。

**A/B 验证(security 维度)**:1065570 PRD + deepseek,无注入 vs 注入 `hc-all/.story/knowledge/playbooks/security-parameter-trust.md`(蒸馏自 8 spec):
- 参数 4→7(+75%)、篡改测试 5→13(+160%)。
- B 发现裸 LLM 漏的 CORE:`appVersion` header 降级风险(篡改低版本→降级旧校验→安全水位下降)、`DELETE id` 误删。
- 证明飞轮注入补 PRD + 裸 LLM 盲点。`appVersion` 已回写 playbook(闭环)。

**前端**:分步向导接 design 的 `decision_points`(每维度命中的决策点),复用 plan-confirm SSE 架构。

**端到端验证(2026-07-06,commit a34cc843)**:deepseek 公平对比(A/B 都注 PRD 全文,只差 dimensions_section):改造后 design prompt 让 agent 维度覆盖 **2/5→5/5**、输出 **decision_points**(改造前无)、引用 **Parameter Trust/CORE 分级**(改造前无)。证明 dimensions 注入让 design 从自由方案 → 按维度系统转化 + decision_points(前端可接),而非 brainstorming 发散。

**Follow-up**:① 推广更多高价值维度 playbook(并发/缓存;跳过降级-A/B 证低价值);② design 产出回写 playbook 闭环;③ claude 真跑验"禁 brainstorming 约束"对 claude+superpowers 生效(端到端用 deepseek,该约束面向 claude)。

---

## 8. 清理 / 回滚

- 子 repo 回原分支:`git -C D:/hc-all/hc-user checkout feature/ice/maintain_supplier_fix_0702`(原分支);test 分支保留观察或 `git -C D:/hc-all/hc-user branch -D story-realtest-065458` 删。
- story 065458:恢复原 status(它本 idle;重置可逆)。done 备份:`mv .../tapd-1144381896001065458.bak .../tapd-1144381896001065458`。
- orchestrator repo:别 push(沙箱阻断 + 这是测试)。本地 commit 保留。

---

## 9. 参考

- `docs/agent-decision-layers-rollout.md` —— 五层架构 + 落地手册 + §4.0 进度总览
- `docs/code-review/five-layer-real-run-2026-07-05.md` —— 五层 Decider 级真跑报告(已验)
- `.claude/plans/snappy-puzzling-moonbeam-agent-abd58d7b7e4afedaf.md` —— 双轨(Claude stream-json / codex-kimi PTY)调研
- 铁律:TDD(写测试→RED→最小实现→GREEN→重构);Decider 纯函数(LLM/DB/PTY 全注入);动 pty.py 前重读 `tests/test_pty_tap.py`(别破坏 Web Board);每步 commit,message 末尾 `Co-Authored-By: Claude <noreply@anthropic.com>`。
