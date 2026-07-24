# DESIGN — Session / PTY ID 模型与生命周期(架构审查)

> **触发**:session/PTY 边界连续出现第二个相关 bug。第一个是 `status` 值不匹配(前端查 `'running'`、DB 返回 `'active'`);第二个是「点了启动 CLI 没反应」(前端拿 DB 的 uuid 去连终端,但存活进程只在 PTY 注册表里,两套 ID 对不上)。
>
> **核心诉求**:**保证 CLI resume 正常** —— 同一个 (story, stage) 重试/崩溃后,必须能 resume 续上历史,不能因为 ID 不一致导致续不上。
>
> 按 AGENTS.md「架构审查触发」规则,第三次相关 bug 前先做状态机/协议设计。本文是那张设计图。
>
> **状态**:设计阶段,未动代码。读完按「迁移路径」分步实施。

---

## 0. TL;DR

**根因**:一个逻辑上的「(story, stage) 的 agent 会话」被两套**永不交叉**的存储跟踪,且 ID 体系完全不同:

| 存储 | ID | 含义 | 进程死后 |
|---|---|---|---|
| DB `story_session` 表 | `uuid5(story:{stage})` 或 kimi 捕获的 `session_<uuid>` | resume 持久化记录 | **还在**(为了 resume) |
| 内存 `_ptys` 注册表 | `pty-{story_id}-{n}`(全局递增计数器) | 活进程句柄 | **还在**(无 reaper,累积) |

两套 ID 从不映射。**所有 8 个问题都是这个分裂的下游症状。** 解法:让 PTY 注册表改用与 DB 同源的 session ID(`uuid5(story:stage:adapter)`),让「(story, stage, adapter) → 唯一 session ID」贯穿 DB / PTY / WS / 前端四层,并补上存活态查询与 cleanup 机制。

**resume 的命脉**(问题 4,最致命):同一个 stage,交互式 spawn 和自动循环 spawn 用**不同的输入串**算 uuid5,产出的 session ID 不同 → `get_session` 查不到 → resume 续不上。修 ID 模型时必须同时统一 uuid5 输入串。

---

## 1. 现状(事实,不带评判)

### 1.1 三者关系(确认你的心智模型)

```
story (工作单, DB story 表)
  └─ stage = design/build/verify (profile 定义, 一个 stage 一个 code cli)
       └─ code cli = claude/kimi/codex (adapter, 真正干活的 AI)
            └─ pty = ManagedPty (承载 cli 的伪终端进程)
```

关系 **story : stage : code cli : pty = 1 : N : 1 : 1**。一个 stage 对应一个 code cli,跑在一个 pty 里。`UNIQUE(story_key, stage, adapter)`(models.py:573)是这条链的字面强制。

### 1.2 两套存储的字段级映射

**PTY 注册表**(`infra/terminal/pty.py:384-389`):

```python
# _ptys: story_id → { session_id → ManagedPty }
_ptys: dict[str, dict[str, ManagedPty]] = {}
_session_counter = 0  # 模块级全局,永不重置

def _next_session_id(story_id: str) -> str:
    global _session_counter
    _session_counter += 1
    return f"pty-{story_id}-{_session_counter}"
```

- 外层 key = `story_id`(真正的 story key)
- 内层 key = `pty-{story_id}-{n}`(**不是** story key,是递增计数)
- value = `ManagedPty`(活进程句柄)
- `ManagedPty.alive` = 实时进程存活态(`_process.isalive()`,pty.py:298)

**DB `story_session` 表**(`infra/db/models.py:557-579`):

```sql
CREATE TABLE story_session (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    story_key   TEXT NOT NULL,
    stage       TEXT NOT NULL,
    adapter     TEXT NOT NULL,
    session_id  TEXT,                          -- uuid5 或 kimi 捕获
    status      TEXT NOT NULL DEFAULT 'active',-- 静态:'active'/'completed'
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (story_key, stage, adapter)
)
```

- 一行 = 一个 (story, stage, adapter) 的会话记录
- `session_id` = `uuid5(story:{stage})`(claude)或 kimi 从 banner 捕获的 `session_<uuid>`
- `status` = 静态字段,只在新写时 `'active'`,stage 完成时 `complete_session` 改 `'completed'`。**永不反映进程实时存活**。

### 1.3 spawn 时两套存储怎么写

`_spawn_story_agent_pty`(`orchestrator/service/api.py:676-779`)是 spawn 的机械核心。关键顺序:

```python
session_uuid = str(uuid5(NAMESPACE_DNS, f"{story_key}:{stage}"))   # [A] 算 DB 的 uuid(2字段)
_use_sid = _db_row["session_id"] if (_db_row and _db_row.get("session_id")) else session_uuid
spec = adapter.start_session(model, prompt=..., session_id=_use_sid, ...)  # [B] 喂给 cli(--session-id/-S)
session_id, pty = ensure_agent_pty(story_key, spec.command, ...)           # [C] 注册 PTY,key=pty-{n}
if not is_resume:
    db.upsert_session(story_key, stage, _adapter_name,
                      session_id=session_uuid if _adapter_name == "claude" else None)  # [D] 写 DB 行
return session_id, pty, is_resume    # [E] 返回 PTY 的 pty-{n},不是 DB 的 uuid
```

**致命分裂点**:
- [C] 注册 PTY 用 `pty-{n}`
- [D] 写 DB 用 `uuid5`
- [E] 返回前端的是 `pty-{n}`

所以**同一个物理会话,在 PTY 注册表里叫 `pty-X-7`,在 DB 里叫 `uuid5(...)`,返回前端的是 `pty-X-7`,但下次 GET /sessions 返回的是 DB 的 uuid**。前端拿着 uuid 去连 WS,`get_pty` 按 `pty-{n}` 查 → 查不到。

---

## 2. 问题清单(8 个,按严重度)

### 问题 1 — 两套 ID 永不对齐【根因】

PTY 注册表 key = `pty-{story_id}-{n}`(pty.py:392),DB session_id = `uuid5(story:{stage})` 或 kimi 捕获(api.py:713)。**两套 ID 无映射函数**,从不相等。

**影响**:WS attach、kill、list 全部受影响。所有下游问题的源头。

### 问题 2 — `api_list_sessions` 注释撒谎,没查存活态【直接 bug】

`api.py:369-402` 的 docstring 声称「再用 PTY 的存活态覆盖 status」,但代码实际:

```python
result.append({
    ...
    "status": row.get("status", "active"),   # ← 直接吐 DB 静态 status,从不查 PTY alive
})
```

PTY 列表只在「PTY 有而 DB 无」时 append 兜底(api.py:398-401),从不覆盖 DB 行的 status。

**影响**:死进程的 DB 行永远显示 `active`,前端以为还活着,连 WS 拿到 4404,重连循环。这正是 `PTY_WEBSOCKET_RECONNECTION_DESIGN.md` 记录的故障模式的根。

### 问题 3 — 同一物理会话在列表里出现两次

`api.py:398-401`:

```python
db_sids = {r["session_id"] for r in result}   # uuid 集合
for p in pty_sessions:
    if p["session_id"] not in db_sids:         # pty-{n} 永远不在 uuid 集合里
        result.append(p)                        # → 每个 PTY 行都被 append
```

`pty-{n}` 永远不等于任何 uuid,所以**每个活进程都额外 append 一行**(DB uuid 行 + PTY `pty-{n}` 行,同一个物理会话两条记录)。

**影响**:前端 `find(s => s.status === 'running')` 可能选中 PTY 行(显示活着),但 `stage === sel` 过滤又可能选中 DB 行(显示 active),状态自相矛盾。

### 问题 4 — uuid5 输入串不一致【直接破坏 resume,最致命】

**这是核心诉求(resume)的直接威胁。**

同一个 (story, stage) 的 claude session id,两条 spawn 路径用**不同输入串**算 uuid5:

| 路径 | 位置 | 输入串 | 字段数 |
|---|---|---|---|
| 交互式(api 路径) | `api.py:713` | `f"{story_key}:{stage}"` | 2 |
| 自动循环(planner 路径) | `planner.py:1170` | `f"{story_key}:{stage}:{adapter_name}"` | 3 |

`uuid5` 是确定性的,输入串不同 → **产出不同 uuid**。

**场景**:用户在 Web 板上交互式启动 design 阶段的 claude(走 api 路径,DB 写入 uuid5(story:design))。之后自动循环接管,想 resume 这个 stage(planner 路径算 uuid5(story:design:claude))→ `get_session` 查 DB 查不到(键不同)→ 判定「无历史」→ 当新会话 spawn → **历史丢失,不能 resume**。

反之亦然。只要会话跨 api/planner 两条路径,resume 必断。

### 问题 5 — 复用分支读不存在的属性(潜伏的 AttributeError)

`_ensure_story_agent_pty`(`api.py:808-820`)复用分支:

```python
existing = get_pty(story["story_key"])    # 无 session_id → 返回第一个 alive 的
reused = bool(existing and existing.alive and existing.purpose == "agent")
if reused:
    return {..., "session_id": existing.session_id}   # ← ManagedPty 无 .session_id 属性!
```

`ManagedPty` 只有 `self.story_id`(pty.py:152,且存的是 session_id 字符串,不是 story key —— 见问题 8)。`existing.session_id` 会 `AttributeError`。

**影响**:这个「复用已有 PTY」的分支**从未真正工作过**(否则早炸)。说明复用逻辑是死代码,每次都走新建分支,也是 PTY 死条目累积的助推(问题 6)。

### 问题 6 — 进程自然死亡后 `_ptys` 条目永不清理

**无 reaper**。进程死亡时:
- `ManagedPty._alive` 在读循环里翻 `False`(pty.py:241-246)
- **但条目不从 `_ptys` 移除**

移除只在显式调用时发生:`kill_pty`(pty.py:522)、`cleanup_all`(pty.py:579)、`atexit`(pty.py:600)。

**影响**:死条目无限累积。`list_pty_sessions` 全返回(包括死的),前端列表越来越乱。

### 问题 7 — `clean_exit_pty` 杀进程但不移除条目

planner stage 完成时(`planner.py:1644`):

```python
clean_exit_pty(_agent_pty)   # 让 cli /exit 干净退出 + flush 转录
_agent_pty.kill()            # 杀进程
# ← 但不调 kill_pty(story_key, session_id),不从 _ptys 移除
```

问题 6 的具体来源之一:每次 stage 完成留一个死条目。

### 问题 8 — `ManagedPty` 构造参数命名谎言

`ManagedPty.__init__` 第一参数叫 `story_id`(pty.py:152),但 `spawn_pty` 传进去的是 **session_id**(`pty-{n}` 字符串):

```python
# spawn_pty (pty.py:398-412)
pty = ManagedPty(session_id, command, cwd, env, purpose=purpose)
                 ^^^^^^^^^^^ 这个值叫 session_id,但构造器参数叫 story_id
```

所以 `pty.story_id` 实际存的是 `pty-{n}`,不是 story key。线程名 `pty-read-{self.story_id}`(pty.py:188)也跟着错。

**影响**:直接导致问题 5(`existing.session_id` 去找不存在的属性,因为真值存在 `existing.story_id` 里)。是问题 5 的共犯。

---

## 3. 目标设计:单一 Session ID 模型

### 3.1 核心原则

**一个 (story_key, stage, adapter) → 一个 session ID,贯穿四层(DB / PTY / WS / 前端)。**

不再有「PTY 的 ID」和「DB 的 ID」两套。PTY 注册表的内层 key 直接用 DB 同源的 session ID。

### 3.2 统一的 session ID 生成

```
session_id = uuid5(NAMESPACE_DNS, f"{story_key}:{stage}:{adapter}")
```

**三个字段,必须三字段,全仓库唯一一处生成。** 这同时修问题 4 —— 消除 2字段/3字段 的输入串分歧。

**为什么含 adapter**:同一个 stage 理论上可能换 adapter(用户在 plan UI 把 design 的 claude 改 kimi)。`UNIQUE(story_key, stage, adapter)` 已经允许同 stage 多 adapter 共存(不同 cli 各自的历史)。三字段 ID 与 DB 唯一约束对齐。

**kimi 的特殊性**:kimi 的 session_id 是 CLI 启动后从 banner 捕获的(`session_<uuid>`,非 uuid5),之前 DB 先写 placeholder(`session_id=None`),捕获后 `set_session_id` 回填。

**目标处理**:
- PTY 注册表的 key **仍然用 uuid5(story:stage:adapter)**(确定性,spawn 前就能算),不用 kimi 捕获值。
- DB 的 `session_id` 列:claude 存 uuid5;kimi 仍存捕获值(给 cli `-S` 用)。
- **PTY key 与 DB session_id 解耦**:PTY key 是后端内部句柄(确定性 uuid5),DB session_id 是喂给 cli 的值(claude=uuid5,kimi=捕获值)。两者通过 (story, stage, adapter) 三元组关联,不强求字符串相等。

> 这是关键设计决策:不强求 PTY key == DB session_id(因为 kimi 做不到),而是让两者都**从 (story, stage, adapter) 派生**,PTY 永远能通过这个三元组找到对应的 DB 行,反之亦然。

### 3.3 目标状态机:session 生命周期

```
                    ┌─────────────────────────────────────────┐
                    │                                         │
                    ▼                                         │
[absent] ──spawn──▶ [alive] ──process exits──▶ [dead] ──reaper──▶ [absent]
                      │  │                        │
                      │  └──kill (force)─────────┘
                      │
                      └──stage complete──▶ clean_exit ──▶ [dead] ──reaper──▶ [absent]

DB 视角(独立轴,只反映业务语义,不反映进程态):
[no row] ──spawn(new)──▶ [active] ──stage complete──▶ [completed]
                              │
                              └──resume spawn(读这里决定 is_resume)
```

**关键分离**:
- **PTY 注册表** = 进程态(`alive`/`dead`),有 reaper 自动清理 `dead` → 移出 `_ptys`。
- **DB story_session** = 业务态(`active`/`completed`),持久,给 resume 判定用,`completed` 不代表进程死(同 stage 仍可 resume 续历史)。
- **前端 status 字段** = 从 PTY alive 实时派生(`running`/`exited`),不再读 DB 的 `active`/`completed`。

### 3.4 存活态查询协议(修问题 2、3)

`api_list_sessions` 改为**以 DB 行为主,PTY 存活态覆盖 status,且合并去重**:

```python
def api_list_sessions(story_key):
    db_sessions = db.list_sessions_for_story(story_key)   # uuid/kimi-id + 业务态
    # 建 (stage, adapter) → PTY 映射(PTY key 是 uuid5(story:stage:adapter),
    # 但 list_pty_sessions 要带上 stage/adapter 元信息,见下)
    pty_by_key = {(p["stage"], p["adapter"]): p for p in list_pty_sessions(story_key)}
    result = []
    for row in db_sessions:
        key = (row["stage"], row["adapter"])
        pty = pty_by_key.get(key)
        result.append({
            "session_id": row["session_id"],       # cli 用的值(claude uuid / kimi 捕获)
            "pty_key": <uuid5(story:stage:adapter)>,  # 新增:前端连 WS 用这个
            "adapter": row["adapter"],
            "stage": row["stage"],
            "status": ("running" if pty and pty["alive"] else "exited"),  # 实时!
            "started_at": row["created_at"],
        })
    return {"sessions": result}
```

**要点**:
- `list_pty_sessions` 必须返回 `stage`/`adapter`/`pty_key`(目前硬编码 `stage=""`,问题根源)。PTY 注册时要把 (story, stage, adapter) 三元组记下来。
- status **实时从 PTY alive 派生**,不读 DB 静态值。
- 不再 append PTY-only 行(去重,修问题 3)。

### 3.5 resume 协议保证(核心诉求)

**不变量**:同一个 (story_key, stage, adapter),无论走哪条 spawn 路径(api 交互式 / planner 自动循环),算出的 session ID **必须相同**。

**实现**:
1. session ID 生成逻辑**抽成一个唯一函数** `compute_session_id(story_key, stage, adapter)`,放 `infra/db/models.py` 或独立模块。
2. `api.py:713` 和 `planner.py:1170` **都改调这个函数**,删掉各自的内联 uuid5。
3. 输入串统一为三字段 `f"{story_key}:{stage}:{adapter}"`。
4. 加回归测试:两条路径对同一 (story, stage, adapter) 算出相同 ID。

**kimi resume**:kimi 的 cli session_id 是捕获的,但 resume 判定仍走 DB(`get_session` 查 (story,stage,adapter))。只要 DB 唯一约束 + 三元组查询正确,kimi resume 不受 ID 模型影响。捕获值的回填路径(`_capture_kimi_session` → `set_session_id`)不变。

### 3.6 cleanup / reaper 机制(修问题 6、7)

**新增轻量 reaper**:PTY 注册表加一个后台清理,周期扫描 `dead`(alive=False)条目并移除。或者更简单 —— 在每次 `list_pty_sessions` / `get_pty` 调用时顺手清 dead 条目(lazy reaping)。

**`clean_exit_pty` 后必须 `kill_pty`**:`planner.py:1644` 的 stage 完成路径,杀进程后调 `kill_pty(story_key, pty_key)` 从注册表移除。

### 3.7 前端 status 词汇表统一

`TerminalTab.tsx` 的 `Session.status` 改成联合类型:

```ts
type SessionStatus = 'running' | 'exited'
interface Session {
  ...
  status: SessionStatus  // 不再有 'active'/'completed' 泄漏到前端
}
```

后端 `api_list_sessions` 只吐 `running`/`exited`。DB 的 `active`/`completed` 是业务态,不进前端 status 字段(若需展示「已完成」用独立字段或从 stage done 推导)。

---

## 4. 迁移路径(分步,每步可独立验证)

> 每步改完提交。顺序按依赖关系:先统一 ID 生成(问题4,resume 命脉),再统一存储 key,再补存活态,最后前端。

### Step 1 — 统一 uuid5 输入串 + 抽函数(修问题 4,resume 核心)
- 新增 `compute_session_id(story_key, stage, adapter) -> str`(三字段)。
- `api.py:713`、`planner.py:1170` 改调它,删内联 uuid5。
- `api.py:640`(deprecated `_build_stage_launch_cmd`)也改,保持一致。
- 加测试:两路径同输入 → 同 ID。
- **验证**:交互 spawn 一个 claude,让自动循环 resume,确认续上历史。

### Step 2 — PTY 注册表记 (stage, adapter) + 改 key 为 uuid5(修问题 1)
- `ManagedPty` 加 `stage`/`adapter` 字段(构造时传入)。
- `spawn_pty` / `ensure_agent_pty` 接收 (story_key, stage, adapter),key 用 `compute_session_id`。
- `_next_session_id` / `_session_counter` 删除(不再需要)。
- `list_pty_sessions` 返回真实 stage/adapter。
- 修问题 8:构造参数正名(`session_id` 不再伪装成 `story_id`)。

### Step 3 — `api_list_sessions` 存活态查询 + 去重(修问题 2、3)
- 按 §3.4 重写:DB 行为主,PTY alive 覆盖 status,去重不 append。
- 返回 `pty_key` 字段(前端连 WS 用)。

### Step 4 — WS handler 接受 pty_key(配合 Step 3)
- `_pty_ws_handler` 的 `get_pty(story_id, session_id)` 现在能查到了(key 已是 uuid5)。
- 前端 `TerminalPanel` 连 WS 用 `pty_key`(从 list 拿),不用 DB 的 cli session_id。

### Step 5 — cleanup / reaper(修问题 6、7)
- `clean_exit_pty` 后加 `kill_pty`。
- 加 lazy reaper 或定期 reaper 清 dead 条目。

### Step 6 — 修复用分支(修问题 5)
- `_ensure_story_agent_pty` 复用分支:`existing.session_id` → `existing.session_id`(属性正名后存在了)。
- 或重写复用逻辑(按 stage/adapter 匹配,而非「第一个 alive」)。

### Step 7 — 前端 status 词汇表 + 反馈(修体验)
- `Session.status` 联合类型 `running|exited`。
- spawn 后即时反馈(loading 态/成功提示),不等 5s 轮询。

---

## 5. 不动的边界

- **DB schema 不动**(`story_session` 表结构、`UNIQUE(story_key, stage, adapter)` 保留)。只改读写它的代码。
- **adapter 层不动**(`SessionSpec` / `start_session` 契约不变,AGENTS.md domain convention)。
- **done file 协议不动**(stage 完成的真相源不变)。
- **driver 生命周期不动**(`claim_story_driver` / `consume_orphan_done`,AGENTS.md domain convention)。
- **kimi 捕获回填路径不动**(`_capture_kimi_session` → `set_session_id`)。

---

## 6. 风险与回滚

- **Step 1 风险最高**(改 resume ID)。回滚:恢复各自的内联 uuid5。测试覆盖是关键 —— 必须有「跨路径 resume」的回归测试才能合并。
- **PTY key 改 uuid5**(Step 2)影响所有读 `_ptys` 的地方(get_pty/kill_pty/list_pty_sessions)。逐个核对调用点(见调查的「Quick-reference: touch points」)。
- **存量死条目**:迁移时 `_ptys` 里可能有旧 `pty-{n}` 条目,重启进程即清空(内存态),无需数据迁移。

---

## 7. 参考

- `PTY_WEBSOCKET_RECONNECTION_DESIGN.md` — 4404/1000 close-code 设计(本文 Step 3-4 让这些 code 真正生效)
- `claude-code-agent-internals.md` — claude session 持久化(`--session-id`/`--resume`,转录格式)
- `STATE-STORY-STATE-MODEL.md` — lifecycle_state 与 story_session.status 是两条独立的状态轴
- AGENTS.md「Driver lifecycle」「Adapter prompt delivery」domain conventions
- grok-build reference §7.6/7.7 — `launch_cli(adapter, stage, focus)` 单一入口精神(本文的统一 ID 是其落地)

---

## 附录 A:问题 → 迁移步骤映射

| 问题 | Step | 性质 |
|---|---|---|
| 1 两套 ID 分裂 | 2 | 根因 |
| 2 list 不查存活态 | 3 | 直接 bug |
| 3 列表重复行 | 3 | 直接 bug |
| 4 uuid5 输入串不一致(resume 断) | 1 | **resume 命脉,最致命** |
| 5 复用分支 AttributeError | 6 | 潜伏 |
| 6 无 reaper | 5 | 累积 |
| 7 clean_exit 不移除 | 5 | 累积 |
| 8 命名谎言 | 2 | 共犯 |
