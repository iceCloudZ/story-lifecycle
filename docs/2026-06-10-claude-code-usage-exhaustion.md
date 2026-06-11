# Claude Code 用量耗尽事故复盘

日期：2026-06-11  
事故窗口：2026-06-10 至 2026-06-11 10:13（Asia/Shanghai）  
影响系统：Story Lifecycle Manager、Claude Code CLI、关联模型供应商账户

## 结论摘要

本次 Claude Code 用量耗尽不是正常人工开发产生的，也不是单纯因为一次批量导入。

直接原因是 `story-lifecycle` 同时存在以下三个缺陷：

1. `list_active_stories()` 将 `paused`、`blocked`、`waiting_subtasks` 也视为启动恢复对象。
2. 服务启动时，`recover_orphan_stories()` 会把上述故事全部提交到 4 线程执行池。
3. `wait_confirm` 将数据库状态更新为 `paused` 后，没有结束当前 LangGraph 执行，反而设置 `_next_action = "plan_stage"`。当 state 中仍有 `plan_summary` 时，路由会再次进入 executor，形成自动重跑循环。

2026-06-10 新增并使用的 `story sync --all` 一次同步了 83 个 TAPD 项，使原有状态机缺陷从少量任务问题扩大为批量用量事故。

截至止损前，4 个失控 Story 的 `execution_count` 已分别达到：

```text
130
129
128
128
```

仅 2026-06-11 00:00 至 10:13，这 4 个 Story 就产生了 511 次 headless Claude Code 执行。每次执行都会新建一个 `claude -p` 进程和独立 Claude Code 会话。

2026-06-11 10:13 左右已停止 `story_lifecycle serve` 及其派生的 headless Claude 进程。10:14 复查时，没有剩余的 `story_lifecycle serve` 或 `claude -p` 进程。

## 影响

### Claude Code 本地用量记录

对 `~/.claude/projects/**/*.jsonl` 中的 assistant usage 记录按以下键去重：

```text
sessionId + message.id
```

必须去重的原因是同一个 assistant message 的 thinking、text 和 tool-use 分片会重复携带同一份 usage。

按 Asia/Shanghai 日期汇总：

| 日期 | Assistant responses | Input tokens | Output tokens | Cache read tokens | 会话数 |
|---|---:|---:|---:|---:|---:|
| 2026-06-08 | 3,033 | 5,644,438 | 789,278 | 253,329,315 | 24 |
| 2026-06-09 | 3,497 | 5,384,755 | 1,211,098 | 292,512,654 | 30 |
| 2026-06-10 | 7,167 | 13,285,291 | 2,185,698 | 743,200,756 | 232 |

2026-06-10 相比 2026-06-09：

```text
responses       约 2.05 倍
input tokens    约 2.47 倍
output tokens   约 1.80 倍
cache read      约 2.54 倍
sessions        约 7.73 倍
```

这些数字是 Claude Code 本地 usage 元数据，不等同于供应商最终账单。不同供应商对 input、cache read 和模型映射的计费规则可能不同，因此本文不将它们直接换算为金额。

### 项目归因

2026-06-10 的项目级汇总：

| 项目 | Responses | Input tokens | Output tokens | Cache read tokens | 会话数 |
|---|---:|---:|---:|---:|---:|
| story-lifecycle | 4,477 | 9,374,016 | 1,389,746 | 324,945,078 | 216 |
| java-agent | 1,610 | 2,507,014 | 488,772 | 319,127,546 | 3 |
| hc-all | 789 | 1,045,708 | 260,719 | 77,056,132 | 6 |
| 其他 | 291 | 358,553 | 46,461 | 21,572,000 | 7 |

`story-lifecycle` 占当天普通 input tokens 的约 70.6%，并产生了 232 个会话中的 216 个，是主要用量来源。

`java-agent` 的长会话带来了较高 cache read，但不是本次会话数量暴增的主因。

### 文件与代码写入影响

本次事故不只是模型用量问题。Story Lifecycle 启动 Claude Code 时使用了以下权限：

```text
--allowedTools Bash,Read,Edit,Write,Glob,Grep
--permission-mode acceptEdits
```

因此 headless 任务具备读取仓库、执行命令、创建文档、修改代码、运行测试以及提交 Git 变更的完整能力。

对 `~/.claude/projects/D--story-lifecycle/*.jsonl` 中事故窗口内、`entrypoint = sdk-cli` 的顶层会话进行工具调用审计，并按 `tool_use.id` 去重后，确认：

| 工具/影响 | 调用数 |
|---|---:|
| 全部工具调用 | 4,213 |
| Bash | 1,157 |
| Read | 1,071 |
| Edit | 227 |
| Write | 148 |
| Write + Edit | 375 |
| 被直接写入或编辑的不同路径 | 119 |

375 次直接文件变更调用按目标分类：

| 目标分类 | Write/Edit 调用 | 不同路径 |
|---|---:|---:|
| 后端源码 | 195 | 30 |
| 测试代码 | 64 | 21 |
| 前端源码 | 51 | 17 |
| `.story/done` | 41 | 32 |
| 普通 `docs` 文档 | 8 | 6 |
| `.story/context` | 4 | 4 |
| `hc-all` 外部仓库 | 5 | 5 |
| 其他及 Claude plan | 7 | 4 |

已确认的高频写入目标包括：

```text
src/story_lifecycle/orchestrator/api.py                       36 次 Edit
src/story_lifecycle/validators/contact_reachability.py       16 次 Write/Edit
src/story_lifecycle/validators/email_validator.py            15 次 Write/Edit
src/story_lifecycle/orchestrator/project_scan.py              14 次 Write/Edit
src/story_lifecycle/orchestrator/project_profile.py           14 次 Write/Edit
frontend/src/App.tsx                                           8 次 Edit
frontend/src/api/client.ts                                     5 次 Edit
```

日志还确认一次自动任务越过当前仓库，直接修改了 `D:\hc-all` 下 5 个前端文件。这说明当 prompt 中出现其他仓库路径时，当前执行方式没有 workspace 边界保护。

自动会话不只修改工作树，也实际执行了 Git 提交。工具结果中可见：

```text
15 files changed, 1824 insertions(+)
6 files changed, 2070 insertions(+), 146 deletions(-)
4 files changed, 573 insertions(+)
```

当前 Git 历史中至少有以下明确带 `Story:` 和 Claude 联署的自动提交：

```text
7ecf712  Story tapd-1144381896001065601
4d122cb  Story tapd-1144381896001066171
4b00f0f  Story tapd-1144381896001066171
```

这些事实证明，批量自动执行阶段真实生成了文档、修改了前后端代码和测试，并创建了提交，不只是扫描代码或消耗 token。

需要区分两个阶段：

1. 2026-06-10 下午至 23:48 的批量自动执行存在大量明确 Write/Edit 和 Git commit 证据。
2. 2026-06-11 02:41 至 10:13 的 4 个 Story 高频循环有 511 次重复 headless 启动证据，但现有日志不能证明这 511 次每一次都修改了文件。该阶段可以确认持续耗量，不能表述为“每轮都改代码”。

工具调用记录证明写操作曾被执行，但不能单独证明每个中间版本最终仍保留在工作树中；部分内容随后可能被覆盖、格式化、提交或冲突合并。

### 共享工作区交叉污染

事故还造成了不同 Story 和不同 feature branch 之间的工作区污染，不只是单个任务重复执行。

4 个进入高频循环的 Story 为：

| Story | 最终 execution_count | 事故窗口内 execute 事件 |
|---|---:|---:|
| `tapd-1144381896001065824` | 130 | 129 |
| `tapd-1144381896001065822` | 129 | 128 |
| `tapd-1144381896001065843` | 128 | 127 |
| `tapd-1144381896001065825` | 128 | 127 |

Git reflog 和提交记录确认了以下共享 checkout 操作：

```text
2026-06-11 02:38:55  从 main 切到 feature/tapd-1144381896001065549
2026-06-11 02:41:28  提交 e1353ee
2026-06-11 02:43:09  创建 refs/stash
2026-06-11 02:43:10  切到 feature/tapd-1144381896001065516
```

提交 `e1353ee` 位于分支 `feature/tapd-1144381896001065549`，但提交正文标记：

```text
Story: tapd-1144381896001065516
```

这证明 Story 与当前分支没有形成强绑定，自动任务可以在错误的 feature branch 上提交另一个 Story 的代码。

02:43 创建的 stash 又包含：

```text
frontend/src/api/client.ts
src/story_lifecycle/orchestrator/api.py
src/story_lifecycle/validators/name_validator.py
tests/test_name_validator.py
```

其中姓名校验此前已由另一个自动分支 `feature/tapd-1144381896001065338` 在提交 `5461010` 中实现过。当前 stash 保存的是另一套不同规模的实现：

```text
提交 5461010:  name_validator.py 212 行，test_name_validator.py 240 行
02:43 stash:   name_validator.py 173 行，test_name_validator.py 409 行
```

当前主工作区因此留下：

```text
UU frontend/src/api/client.ts
MM src/story_lifecycle/orchestrator/api.py
A  src/story_lifecycle/validators/name_validator.py
A  tests/test_name_validator.py
```

这组残留能够解释姓名校验为什么表现为“只写了校验器和测试、核心集成没接完”：它不是一个完整提交，而是并发自动任务在共享 checkout 中 stash、切分支和恢复变更后留下的混合中间态。

影响结论：

1. 自动任务会修改真实代码并提交。
2. 多个 Story 共用同一 checkout，分支和 Story 没有隔离。
3. stash/checkout 把不同 Story 的改动混合，产生未合并冲突和半成品。
4. 仅修复 headless 重试循环不能消除该风险，必须为每个 Story 使用独立 worktree，并校验 Story、branch、workspace 三者绑定关系。

### 模型分布

2026-06-10 的 Claude Code 本地记录同时包含多个模型映射：

| 模型 | Responses | Input tokens | Output tokens | Cache read tokens | 会话数 |
|---|---:|---:|---:|---:|---:|
| deepseek-v4-pro | 3,242 | 6,529,217 | 1,298,280 | 474,463,488 | 96 |
| glm-5.1 | 3,355 | 5,455,818 | 839,580 | 251,547,840 | 60 |
| glm-4.5-air | 395 | 720,390 | 47,838 | 14,511,156 | 9 |
| deepseek-v4-flash | 85 | 579,866 | 0 | 2,678,272 | 未单独归因 |

当天存在模型供应商或模型映射切换，但模型切换只能解释用量分布，不能解释 `story-lifecycle` 为什么创建了 216 个独立会话。会话创建源仍然是 Story Lifecycle executor。

### 已排除的主要假设

#### 不是 1M context 意外开启

本地配置中 `CLAUDE_CODE_DISABLE_1M_CONTEXT=1`，没有证据表明此次事故由 1M context 模式导致。

#### 不是 subagent 数量突然暴增

去重后的 subagent response 数：

```text
2026-06-09: 1,021
2026-06-10:   775
```

6 月 10 日的 subagent response 反而低于前一天。异常增长来自大量独立 `sdk-cli` 会话，而不是单个主会话无限派生 subagent。

#### 不是单一超长人工会话

`java-agent` 确实存在少量长会话和较高 cache read，但 6 月 10 日总会话数由 30 增加到 232，且新增会话主要属于 `story-lifecycle`。因此单一人工会话不是主要原因。

#### 新会话启动成本是放大因素，不是触发原因

抽样的 Story Lifecycle 新会话首轮 input 分别约为 29,598 和 31,339 tokens。系统提示、插件、skills 和项目上下文会在每个新会话中重复注入。

因此 216 个独立会话即使尚未进行实际编码，也会产生显著启动成本。但创建这些会话的根因仍是错误恢复和 `wait_confirm` 循环。

## 时间线

所有时间均为 Asia/Shanghai。

### 2026-06-09

- 14:53，提交 `5db245b` 删除 `wait_confirm` 中的 LangGraph `interrupt()`。
- 删除 interrupt 后，暂停分支不再真正阻塞图执行。
- 原有的 `_next_action = "plan_stage"` 被保留，使 `wait_confirm` 具备立即重新进入执行链的条件。

### 2026-06-10

- 14:32，提交 `1b5702c` 新增 `story sync --all`，允许忽略 TAPD 状态过滤进行全量同步。
- 18 点，数据库新增 83 个 TAPD Story：

```text
43 个 subtask
20 个 bug
20 个 story
```

- 18:39 至 18:40，83 个 Story 首次进入执行记录。
- 当天 Story Lifecycle 数据库记录 142 次 executor 执行，涉及 84 个 Story。
- Claude Code 本地记录显示，17:00、18:00、20:00 是主要用量峰值。
- 23:09，发现的当前 `story_lifecycle serve` 进程启动。

### 2026-06-11

- 02:41 至 02:47，4 个 Story 开始进入高频重复执行。
- 每轮日志均显示：

```text
HeadlessNoDoneFile
-> execution_count_exceeded
-> wait_confirm
-> Gate blocked at design. Manual decision required.
-> 再次 execute
```

- 10:10，现场同时存在 4 个由 `story_lifecycle serve` 直接派生的 `claude -p` 任务。
- 10:13，执行次数最高的 Story 已达到第 130 次执行。
- 10:13 左右，停止 `story_lifecycle serve` 进程树。
- 10:14，复查确认没有剩余 `story_lifecycle serve` 或 `claude -p`。

## 排查链路

### 1. 排除单一人工会话

首先统计 Claude Code JSONL usage，并按项目、小时、模型和会话拆分。

发现 2026-06-10 的主要异常不是单个超长会话，而是会话数从前一天的 30 个增加到 232 个。其中 `story-lifecycle` 产生 216 个会话。

这将排查方向从“某次人工对话上下文过大”转向“自动化系统批量创建独立会话”。

### 2. 从会话入口识别自动任务

`story-lifecycle` 相关会话中，大部分 entrypoint 为 `sdk-cli`，用户 prompt 呈现统一批处理格式，例如：

```text
对需求进行分析与方案设计
根据设计文档进行编码实现
已重试 1 次
第 2 次
本次最后一次
```

这些会话不是普通交互式 Claude Code 对话，而是 Story Lifecycle 阶段执行器启动的 headless 任务。

### 3. 检查运行进程

现场进程树显示：

```text
python.exe -m story_lifecycle serve
  -> cmd.exe /c claude.cmd -p ...
     -> claude.exe -p ...
```

同一时刻存在 4 组上述进程，与代码中的线程池大小一致：

```python
_executor = ThreadPoolExecutor(max_workers=4)
```

这证明 headless Claude 任务由 Story Lifecycle 服务直接启动。

### 4. 检查数据库状态

事故现场数据库状态：

```text
completed: 54
blocked:   18
paused:    12
active:     0
```

虽然没有任何 `active` Story，服务仍然启动了 Claude 任务。

继续检查 `list_active_stories()`，发现查询条件为：

```sql
SELECT *
FROM story
WHERE status IN ('active', 'paused', 'blocked', 'waiting_subtasks')
ORDER BY updated_at DESC
```

因此函数名和实际语义不一致。它不是“列出可自动恢复的 active Story”，而是“列出所有未结束 Story”。

### 5. 追踪启动恢复路径

FastAPI lifespan 在每次服务启动时调用：

```text
recover_orphan_stories()
-> db.list_active_stories()
-> resume_story_async(story_key)
-> ThreadPoolExecutor.submit(resume_story, story_key)
-> compiled.invoke(initial_state, config)
```

`resume_story()` 没有拒绝 `paused` 或 `blocked` 状态。因此数据库中的人工暂停和失败阻塞状态都被重新送入状态机。

### 6. 追踪 wait_confirm 路由

`_do_wait_confirm()` 正确执行了部分暂停动作：

```text
DB status = paused
state status = paused
写入 gate decision
写入 gate report
```

但函数末尾执行：

```python
state["_next_action"] = "plan_stage"
return state
```

图中的 `route_from_router()` 原样返回 `_next_action`，所以当前 LangGraph invocation 没有结束，而是立即重新进入 `plan_stage`。

当 `execution_count` 已超过上限时，`plan_stage_node()` 会再次设置：

```text
_pre_routed_action = wait_confirm
last_error = 执行次数已达上限
```

但 `route_after_plan()` 只有在 `last_error` 存在且 `plan_summary` 不存在时才返回 router：

```python
if state.get("last_error") and not state.get("plan_summary"):
    return "router"
return "execute_and_wait"
```

失控 Story 保留了旧的 `plan_summary`，因此即使状态为 paused、执行次数已超过上限，路由仍进入 `execute_and_wait`，再次启动 Claude。

完整循环为：

```text
execute_and_wait
-> HeadlessNoDoneFile
-> router: execution_count_exceeded
-> wait_confirm
-> DB 写 paused
-> _next_action = plan_stage
-> plan_stage 保留旧 plan_summary
-> route_after_plan = execute_and_wait
-> 再次启动 claude -p
```

### 7. 用事件日志验证循环

4 个主要失控 Story 的事件日志持续重复以下序列：

```text
node_error: HeadlessNoDoneFile
router: wait_confirm / execution_count_exceeded
route_decision: wait_confirm
gate_decision: review_unavailable
prompt_context
execute
```

其中 `retry_limit` 为 2 或 3，但实际执行次数超过 120，证明 retry/gate 上限没有形成 hard stop。

## 代码证据

### 错误的恢复集合

文件：`src/story_lifecycle/db/models.py`

```python
def list_active_stories() -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM story
           WHERE status IN ('active', 'paused', 'blocked', 'waiting_subtasks')
           ORDER BY updated_at DESC"""
    ).fetchall()
```

该条件由提交 `fc67b3e` 于 2026-05-22 引入。

### 启动时批量恢复

文件：`src/story_lifecycle/orchestrator/graph.py`

```python
def recover_orphan_stories():
    stories = db.list_active_stories()
    for s in stories:
        resume_story_async(s["story_key"])
```

异步批量恢复由提交 `8f4b3fd` 于 2026-05-25 引入。

### 并发执行 headless Claude

文件：`src/story_lifecycle/orchestrator/graph.py`

```python
_executor = ThreadPoolExecutor(max_workers=4)
```

文件：`src/story_lifecycle/adapters/claude.py`

```python
return [
    resolve_executable("claude"),
    "-p",
    "--allowedTools",
    "Bash,Read,Edit,Write,Glob,Grep",
    "--permission-mode",
    "acceptEdits",
]
```

### wait_confirm 未结束图执行

文件：`src/story_lifecycle/orchestrator/nodes/graph_nodes.py`

```python
db.update_story(key, status="paused", last_error=gd.human_message)
state["status"] = "paused"
state["_next_action"] = "plan_stage"
return state
```

提交 `5db245b` 于 2026-06-09 删除了该分支原有的 `interrupt()`。提交说明称其为移除 TUI 耦合，但没有同步建立新的“暂停即终止当前 invocation”语义。

### 批量放大器

提交 `1b5702c` 于 2026-06-10 新增：

```text
story sync --all
```

它允许拉取全部 TAPD 需求和缺陷。同步服务将新记录默认写为 `active`，此次执行新增 83 个 Story。

`story sync --all` 本身不是死循环根因，但它显著扩大了错误恢复和错误路由的影响范围。

## 数据库证据

### 按本地日期统计 executor 次数

```sql
SELECT
    date(datetime(created_at, '+8 hours')) AS local_date,
    count(*) AS executions,
    count(DISTINCT story_key) AS stories
FROM event_log
WHERE event_type = 'execute'
GROUP BY local_date
ORDER BY local_date DESC;
```

事故时结果：

```text
2026-06-10: 142 executions, 84 stories
2026-06-11: 588 executions, 45 stories
```

2026-06-11 的 588 次中，511 次来自 4 个失控 Story。

### 查找异常高执行次数

```sql
SELECT
    story_key,
    status,
    current_stage,
    execution_count,
    updated_at,
    last_error
FROM story
ORDER BY execution_count DESC
LIMIT 20;
```

### 验证 wait_confirm 后再次执行

```sql
SELECT
    story_key,
    stage,
    event_type,
    payload,
    created_at
FROM event_log
WHERE story_key = ?
ORDER BY id DESC
LIMIT 30;
```

## 根因分类

### 根因一：恢复集合建模错误

`active`、`paused`、`blocked` 和 `waiting_subtasks` 的恢复语义不同，却由同一个 `list_active_stories()` 查询统一返回。

数据库展示需要“所有未结束故事”，启动恢复只需要“进程异常退出前确实正在执行、且允许自动恢复的故事”。这两个概念被混为一谈。

### 根因二：wait_confirm 没有 hard stop

`wait_confirm` 只更新了数据库展示状态，没有终止当前图执行。数据库事实和执行器事实因此发生分裂：

```text
数据库：paused
LangGraph：仍在运行
Claude CLI：继续启动
```

### 根因三：路由未将 paused 作为终止条件

`route_after_plan()`、`route_from_router()` 等路由主要检查 `_next_action`、`last_error` 和 `plan_summary`，没有统一检查 `status == paused/blocked`。

旧 `plan_summary` 进一步绕过了 error 路由，使超过 retry limit 的任务仍进入 executor。

### 放大因素

- `story sync --all` 一次引入 83 个可执行记录。
- 启动恢复将所有未结束状态批量提交。
- 线程池持续保持 4 个并发任务。
- 每次重跑创建新的 Claude Code 会话，无法复用已有上下文。
- 系统只有 retry limit 配置，没有进程级、Story 级或每日 token/call hard budget。
- headless timeout 为 3600 秒，但没有最大总执行次数的独立熔断器。

## 已确认事实与推断边界

### 已确认事实

- Claude Code 本地日志中，2026-06-10 会话数和 token 用量显著增加。
- `story-lifecycle` 是当天主要 input token 和会话来源。
- Story Lifecycle 直接派生了并发 `claude -p` 进程。
- 数据库没有 `active` Story 时，仍有 `paused` Story 被执行。
- 4 个 Story 的执行次数超过 120。
- 每轮事件日志均记录 `wait_confirm`，随后又记录新的 `execute`。
- 代码中 `wait_confirm` 返回 `plan_stage`，启动恢复包含 paused/blocked。
- 停止 Story Lifecycle 服务后，headless Claude 进程消失。
- 自动 `sdk-cli` 会话执行了 375 次 Write/Edit，涉及 119 个不同路径。
- 自动会话修改了后端、前端、测试和文档，并成功执行了 Git 提交。
- 至少一次自动会话越过 `story-lifecycle` workspace，修改了 `D:\hc-all` 下的文件。

### 合理推断

- 供应商用量耗尽主要由 Story Lifecycle 的批量独立会话和自动循环造成。
- 如果供应商按 UTC 账期统计，北京时间 2026-06-11 00:00 至 08:00 的用量可能仍显示在供应商的 2026-06-10 账期中。

### 当前无法从本地日志直接证明

- 供应商最终对每类 token 的精确计费金额。
- 供应商是否对 cache read token 使用特殊折扣或不同统计方式。
- 2026-06-10 18:39 首批 83 个 Story 是由哪一次具体 UI、CLI 或服务重启动作提交。数据库和事件日志能证明它们在该时间集中创建并执行，但缺少完整的历史进程审计日志。

## 止损动作

2026-06-11 10:13 左右执行：

1. 识别 `python -m story_lifecycle serve` 的进程树。
2. 仅停止该服务及其子进程。
3. 未停止人工打开的 Claude Code 会话和其他开发服务。
4. 复查以下进程均不存在：

```text
python -m story_lifecycle serve
claude -p
```

在代码修复前，不应重新启动 `story_lifecycle serve`。否则 paused/blocked Story 仍可能被自动恢复并再次进入循环。

## 待修复项

本文仅记录事故事实，尚未实施代码修复。最低修复范围应包括：

1. 将“列表展示集合”和“允许启动恢复集合”拆成不同查询。
2. 启动恢复只能恢复明确标记为可恢复运行态的 Story。
3. `wait_confirm` 必须结束当前 graph invocation，不得返回 executor 路径。
4. 所有 graph route 在进入 executor 前统一拒绝 `paused`、`blocked`、`completed`、`aborted` 等非执行态。
5. `resume_story()` 必须校验数据库状态和显式人工恢复意图。
6. 增加 Story 级绝对执行次数 hard stop，不能只依赖路由建议。
7. 增加全局并发、每日调用次数或 token budget 熔断。
8. 为以下场景增加回归测试：

```text
paused Story 在服务重启后不自动执行
blocked Story 在服务重启后不自动执行
wait_confirm 后 invocation 结束
execution_count 超过上限后不再启动 CLI
保留 plan_summary 时，last_error 仍禁止进入 executor
批量同步不会自动执行全部历史 TAPD 项
```

9. 文件系统写入安全必须作为独立控制面实现，不能只依赖“交互模式”或 prompt 约束：

```text
每个 Story 默认在独立 git worktree 中执行
启动前拒绝已有未归属改动的 dirty workspace
拒绝 Write/Edit/Bash 修改 workspace 根目录之外的路径
执行前后记录 git status、diff stat 和变更文件清单
默认禁止自动 git commit/push，只有显式 profile 能力才允许
设置单次执行的最大改动文件数和最大新增行数
Story 暂停、阻塞或超预算时终止对应进程并冻结 worktree
```

交互式 PTY 修复解决的是“普通 Story 不应重复启动不可见 headless Claude”，并不自动解决文件写入隔离。两者必须分别验证。

## 安全说明

排查过程中读取了本地 Claude Code 配置和 usage 日志。本文没有记录 API key、访问令牌、密码、Authorization header 或供应商密钥。
