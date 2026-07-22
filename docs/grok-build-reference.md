# Grok Build 源码阅读:对 story-lifecycle 的可抄清单

> 一个对照阅读 xAI 开源 [grok-build](https://github.com/xai-org/grok-build)(Rust 写的终端 coding agent)源码后,提炼出对 `story-lifecycle`(Python AI 工作流编排器)有借鉴价值的设计要点。
> **自包含**:读者只需懂 Python / 软件架构基础。Grok Build 用 Rust 写,但本文不要求读者懂 Rust——代码片段都附了中文解读。
> **用途**:后续重构(状态机 / LLM audit / 知识检索 / 配置)时的参考设计库,不是要"集成 Grok"。
> 起点日期:2026-07-16。

---

## 0. TL;DR(30 秒版)

Grok Build 2026-07-15 开源了完整 harness(3000+ Rust 文件,模型仍闭源)。**代码本身不能直接用**(语言不同、定位不同),但它有两三个 crate 的设计正好踩中 story-lifecycle 当前的痛点,值得当"参考答案"。

对照 story-lifecycle 现状,最高 ROI 的七件事(按性价比排序):

| # | 借鉴点 | 对应 story-lifecycle 痛点 | 工作量 |
|---|---|---|---|
| 1 | **TrustStore 堵 context_provider 安全缺口** | `context_providers/__init__.py:32` 无脑 importlib 等于无脑 exec | 中等 |
| 2 | **权限靠工具集裁剪而非 prompt** | Resolver/Handler 角色靠 prompt 约束,模型忽略就越权 | 中等 |
| 3 | **config 原子写 + 深合并** | `config.py:37` 非原子写 + 浅合并,写一半崩溃留半个文件 | 10 行 Python |
| 4 | **LLM audit 补 stage 归因 + 脱敏** | `llm_client.py:554` stage 硬编码空串;prompt 原样落库无脱敏 | 中等 |
| 5 | **state 机 CQRS 三分 + STAGE_FAILED 独立** | planner.py:1366 转移逻辑全混编;无失败态分离 | 较大,趁 source-driven 迁移做 |
| 6 | **knowledge 混合检索(FTS5 + 向量)** | `search.py:25` 退化到正则逐行 scan,无 FTS/向量 | 较大,但性能和召回双升 |
| 7 | **接通 policy_engine(deny-wins)** | `policy_engine.py` 设计完备但零生产接线(ghost code) | 中等 |

本文档围绕这些事展开,每条都给出:Grok 怎么做(代码 + 解读)、对应 story-lifecycle 哪个文件哪一行、具体怎么改。

> **阅读建议**:第 1-2 条(§7.1-7.2)是安全缺口,最紧迫。第 3-5 节是"配置/审计/状态机"工程基础。第 4 节是知识检索升级。第 5-7 节是 hook/agent 编排。第 8 节是"不要抄的部分"和风险提示。

---

## 1. 背景

### 1.1 Grok Build 是什么

xAI 的终端 AI 编程助手(和 Claude Code、Codex CLI、Kimi Code 同类)。开源的是**整个 agent harness**——8 个并行 subagent、plan-first workflow、Arena mode、skills、MCP 支持、CLI + TUI。模型(Grok)仍闭源,调用照常付费。

技术栈:纯 Rust,~60 个 crate,集中在 `crates/codegen/` 下。3000+ 文件里大部分是 TUI 渲染(`xai-grok-pager`/`xai-grok-shell`)和采样器协议,和 story-lifecycle 无关。**真正值得看的是 5 个 crate**,本文聚焦这些。

### 1.2 为什么对 story-lifecycle 有参考价值

两个项目解决相邻问题:

| 维度 | Grok Build | story-lifecycle |
|---|---|---|
| 定位 | 单个 coding agent | 编排多个 coding agent 跑过 story 阶段 |
| 语言 | Rust | Python |
| 状态 | turn / session 生命周期 | story / stage 状态机 |
| 知识 | `xai-grok-memory`(跨会话记忆) | `packages/knowledge`(scenario/playbook/failure) |
| 观测 | `xai-grok-telemetry` | LLM audit(最近提交 03e56468) |
| 配置 | 6 层签名配置 | `config.yaml` + env |

定位相邻 + 痛点重合 = 设计模式可迁移。但**不要想着 import 它的代码**——语言和架构都不同,价值在"设计参照"。

### 1.3 本文涉及的 crate 清单

| Crate | 行数 | 本文对应章节 |
|---|---|---|
| `xai-agent-lifecycle` | 15 文件 | §2(生命周期边界) |
| `xai-chat-state` | 17 文件 | §2(状态机 CQRS) |
| `xai-grok-memory` | 15 文件 | §4(知识检索) |
| `xai-grok-telemetry` | 32 文件 | §3(审计脱敏/归因) |
| `xai-grok-hooks` | 14 文件 | §5(hook 事件) |
| `xai-grok-sandbox` | 8 文件 | §5(权限) |
| `xai-grok-config` | 14 文件 | §6(配置) |
| `xai-grok-agent` / `tools` / `plugins` / `mcp` | ~50 文件 | §7(agent 编排) |

---

## 2. 生命周期与状态机(最高 ROI 之一)

> 对应 story-lifecycle:`orchestrator/engine/planner.py` 的 driver 循环、`sourcing/source_loader.py` 的 story_states。
> Grok crate:`xai-agent-lifecycle`、`xai-chat-state`。

### 2.1 contributor 的边界划分:只收数据,不持有控制权

**Grok 怎么做**(`xai-agent-lifecycle/src/lib.rs`)

Grok 把"一个回合(turn)的生命周期"拆成几个独立的观察点(contributor),每个观察点只接收一份**纯数据 input**,自己决定要不要记日志、发通知——但**管不了"这轮要不要继续跑"**,那个权力在主循环(host)手里。

```rust
// send/contributors/turn_lifecycle.rs
pub struct TurnStartInput {
    pub synthetic: bool,   // true = 系统自动触发的(定时/续跑),false = 用户手动
}

#[async_trait]
pub trait TurnLifecycleContributor: Send + Sync {
    async fn on_turn_start(&self, _input: &TurnStartInput) {}
    async fn on_turn_done(&self, _input: &TurnDoneInput) {}
    async fn on_turn_abort(&self, _input: &TurnAbortInput) {}     // 被打断
    async fn on_turn_error(&self, _input: &TurnErrorInput<'_>) {} // 报错
}
```

两个关键设计:

**第一,`synthetic: bool` 区分"用户触发 vs 系统自动"。** 这正是 story-lifecycle 的痛点——有的 story 是用户手动点的"开始",有的是 profile 驱动自动续跑的。两种情况的后续动作(发不发通知、失败重不重试)应该不同,但 story-lifecycle 现在散在各处用 `if` 判断。

**第二,故意做了 `local` 和 `send` 两套 twin trait。** `local` 是 `?Send`(单线程 TUI 用 `Rc/RefCell`),`send` 是 `Send + Sync`(多线程)。注释解释:同一个 hook 逻辑,在单线程和多线程 host 里边界要求不一样,硬给单线程代码套 `Send` 反而把简单事搞复杂。这是踩坑后的教训:**别一开始就要求所有东西都能跨线程**。

**对应 story-lifecycle 哪里**

`planner.py:829-1457` 的 driver 主循环把所有职责揉在一起:profile 兜底 adapter(847)→ done_file 规范化(864)→ `db.update_story`(878)→ 建 worktree(883)→ 拼 prompt(926)→ 写 prompt 文件(944)→ 起 adapter(950)→ 等 done → gate 校验 → 状态转移(1366)。**读状态、决策、写库、起 agent 全在一个 `while` 循环里**。

**怎么改**

照 Grok 的思路拆成两层(不一定现在全做,但 source-driven 迁移时按这个方向):

- **一层只产出"这轮要喂给 agent 的内容"**(prompt 装配、context_providers 检索)——只读,不改任何东西。
- **一层只负责"阶段结束后干什么"**(落库、审计、状态转移)——纯副作用,不回流到决策。

两层之间只靠数据结构传话。这样改审计逻辑时不会碰坏状态判断;改状态机时不会动到 agent 编排。

`synthetic` 字段的直接落地:给 stage 执行加一个 `triggered_by: "user" | "auto"` 维度(planner.py:740 的 `lifecycle_state` 初始化时就能带),`STAGE_FAILED` 时如果是 auto 触发就考虑重试,user 触发就 paused 等人。

### 2.2 状态机的 CQRS 三分(最值得抄的结构)

**Grok 怎么做**(`xai-chat-state/src/`)

`xai-chat-state` 把会话状态拆成四个文件,职责严格分离:

| 文件 | 职责 | 签名特征 |
|---|---|---|
| `state.rs` | 状态数据结构(持有真相) | struct 定义 |
| `queries.rs` | 读查询(纯函数,不改状态) | `fn(&self) -> X` |
| `mutations.rs` | 写变更(每次必发事件) | `fn(&mut self, ...) -> Event` |
| `events.rs` | 事件类型(不负责持久化) | enum + 序列化 |

核心契约:**读用 `&self` 是纯函数,写用 `&mut self` 且每次必以 `send_event` 结尾,事件本身不含持久化职责**(持久化由外层 actor 做)。

**对应 story-lifecycle 哪里**

现状(来自 source-driven 迁移后的代码):
- `sourcing/source_loader.py:34-83`:`SourceProfile` 里 `story_states: dict` 和 `state_map: dict` 是**裸 dict,无类型约束**。
- 状态值是**字符串字面量**(`"开发"`/`"测试"`/`"上线"`/`"结项"`),**没有 enum**。各处硬编码 `"开发"` 作默认(planner.py:757、api.py:907)。
- `planner.py:1366-1457`:**转移逻辑全混编**——状态读、转移决策、`db.update_story` 写、`log_event` 事件产出全在 driver 主循环里。
- 查询侧(`api.py:3067-3118` 的 `story_states_view`)和转移侧(`planner.py`)各自重复调 `resolve_source_profile`,真源不统一。

**怎么改**

CQRS 三分正好是 source-driven 迁移的天然脚手架,**迁移期间查询接口完全不用动**:

1. **状态值先 enum 化**(命中 AGENTS.md "跨系统状态超出 true/false 要建模成 enum"):

```python
# sourcing/story_state.py(新建)
class StoryState(str, Enum):
    DEV = "开发"
    TEST = "测试"
    ONLINE = "上线"
    CLOSED = "结项"
```

YAML 里仍是中文字面量(向后兼容),但代码侧用 enum,消灭硬编码 `"开发"` 散落。

2. **拆三个模块**:
   - `state.py`:状态数据结构 + 拓扑(从 YAML 加载的 `state_map`)。
   - `queries.py`:纯函数,如 `can_advance(state, topology) -> bool`、`next_state(state, topology) -> StoryState`。**只读,签名 `-> X` 不带 DB 写**。
   - `mutations.py`:`advance_state(...) -> StoryStateEvent`,内部算出新状态 + 产出事件,但**不调 `db.update_story`**——持久化由 planner driver 在收到 event 后做。

3. **事件独立**:`StoryStateEvent` 是个 dataclass,planner 拿到它后既写 DB 又写 `event_log`。这样 `planner.py:1366` 的混编块能瘦下来:转移决策挪到 `mutations.py`,planner 只负责"调 mutation → 收 event → 持久化 + 发通知"。

### 2.3 STAGE_FAILED 必须独立(Stop vs StopFailure)

**Grok 怎么做**(`xai-grok-hooks/src/event.rs`)

```rust
pub enum HookEventName {
    Stop,         // turn 正常结束(完成/取消/错误都算)
    StopFailure,  // turn 因 API 错误结束 —— hook 的输出和退出码被忽略
    ...
}
```

注释明确:`StopFailure` 时上游已经失败,hook 再 deny 没意义。**正常完成和上游故障建模成两个状态**,不是一个布尔。

**对应 story-lifecycle 哪里**

story-lifecycle 现在的 stage 结束是单一路径——agent 跑完(detect done_file)就推进,跑挂了也只是 done_file 不出现。没有显式的"阶段失败"事件。`planner.py` 的 `mark_failed` 工具(agent_tools.py)存在,但和状态转移是两条独立路径,没打通。

**怎么改**

给编排过程定义事件枚举时(见 §5.1),`STAGE_END`(正常收尾,可跑清理/审计 hook)和 `STAGE_FAILED`(agent 崩了,清理 hook 该跳过不该再 deny)必须分开。这是 AGENTS.md "跨系统状态超出 true/false 要建模成 enum" 的直接应用。

---

## 3. LLM 审计:脱敏 + 归因 + 耗时分解

> 对应 story-lifecycle:`infra/llm_client.py:482,538` 的 `_trace`、`infra/db/models.py:145,166` 的 audit 表、`infra/db/models.py:875` 的查询。
> Grok crate:`xai-grok-telemetry`。

### 3.1 现状三个缺口

现状确认(来自 `llm_client.py` + `models.py`):

1. **stage 归因断档**:`_trace` 调 `log_llm_trace(stage="", ...)`(llm_client.py:554 硬编码空串)。audit UI 能看到"这个 story 花了多少 token",**看不到花在 design 还是 build 上**。
2. **零脱敏**:`prompt_text = json.dumps(req_body.get("messages", []))`(llm_client.py:572)原样入库。grep `redact|secret|mask|sanitize` 零命中。若 prompt 里夹带 api_key/敏感数据会一起落库。
3. **零截断**:`prompt_text`/`response_text`/`reasoning_text` 全量存,长 prompt 直接进 SQLite,长期会爆。

Grok 的 telemetry crate 这三块都有成熟设计。

### 3.2 三层 correlation id(补 stage 归因)

**Grok 怎么做**(`session_ctx.rs`)

Grok 用 `tokio::task_local!` 挂一个 context,归因是**三层**:

```rust
// TelemetryCtx = { session_id, prompt_index, prompt_id }
//   session_id   — 整个会话
//   prompt_index — 第几轮(≈ turn_number)
//   prompt_id    — 单次请求的 UUID,把 prompt/response/tool_calls 串起来
```

关键细节:`log_event` 在调用点**同步快照** context(注释:"snapshotted synchronously by log_event at call time to avoid racing with turn increments")——不在记录时再读 task_local(可能已被改),而是入口处取一次存局部变量。拿不到 context 就记 `None`,**不抛异常卡住主路径**。

**对应 story-lifecycle 怎么改**

story-lifecycle 已经有 `CURRENT_STORY_KEY: ContextVar`(llm_client.py:26),对应 Grok 的 `session_id`。缺的是后两层:

```python
# infra/llm_client.py
CURRENT_STORY_KEY: ContextVar[str | None] = ContextVar("story_key", default=None)
CURRENT_STAGE: ContextVar[str] = ContextVar("stage", default="")        # 新增
CURRENT_REQUEST_ID: ContextVar[str | None] = ContextVar("request_id", default=None)  # 新增

def _trace(self, ...):
    story_key = CURRENT_STORY_KEY.get()      # 已有
    stage = CURRENT_STAGE.get() or "unknown" # 替代硬编码空串
    request_id = str(uuid.uuid4())           # 每次调用新 UUID
    log_llm_trace(story_key=story_key, stage=stage, ..., request_id=request_id)
```

然后在 stage 执行入口(planner.py 起 agent 前)设 `CURRENT_STAGE`。这样 audit UI 的 `get_story_llm_calls`(models.py:875)就能按 stage 过滤,直接回答"token 花在哪"。`request_id` 把同一调用的 prompt/response/reasoning 关联(虽然现在是一行 llm_call,但将来分表时有用)。

**快照在调用点取**:Grok 的教训是别在记录函数深处读 ContextVar。Python 的 `contextvars.copy_context()` 或在 `_request` 入口显式取三个值存局部变量再传给 `_trace`。

### 3.3 脱敏:多层 fail-closed

**Grok 怎么做**(`redact_common.rs` + `external/redact.rs`)

核心理念写在文件顶部:**"Dropping telemetry on a schema bug is acceptable; leaking is not."**(宁可丢遥测也不能泄漏)。架构是五层防御:

**第 3 层(最值得抄)—— emit 时 secret-scrub**:先按 secret 形状正则脱敏(识别 `sk-` 等 API key),再脱敏用户路径:

```rust
pub(crate) fn redact_owned(input: &str) -> Option<String> {
    let secrets = xai_grok_secrets::redact_secrets(input);          // 第一遍:密钥形状
    match xai_grok_secrets::redact_user_paths(secrets.as_ref()) {   // 第二遍:用户路径
        Cow::Owned(paths) => Some(paths),
        Cow::Borrowed(_) => None,   // 都没变 → 返回 None
    }
}
```

**第 4 层—— export 时 fail-closed 校验**:即使前几层漏了,导出时对每条记录逐字段检查,如果 string 值**仍含 secret 形状**(说明第一次没脱干净),**整条记录丢掉**:

```rust
AnyValue::String(s) => {
    if crate::redact_common::redact_owned(s.as_str()).is_some() {
        return false;  // 还能脱敏却没脱 → 整条丢
    }
}
```

**对应 story-lifecycle 怎么改**

`infra/llm_client.py:572` 的 prompt 落库前加一道脱敏。Python 版:

```python
import re
SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "[REDACTED:api_key]"),       # OpenAI/xAI
    (re.compile(r"Bearer\s+\S+"), "Bearer [REDACTED]"),               # Auth 头
    (re.compile(r"gh[ps]_[A-Za-z0-9]{36}"), "[REDACTED:github_token]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED:aws_key]"),
]

def redact_secrets(text: str) -> str:
    for pattern, repl in SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text

# _trace 里:
prompt_text = redact_secrets(json.dumps(req_body.get("messages", [])))
```

占位符带类型标记(`[REDACTED:api_key]`),审计时还能看到"这里有个 key"。通用高熵串(`[A-Za-z0-9_-]{32,}`)**谨慎用**,误杀率高。

**gate 机制**:给"是否存 prompt/response 原文"一个开关(环境变量 `STORY_AUDIT_LOG_PROMPTS`),tool_calls 参数单独 gate。Python 侧不必像 Rust 那么激进地整条丢,可以记"脱敏后仍可疑"标记 + 脱敏后存储。

### 3.4 截断:三档 + char 计数

**Grok 怎么做**(`external/truncate.rs`)

| 对象 | 上限 | 超了怎么办 |
|---|---|---|
| 普通字符串属性 | 512 字符 | 前 128 + `…[truncated]` |
| prompt / 内容文本 | 60 KB | char boundary 截 + marker |
| tool 参数 JSON | 4 KB / 深度 2 / 每集合 20 项 | 结构化降维后截 |

关键:**按 char 计数不按 byte**(非 ASCII 稳定);**UTF-8 安全截断**(`floor_char_boundary` 往前回退到字符边界)。

**采样策略**:Grok **不做随机采样**,做分级——`Disabled` / `SessionMetrics`(只元数据)/ `Enabled`(全量)。存储成本控制靠 gate + 截断 + 模式分级,不是丢一部分请求。

**对应 story-lifecycle 怎么改**

1. `prompt_text`/`response_text` cap 32KB(超过截断 + marker);`reasoning_text` 更激进 cap 8KB;`tool_calls_json` JSON 降维(深度限 2,每数组 20 项)。
2. **分级而非采样**:默认档只记 token/duration/error/model/stage(便宜);详细档加 prompt/response(截断后);完整档加 reasoning。用开关切,不丢请求。
3. **异常驱动采样**(story-lifecycle 可以比 Grok 做得更好):成功且 duration 正常的只记元数据,**慢请求/error/高 token 的记全量**——比随机采样更有审计价值。

### 3.5 耗时分解(prompt_timing)

**Grok 怎么做**(`prompt_timing.rs`)

测一个 turn 的各阶段耗时,5 个维度:**反推差值减少埋点**:

```rust
// model_call_ms 是入参(调用方测),其余算出来:
let total_ms = turn_start.elapsed();
let pre_model_ms = total_ms.saturating_sub(model_call_ms);  // 模型调用前的一切
```

`total = mcp_wait + tool_collection + model_call + 其他`(pre_model 是兜底差值)。

**对应 story-lifecycle 怎么改**

这是 audit 缺的一块——**有 duration 但没分解**。补一个 `PhaseTiming`:

- `context_assembly_ms`:拼 prompt(context_providers 检索 + 历史)——story-lifecycle 里这块可能很重。
- `llm_call_ms`:HTTP 净耗时。
- `tool_execution_ms`:agent 回 tool_calls 时的执行耗时。
- `total_ms`:整阶段。

反推:`context_assembly_ms = total - llm_call - tool_exec`,只在几个边界打 `time.perf_counter()`。补完后前端审计 UI 能直接定位"为什么这个 story 慢":检索慢?LLM 慢?工具慢?

---

## 4. 知识检索:从正则升级到混合检索

> 对应 story-lifecycle:`knowledge/knowledge_store/search.py:25`(纯正则逐行 scan)、`packages/knowledge/schema.md`(scenario/playbook/failure)。
> Grok crate:`xai-grok-memory`。

### 4.1 现状:检索退化到正则

现状确认(来自 `search.py`):

```python
# search.py:25-58
def search_knowledge(workspace, keyword, target_type, limit):
    pattern = re.escape(keyword)
    for sp in search_paths:
        if sp.is_dir():
            results.extend(_search_dir(sp, pattern, ...))   # 逐文件逐行 scan
        elif sp.exists():
            results.extend(_search_file(sp, pattern, ...))
```

**无 FTS、无向量、无 embedding**。grep `sqlite.vec|chromadb|faiss|embedding` 在 src 下零命中。大知识库下性能(全量 scan)和召回(正则匹配不到同义词)双输。

Grok 的 `xai-grok-memory` 给了完整的混合检索方案。

### 4.2 混合检索八步流水线

**Grok 怎么做**(`search.rs` + `index.rs` + `embedding.rs` + `mmr.rs`)

**第 1 步:FTS5 永远在线,并补一轮 evergreen 召回。** session 日志体积大会把长期知识挤出候选集,所以除全量 FTS,再单独按 source 跑一次补召回:

```rust
let mut fts_results = index.search_fts(query, candidate_limit).unwrap_or_default();
let evergreen = index.search_fts_by_sources(query, candidate_limit, &["global", "workspace"]).unwrap_or_default();
// 去重后并入
```

**第 2 步:query 先过停用词。** 不直接丢给 FTS5,而是 `extract_keywords` 去停用词后用 `OR` 拼。全停用词时返回空,触发纯向量降级。

**第 3 步(最值得抄的细节):分数归一化——FTS 相对,向量绝对。**

```rust
// FTS5 rank 是负数,用 min/max 相对归一化:
let normalized = 1.0 - (r.rank - min_rank) / range;  // 最好=1.0

// 向量距离故意不用相对归一化,用绝对尺度:
const MAX_L2_DISTANCE: f64 = 2.0;
let similarity = (1.0 - (*distance as f64 / MAX_L2_DISTANCE)).clamp(0.0, 1.0);
```

注释解释:高维向量有"测度集中"现象,候选挤在窄带里,相对归一化(`1 - d/max_d`)会把分数压到接近 0。**这个坑你的 knowledge 检索上向量后一定会踩**。

**第 4 步(关键坑):三态合分,FTS-only 不被向量拖累。** 如果用 `text_weight*fts + vector_weight*vec` 统一公式,只有 FTS 命中(没向量)的 chunk 会被乘上 `text_weight=0.3` 掉到阈值下。Grok 分三种情况:

```rust
let score = if fts > 0.0 && vec > 0.0 {
    let hybrid = text_weight * fts + vector_weight * vec;
    hybrid.max(fts)          // 双命中:加权但保底不低于纯 FTS
} else if fts > 0.0 {
    fts                       // 纯 FTS:满分,不惩罚
} else {
    vector_weight * vec       // 纯向量:加权
};
```

**第 5 步:降级。** 向量不可用时 `vec_available=false`,直接走 FTS-only,`text_weight` 实质为 1.0。embedding API 指数退避重试(429/5xx,3 次)。

**第 6-8 步**:temporal decay(见 4.3)、MMR 去冗余、truncate。

**对应 story-lifecycle 怎么改**

1. **存储用 SQLite + FTS5**(contentless 虚表)+ `sqlite-vec`。一份库承载结构化字段 + BM25 + KNN,部署零依赖。比引入 chromadb/faiss 轻。
2. **向量距离绝对归一化**(`1 - L2/2`),别按批次 max 归一化。
3. **三态合分**逻辑直接照抄,否则 FTS-only 的 scenario(没 embed 的)系统性失分。
4. **query 先过停用词**再喂 BM25。
5. **降级做成显式分支**:向量层 try/except,失败 warn 并把 `text_weight` 提到 1.0。
6. **补一轮 evergreen 召回**:scenario/playbook 单独按 type 跑一次 FTS,防止被大量 failure 挤掉。

### 4.3 evergreen vs decaying 二分

**Grok 怎么做**(`search.rs`)

时间衰减由 source 决定:

```rust
fn is_evergreen_source(source: &str) -> bool {
    matches!(source, "global" | "workspace")  // 人工策展的长期知识 → 不衰减
}
// session 自动生成的 → 衰减

fn temporal_decay_multiplier(source, created_at, now, half_life_days: Option<f64>) -> f64 {
    let Some(half_life) = half_life_days else { return 1.0; };
    if is_evergreen_source(source) { return 1.0; }      // evergreen 永不衰减
    let lambda = f64::ln(2.0) / half_life;              // 半衰期模型
    (-lambda * age_days).exp()                           // e^(-λ·age)
}
```

**对应 story-lifecycle 怎么改**

knowledge 三类知识正好对应:
- **scenario / playbook** → evergreen(不衰减)。好用的模式不会因时间变差。
- **failure** → decaying(衰减)。半年前的坑,现在工具链可能已修。

落地:
1. 给三类加 `decays: bool`(或按 type 推断)。
2. failure 用较短半衰期(14–30 天,因为"某库的 bug 怎么修"过几个月库都换了);per-type 可配。
3. **软衰减不硬删除**——`min_score` 自然筛掉老的,保留历史。

### 4.4 dream:定期把会话经验提炼成长期知识

**Grok 怎么做**(`dream.rs` + `dream_lock.rs`)

dream 是**后台知识固化**:把近期 session 日志喂给 LLM,合成结构化 markdown 写进 workspace MEMORY.md,然后删掉已消化的 session 文件。

**prompt 设计**(`DREAM_SYSTEM_PROMPT`)用 5 个动词,非常值得抄:
- **Merge**:相关信息合并成主题摘要
- **Resolve**:矛盾以最新为准
- **Convert**:相对日期("昨天")→ 绝对日期
- **Discard**:寒暄、meta、工具噪声、计数、"下一步"section
- **Preserve**:决策、理由、架构、偏好、问题/解法对
- 没东西可存就回 `NO_REPLY`

**三道门触发**(最便宜的先查):config 开关 + 时间门(距上次 ≥ `min_hours`)+ 数量门(新 session 数 ≥ `min_sessions`)。

**输入构建**:先放 existing memory(让模型 merge 而非覆盖);硬上限 32K 字符,超了就停——**关键**:`processed_stems` 精确记录"实际读了的",清理只清这些,避免"提了一半却把原始数据全删了"。

**输出质检三件套**:非空 + 非 NO_REPLY + **必须有 markdown 标题**(强制结构化),否则丢弃。

**对应 story-lifecycle 哪里**

现状:story-lifecycle 的 `_persist_playbook_for_story`(planner.py:489-528)**只在单个 story 完成时同步触发一次** reflect → persist。没有跨 story 的全局 consolidate/dedup,没有后台/定时。

**怎么改**

dream 几乎能 1:1 映射到"从完成 story 提炼 scenario/playbook/failure":

1. **抄 5 动词 prompt**:Preserve 决策/理由 → scenario;Preserve 问题/解法对 → playbook/failure;Discard 列表直接复用。
2. **抄三道门**:按"已完成的 story 数"触发(如每攒 5 个跑一次),而不是每个 story 跑一次——跨 story consolidate 才能去重。
3. **抄输入上限 + processed 追踪**:别一次性塞所有 transcript,超上限留到下次,清理只清实际处理的。
4. **抄输出质检**:必须有 scenario/playbook/failure 的必填字段,否则丢弃。NO_REPLY 机制——不是每个 story 都值得沉淀。
5. **现有 memory 先入 prompt**:让模型 merge 而非覆盖(对应"同主题 scenario 合并去重")。

> **限制**:Grok 的 dream 是**整体覆盖** workspace MEMORY.md(`fs::write` 全量)。knowledge 要保留已有条目,要么靠 prompt merge,要么改增量写——明确这点否则丢历史。

### 4.5 chunker 切分 + 内容过滤

**chunker.rs**:四级降级切分(整体 ≤ 限制 → 按 `##` 标题 → 按段落 `\n\n` → 按行),维护 header_stack 给每个 chunk 拼祖先上下文前缀(`[Context: ## Parent]`)。段落切分有 overlap 保证 embedding 连续性。reindex 用 blake3 hash 增量(未变不重新 embed)。

**search.rs 的 `is_structurally_empty`**:过滤纯标题/注释/空白组成的"空架子"chunk。**最值得抄的边界处理**:HTML 注释跨 chunk 边界被切开时,未闭合的注释把剩余当字面文本保留,防止误判为空。

**对应 story-lifecycle**:
- story-miner ingest transcript 时用四级降级(按 turn 切 → 段落 → 行),每个 chunk 前缀 `[Context: story=XXX phase=design]`。
- `is_structurally_empty` 过滤 boilerplate 噪音(权限提示、空 section)。
- 注意:字符数代理 token 对中文不准(1 中文字符 ≈ 1 token,不是 0.25),`max_chunk_chars` 要调小。

---

## 5. hook 事件 + 权限沙箱

> 对应 story-lifecycle:`policy_engine.py`(ghost code)、`agent_tools.py` adapter 枚举。
> Grok crate:`xai-grok-hooks`、`xai-grok-sandbox`。

### 5.1 事件枚举:只有 PreToolUse 是 blocking

**Grok 怎么做**(`event.rs` + `result.rs` + `dispatcher.rs`)

事件枚举分生命周期组(见 §2.3 的 Stop/StopFailure)。核心设计三处:

1. **只有 `PreToolUse` blocking,其余 non-blocking**:
```rust
pub fn is_blocking(&self) -> bool { matches!(self, Self::PreToolUse) }
```
只有它返回 `HookDecision::Deny { reason, hook_name }` 能阻断;其余是 fire-and-forget 观察点。

2. **first-deny-wins + fail-open**:任何一个 hook 返回 Deny 就短路。但 hook **崩溃/超时/输出畸形走 fail-open**——记进 `Failed` 给 UI 回放,但不阻断。

3. **deny 带 reason + hook_name**:让 UI 说清"谁拦的、为什么"。

dispatcher 注释把威胁模型写得很直白:**fail-open 是产品取舍(避免误杀),不是安全设计**;真安全靠沙箱(§5.3)。

**对应 story-lifecycle 怎么改**

story-lifecycle 目前没有 hook 机制,但 `policy_engine.py` 有完整的 `GUARDED_RULES` 矩阵(`policy_engine.py:71-140`,L0-L5 × 10 类 action = 60 条规则)。**问题:它是 ghost code——planner 完全不调它,实际安全靠 `confirm=True` YAML 和 `_story_state_gate` 人工闸,两者没打通**(只有 `test_phase6.py` 单测覆盖)。

借鉴点:
- 定义 `OrchestratorEvent` 枚举(planner 加观察点时用):`STAGE_START` / `STAGE_END` / `STAGE_FAILED`(独立!)/ `PRE_AGENT_INVOKE`(唯一 blocking)/ `POST_AGENT_INVOKE`。
- **只对 `PRE_AGENT_INVOKE` 做 deny 决策**(等价 Grok 的"只有 PreToolUse blocking"),其余只观察+日志。
- **接通 policy_engine**:现在它的 `evaluate_guarded`(`policy_engine.py:255-316`)设计完备(静态查表 + 预算耗尽降级 + 连续拒绝升级禁),但零生产接线。在 `PRE_AGENT_INVOKE` 处调它,deny 结果落 `supervisor_decision`。

### 5.2 deny-wins 语义

**Grok 怎么做**:`first_deny_wins` + `allow_then_deny_denies` 测试——任何一条规则说 deny,最终就是 deny,即使别的说 allow。

**对应 story-lifecycle**:`GUARDED_RULES` 现在是单次 dict 查表 + 两个 if 后置修正(`policy_engine.py:283`),不是 deny-wins 链。如果同时命中"用户全局策略禁止 MODEL_SWITCH"和"当前 autonomy 允许",应该禁止。改成:**任一 FORBIDDEN 即 FORBIDDEN**。

### 5.3 沙箱 profile + deny 优先 + 子进程断网

**Grok 怎么做**(`xai-grok-sandbox`)

- **profile 预设**:`Workspace`(默认只 workspace 可写)/ `Devbox` / `ReadOnly` / `Strict` / `Off`。
- **deny 优先于 allow**:Landlock/Seatbelt 上 deny 要展开成每个具体 write 子动作(`file-write-data`/`file-write-unlink`/...)才赢过宽 allow,既挡 overwrite 也挡 rename/unlink 绕过。deny 表达失败时 **fail-closed**(拒启动,而非"报告激活但漏了")。
- **项目 profile 不能覆盖全局受信任 profile**(`entry().or_insert()`):防止恶意 workspace 把受信任 profile 掏空(空 deny + 宽 read_write)还保留名字。
- **子进程网络拦截**(seccomp BPF 拦 `connect/bind/sendto/...`):**父进程开网(agent 调 API),子进程断网(agent spawn 的 bash 不能联网)**。精准切分。

**对应 story-lifecycle**

story-lifecycle 起 claude/codex/kimi(`claude_stream.py` 的 `subprocess.Popen`),agent 在用户 repo 跑命令改文件,**目前零文件系统/网络权限控制**,完全信任 agent CLI 自己的权限。

Python 做 kernel 级沙箱不现实(Landlock/Seatbelt 是 Rust + libc)。**现实可行**:Linux/macOS 用 Docker/Podman 容器包一层(只挂载 workspace);至少在 `doctor` 提示"test 阶段建议容器里跑"。但有两个语义能直接抄:
1. **profile 概念对应 `GuardedAutonomy`**:加文件系统 profile 层(design 阶段 ReadOnly,test 阶段隔离目录)。
2. **"项目配置不能放松全局"反 hijack**:workspace 级配置(`<workspace>/.story/config.yaml`)只能加严不能放松用户全局。

### 5.4 SSRF + secret 分离(如果要加 webhook)

**Grok 怎么做**(`runner_http.rs` + `result.rs`)

HTTP hook 强制 HTTPS,DNS 解析后逐 IP 查私网/元数据。**`169.254.169.254` 显式拦截**——云元数据端点,POST 到它能偷实例凭证。

secret 防泄漏用 `raw_url` vs `url` 分离:`url` 是展开后真实目标(可能带 `${TOKEN}`),`raw_url` 是配置文件原始串。**给用户展示必须用 `raw_url`**,否则 `?token=ghp_REAL` 泄漏到日志。连 `reqwest::Error` 的 Display 都特意 `without_url()` 剥一次(defense-in-depth)。

**对应 story-lifecycle**:如果加任何 HTTP 出站(webhook / 远程决策服务),抄 `is_blocked_ip`(`ipaddress` 库:`is_private`/`is_link_local`/`is_unspecified`)。config 的 `api_key`/`base_url` 加 `display_value()` 方法,异常处理 `str(e).replace(url, "***")`。

---

## 6. 配置:原子写 + 深合并 + enum 返回

> 对应 story-lifecycle:`infra/config.py:20-45`、`entry/cli/setup.py`、`entry/cli/doctor.py`。
> Grok crate:`xai-grok-config`。

### 6.1 原子写 + 深合并(立刻可做,10 行)

**Grok 怎么做**(`fs_atomic.rs`)

```rust
let tmp = dir.join(format!("{name}.{pid}.{nonce}.tmp"));  // 唯一名
options.write(true).create_new(true);                     // 不覆盖已存在
#[cfg(unix)] if let Some(mode) = mode { options.mode(mode); }  // 创建时就设权限
options.open(&tmp).and_then(|f| write).and_then(|()| rename(&tmp, final));  // 原子 rename
```

三点:temp 名用 `pid + 计数器`保证并发不撞;`create_new` 不覆盖;权限创建时设(不是写后 chmod)。

**对应 story-lifecycle 现状**(`config.py:37-45`)

```python
def save_config(config: dict):
    existing = get_config()
    merged = _merge_config(existing, config)   # 浅合并!(dict.update)
    CONFIG_FILE.write_text(yaml.dump(merged, ...))  # 非原子!写一半崩溃留半个文件
```

**怎么改**(Python 版):

```python
import os, tempfile
def save_config_atomic(config: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    merged = _merge_config_deep(get_config(), config)   # 深合并
    fd, tmp = tempfile.mkstemp(dir=CONFIG_DIR, prefix=".config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(yaml.dump(merged, allow_unicode=True))
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, CONFIG_FILE)   # 原子 rename
    except Exception:
        os.unlink(tmp); raise

def _merge_config_deep(base: dict, updates: dict) -> dict:   # 替代 dict.update
    for k, v in updates.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = _merge_config_deep(base[k], v)
        else:
            base[k] = v
    return base
```

story-lifecycle 的 config 写得勤(setup 向导、每次 autonomy trace),非原子写迟早出半个文件。**这是全文档 ROI 最高的一条**。

### 6.2 加载返回 enum 而非空 dict

**Grok 怎么做**(`signed_policy.rs`)

加载结果是一个显式 enum,不是布尔:

```rust
pub enum SignedVerdict {
    Inactive,           // 暗构建(无内嵌 key)
    NoAuthenticSidecar, // 无 sidecar 或签名不过
    SidecarUnreadable,  // 瞬时 IO 错(非篡改)→ fallback
    Trusted,            // 签名有效且绑定本 principal
    Compromised,        // 已被改/过期/绑别处 → 拒
}
```

`SidecarUnreadable`(瞬时错)和 `Compromised`(真篡改)分开——前者 fallback 不拒,后者无条件拒。

**对应 story-lifecycle 现状**:`get_config`(`config.py:27-34`)文件不存在/YAML 错都返回 `{}`,把"未配置/畸形/缺字段"全压成一个"空"。用户配置写坏了无声当成"未配置",难诊断。

**怎么改**:

```python
class ConfigLoadResult(Enum):
    OK = auto()
    MISSING = auto()        # 首次运行,引导 setup
    MALFORMED = auto()      # YAML 畸形 → 报错别静默吞
    INCOMPLETE = auto()     # 缺关键字段(api_key)→ doctor 报告
```

直接服务 AGENTS.md "每个非可执行分支要有可见反馈"。**不需要上签名**,但 enum 建模思路值得学。

### 6.3 doctor 加配置自洽检查

**Grok 怎么做**(`validation.rs`):校验很窄——只查 `fail_closed + version_overrides` 组合会导致启动失败的关键路径,不做全 schema 校验。错误带 `path + source`。

**对应 story-lifecycle 现状**:`doctor.py:153-211` 只查工具是否装了(claude/codex/git/...)。**配置自洽零检查**——provider 不在 PRESET_PROVIDERS、base_url 格式错、model 拼错,只在运行时炸。

**怎么改**(doctor.py 加):

```python
def check_config_consistency() -> list[str]:
    problems = []
    cfg = get_config()
    provider = cfg.get("provider")
    if provider and provider not in {p["name"] for p in PRESET_PROVIDERS.values()}:
        problems.append(f"provider={provider!r} 不在已知列表,custom 需手填 base_url")
    if provider == "custom" and not cfg.get("base_url"):
        problems.append("provider=custom 但 base_url 为空")
    return problems
```

错误带文件路径:"`~/.story-lifecycle/config.yaml` 的 provider='deepseek' 但 base_url 指向 anthropic.com"。两档失败:setup soft-fail(重问),`story serve` fail-closed(致命配置错拒启)。

### 6.4 分层覆盖 + 优先级链

**Grok 怎么做**(`lib.rs`):6 层(系统 managed → 用户 managed → 用户 config → 云缓存签名 → 系统 requirements → MDM),`deep_merge_toml` 递归合并 table、叶节点整体替换。

**对应 story-lifecycle**:优先级链现在隐式(config.yaml + 散落 os.environ)。`load_config_to_env()`(setup.py:228)`if not os.environ.get(...)` 才写——**env 优先于文件**。建议在 `config.py` 顶部注释明确:

```
1. 命令行 flag (--api-key)          最高
2. 环境变量 (STORY_API_KEY)
3. workspace config (.story/config.yaml)
4. 用户 config (~/.story-lifecycle/config.yaml)
5. PRESET_PROVIDERS 默认             最低
```

写 `load_effective_config()` 按此顺序 deep-merge。env 映射规范化(现在 setup.py 散着读)。

### 6.5 签名策略(短期 over-engineering,但 enum 思路值得学)

`signed_policy.rs` 解决远程下发配置防篡改:Ed25519 签名 + 编译期内嵌公钥 + 身份绑定防跨租户重放 + on-disk 字节级匹配。

story-lifecycle 单机个人工具,**短期用不上**。仅当出现"远程下发配置""团队统一策略"需求时回头看。但即便不上签名,`SignedVerdict` 的 enum 建模(§6.2 已吸收)、"本地 env 只能加严不能放松"(`resolve_fail_closed_mode`:env 只能 `|| true`)的原则值得记住。

---

## 7. agent 编排、插件信任与 prompt 装配

> 对应 story-lifecycle:`agent_tools.py:6-140`、`stage_library.py:70-184`、`knowledge/context_providers/__init__.py:32-49`、`prompt_renderer.py`、`infra/llm_client_kimi_cli.py`。
> Grok crate:`xai-grok-agent`、`xai-grok-tools`、`xai-grok-plugin-marketplace`、`xai-grok-mcp`。
> 完整代码对照见 `.zcode/grok-analysis-part4-agent-tools-plugins-mcp.md`。

这一节有**两个 P0 级发现**(TrustStore、工具集裁剪权限),比前几节的技术细节更紧迫。

### 7.1 TrustStore:堵 context_provider 的最大安全缺口(P0)

**story-lifecycle 现状**(`context_providers/__init__.py:32-49`):

```python
def _load_provider(cfg):
    module = importlib.import_module(cfg["module"])   # 无脑 exec 任意代码
    cls = getattr(module, cfg["class"])
    ...
    sys.path.insert(0, extra)   # 任意路径注入
    return cls()
```

这是**软 seam 的代价**:try/except 吞异常让它"优雅降级",但代价是**用户随便 pip install 一个 provider 就能在 story-lifecycle 进程里跑任意代码**,没有任何信任管理。

**Grok 怎么做**(`plugins/trust.rs`)

信任粒度是 **per-plugin-root**(不是 per-worktree),信任 key 是 plugin root 的 **canonical 绝对路径**(`dunce::canonicalize`),存储在 `~/.grok/trusted-plugins`(每行一个 canonical path)。未信任插件的分级行为:

```rust
// 未信任插件:
// - Skills 和 agents:**发现并列出**(只元数据,不执行)
// - Hooks、MCP servers、scripts:**阻断**(完全不加载)
```

`is_config_path_auto_trusted`:路径在用户 home 下 → 自动信任;否则要显式 `grant_trust`。canonicalize 失败 → 当不信任处理(**fail-closed**)。

**怎么改**

引入 `TrustStore` 到 `context_providers/`:

```python
# infra/trust.py(新建)
class TrustStore:
    def __init__(self, path="~/.story-lifecycle/trusted-providers"):
        self.trusted = {line.strip() for line in Path(path).read_text().splitlines()}

    def is_trusted(self, module_path: str) -> bool:
        canonical = str(Path(module_path).resolve())   # canonicalize
        if canonical.startswith(str(Path.home())):      # home 下 auto-trust
            return True
        return canonical in self.trusted

# context_providers/__init__.py 改造
def _load_provider(cfg):
    if not trust_store.is_trusted(cfg["module"]):
        logger.warning(f"provider {cfg['module']} 未信任,仅列元数据不实例化")
        return _UntrustedProviderMetadata(cfg)   # 只返回 name/description
    module = importlib.import_module(cfg["module"])
    ...
```

这是**当前最大安全缺口**,优先级高于所有技术借鉴。canonicalize 失败 fail-closed(不加载),home 下 auto-trust(和 Grok 一致,不挡正常开发)。

### 7.2 权限靠工具集裁剪,而非 prompt 拜托(P0)

**story-lifecycle 现状**:AGENTS.md 写"Resolver 只读,Decider 纯函数,Handler 唯一可改 DB"——但这是**靠 prompt 约束**(给 supervisor LLM 的 prompt 里写"你是 Resolver 只读")。模型一旦忽略 prompt,越权无机制阻挡。

**Grok 怎么做**(`types/session_mode.rs` + config.rs)

"Plan mode toolset" 是独立 toolset,注释明确:

> Enforces read-only at the toolset: the agent may inspect the repo and keep a todo list, but `search_replace` (file edits) and `run_terminal_command` (shell) are both omitted so it cannot mutate the workspace.

**read-only 靠不注册 edit/shell 工具实现**,模型根本没有越权的能力位。这是硬保证,不依赖模型遵守 prompt。

**怎么改**

story-lifecycle 的角色(Resolver / Decider / Handler)对应不同 toolset:

- **Resolver toolset**:只给 read/grep/list 工具,**不给 `launch_cli`(改 DB 的能力)**。
- **Decider toolset**:纯查询 + 返回决策,不给任何写工具。
- **Handler toolset**:全工具集(包括 `update_story` / `launch_cli` / `mark_complete`)。

`agent_tools.py:6-140` 的 `ORCHESTRATOR_TOOLS` 现在是全量注册给 supervisor。改成按角色子集注册。这样即使 supervisor LLM 产生越权的 tool_call(比如 Resolver 阶段调 `launch_cli`),工具根本不存在,直接被 function calling 层拒绝。这呼应 AGENTS.md "Handler 唯一可改 DB/起线程"——用**机制**而非**约定**保证。

### 7.3 AgentDefinition + AgentRegistry:治 adapter 硬编码(P1)

**story-lifecycle 现状**:adapter 散在三处——`agent_tools.py:20,48`(JSON enum `["claude","codex","kimi"]`)+ `planner.py:583`(`_next_adapter_fallback` 硬编码轮转)+ `stage_library.py`(不查)。改一个容易漏另两个。

**Grok 怎么做**(`agent.rs` + `builder.rs` + `bridge.rs`):

**`AgentDefinition`(可移植) vs `Agent`(不可移植)** 分离:
- `AgentDefinition`:从 `.grok/agents/*.md` parse 出来的纯数据,serde 序列化、跨进程、不绑 session。
- `Agent`:`AgentBuilder::build()` 把 definition + 具体 session 的 ToolBridge/PromptContext 缝合的产物,doc 写 "NOT portable — tied to a specific session"。

`Agent` 构造后**全私有、effectively immutable**,所有变更走 `ToolBridge` 内部 async 锁(不直接改 Agent)。

动态注册两路径:进程级 `register_tool_pack`(OnceLock 全局表,out-of-tree 代码启动时注入)+ 会话级 `register_mcp_tools`/`unregister_tools_by_prefix`(MCP 上下线时增删)。

**怎么改**

1. 新建 `agent_definition.py`:

```python
@dataclass
class AgentDefinition:
    name: str                      # "claude" / "codex" / "kimi"
    cli_bin: str
    default_model: str | None
    capabilities: set[str]         # {"edit","test","read_only"}
    done_file_template: str        # ".story/done/{key}/{stage}.json"
    scope: Literal["builtin","user","project"]

_REGISTRY: list[AgentDefinition] = []
def register_adapter(d: AgentDefinition) -> None: _REGISTRY.append(d)
def iter_definitions() -> Iterator[AgentDefinition]: return iter(_REGISTRY)
```

builtin 的 claude/codex/kimi 各自调 `register_adapter`。`agent_tools.py` 的 enum 改成 `[d.name for d in iter_definitions()]`(动态),加 adapter 不用改三处 JSON。

2. 运行时上下文单独成 `AgentRunContext`(story_key/workspace/stage/focus/done 路径)。`launch_cli(adapter, stage, focus)` 变成 `launch(definition, ctx)`——这就是 Grok `Agent = definition + session context` 的 Python 对应。

### 7.4 PromptContext + 占位符渲染:替代正则去重(P2)

**story-lifecycle 现状**:`prompt_renderer.py` 的 `_strip_planner_contract_duplicates` 用 `blocked` 关键词集合({"完成后","边界","配置",...})机械砍掉 stage 模板固定段,让 planner 输出和 stage contract 不重复。这种**事后正则去重**维护成本高。

**Grok 怎么做**(`prompt/context.rs`)

system prompt 是 `PromptContext` 这个**可序列化结构体**(不是字符串),render 走 MiniJinja 占位符:

```rust
// 工具名占位符 —— 改名时模板自动跟上
${{ tools.by_kind.read }}    // 替换成当前注册的 read 工具名
${{ tools.by_kind.execute }}
```

`PromptMode::Extend`(base + body 拼接)vs `PromptMode::Full`(body 自包揽)。base template 按 `TemplateOverride`(codex / custom / none)选。

**怎么改**

1. 引入 `PromptContext` dataclass,把散在 `prompt_renderer.py` / `planner._build_agent_system_prompt` 的字段聚成可序列化对象(调试能 dump JSON、resume 能持久化、测试能固定)。
2. stage 模板里"完成标准/输出要求"段写成 `{{ stage_contract.done_section }}` 占位符,planner 输出和 stage contract 各填各的——**天然不重复**,不需要事后正则砍。
3. **PromptMode 二分**治 grill-me 的病:single-pass profile 用 `Full`(body 包揽 design+build+verify),多阶段用 `Extend`。

### 7.5 AGENTS.md 链式发现 + origin stamping(P2)

**这对 story-lifecycle 特别有价值**——因为它自己就是用 AGENTS.md 的(本文档的来源),但它的 supervisor LLM 反而看不到 AGENTS.md(通过 shell-out 调 CLI,CLI 自己读,orchestrator 不注入)。

**Grok 怎么做**(`prompt/agents_md.rs`)

链式发现:从 cwd 往上走到 git root,收集整条链,**root → CWD 顺序**(更深的后出现,deeper overrides)。每个文件加 `## From: {file_path}` 头(origin stamping),整体包在 `<system-reminder>` 块。**全量不截断**(5000 字原样塞,测试明确写 "No cap")。兼容多文件名(`AGENTS.md / Claude.md / CLAUDE.md / .claude/rules/*.md`)。gitignore 过滤。

**怎么改**

给 orchestrator supervisor LLM 装 AGENTS.md 注入(在 `_build_agent_system_prompt` 加一段):用链式发现收集所有 AGENTS.md,带 `## From: {path}` 头拼进 system prompt。这样 supervisor 规划 `plan_step` 时能尊重项目约束("Resolver 只读、Decider 纯函数"这些规则)。

**origin stamping 直接抄**:`## From: {file_path}` 头在 grill-me 中断 resume、verify 发现规划偏离时,能告诉用户"这条约束来自 repo-root/AGENTS.md 还是 workspace/AGENTS.md"。

**全量不截断**:`_load_story_knowledge` 现在把每个 md 截 800 字(`[:800]`),knowledge 可以截,但 AGENTS.md 是行为约束截了丢规则——**两条路分开**。

### 7.6 grill-me 分 Nudge/Gate 两档(P1)

**story-lifecycle 现状**:grill-me 是"LLM 决策 + mode 兜底"的交互式提问,只有"主动提问"一轴。

**Grok 怎么做**(`system_reminder.rs`)

`ReminderPolicy` 分两个独立机制:

```rust
pub struct ReminderPolicy {
    pub todo_nudge: TodoNudgeConfig,   // 软提醒:3 轮没调 todo_write 就戳
    pub todo_gate: TodoGateConfig,     // 硬闸门:turn 结束 todo 没清就强行续命
}
```

- **Nudge**:软提醒,模型可忽略,按"距上次产出产物的轮数"触发,有最小间隔防刷屏。
- **Gate**:硬闸门(默认关,opt-in),turn 结束没完成 todo 就**注入 reminder 强行再续一回合**,直到清空或撞 `max_fires_per_prompt` 上限(默认 2)——这是"最坏情况额外推理成本"的**硬背板**。联动 `carries_task_completion_discipline`:只有 prompt 真的载有完成协议才开火。

**怎么改**

1. grill-me 拆两档:**Nudge**(CLI 跑 N 轮没写 design.md 就注入"考虑产出中间产物")+ **Gate**(done 文件没产出且任务未清空时拒绝接收 done 信号,最多 `max_fires=2` 次强制续命)。
2. `max_fires_per_prompt` 硬背板直接抄——卡死的 CLI 不能无限续命烧 token。
3. **联动 prompt 模板载有规则**:只有该 stage 的 prompt 真载入 grill-me 指令块,才允许 gate 开火。

### 7.7 CompletionRequirement:杜绝"规划了不执行"(P1)

**Grok 怎么做**(`config.rs`):`AgentDefinition.completion_requirement` 声明"本 agent 必须在 turn 结束前调用某工具",和 TodoGate 联动——turn 结束没调 → 注入 reminder → 强行续命。

```rust
pub struct CompletionRequirement {
    pub tool: String,          // "launch_cli"
    pub reminder: String,      // "本 stage 尚未启动 CLI"
    pub recovery: Option<RecoveryPolicy>,  // max_retries/base_delay/max_delay
}
```

**怎么改**:每个 stage 声明 `completion_requirement: {"tool": "launch_cli", "reminder": "本 stage 尚未启动 CLI,请调用 launch_cli"}`。supervisor 规划完但没调 launch_cli 时注入 reminder 强制补——杜绝"规划了不执行"。

### 7.8 其它(adapter 兼容、路径安全、MCP、compaction)

- **ToolServerConfig 的 name_override**:多 adapter 的 CLI 参数名不一致(`--model` vs `-m`),用声明式配置层描述参数映射,而非 if-else 散在 `llm_client_kimi_cli.py`。
- **`SafeRelativePath`**(`plugin-marketplace/types.rs`):context_provider 的 `path` 字段(直接 `sys.path.insert`)是注入点,包一层 newtype 拒绝 `..`/绝对路径,6 种错误枚举照搬。
- **MCP 状态机**(`xai-grok-mcp`):如果要支持 MCP,Empty→Pending→Initializing→Ready 状态机 + 500ms liveness 轮询 + 凭证隔离(`mcp_credentials.json` 不混 config)+ 两层 OAuth 去重(文件锁 + asyncio.Event)。
- **CompactionPolicy**(`compaction.rs`):触发条件从文件数(4 个)改成 token 百分比(85%);`compact_model` 用便宜 model 压缩省成本;`wall_clock_budget_secs=300` 防压 LLM 跑飞;`memory_flush_enabled` 压缩前先沉淀 knowledge(和 dream 联动)。

---

## 8. 不要抄的部分 + 风险提示

### 8.1 明确不抄

| Grok 的东西 | 为什么不抄 |
|---|---|
| markdown 文件 + sqlite-vec 存储布局 | story-lifecycle 明文规定 zero ORM、raw SQL(`db/models.py`),别引入文件型存储 |
| 整个 tool registry / sampling / pager / shell | 3000 文件里 2800 个是 TUI 渲染和采样器协议,跟 Python 后端编排器完全不搭 |
| OTLP 闭集 enum(ExternalKey) | 强类型语言优势,Python 用 `ALLOWED_FIELDS` 白名单 + pydantic 即可 |
| kernel 级沙箱(Landlock/Seatbelt/seccomp) | Rust + libc 才能做,Python 不现实,用容器代替 |
| 签名策略 / MDM / version_overrides | 短期 over-engineering,除非有远程下发/团队管理需求 |
| dream 的全量覆盖写 | 会丢历史,knowledge 要改增量写或靠 prompt merge |

### 8.2 隐私争议(重要)

Grok Build CLI **会上传整个 git repo(含全历史)到 xAI 的 GCS bucket**——12GB 仓库实测传了 5.1GB,而实际任务只需 192KB([The Hacker News 报道](https://thehackernews.com/2026/07/grok-build-uploads-entire-git.html))。

**对 story-lifecycle 的含义**:如果考虑把 Grok Build 当作第 4 个被编排的 coding agent(adapter 加 "grok"),等于让每行被它处理的代码进 xAI 的 `grok-code-session-traces` bucket。**目前不建议纳入编排**——风险对 monorepo 不值得。如果只做 miner 的 transcript adapter(摄入 Grok Build 的 .jsonl),那是纯增量零侵入的,可以考虑。

### 8.3 借鉴的边界

Grok 的设计是为"单 agent + 强类型 + 内核沙箱"优化的。story-lifecycle 是"多 agent 编排 + 动态类型 + 用户信任"。直接搬某些设计(如 fail-closed 整条丢遥测)在 Python 单机工具里过重。**原则:抄设计意图和边界划分,不抄具体实现强度**。

---

## 附录 A:落地优先级与工作量估计

| 优先级 | 借鉴点 | story-lifecycle 文件 | 工作量 | 依赖 |
|---|---|---|---|---|
| **P0** | TrustStore 堵 context_provider 安全缺口 | `knowledge/context_providers/__init__.py:32` | 中 | 无 |
| **P0** | 权限按角色裁剪工具集(机制非约定) | `orchestrator/engine/agent_tools.py:6-140` | 中 | AgentDefinition |
| **P0** | config 原子写 + 深合并 | `infra/config.py:20-45` | 10 行 | 无 |
| **P0** | LLM audit 补 stage 归因 | `infra/llm_client.py:26,554` | 小 | 无 |
| **P1** | LLM audit 脱敏(secret 正则) | `infra/llm_client.py:572` | 小 | 无 |
| **P1** | AgentDefinition/AgentRegistry 解耦 adapter enum | 新建 `agent_definition.py` + `agent_tools.py:20,48` | 中 | 无 |
| **P1** | grill-me 分 Nudge/Gate + max_fires 背板 | `orchestrator/` + `DESIGN-task-actions-and-grill-me.md` | 中 | 无 |
| **P1** | CompletionRequirement 杜绝"规划了不执行" | stage 定义 + planner | 中 | AgentDefinition |
| **P1** | story_state enum 化 | `sourcing/source_loader.py` + 新建 `story_state.py` | 中 | source-driven 迁移 |
| **P1** | doctor 配置自洽检查 | `entry/cli/doctor.py:153` | 小 | 无 |
| **P2** | state CQRS 三分 | `orchestrator/engine/planner.py:1366` | 大 | enum 化先做 |
| **P2** | audit 耗时分解(PhaseTiming) | `infra/llm_client.py` + audit 表 | 中 | 无 |
| **P2** | 接通 policy_engine(deny-wins) | `policy_engine.py` + `planner.py` | 中 | OrchestratorEvent 定义 |
| **P2** | PromptContext + 占位符渲染替代正则去重 | `prompt_renderer.py` | 中 | 无 |
| **P2** | AGENTS.md 注入 supervisor + origin stamping | `planner._build_agent_system_prompt` | 中 | 无 |
| **P2** | CompactionPolicy token 阈值 + compact_model | `planner.compress_context` | 中 | 无 |
| **P3** | knowledge 混合检索(FTS5+向量) | `knowledge/knowledge_store/search.py` | 大 | 引入 sqlite-vec |
| **P3** | knowledge dream 定期提炼 | `orchestrator/learning/reflection.py` | 大 | 检索层先升级 |
| **P3** | STAGE_FAILED 独立事件 | `planner.py` + 事件枚举 | 中 | OrchestratorEvent 定义 |
| **P3** | SafeRelativePath 防 context_provider 注入 | `context_providers/__init__.py:44` | 小 | 无 |
| **P3** | MCP 支持(状态机 + liveness + 凭证隔离) | 新建 `infra/mcp/` | 大 | 视场景 |

建议路径:**P0 立刻做(安全 + 零风险高收益)→ P1 随近期工作顺手做 → P2/P3 按重构窗口规划**。

---

## 附录 B:Grok 侧值得重读的文件

| 文件 | 为什么值得重读 |
|---|---|
| `xai-grok-agent/src/plugins/trust.rs` | TrustStore 范本 —— story-lifecycle 最大安全缺口的参考答案 |
| `xai-grok-agent/src/plugins/hooks_adapter.rs` | env 注入所有权(防 provider pin 原生契约 env) |
| `xai-chat-state/src/{state,queries,mutations,events}.rs` | CQRS 三分的范本 |
| `xai-grok-agent/src/agent.rs` + `builder.rs` | AgentDefinition(可移植)vs Agent(不可移植)分离 |
| `xai-grok-agent/src/prompt/context.rs` | PromptContext 可序列化 + 占位符渲染 |
| `xai-grok-agent/src/prompt/agents_md.rs` | AGENTS.md 链式发现 + origin stamping(story-lifecycle 自己用 AGENTS.md,最该读) |
| `xai-grok-agent/src/system_reminder.rs` | ReminderPolicy 分 Nudge/Gate + max_fires 背板 |
| `xai-grok-memory/src/search.rs` | 混合检索 + 三态合分 + 绝对归一化 |
| `xai-grok-memory/src/dream.rs` | 5 动词 prompt + 三道门 + processed 追踪 |
| `xai-grok-telemetry/src/external/redact.rs` | fail-closed 多层脱敏 |
| `xai-grok-telemetry/src/session_ctx.rs` | 三层 correlation id + 调用点快照 |
| `xai-grok-hooks/src/{event,result,dispatcher}.rs` | Stop/StopFailure + first-deny-wins + fail-open |
| `xai-grok-sandbox/src/deny/mod.rs` | deny 如何赢过 allow + fail-closed |
| `xai-grok-tools/src/types/session_mode.rs` | Plan mode toolset —— 权限靠工具集裁剪 |
| `xai-grok-tools/src/types/requirements.rs` | `Expr<T>` 声明式工具依赖校验 |
| `xai-grok-config/src/signed_policy.rs` | SignedVerdict enum 建模 |
| `xai-grok-config/src/fs_atomic.rs` | 原子写的极简正确实现 |

---

## 附录 C:分析中间产物

本文档基于对 grok-build 12 个 crate、约 160 个 Rust 文件的通读。分析中间产物(含完整代码片段)存于:
- `.zcode/grok-analysis-part2-memory-telemetry.md`(memory + telemetry 完整分析)
- `.zcode/grok-analysis-part3-hooks-sandbox-config.md`(hooks + sandbox + config 完整分析)
- `.zcode/grok-analysis-part4-agent-tools-plugins-mcp.md`(agent + tools + plugins + mcp 完整分析)

(本文为整合后的正式文档,中间产物保留供深入查阅代码细节。)
