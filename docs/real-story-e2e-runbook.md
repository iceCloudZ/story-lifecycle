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
- **agent 选择**:codex 阻断 → story 跑 claude 轨(headless `claude -p`)走 `supervise_claude_stream`(已建好,`claude_stream.py`);PTY 轨(codex/kimi)走 `supervise_pty_session`。**065458 用 claude → 接 `supervise_claude_stream` 到 headless stdout**(planner 里 `headless_launch_cmd` 那条分支,~line 589)。

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

- [ ] §4.1 supervisor 接 planner(test GREEN,commit)
- [ ] §4.2 judge 接 gate(test GREEN,commit)
- [ ] §4.4 hc-user / hc-admin 在 `story-realtest-065458`(从老 master),干净,代码是老版本
- [ ] §4.5 story 065458 重置 + `prd_path` 设到真 PRD.md
- [ ] §4.6 真跑通(design → … → 至少 implement+verify)
- [ ] §4.7 五层真事件 + 子 repo 真 diff 证据贴下:

```
(贴 event_log 查询结果)
(贴 git diff --stat)
(贴 story 最终 status/stage)
```

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
