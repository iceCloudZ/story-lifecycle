# BUGLOG — 全自动 FC 流程人工走查发现的问题

> **来源**：2026-07-10 对"全自动 FC"流程做人工端到端走查，story `tapd-1144381896001065488`（profile `minimal`）。
> **状态**：全部待评估，未修复。每个 bug 含代码引用 + 文档引用 + 修复方向。
> **关联文档**：`docs/handoff-design-hitl.md`（§11 clarify 双轨制决策）、`docs/PLAN-stage-confirm-gate.md`、`docs/STORY-STATE-MODEL.md`。

---

## 索引

| # | 标题 | 根因文件 | 严重度 |
|---|---|---|---|
| #1 | planning 确认按钮需手动刷新才出现 | `StoryDetailPage.tsx` SSE/轮询时机 | 中 |
| #2 | 点确认后停在概览页，不跳终端 | `StoryDetailPage.tsx:134` | 中 |
| #3 | 删除 PTY 后 claude.exe 成孤儿进程 | `pty.py` kill 路径 | 中 |
| #4 | `/start` 建即规划，跳过用户主动触发 | `api.py:2835/2884` + 前端 SSE 自动建连 | 中（待评估：设计意图） |
| #5 | `/plan/stream` 流式 UI 在"建即规划"场景下从不触发 | `StoryDetailPage.tsx:97` 守卫 + #4 联动 | 中 |
| #6 | 「开始 design (N 步)」按钮文案误导 | `OverviewTab.tsx:234` | 低 |
| #7 | done_file 路径由 LLM 自由生成，跨 story 竞争污染 | `planner.py:296` | **高** |
| #8 | 长 prompt（含宽表格知识）经 120 列 PTY 注入，渲染断裂/知识丢失风险 | `pty.py:199` + `:513` | 中（待验证） |
| #9 | 交互式 clarify 双轨制未落地，`interactive` 旗标断裂 | `planner.py:643` + `:679` | **高** |
| #10 | design clarify 阻塞期间 poll loop 仍计超时，等澄清时被误判 fail | `planner.py:897`+`:1168` | **高** |

---

## BUG #1 — planning 确认按钮需手动刷新才出现

**严重度**：中
**现象**：story 进入 `status=planning` 后，前端「确认规划」按钮不自动出现，需手动刷新页面（F5）才显示。

**根因**：
- 前端 detail 查询 `StoryDetailPage.tsx:60-64` 用 react-query，`refetchInterval: 5000` 固定轮询。story 从 `active`/`idle` 翻到 `planning` 时，detail 会刷新，但确认按钮渲染依赖 `OverviewTab.tsx:231`：
  ```tsx
  {detail.status === 'planning' && !isConfirmed && resolvedActions.length > 0 && (...)}
  ```
- `resolvedActions`（`StoryDetailPage.tsx:122`）优先取 `streamingActions`，无则取 `planData?.actions`。`planData` 来自 `/plan` 查询（`:84-91`），其 `enabled` 要求 `detail.status ∈ [planning, active, paused, implementing]`。
- **时序问题**：`/start` 把 status 设成 `planning`（见 #4），但 `planData` 的首次查询要等 `detail.status` 刷新到 `planning` 才 `enabled`，且规划 LLM 跑完写 `_agent_actions` 也需要时间。在这之间 `resolvedActions.length === 0`，按钮不显示。刷新页面强制重新走 query，才补上。

**代码引用**：
- 渲染条件：`frontend/src/components/OverviewTab.tsx:231-240`
- detail 轮询：`frontend/src/pages/StoryDetailPage.tsx:60-64`
- planData enabled 条件：`frontend/src/pages/StoryDetailPage.tsx:89`
- resolvedActions 推导：`frontend/src/pages/StoryDetailPage.tsx:122`

**修复方向**：
- 规划完成后（SSE `done` 事件，`StoryDetailPage.tsx:110`）主动 `qc.invalidateQueries(['plan'])` 强制刷新 planData。
- 或 detail 轮询检测到 `status==='planning'` 且 `planData` 为空时，缩短 refetchInterval。

---

## BUG #2 — 点确认后停在概览页，不跳终端

**严重度**：中
**现象**：点「确认规划」成功后，前端停在概览页，没有自动跳转到终端视图，用户看不到 claude 实时输出。

**根因**：`handleConfirmPlan` 成功后强制 `setActiveTab('overview')`，没有根据执行已启动（`_active_execution` 出现）跳转 `terminal` tab。

**代码引用**：
- 根因行：`frontend/src/pages/StoryDetailPage.tsx:130-138`
  ```tsx
  async function handleConfirmPlan() {
    const r = await fetch(`/api/story/${storyKey}/plan/confirm`, { method: 'POST' })
    if (r.ok) {
      refetch()
      setActiveTab('overview')   // ← :134 强制留在 overview
    } else { ... }
  }
  ```
- 确认后后端写 `_active_execution`：`orchestrator/engine/planner.py:884-893`
- 终端 tab 入口：`frontend/src/pages/StoryDetailPage.tsx:226-228`（`tab=terminal`）

**修复方向**：`:134` 改为 `setActiveTab('terminal')`；或在 detail 轮询到 `_active_execution` 出现时自动切 tab。

---

## BUG #3 — 删除 PTY 后 claude.exe 成孤儿进程

**严重度**：中
**现象**：`DELETE /api/pty/{story_id}` 删除 PTY 会话后，底层 `claude.exe` 未被回收，需手动 `taskkill /F` 清理。实测 2 个孤儿 claude.exe（PID 14708/17804）。

**根因**：`api_kill_pty`（`:596`）直接调 `kill_pty`（`pty.py:517`），走**纯 force-kill 路径**，不经过 `clean_exit_pty`。若 claude 卡住（PTY 空转、未响应 `/exit`），`kill_pty` 的 kill 逻辑在 Windows 上依赖 Job Object / `taskkill /T`，可能漏掉 detached 的 claude 子进程（node helper 等）。

**代码引用**：
- API 端点：`orchestrator/service/api.py:595-599`（`api_kill_pty` → `kill_pty`）
- `kill_pty`：`infra/terminal/pty.py:517-532`
- `ManagedPty.kill`：`infra/terminal/pty.py:338-381`（Job Object / killpg 路径）
- 对照：`clean_exit_pty`：`infra/terminal/pty.py:543-571`（先 `/exit` 等 10s 再 kill）
- 对照：`cleanup_all(prefer_clean_exit=True)`：`pty.py:574-592`（全量清理走 clean-exit）

**关联**：`docs/handoff-design-hitl.md` §待办提到"干净 resume 需先杀掉旧 serve 留的孤儿 claude"——同类问题。

**修复方向**：`api_kill_pty` 改为优先走 `clean_exit_pty` + kill 兜底（对齐 `cleanup_all` 的 `prefer_clean_exit` 语义），而非直接 force-kill。

---

## BUG #4 — `/start` 建即规划，跳过用户主动触发

**严重度**：中（需评估：可能是设计意图）
**现象**：新建/启动 story 后，系统自动进入规划，直接停在 `status=planning`（`_agent_actions` 已填满）。用户没有"点生成规划"的主动触发步骤。

**根因（组合触发）**：
1. `POST /start`（`api.py:2814`）把 `status` 直接设成 `"planning"`（`:2835` 候选提升时、`:2884` 正常路径），文档字符串自称 "triggers LLM planning"。
2. `/start` 本身**不调 LLM**，但前端 `StoryDetailPage.tsx:94-101` 的 SSE effect 检测到 `detail.status === 'planning'` 且 `planData.actions` 为空时，**自动建连** `/plan/stream`（`:101`），从而触发后端 `run_orchestrator_agent`。
3. 两者组合 → "建/启动即规划"。

**代码引用**：
- `/start` 设 planning：`orchestrator/service/api.py:2835`、`:2884`
- `/start` 文档字符串：`api.py:2816`（"triggers LLM planning"）
- 前端 SSE 自动建连：`frontend/src/pages/StoryDetailPage.tsx:94-101`
- 后端规划执行：`orchestrator/engine/planner.py:173`（`run_orchestrator_agent`）

**评估问题**：这是 bug 还是设计意图？
- 若意图是"start 即规划"：#1、#5 是该意图下的配套缺陷。
- 若意图是"用户主动生成规划"：`/start` 不该把 status 设成 planning，应停留在一个"待规划"态，等用户点「生成规划」按钮。

**修复方向（取决于评估）**：
- 若改意图：`/start` 设 `status="idle"`（或新增 `ready` 态），保留「生成规划」按钮主动触发 SSE。
- 若保留意图：修 #1/#5 让自动规划链路体验顺畅。

---

## BUG #5 — `/plan/stream` 流式 UI 在"建即规划"场景下从不触发（死代码）

**严重度**：中
**现象**：`/plan/stream` 设计为 SSE 流式推送（action 一条条出现），但用户从未看到流式效果——规划结果一次性全显示。

**根因**：SSE effect 的守卫 `if (existingActions?.length) return`（`:97`）。因 #4 建即规划，前端首次拿到 `planData` 时 `_agent_actions` 已非空（规划秒级完成），守卫直接 return，EventSource 永不建立。

**代码引用**：
- 守卫行：`frontend/src/pages/StoryDetailPage.tsx:97`
  ```tsx
  const existingActions = planData?.actions
  if (existingActions?.length) return   // ← 已有 actions → 永不建 SSE
  ```
- SSE 建连：`frontend/src/pages/StoryDetailPage.tsx:101`
- 后端流式实现：`orchestrator/service/api.py:3000-3066`（`api_plan_stream`）

**依赖链**：#5 是 #4 的下游症状。修 #4（建即规划）后，进入 effect 时 actions 为空，守卫不拦，SSE 自然建连，流式恢复。

**修复方向**：
- 方案 A（推荐）：修 #4，让 actions 在 SSE 建连时为空。
- 方案 B：守卫改为"仅当规划进行中（有 started 无 done）才拦"，而非"已有 actions 就拦"。

---

## BUG #6 — 「开始 design (N 步)」按钮文案误导

**严重度**：低
**现象**：确认规划按钮文案「✅ 开始 design (3 步)」让人以为点击会一口气跑完 3 个阶段。实际它只是"确认规划 → 开始执行"，且 design 跑完就停在第一道阶段间闸（`design.confirm: true`），不会自动连跑。

**根因**：按钮文案同时表达"开始 design"和"3 步"，但语义上它=确认整个规划，且中途会停。

**代码引用**：
- 根因行：`frontend/src/components/OverviewTab.tsx:233-235`
  ```tsx
  <button className="btn btn-primary" onClick={onConfirmPlan}>
    ✅ 开始 design ({resolvedActions.filter((a) => a.action === 'launch').length} 步)
  </button>
  ```
- 注释（作者意图）：`OverviewTab.tsx:224-230`
- 点击调 `handleConfirmPlan` → `/plan/confirm`：`StoryDetailPage.tsx:130-138`
- 中途会停的原因：`planner.py:1119-1161`（阶段间闸，`design.confirm:true` → paused）

**修复方向**：文案改为「✅ 确认规划，开始执行」或「✅ 确认并开始 design」，弱化步数暗示。

---

## BUG #7 — done_file 路径由 LLM 自由生成，跨 story 竞争污染

**严重度**：**高**
**现象**：done_file 路径由规划期 LLM 通过 `plan_step` 工具自由生成，导致同一 workspace 内多个 story 的 done 文件格式混乱（实测 4 种），其中 2 个 story（5488、6272）done_file 完全相同（`.story-design.done`，**不带 story_key**），并发执行时会互相误判完成。

**数据证据**（workspace `D:\hc-all`，23 个 story 共用）：
| 格式 | 示例 | 带 key? | 数量 |
|---|---|---|---|
| `.story/done/<key>/<stage>.json`（正确默认值） | `.story/done/tapd-.../design.json` | 是 | 18 |
| `.story-<stage>.done`（**撞名**） | `.story-design.done` | **否** | 2 |
| `.story-done/<key>-<stage>.json` | `.story-done/1064584-design.json` | 是 | 1 |
| `.story-<key>-<stage>.done` | `.story-tapd-...-design.done` | 是 | 1 |

**根因**：`planner.py:296-299` LLM 传的 `done_file` 被直接采用，仅当 LLM 不传时才用 `stage_done_file_rel()` 兜底。没有任何地方校验/规范化 done_file 必须含 story_key 或落统一目录。

**代码引用**：
- LLM done_file 被采用：`orchestrator/engine/planner.py:296-299`（`plan_step` → action）
- 兜底默认值：`orchestrator/engine/planner.py:619-622`（`action.get("done_file", stage_done_file_rel(...))`）
- 轮询用同一值：`orchestrator/engine/planner.py:896`（`done_path = Path(workspace) / done_file_rel`）
- 默认值定义：`infra/paths.py:29-41`（`stage_done_file_rel` = `.story/done/<key>/<stage>.json`）
- resume orphan-done 认领（撞名时会误领）：`planner.py:553-580`

**影响**：
- 同 workspace 并发 story → done 文件撞名 → **错误完成判定 + 状态污染**（拿到别人的 summary/files_changed）。
- resume 认领逻辑误领别人的 done。
- 布局混乱增加调试难度。

**修复方向**：done_file **不应由 LLM 决定**。`planner.py:619` 构造 action 时**强制**用 `stage_done_file_rel(story_key, stage)`，忽略 LLM 传的值（或仅记录、不用于轮询）。

---

## BUG #8 — 长 prompt（含宽表格知识）经 120 列 PTY 注入，渲染断裂/知识丢失风险

**严重度**：中（待验证）
**现象**：design prompt 含多个 200+ 字符的 markdown 表格行（安全维度 playbook）。经 PTY 注入 claude TUI 后，终端实际显示（用户复制）：表格行截断、「框架」段重复出现 2 次且残缺、大量尾随空格填充。

**根因**：PTY 初始化列宽固定 120（`dimensions=(30, 120)`），长表格行写入后软换行；claude TUI 是否能正确还原折行内容**未确认**。

**代码引用**：
- PTY 列宽：`infra/terminal/pty.py:199`（`dimensions=(30, 120)`）
- prompt 字节注入：`infra/terminal/pty.py:513`（`pty.write(prompt.encode("utf-8") + b"\r")`）
- prompt 源（含宽表格）：`orchestrator/engine/planner.py:1276-1398`（`_build_cli_prompt`）
- 安全 playbook 内容来源：`orchestrator/engine/prompt_sections.py`（`build_design_dimensions_section`）

**未确认**：claude TUI 在 120 列下读带软换行长 prompt 时，解析到的内容是否损坏（还是仅终端回显损坏）。

**验证方法**：design 跑完后查 claude 产出的 spec 安全章节，看"校验三要素""历史决策模式表"是否完整（缺失则证实知识丢失）。

**修复方向**（待评估）：
- 增大 PTY 列宽（`dimensions=(50, 500)` 类）。
- 或长 prompt 走文件让 claude `@读取`（prompt 已写盘 `prompt_design.md`），不注入全文。
- 或对 prompt 预处理：宽表格转窄格式。

---

## BUG #9 — 交互式 clarify 双轨制未落地，`interactive` 旗标断裂

**严重度**：**高**
**现象**：`minimal.yaml` 的 `execution_mode: interactive_pty`，按决策应"在终端直接问人"（不走 MCP），但实际跑起来 claude 调用了 `mcp__lifecycle__clarify`（MCP 提问），前端 ClarifyDialog 卡片正常显示 MCP 澄清问题。

**决策依据**：`docs/handoff-design-hitl.md` §11 + 后续（2026-07-08）定的双轨制：
- 交互式终端路径（`interactive=True`）→ "在终端直接问人"，**不注入 MCP**。
- headless/自主路径（`interactive=False`）→ MCP clarify。

**根因（两处漏改）**：

1. **`planner.py:643-654`**：`_build_cli_prompt(...)` 调用**未传 `interactive=`**，旗标恒为默认 `False`。
   ```python
   cli_prompt = _build_cli_prompt(
       story_key=story_key,
       ...
       transcript_section=transcript_ctx or "",
       # ← 缺 interactive=not headless
   )
   ```
   - `_build_cli_prompt` 签名：`planner.py:1276, 1288`（`interactive: bool = False`）
   - 传给 `build_design_dimensions_section`：`planner.py:1338-1339`（`interactive=interactive`）
   - `interactive` 分支：`prompt_sections.py:244`（`if interactive:` → "在终端问人"）

2. **`planner.py:679`**：`--mcp-config` 注入条件缺 headless 守卫，交互式路径也注入 MCP。
   ```python
   if stage == "design" and adapter_name == "claude":   # ← 缺 and headless
       ...
       launch_cmd = list(launch_cmd) + ["--mcp-config", str(_mcp_cfg)]
   ```

**连锁后果**：`interactive` 恒 False → prompt 文案走 MCP 分支 → `--mcp-config` 无条件注入 → 交互式 claude 带 MCP 起来 → 调 MCP clarify 提问。与决策方向相反。

**代码引用**：
- 漏改点 1：`orchestrator/engine/planner.py:643-654`
- 漏改点 2：`orchestrator/engine/planner.py:675-702`（条件在 `:679`）
- 旗标定义：`orchestrator/engine/planner.py:1288`
- 旗标消费：`orchestrator/engine/prompt_sections.py:214-244`
- MCP clarify server：`orchestrator/mcp/clarify_server.py`

**文档引用**：
- 决策来源：`docs/handoff-design-hitl.md:161-203`（§11 + 交互式 clarify 协议后续）

**修复方向**：
- `:643` 加 `interactive=not headless`。
- `:679` 条件改为 `if stage == "design" and adapter_name == "claude" and headless:`（或 `and not interactive`）。
- 验证：交互式路径 prompt 文案为"在终端问人"，`launch_cmd` 不含 `--mcp-config`，claude 不调 MCP。

---

## BUG #10 — design clarify 阻塞期间 poll loop 仍计 45min 超时，等澄清时被误判 fail

**严重度**：**高**
**现象**：design 阶段 claude 调 MCP clarify 提问，阻塞等人答（设计内的 HITL 暂停）。但用户响应稍慢（实测 <45min 内未答），planner 的 done 文件轮询循环超时触发，把一个正在"等人答澄清"的 design 判成 `failed`（`last_error: 'Stage design timed out'`）。story `tapd-1144381896001065488` 实测命中：design 12:26 启动，13:11（~45min）超时 fail。

**根因**：poll loop 只检查 `done_path.exists()`，不感知 clarify 阻塞。clarify 的 `waiting=true` 状态与 poll 超时计时完全解耦——clarify 阻塞时，`elapsed` 照常累加，45min 照常触发 fail。

**代码引用**：
- 超时常量：`orchestrator/engine/planner.py:897-901`（`poll_timeout = 45 * 60`）
- 超时判定 → failed：`planner.py:1168-1180`（`else` of while → `_kill_headless` + `status="failed"` + `last_error="Stage <s> timed out..."`）
- poll loop 只看 done：`planner.py:983`（`if done_path.exists():`），无 clarify 感知
- 注释自承"本 poll loop 无需特殊 clarify 处理"：`planner.py:978-980`（**但超时也没特殊处理，是契约缺口**）
- clarify waiting 独立查询：`orchestrator/service/api.py:3191-3217`（`get_pending_clarification`）

**影响**：任何需要人工澄清的 design，用户响应慢（>45min）即被误判 fail，design 前功尽弃，需重跑。

**修复方向**：poll loop 每轮检查 clarify waiting 状态；若 `waiting=true`，**暂停超时计时**（`elapsed` 不递增，或检测到 waiting 后重置 elapsed 起点），答完澄清 claude 恢复执行后再继续计时。需引入"clarify 等待不计入超时"的契约。

---

## 附：bug 之间的依赖关系

```
#4 (建即规划) ──导致──▶ #1 (按钮要刷新) 
                 └─导致─▶ #5 (流式 UI 死代码)
#9 (interactive 断裂) — 独立，但与 #4 共同影响"交互式体验"
#7 (done_file 竞争)   — 独立，定时炸弹(并发才触发)
#3 (PTY 孤儿)         — 独立
#8 (PTY 渲染)         — 独立，待验证
#2 (不跳终端)         — 独立
#6 (文案)             — 独立
```

**建议评估优先级**：#7、#9（高，影响正确性）→ #4（决定 #1/#5 走向）→ #3/#2/#8 → #6。
