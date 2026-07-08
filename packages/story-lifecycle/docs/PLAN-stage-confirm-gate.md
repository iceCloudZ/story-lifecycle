# 全自动链路 stage 间确认闸 — 设计文档

> 状态：已与用户确认方案，待实施。
> 创建：2026-07-08。明天新会话照此文档执行即可。
> 范围：`packages/story-lifecycle`。

---

## 背景与问题

`continue_orchestrator_agent`(`orchestrator/engine/planner.py:530-995`)是单线程 while 循环，**一口气跑完所有 stage 无中间停顿**，只有 verify stage 有 gate（910-993）。由此产生两个问题：

1. **UX 断链**：design 做完停在终端 tab，用户不知道怎么进 build。两个并列按钮（「启动终端(HITL)」调孤立 `/sessions/spawn` vs「确认并执行」调 `/plan/confirm` 走自动链路）语义冲突，用户点了前者（旁路自动链路），跑完 design 后无人衔接下一阶段。
2. **进程堆积**：interactive PTY done 后不 kill（`planner.py:890` break 不回收），design/build/verify 三个 claude 进程同时存活，各占几百 MB。只有 headless 分支补了 `_kill_headless`（planner.py:888-889）。

**复测真实 story**：`tapd-1144381896001067383`（workspace `D:\hc-all`，profile `minimal`）。design.json 已手动产出（`D:\hc-all\.story\done\tapd-1144381896001067383/design.json`），但 `_plan_confirmed: false`、`status: planning`、`execution_count: 0`、`event_log` 全空 —— 从未走自动链路。

---

## 已确认的 4 个决策（不要推翻）

1. **统一走自动链路** — 去掉 OverviewTab 孤立「启动终端(HITL)」主按钮，主按钮变「开始 design」（走 confirm → `continue_orchestrator_agent`）。design 终端由自动链路 spawn，前端能发现。
2. **确认闸默认开启** — 复用死配置 `StageConfig.confirm`（`profile_loader.py:25,138` 已解析进 dataclass 但执行路径**从不读**，grep 确认零命中）。`confirm: true` = stage 完成后 paused 等人确认；`false` = 自动推进。`minimal.yaml` 的 design/build 改 `confirm: true`。
3. **PTY 生命周期：done 即 clean-exit + kill** — interactive 分支对齐 headless（planner.py:888-889 已有 headless 的 `_kill_headless`）。需要回查时用 `claude --resume <per-stage uuid5>`（机制已存在，`_build_stage_launch_cmd` per-stage uuid5）。**不搞主 PTY 常驻**。
4. **resume 跳过已完成 stage** — `context_json._completed_stages` 记录进度，resume 时从第一个未完成 stage 开始，不重跑、不重 spawn PTY。

---

## 对抗循环澄清（避免明天又绕进去）

对抗循环目前是**死代码**（LangGraph 时代设计，FC 重写时删除，`evaluator_loop.py:1-8` 明确声明 ISS-008 移除了死 helper）。profile 里的 `adversarial:` 配置被解析进 `ResolvedProfile.adversarial`，但执行路径从不读。

对抗循环的真实位置（设计意图，未接线）：
- **plan 阶段内**的 plan↔review（LLM `invoke_with_tools` 循环，不开第二个 PTY）。
- **code/verify 之间**的对抗。

**design↔build 之间没有对抗**，是纯顺序。build 阶段靠 `.story/context/<key>/*.md` 文件读 design 产出，不靠 design 的活 PTY。**所以 design PTY done 后可以放心 kill**。

业界验证（2026-07-08 联网查证 Claude Code 架构）：主会话 / 临时 task / 持久 subagent 三种模式中，即使"持久 subagent"也是每次新建上下文（不继承历史），持久的是配置/角色而非对话状态。真正能保留对话状态的只有 `--resume` 同一 session id。正确心智模型：**PTY 是一次性执行载体，transcript 是持久对话状态**。stage 间 kill 进程 + 需要时 resume，比保持进程常驻更省资源。

---

## 待执行计划（实施顺序）

### 1. `infra/terminal/pty.py` — 公开 clean_exit_pty（前置小改）

`_clean_exit_pty`（pty.py:541）去掉下划线 → `clean_exit_pty`，供 planner.py import。`cleanup_all`（568-586）内部调用同步改名。**纯重命名，行为不变**。这步先做，后面 planner 依赖它。

### 2. `orchestrator/engine/planner.py` — 确认闸 + PTY 回收 + resume 跳过 + 认领游离 done（核心）

**(a) resume 跳过已完成 stage + 认领游离 done**（入口，planner.py:529 附近，把 `idx = 0` 改成算 start_idx）：
- 读 `ctx.get("_completed_stages", [])`。
- **认领游离 done**：若 `_completed_stages` 为空，扫一遍 `actions`，凡是 done file（`Path(workspace)/stage_done_file_rel(story_key, stage)`）已存在的 stage → 加入 `_completed_stages`。覆盖你当前 story：design.json 手动产出，点「开始」后不重跑 design。
- 算 `start_idx`：遍历 actions，找到第一个 `stage ∉ _completed_stages` 的 launch action 下标。while 循环从 `start_idx` 开始（替原来的 `idx = 0`）。
- 入口处清除 `_stage_gate` 标记（进入执行即清，planner.py:504 附近）。

**(b) done 后记进度 + 回收 PTY + 触发确认闸**（planner.py:878-890，done 检测成功后）：
- 追加当前 stage 到 `ctx["_completed_stages"]` 并 `db.update_story` 持久化（在 `log_event("completed")` planner.py:879 之后）。
- **interactive 分支补 PTY 回收**（对齐 headless）：break 前，else 分支对 `_agent_pty`：
  ```python
  if _agent_pty is not None:
      try:
          clean_exit_pty(_agent_pty)   # flush transcript (/exit 握手, 最多 _CLEAN_EXIT_TIMEOUT=10s)
      except Exception:
          pass
      _agent_pty.kill()                # 兜底 force-kill
  ```
  headless 分支已有 `_kill_headless`（888-889）不动。
- **确认闸**：break 前，若该 stage 的 `stage_cfg.confirm == True` **且**后面还有未完成的 launch action → 设 `status="paused"`，写：
  ```python
  ctx["_stage_gate"] = {
      "completed_stage": stage,
      "next_stage": <下一未完成 launch action 的 stage>,
      "awaiting_confirm": True,
  }
  ```
  `db.log_event(story_key, stage, "stage_gate_reached", {...})`，`return`（退出循环，释放 driver claim）。
  `confirm == False` → 维持现状直接 `idx += 1`。
  verify 是最后阶段无下一 stage，不受影响（它走自己的 gate）。

### 3. `orchestrator/service/api.py` — 响应加字段 + advance 清标记

- `GET /plan`（api.py:2785-2829）响应增加：
  - `stages`：从 `_agent_actions` 的 launch actions + `_completed_stages` 组装 `[{"name", "focus", "adapter", "done": bool}]`。
  - `stage_gate`：`ctx.get("_stage_gate")`（前端用来显示确认闸卡片）。
- `PUT /advance`（api.py:783-796）：resume 分支（`status == "paused"`）里清除 `ctx["_stage_gate"]`（进入执行即失效）。

### 4. `entry/profiles/minimal.yaml` — 默认开启确认闸

- `design.confirm: false → true`
- `build.confirm: false → true`
- verify 不改（最后阶段，走自己的 gate）。
- 其他 profile（`realtest.yaml` / `swebench.yaml` / `strict.yaml` 等 CI/自动化场景）保持 `confirm: false` 不变，加注释说明 `confirm` 字段语义。

### 5. 前端

- **`frontend/src/components/OverviewTab.tsx`**：
  - 去掉孤立 spawn 主按钮（105-107 的 `startTerminal` /「🖥️ 启动 {currentStage} 终端(HITL)」）。`planning` 状态主按钮 = 「✅ 开始 design」（`onConfirmPlan`，110-112 已存在），由自动链路 spawn design 终端。
  - **stage 进度条用真实数据**：从 `/plan` 的 `stages` 读（替写死的 design/implement/test，31-35），`done` 字段驱动状态（✓完成 / 进行中 / 待开始）。
  - **确认闸卡片**：当 `stage_gate.awaiting_confirm === true`（story paused 且有 `_stage_gate`）显示醒目卡片「✅ {completed_stage} 已完成 → 确认推进到 {next_stage}」，点击调 `/advance`。paused 状态的「继续执行」按钮已存在（`StoryDetailPage.tsx:37` ACTIONS），卡片是更醒目的引导。
  - TerminalTab 保留（自动链路 spawn 的 PTY 前端能发现），「+ 新建」弱化为次要 debug 入口。
- **`frontend/src/api/client.ts`**：`Plan` 类型加 `stages?: {name, focus, adapter, done}[]` 和 `stage_gate?: {completed_stage, next_stage, awaiting_confirm}`。

### 6. 测试 + 实测

- **planner 单测**：`confirm=true` 触发 paused + `_stage_gate`；`confirm=false` 一气跑完；resume 从 start_idx 跳过已完成 stage（断言不重 spawn design PTY）；认领游离 done file。
- **pty 单测**：`clean_exit_pty` 改名后行为不变。
- **minimal.yaml 断言**：design/build `confirm=true`。
- **现有测试不回归**：headless 路径、verify gate、recovery 不受影响。
- **实测**：用 story `tapd-...7383`（design.json 已在）点「开始」→ 认领 design → 确认闸 →「推进 build」→ 自动开 build 终端，且 design 进程已 clean-exit。

---

## 两点技术细节（已和用户逐点确认）

### resume 跳过已完成 stage

**现状问题**：`continue_orchestrator_agent` 每次从 `idx=0`（planner.py:529）遍历。确认闸 paused 后 resume（点「推进」→ `/advance` → `start_story_async` → 再进 `continue_orchestrator_agent`），第 2 次会**重新 spawn design PTY**（重接 clarify MCP、起 MCP server 子进程、claude 启动成本），虽然 done file 已存在 poll 会很快命中 break，但仍是浪费；若 done file 被删/路径错位还会误判重跑。

**改法**：
- 入口算 `start_idx`，跳过 `_completed_stages` 里的 stage。
- done 后追加 stage 到 `_completed_stages` 并持久化到 `context_json`。
- 认领游离 done：`_completed_stages` 为空时扫 actions 把已存在 done file 的 stage 加进去（覆盖手动跑出 design.json 的场景）。
- `_completed_stages` 存 `context_json`（和 `_agent_actions`/`_plan_confirmed`/`_active_execution` 同层），跨进程可见。

### interactive PTY done 后 clean-exit + kill

**现状问题**：planner.py:887-890 只有 headless 分支回收（`_kill_headless`），interactive 分支 break 不回收 → 进程堆积。

**为什么不能直接 force-kill，要先 clean-exit**：claude 只在 `/exit` 干净退出时 flush `~/.claude/projects/<proj>/<uuid>.jsonl` transcript；force-kill 会截断 transcript，导致之后 `claude --resume <uuid>` 历史残缺。而点 2 决策是"需要时 resume 回查"，**必须保证 transcript 完整**。

**改法**：break 前 else 分支对 `_agent_pty` 先 `clean_exit_pty`（flush）再 `.kill()`（兜底）。和 `cleanup_all`（pty.py:568-586）两步完全一致。**最准确的回收点是 stage done 被确认的那一刻**（planner.py:890），不在 ensure_agent_pty（职责不清）也不在 kill_pty(story_id) 全杀（会误杀当前 PTY）。

**作用域注意**：`_agent_pty` 只在 else（interactive）分支（planner.py:731）赋值，`headless_proc` 在 if 分支（654 初始化 None，676 赋值）。done 检测（873）在两者之后的同一 launch 块内，能引用到。else 分支要判 `_agent_pty is not None`（launch 异常时可能未赋值）。

---

## 不改的（避免范围蔓延）

- verify gate（planner.py:910-993）
- `transition.py` / `recovery.py`
- headless 路径（已有自己的 spawn/kill）
- TerminalTab 多 session 管理
- `_build_stage_launch_cmd` 的 per-stage uuid5 resume 机制
- 其他 profile（realtest/swebench/strict）

---

## 风险与验证

- **R1 resume 重跑已完成 stage**：用 `_completed_stages` + start_idx 跳过，单测断言"resume 不重 spawn design PTY"。
- **R2 paused 与 orphan paused 混淆**：确认闸的 paused 带 `_stage_gate` 标记；`recover_orphan_stories`（graph.py:398-413）只处理 `intake_state=ready` 且无 `_stage_gate` 的。实施时核对其过滤条件，必要时加 `_stage_gate` 排除。
- **R3 clean-exit 阻塞**：`clean_exit_pty` 最长 `_CLEAN_EXIT_TIMEOUT`（10s），stage 间调用可接受；超时则 `.kill()` force-kill 兜底。

---

## 开工前先重读对齐行号（代码可能已变动）

- `planner.py:528-530`（while 入口 `idx = 0`）
- `planner.py:873-895`（done 检测 + break）
- `planner.py:910-993`（verify gate，确认闸的模板）
- `pty.py:541-586`（clean_exit_pty + cleanup_all）
- `profile_loader.py:25,138`（StageConfig.confirm 解析）
- `api.py:783-796`（/advance）、`api.py:2785-2829`（/plan）
- `OverviewTab.tsx:31-35`（写死 stages）、`105-117`（两个按钮）
- `StoryDetailPage.tsx:37`（paused 的「继续执行」/advance）

---

## 第一步建议

从 `pty.py` 公开 `clean_exit_pty` 开始（最小、前置），再做 `planner.py` 核心逻辑。开工前先重读上面"开工前先重读对齐行号"列出的代码段，确认行号未漂移。
