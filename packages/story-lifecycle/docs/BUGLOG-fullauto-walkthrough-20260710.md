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

## BUG #14 — design prompt 错误禁止 brainstorming skill（与 profile 配置矛盾）

**严重度**：**高**
**现象**：design 阶段注入的 prompt 里写明「**不要调用 brainstorming skill**（hc-all 重环境发散/context rot，自由探索会卡死）」，但 minimal.yaml 里 design 阶段显式配了 `skill: "/brainstorming"`。CLI 收到两条互相打架的指令——profile 让它调，prompt 又禁它调。

**根因**：历史遗留。后来加了"13 维度 checklist"想做结构化的产品→技术转化，顺手在 prompt 里写了禁令替代 brainstorming 自由探索，但：(1) 没跟产品确认就废了 skill；(2) 忘了同步删 profile 的 `skill` 字段。两边都没改干净，留下矛盾。

**代码引用**：
- prompt 禁令（要删）：`orchestrator/engine/prompt_sections.py:252`
  ```python
  "**不要调用 brainstorming skill**（hc-all 重环境发散/context rot，自由探索会卡死）；"
  ```
- docstring 同样表述（要改）：`prompt_sections.py:221、227-228`
- profile 配置（要保留）：`entry/profiles/minimal.yaml:20` —— `skill: "/brainstorming"`
- 另外两个 profile 也有同款 skill 字段：`demo.yaml:10`、`strict.yaml:13`

**影响**：design 阶段本应让 CLI 调 brainstorming 做发散，结果被 prompt 显式禁止。维度 checklist 与 brainstorming 本可共存（发散 + 收敛），现在被禁令卡死一边。

**修复方向**（已与产品确认：**要调 brainstorming**）：
- 删 `prompt_sections.py:252` 的禁令；docstring 221/227 同步改。
- 维度 checklist 与 brainstorming 共存——brainstorming 做发散，checklist 做收敛/兜底。
- 不动 profile 的 `skill` 字段。

---

## BUG #15 — 安全 playbook 全量注入，价值存疑 + 可能加重 context rot

**严重度**：中
**现象**：design 阶段把整个 `security-parameter-trust.md`（Parameter Trust 框架 + 分级表 + 三要素表 + 历史模式表 + §9 要求 + 常见坑）全量塞进 prompt，实测占整段 prompt ~60%。用户实测反馈"没什么作用，都没看懂"。讽刺的是：代码注释本意是防 context rot（`prompt_sections.py:227`），结果全量注入反而可能加重它。

**根因**：`build_design_dimensions_section` 的 playbook 注入只做了 `split("## 怎么用")[0]` 的粗截断（`prompt_sections.py:278`），把"怎么用"章节之前的全部内容（框架/分级/三要素/模式/§9/坑）无差别塞进去。**不区分需求类型**——一个"前端展示组件"的需求和"配置类 CRUD"的需求拿到的是同一份完整安全框架。

**代码引用**：
- 全量截断逻辑：`orchestrator/engine/prompt_sections.py:278`
  ```python
  _snippet = _content.split("## 怎么用")[0]
  ```
- 注入位置：`prompt_sections.py:279-283`（`section += "### 安全维度参考..."`）
- 注入文件：`<workspace>/.story/knowledge/playbooks/security-parameter-trust.md`（实测 59 行，注入约 54 行）
- failsafe 吞异常：`prompt_sections.py:284`（`except Exception: pass`）——文件不存在不报错，但存在就全塞

**影响**：
- prompt 体积虚胖（~60% 给了一个维度），挤占真正跟需求相关的上下文。
- 对低风险需求（纯展示/只读）过度注入，LLM 可能被带偏去过度设计安全校验。
- 人审 prompt 时读不懂、抓不住重点。

**修复方向**（需产品拍板，有分叉）：
- A. **触发式注入**：prompt 里只放一句"做安全维度时，读 `<path>`"，让 claude 按需自查文件（agentic RAG 思路，跟 kb.py 一致）。
- B. **按需求类型条件注入**：`task_type` 命中 admin/CRUD/资金类才注入完整框架；展示/只读类只注入 §9 清单骨架。
- C. **缩成骨架**：只注入"三步框架 + §9 必填项"，分级表/模式表/坑留给 claude 自查文件。

---

## BUG #16 — task_type 关键词分类顺序错误，"Loan" 类前端需求被误分到 fund-flow

**严重度**：中
**现象**：story `tapd-1144381896001066924`（标题「【HC】新增Loan Disclosure Statement展示+贷款协议更新」，本质是**前端展示 + 协议更新**）被 `classify_task_type` 分成了 `fund-flow`。实测 DB `context_json.task_type = "fund-flow"`，导致 `kb.py bugs fund-flow` 注入的高风险文件/磁铁指向资金流，跟"前端展示组件"不对路——**知识注入指向错了**。

**根因**：`classify_task_type` 是**首个关键词命中即返回**（`prompt_sections.py:101-102`），而 `TASK_TYPE_KEYWORDS` 列表里 `fund-flow`（含 "loan"/"Loan"，第 50 行）排在 `frontend`（第 83 行）**前面**。标题里的 "Loan" 先命中 fund-flow 就直接返回，**根本没机会走到 frontend**。纯关键词 + 顺序敏感，没有权重/多命中投票。

**代码引用**：
- 首命中即返回：`orchestrator/engine/prompt_sections.py:99-102`
  ```python
  for task_type, kws in TASK_TYPE_KEYWORDS:
      for kw in kws:
          if kw.lower() in haystack:
              return task_type
  ```
- fund-flow 关键词含 "loan"：`prompt_sections.py:39-52`（"loan" 在第 50 行）
- frontend 关键词：`prompt_sections.py:83`（"前端/admin/页面/frontend/protable/proform/组件"）
- 顺序问题：fund-flow（39）排在 frontend（83）前
- 调用点（story 创建时实时分类）：`orchestrator/service/story_service.py:140-144`
  ```python
  from ..engine.prompt_sections import classify_task_type
  task_type = classify_task_type(title, description)
  ```

**数据证据**：标题「...Loan Disclosure Statement展示+贷款协议更新」—— "Loan" 命中 fund-flow；"展示" 本可命中 frontend，但永远轮不到。

**影响**：
- 知识注入指向错（`kb.py bugs/playbook fund-flow` 拿到资金流知识，而非前端知识）。
- 影响所有"Loan/放款/还款"字样但同时是前端/展示类的需求——在借贷业务里这类需求不少。
- task_type 还会流向 `knowledge_provider` 的 bootstrap（项目结构注入），误导扩大。

**修复方向**（已与产品确认 2026-07-13：**关键词方法本身不对,升级为 LLM 分类**）：
- **落点**：`create_and_start_story`（`story_service.py:140-144`），把 `classify_task_type`（关键词）换成 `call_llm_json`（LLM）。
- **时机**：点「读取 TAPD」→ `create_story_from_source` → 此时 `item.description` + PRD 都已在手 → 传 LLM 分类 → **纯同步**（阻塞 create 返回几秒,前端可接受）→ 写 `context_json.task_type`。
- **输入**：title + description（PRD 此时刚 `fetch_prd_content` 落盘,可选喂摘要）。
- **输出**：13 个受控词汇之一（`TASK_TYPE_KEYWORDS` 的 key,保证下游 `build_kb_tool_section` 认）。
- **兜底**：LLM 失败/超时 → 回退关键词分类（沿用现有 `except: pass` 防御,不阻塞 story 创建）。
- **基建**：复用 `sourcing/planner/llm.py:call_llm_json`（decomposer/idea_expander 已在用）。
- **关键词分类保留**：降级为 LLM 兜底,不删（`classify_task_type` 函数留着）。
- **竞态**：纯同步无竞态——分类在 `create_and_start_story` 内完成,后续 `start_story_async`（:447）读 task_type 时已就绪。
- **不做**：不在 TAPD batch sync（`sync_service.py`）加分类——那条路径 story 是 candidate/idle,等 promote 时再分;避免 sync 批处理被 LLM 调用拖慢。

---

## BUG #17 — stage 产出文档不登记进 story_document 表，前端「文档」卡片永远只有 PRD

**严重度**：**高**
**现象**：design/build/verify 完成后，产出文件（design.md / plan.md / test-report.md）不进 `story_document` 表，前端「文档」卡片永远只显示 intake 时登记的 PRD。实测 story `tapd-1144381896001066924`：design 阶段已成功完成，`design.md`（10KB）产出在证据目录，done JSON 明确写了 `files_changed: ["story/.../design.md"]`，但 `story_document` 表仍只有 1 条 PRD 记录，卡片显示「文档(1)」。

**根因**：**功能缺失（不是代码坏了，是压根没写）**。planner 的 done 消费块能感知 done 文件、读出整个 `done_data`（含 `files_changed`），但感知之后**只用 `summary` 写进 event log，`files_changed` 读进来就扔了**——没有任何代码把它落进 `story_document`。全包只有 PRD 在 intake 时被登记（`story_service.py:150`），design/build/verify 的产出**全都不登记**。

**代码引用**：
- done 消费块（感知了但不登记）：`orchestrator/engine/planner.py:1026-1032`
  ```python
  if done_path.exists():
      done_data = robust_json_parse(done_path) or {}      # :1031  files_changed 已解析进来
      db.log_event(story_key, stage, "completed", done_data)  # :1032  只写 event log
      # ← 缺口：done_data["files_changed"] 之后再没人读
  ```
- `files_changed` 唯一被读的地方（只写进自由文本 retrospect.md，不进 DB）：`planner.py:418`
- PRD 登记点（intake 时，全包唯一的 stage 外登记）：`story_service.py:150` + `api.py:2938`
- `db.create_document` 定义（幂等 on `(story_key, kind, ref)`）：`infra/db/models.py:1781`
- **现成但没接进流程的逻辑**：`auto_discovery.py` 的 Scanner（扫 `**/story/**/spec.md`/`research.md`/`plan.md`/`test-report.md`，:141-158）+ Decider（emit `new_documents`，:190-246）+ Handler（`INSERT INTO story_document`，:310-328）—— 只挂在手动接口 `POST /api/story/{key}/context/refresh`（`api.py:1967`），**从未接进 stage 完成流程**。
- **连带缺口**：design prompt 模板让 claude「使用 `story-context` 工具回写 research/spec 文档引用」（`infra/prompts/design.md:22`），但**这个工具不存在**（`agent_tools.py` 无定义）。claude 只能把路径写进 done JSON，无人接。

**关键事实（答两个澄清）**：
1. **这步不需要 LLM 介入**——done JSON 里 claude 已写 `files_changed`，planner 做"解析 JSON → 拿路径 → 调 `create_document`"即可，纯确定性。`kind`（文档类型）按 stage 名推导（design→spec/research，build→plan，verify→test_report），也是确定性的。
2. **done 文件编排有感知**——`planner.py:1031` 读出整个 `done_data`（含 `files_changed`），但感知到之后没动作。**感知有了，动作没接上**，这就是缺的那一步。

**影响**：
- 前端「文档」卡片永远只有 PRD，用户看不到 design/plan/test-report 产出，失去 stage 产物的可追溯性。
- done JSON 里 claude 如实写的 `files_changed`/`spec_path` 被白白丢弃。
- `auto_discovery` 的现成扫描逻辑闲置，只在用户手动点 refresh 时才跑。

**修复方向**（建议，待产品确认）：
- **落点**：`planner.py:1032`（`db.log_event` 那行）之后，插入一段确定性登记逻辑。
- **做法（done JSON 驱动，推荐）**：读 `done_data["files_changed"]`，对每个路径调 `db.create_document(story_key, kind, ref=path)`；`kind` 按 stage 推导（design→spec/research，build→plan，verify→test_report）。纯 Python，不调 LLM。
- **兜底**：若 done JSON 没写 `files_changed`（claude 偶尔漏写），可回退调 `auto_discovery` 扫证据目录。
- **顺带**：要么实现 `story-context` 工具让 claude 显式回写，要么删掉 `design.md:22` 那条引用（避免 prompt 指向不存在的工具）。

---

## BUG #18 — build 阶段不走 worktree、不建 feature 分支，claude 就地改 master/test

**严重度**：**高**
**现象**：点「开始实现」进 build 阶段后，claude 在主工作区（`D:\hc-all`）的**当前 checkout 分支**（master/test）上就地改代码，既没创建 git worktree，也没按 `branch_rule` 建 feature 分支。实测 story `tapd-...066924`：hc-config 在 master 上改、hc-order 在 test 上改，均未建分支——违反项目的「feature 分支隔离」纪律。

**根因**：**PRESENT-but-BROKEN**。worktree 引擎（resolver→decider→handler）完整存在且能用，但 planner 的 build 启动路径**从不调它**，claude 的 `cwd` 直接是主工作区。planner 自己的注释承认了这点，并把分支隔离的判断**甩给了 LLM**（prompt 只给 advisory 文本 + 两条示例 git 命令），LLM 实际不会主动建 worktree。`branch_rule` 在 story start 时渲染成字符串存进 `story_project.branch`，但**从没被用来真正 `git branch`/`git worktree`**。

**代码引用**：
- worktree 引擎完整存在：`orchestrator/workspace/worktree/handler.py:22`（`prepare_worktrees`）+ `:323`（`_create_worktree` 跑 `git worktree add`）+ `:361`（`_derive_worktree_path`）
- **唯一调用方是手动接口**：`orchestrator/service/api.py:2445-2452`（`POST /api/story/{key}/worktrees/prepare`）—— planner 从不 import/调用 `prepare_worktrees`
- planner 自承甩给 CLI（问题根源）：`orchestrator/engine/planner.py:1391-1394`
  ```python
  # 项目仓库与分支隔离：注入每个绑定仓库的分支/基线/路径，由 CLI 自行判断
  # 是否需要 worktree 或切分支。后端的 prepare_worktrees 仍是可选的手动 API，
  # 这里走"让 CLI 判断"的路线。
  ```
- claude cwd 永远是主工作区：`planner.py:494`（`workspace = story.get("workspace")`）、`:783`（headless `cwd=workspace`）、`:845`（interactive PTY `cwd=workspace`）、`:960`（retry）
- build prompt 只给 advisory：`planner.py:1395-1412`（`worktree_section` 只打印分支名 + 两条示例命令，让 claude 自己决定是否隔离）
- branch_rule 渲染存库但不落地：`workspace/branch_naming.py:74`（`generate_branch_for_story`）+ `api.py:2851-2859`（start 时 `bind_story_project(branch=..., worktree_state="unprepared")`）
- start 时 workspace 设为主 repo 根（非 worktree）：`api.py:2874-2875`（`_workspace_root_for_project`）

**影响**：
- claude 直接污染 master/test 分支工作区，未上线代码混入测试汇聚分支（hc-order 的 test）。
- `branch_rule` 形同虚设——配了 `feature/{author}/{key}_{summary}_{date}` 却从没建出来。
- 多 story 并发时无隔离，改动互相覆盖。
- worktree 引擎闲置，只在用户手动点「prepare worktree」时才跑。

**修复方向**（建议，待产品确认）：
- **落点**：planner build 阶段 launch 前（`planner.py:624-693` 的 `launch` action handler 里），对每个 `db.get_story_projects(story_key)` 里 `worktree_state == "unprepared"` 的项目，调 `prepare_worktrees(story_key)`。
- **launch cwd 改成 worktree**：`sp["worktree_path"]` 替代 `workspace`（`planner.py:783,845,960`）。
- **prompt 改成确定性指令**：`worktree_section` 从 advisory 改为"你已在 worktree `<path>` 分支 `<branch>` 上，直接在此改代码"（不再是让 claude 自己判断）。
- 现成基建：`prepare_worktrees`/`_create_worktree`/`WorktreeState`/decider 全写好且测试过，**只差接进 launch 路径这一步**。

---

## BUG #19 — build 阶段不用 kimi 编码 + ClaudeAdapter 完全忽略 model 参数

**严重度**：**高**
**现象**：用户预期 build（编码）阶段用 kimi 写代码，实测 minimal profile 跑的是 claude。claude 启动命令不含 `--model` flag，用 claude 自己的默认模型。

**根因**：两个独立问题叠加。

1. **配置错位**：`minimal.yaml:34` build 阶段 `cli: claude`，**不是 kimi**。kimi 只在 `realtest.yaml:28` 配过（`cli: kimi`）。用户的意图"build 用 kimi"对应的是把 minimal 的 build `cli` 改成 `kimi`，但当前配置没这么写。
2. **ClaudeAdapter 忽略 model**：即使 profile 里写了 `model` 字段，`ClaudeAdapter` 的三个 launch 方法都**丢弃 model 参数，不加 `--model` flag**。`allowed_providers` 列表更是纯 advisory（只喂给 router LLM 的提示文本），**没有任何强制效果**。

**代码引用**：
- minimal build 用 claude（配置错位）：`entry/profiles/minimal.yaml:34`（`cli: claude`）；对比 `realtest.yaml:28`（`cli: kimi  # 用户确认:编码用 kimi`）
- ClaudeAdapter 忽略 model：`knowledge/adapters/claude.py:30-31`（`launch_cmd` 返回 `"claude"`）、`:33-58`（`interactive_launch_cmd` 无 `--model`）、`:60-68`（`headless_launch_cmd` 无 `--model`）
  ```python
  def launch_cmd(self, model: str) -> str:
      return "claude"          # model 参数被忽略
  ```
- planner 读 model 但下游丢弃：`planner.py:684-693`（`model = cfg.model` → `adapter.interactive_launch_cmd(model=model)`）—— 传了但 adapter 不用
- allowed_providers 无强制：`orchestrator/engine/router.py:29`（唯一消费点，advisory 文本）；`profile_loader.py:152`（读入 dataclass 后无人校验）
- kimi 注册为 ShellAdapter：`~/.story-lifecycle/adapters.yaml`（`kimi: {binary: kimi, launch_cmd: kimi, inject_method: stdin}`，**无 model_flag**）；`knowledge/adapters/shell.py:78-81`（ShellAdapter 支持 model_flag，但 kimi 配置没写）
- get_story_cli_model 默认 sonnet：`story_service.py:173`（`cfg.get("model", "sonnet")`）

**影响**：
- build 阶段实际跑 claude 默认模型，不是用户想要的 kimi。
- `allowed_providers` 字段给人"在约束模型选择"的错觉，实际是死的。
- 即使后续把 build `cli` 改 kimi，kimi 的 `adapters.yaml` 也没配 `model_flag`，照样不传模型。

**修复方向**（建议，待产品确认，两层）：
- **配置层**：minimal.yaml build 阶段 `cli` 改成 `kimi`（或在 profile 顶层加 `cli: kimi` for build）；kimi 的 `adapters.yaml` 补 `model_flag`（如 `--model`）+ 指定 model。
- **代码层**：`ClaudeAdapter` 的三个 launch 方法加 `--model` 支持（当 `model` 非空时 `cmd += ["--model", model]`），否则 profile 里写 model 永远不生效。顺带评估 `allowed_providers` 要么做成真约束（launch 前校验），要么从 profile 删掉避免误导。

---

## BUG #20 — story_state advance 按钮（"进入测试"）不跳终端 tab，与 #2 同类

**严重度**：**高**
**现象**：story `tapd-1144381896001066924` build 完成后停在"开发"状态闸(paused),点"完成开发，进入测试"按钮,后端成功 transition(开发→测试)+ verify launch 跑了(start_time 在 transition 后 30 秒),但**前端不跳终端 tab**(停在概览页)。实测:`status=active, current_stage=verify, lifecycle_state=测试, _active_execution={stage:verify}` 全对,但用户看不到 verify 在跑。

**根因**：与 BUG #2 完全同类。`OverviewTab.tsx:153` 的 story_state_gate 按钮 onClick 是裸调:
```tsx
onClick={() => storyApi.advanceLifecycle(storyKey)}
```
`apiAction`(client.ts:269-276)只做 fetch + 返回 boolean,既不 `setActiveTab('terminal')` 也不 invalidate query。对比 BUG #2 已修的 `handleConfirmPlan`(StoryDetailPage.tsx:134-143),它在成功后做了三件事(`refetch()` + `setActiveTab('terminal')` + 错误 alert),而 advance 路径一件都没做。

**代码引用**：
- 按钮 + 裸调 handler：`frontend/src/components/OverviewTab.tsx:151-156`
  ```tsx
  <button className="btn btn-primary"
    onClick={() => storyApi.advanceLifecycle(storyKey)}>
    {storyStateGate?.label || `进入 ${storyStateGate?.to}`} →
  </button>
  ```
- `advanceLifecycle` 只做 fetch：`frontend/src/api/client.ts:298`（`apiAction` → fetch,无后续）
- `apiAction` 返回 boolean 不做副作用：`client.ts:269-276`
- 对比 BUG #2 的正确修法(handleConfirmPlan)：`StoryDetailPage.tsx:134-143`
  ```tsx
  async function handleConfirmPlan() {
    const r = await fetch(`/api/story/${storyKey}/plan/confirm`, { method: 'POST' })
    if (r.ok) {
      refetch()
      setActiveTab('terminal')   // ← advance 路径缺这个
    } else { alert(...) }
  }
  ```
- 全前端仅 1 处 `setActiveTab('terminal')`（BUG #2 修的 handleConfirmPlan）：`StoryDetailPage.tsx:139`

**影响**：
- 每次跨 story_state(开发→测试→上线)用户点"进入下一状态"后,都看不到终端,误以为没在跑。
- 加上没有 query invalidation,前端靠 5 秒轮询刷新,卡片状态滞后。
- BUG #2 只修了"确认规划"一个入口,story_state_gate 这个入口漏了——同一类 bug 蔓延。

**修复方向**（建议）：
- 把 handler 从 OverviewTab 提到 StoryDetailPage（像 handleConfirmPlan 一样）,通过 prop 传下去（如 `onAdvanceLifecycle`）。
- 成功后：`refetch()` + `qc.invalidateQueries(['plan', storyKey])` + `qc.invalidateQueries(['sessions', storyKey])` + `setActiveTab('terminal')`。
- 失败 alert。
- 顺带审视：所有"启动执行"类按钮(确认规划 / advance stage gate / advance story state)都该统一跳终端——考虑抽一个 `handleStartAndGoTerminal` helper 避免同类 bug 再发（符合 AGENTS.md 架构审查触发器第 3 条"多个入口做相似但不一致的决策"）。

---

## BUG #21 — prompt 注入 PTY 用裸 write(无 bracketed paste)，claude Ink 输入框不提交

**严重度**：**高**
**现象**：verify 阶段(advance 后重新 spawn 的 PTY),prompt 被黏贴进了 claude 的输入框,但**没按回车执行**——claude 停在输入框含完整 prompt 文本、光标闪烁的状态,不开始干活。实测 story `tapd-...066924` verify 阶段命中。

**根因**：`ensure_agent_pty` 发 prompt 用**裸 PTY write**(`prompt + \r`),没有 bracketed paste 包裹。claude 的 Ink-based 输入框把大段裸 paste(含换行符的多行文本)当作"正在编辑的文本",`\r` 被吞或被多行结构稀释,提交不了。讽刺的是:同文件的 `clean_exit_pty` 发 `/exit` 时**用了 bracketed paste**,注释明确写了原因——"bare PTY writes are treated as a paste by claude's Ink input and never submit (claude-code#15553)"。但发 prompt 时没沿用这个教训。

**代码引用**：
- prompt 裸 write(根因)：`infra/terminal/pty.py:512-513`
  ```python
  if prompt:
      pty.write(prompt.encode("utf-8") + b"\r")   # ← 裸 write,无 bracketed paste
  ```
- 对比:clean_exit_pty 的正确做法：`pty.py:563-565`
  ```python
  pty.write(b"\x1b[200~" + b"/exit" + b"\x1b[201~")   # bracketed paste 包裹
  time.sleep(_CLEAN_EXIT_PASTE_DELAY)                   # 等 paste settle
  pty.write(b"\r")                                      # 再单独发回车提交
  ```
- clean_exit_pty 的注释自承问题：`pty.py:550-553`("bare PTY writes are treated as a paste by claude's Ink input and never submit")
- planner 调用点(传 prompt 给 ensure_agent_pty)：`planner.py:926-933`
- api.py 调用点(interactive launch terminal 路径)：`api.py:561`
- 加重因素:claude readiness_marker `❯` 永不匹配(`claude.py:20`,claude v2.1.195 prompt 是 `>`),`_wait_ready` 等 30 秒超时后才注入——注入时机也偏晚(claude 已完全就绪但等了 30 秒)

**影响**：
- 每个走 interactive PTY 路径的 stage(design/build/verify),prompt 都可能黏贴进去不执行——用户得手动到终端按回车。
- 多行 prompt(含 markdown 表格/换行)尤其严重——Ink 把换行当文本不当提交。
- readiness_marker 不匹配叠加 30 秒空等,体验差。

**修复方向**（建议，与 clean_exit_pty 对齐）：
- `ensure_agent_pty:512-513` 改成 bracketed paste 注入:
  ```python
  if prompt:
      pty.write(b"\x1b[200~" + prompt.encode("utf-8") + b"\x1b[201~")
      time.sleep(_CLEAN_EXIT_PASTE_DELAY)   # 复用 clean_exit 的 paste settle 常量
      pty.write(b"\r")
  ```
- 顺带修 readiness_marker:`claude.py:20` 的 `❯` 改成 `>`(或 `r"[>❯]"` 兼容多版本),避免 30 秒空等。

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
#14 (禁 brainstorming) — 独立，profile↔prompt 矛盾
#15 (playbook 全量)    — 独立，prompt 体积/价值
#16 (task_type 误分)   — 独立，但影响面广(知识注入+bootstrap 都吃 task_type)
#17 (产出文档不登记)   — 独立，影响所有 stage 的产物可追溯性
#18 (build 不走 worktree) — 独立，worktree 引擎现成但没接进 launch
#19 (build 不用 kimi)   — 两层:配置错位(minimal build=claude) + ClaudeAdapter 忽略 model
#20 (advance 不跳终端) — 与 #2 同类,story_state_gate 入口漏了 setActiveTab
#21 (prompt 不提交)    — 裸 write 无 bracketed paste,claude Ink 吞掉;clean_exit_pty 已知此坑但 prompt 路径没沿用
```

**建议评估优先级**：
- **高（影响正确性）**：#7、#9、#14（禁 brainstorming 与产品意图直接相反）、#17（stage 产物丢失可追溯性）、#18（build 污染主分支）、#19（build 没用对的编码工具）、#20（advance 不跳终端）、**#21（prompt 不执行,每个 interactive stage 都可能命中）**
- **中（影响质量/体验）**：#16（知识注入指向错）、#4（决定 #1/#5 走向）、#15（prompt 体积/价值，需产品拍板修复方向）
- **低/延后**：#3/#2/#8（体验/渲染）、#6（文案）、#1/#5（依赖 #4 决策）

**状态**（2026-07-14 更新）：
- 已修复：#1、#2、#5、#6、#7、#9、#10（提交 `df3d105e`）+ #14、#16、#17、#18、#19（提交 `90979d80`）+ 终端 UI #11/#12/#13
- 延后：#3（PTY 孤儿，独立评估）、#8（PTY 120 列渲染，待验证）
- 待修：#15（修复方向待拍板）、#20（已定调可动手）、**#21（已定调可动手,与 clean_exit_pty 对齐用 bracketed paste）**
