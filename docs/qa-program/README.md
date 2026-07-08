# QA Program — 体系化测试长程执行手册

> **AI agent 跨会话长程执行手册**。本目录是单一调度入口:一个 AI 会话满 / 中断 / 换窗口后,
> 新会话读本目录三个文件即可无缝接续,不依赖任何对话历史。
>
> 目标:按 [业务模块](../module-architecture/02-modules-overview.md) 体系化补测试,
> 提升代码质量、业务流畅度、对项目的理解。**每一步都是 atomic 任务卡 + 可执行验收 + 报告产出。**

## 本目录三文件(读完即可接续)

| 文件 | 作用 | AI 该怎么用 |
|---|---|---|
| **[README.md](README.md)**(本文件) | 执行须知 + 执行协议 + 进度表 + 交接规则 | 每次开窗第一件事读 §进度表,找下一个 `[ ]` |
| **[tasks.md](tasks.md)** | 全部任务卡(分阶段 atomic) | 取下一个 `[待办]` 卡执行;验收后改 `[已完成]` |
| **[report-template.md](report-template.md)** | 验收报告模板 + 填写规范 | 每完成一张卡,产出一份报告存 `reports/` |

## 快速上手(任何新窗口照做)

```text
1. 读 README.md §进度表 → 找最早的 status=[ ] 卡
2. 读 tasks.md 对应卡 → 搞清 现状/目标/步骤/验收/约束
3. 执行(写代码 + 写测试)
4. 跑验收命令(卡里给的 pytest 命令)→ 必须全绿
5. 按 report-template.md 写报告 → 存 reports/<卡号>-<短名>.md
6. 更新 README.md §进度表(卡改 [已完成],补 commit/报告链接)
7. git add docs/qa-program/ + 测试文件,commit,push
8. 若上下文快满 → 停。写交接 NOTE 到本次报告末尾
```

## 进度表(全局真相源 — 改这里 = 改进度)

> **任何会话开始/结束,第一件事和最后一件事都是更新此表。**
> 状态:`[待办]` / `[进行中]<窗口号>` / `[已完成]` / `[阻塞]`

### 阶段一:质量闸(模块④,最高优先级 — 系统心脏,现在最薄)

| 卡号 | 任务 | 状态 | 报告 | commit |
|---|---|---|---|---|
| T1.1 | gate 硬闸不可绕(max_retries 强制 fail) | `[已完成]` | [reports/T1.1-gate-hard-fail.md](reports/T1.1-gate-hard-fail.md) | `ec0d2ec4` |
| T1.2 | gate 三判定(advance/retry/fail)分支覆盖 | `[已完成]` | [reports/T1.2-gate-branches.md](reports/T1.2-gate-branches.md) | `f36936e0` |
| T1.3 | no_progress 终止(防死循环) | `[阻塞]` | [reports/T1.3-gate-no-progress.md](reports/T1.3-gate-no-progress.md) | `a246e5d6` |
| T1.4 | evaluator_loop repair-packet 构造 | `[已完成]` | [reports/T1.4-repair-packet.md](reports/T1.4-repair-packet.md) | `3dd69ed0` |
| T1.5 | Finding 生命周期(quality 飞轮) | `[已完成]` | [reports/T1.5-finding-lifecycle.md](reports/T1.5-finding-lifecycle.md) | `17b1a426` |

### 阶段二:执行编排(模块③,第二大风险区)

| 卡号 | 任务 | 状态 | 报告 | commit |
|---|---|---|---|---|
| T2.1 | FC 规划循环(mock LLM 生成 actions) | `[已完成]` | [reports/T2.1-fc-planning-loop.md](reports/T2.1-fc-planning-loop.md) | `93d6af4f` |
| T2.2 | plan_confirm 暂停语义 | `[已完成]` | [reports/T2.2-plan-confirm-pause.md](reports/T2.2-plan-confirm-pause.md) | `08c3df94` |
| T2.3 | .done 握手轮询(超时/成功) | `[已完成]` | [reports/T2.3-done-handshake.md](reports/T2.3-done-handshake.md) | `5210a81f` |
| T2.4 | 三启动模式一致性(-p / query / release) | `[已完成]` | [reports/T2.4-launch-modes.md](reports/T2.4-launch-modes.md) | `c642f187` |
| T2.5 | PTY/HITL bug 回归(最近 5 个 fix(pty)) | `[已完成]` | [reports/T2.5-pty-injection-regression.md](reports/T2.5-pty-injection-regression.md) | `bd0b8075` |

### 阶段三:HITL(模块⑦)

| 卡号 | 任务 | 状态 | 报告 | commit |
|---|---|---|---|---|
| T3.1 | clarify MCP 阻塞 + 交互式双路径 | `[待办]` | — | — |
| T3.2 | supervisor HITL 决策 | `[待办]` | — | — |
| T3.3 | approval_queue 阻塞部署 | `[待办]` | — | — |

### 阶段四:补齐其余模块(①②⑤⑥)

| 卡号 | 任务 | 状态 | 报告 | commit |
|---|---|---|---|---|
| T4.1 | ① 接入:tapid 幂等 + prd 边界 | `[待办]` | — | — |
| T4.2 | ② 上下文:resolver 零副作用不变量 | `[待办]` | — | — |
| T4.3 | ② 上下文:SOFT 缝降级 | `[待办]` | — | — |
| T4.4 | ⑤ 交付:worktree 清理无残留 | `[待办]` | — | — |
| T4.5 | ⑥ 飞轮:anchors round-trip + 卸包照跑 | `[待办]` | — | — |

### 阶段五:机制建设(长期可维护)

| 卡号 | 任务 | 状态 | 报告 | commit |
|---|---|---|---|---|
| T5.1 | coverage 度量 + 按模块报告 | `[待办]` | — | — |
| T5.2 | tests/invariants/ 目录 + 架构不变量集 | `[待办]` | — | — |

---

## 执行协议(必须遵守)

### P1 · 一卡一会话原则
- **一次窗口尽量只做一张卡**(做完做完,做不完交接)。不并行多卡——避免半成品交叉污染。
- 卡做完的标志 = **验收命令全绿 + 报告写完 + 进度表更新 + commit**。四者缺一不算完。

### P2 · 测试优先(Test-First 鼓励,不强求 TDD)
- 鼓励先写测试(红)再改代码(绿),但若现有代码已成型,补测试也可。
- **测试必须 mock LLM + mock CLI 进程**——确定性、快、不花 token。
- **每个历史 bug 必须配回归测试**(AGENTS.md 硬要求)。T2.5 就是专门收这批债。

### P3 · 不破坏架构不变量
这些是 [`module-architecture/02-modules-overview.md`](../module-architecture/02-modules-overview.md) §边界规则列的红线,代码改动后必须仍成立:
1. `ContextResolver` 只读(零副作用)
2. Gate 是硬闸(`round_count > max_retries` 不可绕)
3. adapters↔miner 通过 anchors.jsonl 文件契约(非 import)
4. SOFT 缝 try/except(miner/knowledge 是 optional)
5. infra 零内部 import
6. HITL 是横切不是 stage

### P4 · 诚实验收
- **验收命令必须实际跑过,输出贴进报告**。不许"应该能过"。
- 失败了就在报告里写失败,标 `[阻塞]`,留现场给下一窗口。**不准粉饰**。

### P5 · 交接纪律
- 上下文快满前**主动停**,不要被截断。
- 停前必做:① 报告末尾写"**交接 NOTE**(下一窗口接什么、卡在哪)";② 更新进度表状态。
- 新窗口第一步:读本目录三文件 + 上一份报告的交接 NOTE。

---

## 环境与命令

```bash
# venv(必须用这个,不是项目 .venv)
source .venv-monorepo-test/Scripts/activate   # Windows Git Bash

# 跑测试(从仓库根)
./.venv-monorepo-test/Scripts/python.exe -m pytest packages/story-lifecycle/tests/ -q

# 跑单个测试文件
./.venv-monorepo-test/Scripts/python.exe -m pytest packages/story-lifecycle/tests/test_xxx.py -v

# 覆盖率(阶段五启用)
./.venv-monorepo-test/Scripts/python.exe -m pytest packages/story-lifecycle/tests/ \
  --cov=packages/story-lifecycle/src/story_lifecycle --cov-report=term-missing
```

## 相关文档(执行前必读)

- 业务模块全景:[`docs/module-architecture/02-modules-overview.md`](../module-architecture/02-modules-overview.md)
- 各模块代码落点:[`docs/module-architecture/03-module-details.md`](../module-architecture/03-module-details.md)
- 代码分层真相源:[`packages/story-lifecycle/docs/ARCHITECTURE.md`](../../packages/story-lifecycle/docs/ARCHITECTURE.md)
- 项目约定:[`AGENTS.md`](../../AGENTS.md)(中文模板 / 无 ORM / editable install / 架构评审触发)
