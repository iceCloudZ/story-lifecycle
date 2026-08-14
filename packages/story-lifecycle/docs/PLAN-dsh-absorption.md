# PLAN: dsh 吸收清单(不迁移,抄柜子)

> **背景**:PROPOSAL-dsh-plugin-integration 已落决策「不采用」(为本项目 4 目标)。
> 本文档是它的姊妹篇:**dsh 不住进去,但值得定期抄走好柜子**。每项标注状态,
> 做完打勾,新吸收点追加到表尾。
> **日期**:2026-08-14

## 吸收点总表

| # | dsh 里是什么 | 对到本项目哪 | 状态 |
|---|---|---|---|
| A1 | `guard/` 行进中 inline 信号(健康梯度) | supervisor 的持续 tap 只做 awaiting 检测,`detect_stuck` 是事后二值 | ✅ **本轮已做**(NOW-1) |
| A2 | seam 三分法(Definition/Provider/Consumer) | `BaseAdapter`/`StorySource` 已是雏形,但没成文 | ✅ **本轮已做**(NOW-2,AGENTS.md 立规) |
| A3 | 语义 token 纪律(`--dsw-alias-*`,禁字面色值) | frontend `.ui-*` + tokens | ✅ **已有覆盖,无需改**(frontend/AGENTS.md §1/§4 已是同款规矩) |
| A4 | 完整 turn 事件序全 waterfall(事件驱动感知) | `OrchestratorThread` 固定 5s 轮询 | ⬜ NEXT-1(见下,有设计草图) |
| A5 | `hooks/` = Claude Code / Codex 线协议共享库 | headless 优先(claude `-p` stream-json + 退出码)时的参考实现 | ⬜ NEXT-2(headless 线启动时去读) |
| A6 | durable replay(`session/event`)vs live status(`agent/*`)分离 | events.jsonl + `orchestrator_decision` 审计表 | ✅ 只读验证:与「无状态编排」(DESIGN §4.6)同一哲学,无需改代码 |

## NOW-1:健康信号(`agent_health` 事件)— 已完成 ✅

**吸收自**:dsh `guard/` 的"行进中信号"概念——healthy ↔ stuck 之间要有**梯度**,不是只有二值翻转。

**改动**(`orchestrator/engine/supervisor.py`):
- 新增纯累积器 `HealthTracker`(零 I/O):`observe(text, ts)` 累积(首/末输出时间、输出量、chunk 数、连续错误 chunk 数,错误启发式与 `detect_stuck` 规则 3 同源);`snapshot(now)` 出梯度快照;`emit_due(now, interval)` 节流。
- `supervise_pty_session`(唯一持续 tap 消费者)每轮循环喂 tracker,每 `health_interval`(默认 30s,参数可注入)落一条 `agent_health` 事件(payload 含 adapter + 快照)。**idle 也发**(超时路径同样检查)——空转阶段的 last_output_age_s 增长正是最有价值的梯度。
- 发射是 best-effort:log 失败 debug 吞掉,绝不炸监督循环。

**红线(未碰)**:观察 ≠ 判定。supervisor 仍然**不判完成/不判卡住**(DESIGN §3.2)——卡住判定归调度线程 tick 的 `detect_stuck`,完成判定归 declare/judge。`agent_health` 只是可观测性:前端可画活动曲线,未来的漂移检测可消费。

**测试**:`tests/test_supervisor.py` 追加 `TestHealthTracker`(4 个纯函数测试)+ `TestAgentHealthEmission`(wire 测试,`health_interval=0` 强制发射)。默认 30s 周期下既有断言不受影响。

## NOW-2:seam 三分法立规 — 已完成 ✅

根 `AGENTS.md` Conventions 新增:**新能力先立 seam**——Definition(中立接口)/ Provider(可多实现并存)/ Consumer(只 import Definition,禁按实现名/isinstance 分支)。是 adapter 契约(SessionSpec 两次事故)的泛化。范例:`BaseAdapter`、`StorySource`。

## NEXT-1:事件驱动感知最小形态(scheduler.wake)— 设计草图

**目标**:重要进程内信号(PTY 死亡 / awaiting 命中 / spawn 完成)把编排线程的 `wait(poll_interval)` 提前唤醒(≤5s → ~0s),**不新增第二条调度路径**(硬规则:编排线程是唯一调度入口——wake 只是让同一次 tick 提前发生)。

**已知坑(做之前先解决)**:
1. **循环导入**:`scheduler.py` imports `executors.py`;supervisor 若 import scheduler 会成环。解法:中立小模块(如 `orchestrator/engine/wake.py`)持模块级注册表,`OrchestratorThread.run` 启动时注册自己,信号方调 `wake()`;或 executors 经 DI 回调注入。
2. **跨进程信号唤不醒**:`story tool declare` 是独立 CLI 进程写 DB,in-process wake 够不着——declare→judge 的时延仍靠 5s DB 轮询(或改文件信号,过度设计,不建议)。
3. `run()` 主循环从 `_stop_event.wait(interval)` 改为 `_wake_event.wait(interval)` + `stop()` 同时 set 两者(保停机响应)。

**收益评估**:PTY 死亡→judge 时延 5s→~0s。锦上添花,排后。

## NEXT-2:hooks/ 参考阅读(headless 线启动时)

做 headless 优先(claude `-p --output-format stream-json` 结构化事件 + 退出码 = 内核级完成信号)时,读 dsh `packages/hooks/`——它同时处理 Claude Code / Codex 两个 CLI 的线协议差异,省摸协议的时间。注意 license(MIT)与直接抄的边界:参考协议解析思路,不整文件搬运。

## 进度

- [x] A1 健康信号:`HealthTracker` + `agent_health` 事件(2026-08-14)
- [x] A2 seam 三分法立规进 AGENTS.md(2026-08-14)
- [x] A3 核对 frontend token 纪律——已有,无改动(2026-08-14)
- [x] A6 只读验证 replay/status 分离哲学一致(2026-08-14)
- [ ] A4 scheduler.wake()(NEXT-1,含循环导入解法)
- [ ] A5 hooks/ 阅读(headless 线启动时)
