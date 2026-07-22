# grok-build 源码阅读:对 story-lifecycle 的设计借鉴要点(agent + tools + plugins + mcp)

阅读范围:`xai-grok-agent`、`xai-grok-tools`、`xai-grok-mcp`、`xai-grok-plugin-marketplace` 四个 crate。下面按 A–J 十节给出 Grok 的做法、对应 story-lifecycle 的现状、以及可操作的改法。

---

## A. Agent 的不可变构造与 ToolBridge

### Grok 怎么做

`Agent`(agent.rs)构造后**字段全私有、没有 `&mut self` 改状态的公开方法**,doc 明确写"effectively immutable after construction"。它持有 `Arc<ToolBridge>`,所有工具层状态变更(MCP 注册、completion 追踪、retry 配置)都走 ToolBridge **内部的 async 锁**,而不是改 Agent 自己。

```rust
pub struct Agent {
    definition: AgentDefinition,          // 不可变定义
    prompt_context: PromptContext,         // 渲染 prompt 的上下文(可序列化)
    system_prompt: String,                 // 缓存的渲染结果
    tool_bridge: Arc<ToolBridge>,          // 工具层 —— 状态变更走它内部锁
    reminder_policy: ReminderPolicy,       // 会话级策略
    compaction_policy: CompactionPolicy,
    hosted_tools: Vec<HostedTool>,
    backend_search_enabled: bool,
}
```

关键分离:**`AgentDefinition`(可移植) vs `Agent`(不可移植)**。
- `AgentDefinition`(config.rs)是从 `.grok/agents/*.md` 的 YAML frontmatter + 正文 parse 出来的纯数据结构,带 `source_path` / `scope`(Project/User/Bundled/BuiltIn)。它能 serde 序列化、能跨进程传递、不绑定任何 session。
- `Agent` 是 `AgentBuilder::build()` 把 definition + 一个具体 session 的 ToolBridge/PromptContext/策略 缝合出来的产物,doc 直接写 "NOT portable — tied to a specific session"。

`AgentBuilder`(builder.rs)是 fluent API,装配流程在 `build()` 里很清晰:

```rust
// 1. resolve definition (from_definition 或 with_* 累积)
// 2. discover skills (list_skills_with_plugins)
// 3. clone definition.tool_config,按 session 能力注入/裁剪工具:
//    - memory_backend / web_search / web_fetch / lsp / image_gen ...
//    - subagents_enabled ? 保留 task 工具并改写其 description : 删除
//    - ask_user_question_enabled / write_file_enabled ...
// 4. ToolBridge::finalize_builder(config, SessionContext{...})
// 5. seed_skill_discovery / seed_agents_md / seed_gitignore_filter
// 6. 构 PromptContext,render 出 system_prompt
// 7. Agent::new(...)
```

中间还有一个会话级"操作员夹钳"机制:`session_tools_allowlist` / `session_tools_denylist`(来自 `--tools`/`--disallowed-tools`)在最后对装配好的 toolset 做交集,绑死无论后续 tool_config 怎么变。注意 `update_policies_from_definition` 的注释:"TODO … Mid-session policy updates are not yet supported" —— 即模式切换目前是**重新渲染 prompt**(`render_prompt_for_definition`)而不是热改状态。

### 对应 story-lifecycle

`orchestrator/engine/agent_tools.py` 里 `plan_step` / `launch_cli` 工具的 `adapter` 字段硬编码 enum `["claude", "codex", "kimi"]`;`planner.py` 的 `stage_to_cli = {name: cfg["cli"] for ...}` 从 profile 反查。这里没有"agent 定义"和"运行时上下文"的分离 —— adapter 名 + stage 名 + focus 字符串直接揉进一次工具调用。

### 具体怎么借鉴

1. **把 adapter 抽象成 `AgentDefinition`**。新建 `orchestrator/engine/agent_definition.py`,把现在散在 profile / planner 里的 per-adapter 信息(cli 二进制名、默认 model、prompt 语言、能力位 can_run_tests / can_edit / read_only、completion 协议路径模板)聚成一个 dataclass:

   ```python
   @dataclass
   class AgentDefinition:
       name: str                      # "claude" / "codex" / "kimi"
       cli_bin: str
       default_model: str | None
       capabilities: set[str]         # {"edit","test","read_only"} 等
       done_file_template: str        # ".story/done/{key}/{stage}.json"
       scope: Literal["builtin","user","project"]
       source_path: Path | None
   ```

   这就解耦了"agent 是什么"和"本次 story 跑它"。`agent_tools.py` 的 enum 改成从 `AgentRegistry` 动态拿名字列表(`definition.name for definition in registry.iter()`),新加 adapter 不用改 enum。

2. **运行时上下文单独成 `AgentRunContext`**:story_key / workspace / stage / focus / 当前 done 路径 / 环境变量。`launch_cli(adapter, stage, focus)` 变成 `launch(definition: AgentDefinition, ctx: AgentRunContext)`。这就是 Grok 的 `Agent = definition + session context` 在 Python 里的对应物。

3. **会话级夹钳**:Grok 的 `session_tools_allowlist/denylist` 思路可以用来实现"single-pass profile 锁死只跑一个 adapter" / "verify 阶段禁用某 adapter" —— 用一个 `SessionAdapterClamp(allow:set, deny:set)` 在最后做交集,而不是散落在 `if stage == "verify"` 硬编码里(正是 grill-me 设计文档 §1.1 抱怨的病根)。

---

## B. prompt 装配的分层

### Grok 怎么做

System prompt 不是一段字符串,而是 `PromptContext`(prompt/context.rs)这个**可序列化的结构体**,render 走 `ToolBridge::render_prompt()`(委托 MiniJinja `TemplateRenderer`)。分层:

- **base template**(template.rs):三种 —— 主会话 `base_template()`、子代理 `subagent_template()`、apply-patch `apply_patch_template()`。XOR 混淆存储、按需解密、`Zeroizing<String>` 用完清零(防 strings 泄漏,非安全边界)。
- **prompt_mode**(PromptMode 枚举):`Extend` = base + body 拼接(默认);`Full` = body 就是完整 prompt,不叠 base。
- **prompt_body**:agent 文件正文 + skills 注入(preload skills 拼到 body 前面)。
- **TemplateOverride**:agent 可以 `system_prompt: codex | custom("…") | none` 覆盖 base template 选择。
- **PromptAudience**:Primary / Subagent。Subagent 用 compact base、砍掉 personas。`normalize_for_persistence()` 在存盘前按 audience 规整。
- **placeholders**:agent 特定字段(`memory_enabled`、`role_instructions`、`os_name`、`working_directory`、`current_date` …)打包成 JSON,merge 进模板上下文。
- **工具名占位符**:`${{ tools.by_kind.read }}` / `${{ tools.by_kind.execute }}` 这种 MiniJinja 变量由 `TemplateRenderer` 从 FinalizedToolset 的 kind→name 映射解析。**工具名绝不硬编码** —— 改名/换 namespace 时模板自动跟上。

render 的核心:

```rust
pub async fn render(&self, tool_bridge: &ToolBridge) -> Option<String> {
    let placeholders = self.placeholders();
    match self.prompt_mode {
        PromptMode::Extend => {
            let base = match &self.system_prompt {
                TemplateOverride::None => if self.audience == Subagent { subagent_template() }
                                         else { base_template() },
                TemplateOverride::Codex => apply_patch_template(),
                TemplateOverride::Custom(s) => s,
            };
            let mut p = tool_bridge.render_prompt(base, &placeholders).await?;
            if let Some(body) = &self.prompt_body {
                p.push_str("\n\n");
                p.push_str(&tool_bridge.render_prompt(body, &placeholders).await?);
            }
            Some(p)
        }
        PromptMode::Full => tool_bridge.render_prompt(self.prompt_body.as_deref()?, &placeholders).await,
    }
}
```

缓存策略:`Agent.system_prompt` 是 build 时 render 一次缓存的字符串;`finalize_prompt()` 在工具层变更后(改名、禁用)重渲染;`compact_system_prompt()` 返回常量(压缩后用的极简 prompt)。`should_auto_compact` 用百分比阈值判断。

### 对应 story-lifecycle

`prompt_renderer.py` 的 `_build_stage_contract` / `_strip_planner_contract_duplicates` 是中文 stage 模板的装配,目前是**字符串拼接 + 正则去重**。`_strip_planner_contract_duplicates` 用一个 `blocked` 关键词集合({"完成后","边界","配置",...})机械地砍掉 stage 模板里的固定段,让 planner 输出和 stage contract 不重复 —— 这种正则维护成本高。

### 具体怎么借鉴

1. **引入 PromptContext dataclass**:把现在散在 `prompt_renderer.py` / `planner._build_agent_system_prompt` 里的字段(story_title / story_key / workspace / profile_stages / 当前 stage / team_knowledge / story_knowledge / 预期产出 / done 路径)聚成一个可序列化对象。好处:①调试时能 dump 成 JSON 看每段来源;②grill-me 的 resume 路径能持久化它;③测试能固定上下文。

2. **模板用占位符渲染而不是正则去重**。把 stage 模板里的"完成标准/输出要求/边界/配置"段写成 `{{ stage_contract.done_section }}` 这种占位符,planner 输出和 stage contract 各自填占位符 —— 自然不会重复,而不是事后正则砍。Grok 的 `${{ tools.by_kind.X }}` 思路在这里对应成 `{{ adapter.cli_bin }}` / `{{ done_file_path }}`,工具调用约定改名时一处改全场跟。

3. **PromptMode 二分**。给 single-pass profile 用 `Full` 模式(body 自己包揽 design+build+verify),多阶段用 `Extend` 模式(base 给通用约束,body 给 stage 特定任务)。这正好治 grill-me 文档 §1.1 的病:verify stage 在 single-pass 下被"禁止跑测试"约束卡住,根因就是没用 mode 区分。

4. **build_timestamp_utc 进 prompt**。Grok 把构建时间戳塞进 PromptContext,prompt 里带"当前构建时间"对模型时间感有帮助。story-lifecycle 的中文 prompt 也可以加 `{{ build_time }}`。

---

## C. AGENTS.md 的读取与注入

### Grok 怎么做

`prompt/agents_md.rs` 是 story-lifecycle 最该细读的一节,因为 story-lifecycle **自己就是用 AGENTS.md 的**。Grok 的做法:

**发现(多源 + 链式 + 去重)**:
```rust
async fn read_agents_config_with_options(working_directory, workspace_user_dir, compat) {
    // 1. 永远先加 grok_home (~/.grok/)
    // 2. 加兼容家目录 (~/.claude/, ~/.cursor/) —— 由 compat 配置门控
    // 3. 从 cwd 往上走到 git root,收集整条链
    // 4. chain.reverse() —— 关键:root → CWD 顺序(更深的后出现,"deeper overrides")
    // 5. workspace_user_dir 插在 repo root 之后(优先级高于 root,低于中间目录)
    // 6. 每个 dir 找 agent_filenames (AGENTS.md / Claude.md / CLAUDE.md ...) 和 rules/*.md
    // 7. gitignore 过滤
    // 8. canonical path 去重(防符号链接 / 大小写不敏感 FS)
}
```

`compat.agent_filenames()` / `compat.rules_dirs()` 是预计算的 gated 列表,walk 时每目录不重复 alloc —— 性能细节。

**origin stamping(标记来源)**:`render_agents_md` 给每个文件加 `## From: {file_path}` 头,整体包在 `<system-reminder>` 块里:

```rust
section.push_str(LEGACY_AGENTS_MD_REMINDER_PREFIX);  // 常量前缀,供恢复时结构检测
section.push_str(" (ordered from repo root to current directory - deeper files take precedence on conflicts):\n");
for config in configs {
    section.push_str(&format!("\n## From: {}\n", config.file_path));
    // rules 文件去掉 YAML frontmatter,避免 globs 元数据泄漏进 prompt
    let content = if is_rules_file { extract_skill_body(&config.content) } else { config.content.clone() };
    section.push_str(&content);
}
section.push_str("\nFollow these instructions exactly. When working in subdirectories not listed above, check for additional project instruction files (AGENTS.md, Claude.md, etc.).");
```

**全量交付,不截断**:测试 `format_agents_md_section_delivers_full_content` 明确写 "No cap: the full content is delivered verbatim, with no truncation marker" —— 5000 字也原样塞。

**audience 处理**:`agents_md_user_reminder()` 对 Primary 和 Subagent **都给全量**(子代理验证者要看到和主 agent 一样的项目指令);只有 personas 对子代理砍掉。

### 对应 story-lifecycle

story-lifecycle 自己有 `AGENTS.md`(给 Claude/Codex/Kimi coding assistant 读),但它**通过 shell-out 调 CLI** —— CLI 进程自己会读 AGENTS.md,story-lifecycle 的 orchestrator 并不主动发现/注入。问题:orchestrator 的 supervisor LLM(做规划的那个)看不到 AGENTS.md,它的规划可能和项目约束冲突。

### 具体怎么借鉴

1. **给 orchestrator supervisor LLM 装 AGENTS.md 注入**。在 `_build_agent_system_prompt` 里加一段,用 Grok 的链式发现逻辑(从 workspace 往上到 git root)收集所有 AGENTS.md,带 `## From: {path}` 头拼成 system prompt 的一段。这样 supervisor 规划 plan_step 时能尊重项目约束(比如"Resolver 只读、Decider 纯函数"这种 AGENTS.md 里的状态机规则)。

2. **origin stamping 直接抄**。`## From: {file_path}` 头极有用 —— grill-me 中断后 resume,或者 verify 发现规划偏离时,能告诉用户"这条约束来自 repo-root/AGENTS.md 还是 workspace/AGENTS.md"。story-lifecycle 的多 workspace / `.story` 目录结构特别需要这个。

3. **全量不截断**原则。story-lifecycle 现在 `_load_story_knowledge` 把每个 md 截断 800 字(`[:800]`),这对 knowledge 还行,但 AGENTS.md 是行为约束,截断会丢规则。AGENTS.md 走全量、knowledge 走截断,两条路。

4. **兼容多供应商文件名**。Grok 扫 `AGENTS.md / Claude.md / CLAUDE.md / .claude/rules/*.md / .grok/rules/*.md`。story-lifecycle 的三 adapter(claude/codex/kimi)各有自己的指令文件习惯(Kimi 有 `.kimi-code/`),可以类似地维护一个 `agent_filenames` 列表,统一发现。

5. **gitignore 过滤**。Grok 用 `ignore` crate 的 `Gitignore` 过滤掉被 ignore 的 AGENTS.md(避免 node_modules 里的污染 story-lifecycle)。Python 侧用 `pathspec` 库等价实现。

---

## D. system_reminder 机制

### Grok 怎么做

`system_reminder.rs` 把"什么时候戳一下模型"做成 **`ReminderPolicy`**,分两个独立机制:

```rust
pub struct ReminderPolicy {
    pub enabled: bool,
    pub todo_nudge: TodoNudgeConfig,   // 周期性提醒
    pub todo_gate: TodoGateConfig,     // turn-end 闸门
}

pub struct TodoNudgeConfig {
    pub enabled: bool,
    pub turns_since_todo_write: u32,   // 默认 3:3 轮没调 todo_write 就戳
    pub turns_between_reminders: u32,  // 默认 5:两次戳至少隔 5 轮
}

pub struct TodoGateConfig {
    pub enabled: bool,                 // 默认 false(opt-in)
    pub max_fires_per_prompt: u32,     // 默认 2:每个 user prompt 最多强行续命 2 次
}
```

两者关系测试很明确:"flipping one must not change the other"。关键设计点:

- **TodoNudge 是软提醒**(模型可以忽略),按"距上次调用 todo_write 的轮数"触发,有最小间隔防刷屏。
- **TodoGate 是硬闸门**(默认关闭,远程设置或 `--todo-gate` 显式开):turn 结束时如果 `TodoState` 还有 pending / 未背书的 in-progress todo,就**注入 system-reminder 强行再续一回合**,直到清空或撞 `max_fires_per_prompt` 上限。上限是"最坏情况额外推理成本"的硬背板。
- 闸门还和 `carries_task_completion_discipline(audience)` 联动:只有当前 prompt 模板**真的载有**完成纪律规则时才开火(目前所有内置模板都返回 false,即闸门保留稳定性,不轻易开火)。

注入载体是 `<system-reminder>` 块(和 AGENTS.md 同一个包装),作为 user message 前缀塞回对话。

### 对应 story-lifecycle

grill-me 文档 §1 写"设计阶段该有追问拉扯 —— CLI 提问、中断等人答、resume 继续"。当前 grill-me 是"LLM 决策 + mode 兜底"。Grok 的 ReminderPolicy 给了**两类机制的两个轴**:被动 nudge(提醒补全 todo) vs 主动 gate(强制续命),而 grill-me 目前只规划了"主动提问"一轴。

### 具体怎么借鉴

1. **把 grill-me 拆成 Nudge 和 Gate 两档**。
   - **Nudge 档**:CLI 跑了 N 轮没产出阶段性产物(没写 design.md / 没跑测试)时,orchestrator 注入一句 system-reminder"你已有 X 轮没更新 done 文件,考虑产出中间产物或调用 grill-me 提问"。软提醒,不阻塞。
   - **Gate 档**:stage 该交付的 done 文件没产出 + 任务清单未清空时,orchestrator **拒绝接收 done 信号**,强制 CLI 再跑一回合(最多 `max_fires_per_prompt` 次,比如 2)。这正是 verify-gate 的通用化。

2. **`max_fires_per_prompt` 硬背板**直接抄。story-lifecycle 的 verify-gate 现在如果没有上限,一个卡死的 CLI 会无限续命烧 token。给每个 stage 一个"最多强制重试 N 次,之后放行 + 标记风险"。

3. **联动 prompt 模板是否载有规则**。Grok 的 `carries_task_completion_discipline` 检查:只有 prompt 里真写了完成协议,闸门才开火。story-lifecycle 的 grill-me 也该检查:只有该 stage 的 prompt 真的载入了 grill-me 指令块,才允许 gate 强制续命 —— 否则模型根本不知道怎么响应。

4. **TodoNudge 的"距上次 X 轮"阈值化思路**用于 done 文件:把 `turns_since_todo_write` 换成 `turns_since_done_update`,默认 3 轮没动 done 就戳。

---

## E. 工具注册的 ToolBridge / registry

### Grok 怎么做

**ToolBridge**(bridge.rs)是 `FinalizedToolset`(注册表)的 session 层适配器,持有 `Arc<FinalizedToolset>` + terminal backend。它自己 `#[derive(Clone)]`,所有方法吃 `&self`,状态全在 `Resources`(注册表内部)的 async 锁里。注释明确:"All state lives in `Resources` on the registry — no separate `ToolState`"。

```rust
#[derive(Clone)]
pub struct ToolBridge {
    registry: Arc<FinalizedToolset>,
    terminal: Option<Arc<dyn TerminalBackend>>,  // 单独存,防 cancel 死锁
}

impl ToolBridge {
    pub fn get_builder() -> ToolRegistryBuilder { ... }
    pub async fn finalize_builder(builder, config: ToolServerConfig, ctx: SessionContext) -> Result<Self>;
    pub async fn tool_definitions(&self) -> Vec<ToolDefinition>;
    pub async fn register_mcp_tools<T>(&self, mcp_name, tool, schema) -> Result<()>;  // 运行时动态注册
    pub fn unregister_tools_by_prefix(&self, prefix: &str) -> usize;                  // MCP 断开时批量删
    pub async fn call(&self, name, params, tool_call_id) -> Result<ToolRunResult>;
    pub async fn render_prompt(&self, template, placeholders) -> Option<String>;      // 委托 TemplateRenderer
    pub async fn tool_for_kind(&self, kind: ToolKind) -> Option<String>;              // kind → 当前名字
}
```

**ToolServerConfig**(registry/types.rs)是"客户端要哪些工具、怎么改名、怎么改参数"的声明式配置:

```rust
pub struct ToolConfig {
    pub id: String,                         // "GrokBuild:read_file"
    pub params: Option<Map<String, Value>>, // 工具参数
    pub name_override: Option<String>,      // 客户端面向名字
    pub params_name_overrides: Option<HashMap<String,String>>, // 参数改名
    pub description_override: Option<String>,
    pub behavior_version: Option<String>,
    pub kind: Option<ToolKind>,             // 自动从 ToolMetadata 派生
}
```

**动态注册两条路**:
1. **进程级 `register_tool_pack`**:`OnceLock<Mutex<Vec<ToolPack>>>` 全局表,out-of-tree 代码在进程启动时调 `register_tool_pack(fn)` 把自己注入,每个新 `ToolRegistryBuilder::new()` 都会跑一遍所有 pack。ordering contract:"MUST run before the FIRST builder"。
2. **会话级 `register_mcp_tools` / `unregister_tools_by_prefix`**:MCP server 上线时 `register_mcp_tools`,断开时 `unregister_tools_by_prefix("mcp/servername/")` 批量删。这就是"动态注册"。

**completion tracking**:`AgentDefinition.completion_requirement: Option<CompletionRequirement>`,声明"本 agent 必须在 turn 结束前调用某工具":

```rust
pub struct CompletionRequirement {
    pub tool: String,          // 必须调用的工具名
    pub reminder: String,      // 没调时的提醒文案
    pub recovery: Option<RecoveryPolicy>,  // max_retries/base_delay/max_delay
}
```

这和 TodoGate 联动:turn 结束没调指定工具 → 注入 reminder → 强行续命。

**tool_taxonomy**(tool_taxonomy.rs + types/tool.rs)用 `ToolKind` 枚举(31 个变体 + `Other` sink)给工具分类,每个 kind 有 `presentation_name()`(统一展示名,跨 toolset 一致:`read_file` 和 `Read` 都显示 "Read")和 `is_read_only()`(分类级默认,个别工具可覆盖)。这是 harness 无关的词汇表。还有 `x.ai/tool` 的 `_meta` 信封(normalization.rs),把工具身份(kind/namespace/read_only)盖戳到每次 tool call 上,跨语言一致。

### 对应 story-lifecycle

`agent_tools.py` 的 `adapter` enum `["claude","codex","kimi"]` 是**编译期硬编码**(虽然是运行时 dict,但加 adapter 要改三处工具定义)。profile 的 `cli` 字段只能填这三个之一。没有"动态注册 adapter"能力,没有"adapter 必须在某 stage 完成某动作"的声明式约束。

### 具体怎么借鉴

1. **建 `AgentRegistry` 单例 + `register_adapter(pack)`**。仿 Grok 的 `register_tool_pack`,在 `orchestrator/engine/agent_registry.py` 里:

   ```python
   _ADAPTER_PACKS: list[AdapterPack] = []
   def register_adapter(pack: AdapterPack) -> None: _ADAPTER_PACKS.append(pack)
   def iter_definitions() -> Iterator[AgentDefinition]: ...
   ```

   builtin 的 claude/codex/kimi 各自调 `register_adapter`。第三方包(story-miner 装了个新 CLI)也能 import 时注册。`agent_tools.py` 的 enum 改成 `{"enum": [d.name for d in iter_definitions()]}`(动态构造 JSON schema)。这就解了硬编码病。

2. **ToolKind 分类思想用于 stage**。story-lifecycle 的 stage(design/implement/test/verify)本质也是"能力类别"。引入 `StageKind` 枚举(read_only / mutating / verifying / planning),profile 的 stage 配上 kind,AdapterDefinition 声明 `capabilities: set[StageKind]`。single-pass profile 让一个 adapter 承担所有 kind,多阶段让各 stage 用匹配 kind 的 adapter。这比"stage 名隐含该干什么"清晰(grill-me 文档 §1.2 的诉求)。

3. **CompletionRequirement 直接抄**。每个 stage 声明 `completion_requirement: {"tool": "launch_cli", "reminder": "本 stage 尚未启动 CLI,请调用 launch_cli", "recovery": {"max_retries": 2, ...}}`。supervisor 规划完但没调 launch_cli 时,注入 reminder 强制补 —— 杜绝"规划了不执行"。

4. **ToolServerConfig 的 name_override / params_name_overrides** 思路用于多 adapter 兼容:claude/codex/kimi 的 CLI 参数名不一致(`--model` vs `-m`),用一个 `CliAdapter` 配置层声明式描述参数映射,而不是 if-else 散在 `llm_client_kimi_cli.py` / 别处。

---

## F. 工具的 requirements / session_mode / permission

### Grok 怎么做

**requirements(types/requirements.rs)** 是工具的**声明式依赖校验**,用 `Expr<T>` 布尔表达式树。三层 eval:

```rust
pub enum Expr<T> { Value(T), And(Vec<Expr<T>>), Or(Vec<Expr<T>>), Not(Box<Expr<T>>), True, False }

pub enum ToolRequirement {
    Tool { namespace, id, if_params: Option<Expr<ToolParamsRequirement>> },  // "需要某工具存在"
    ToolKind { kind: Expr<ToolKind>, if_params },                            // "需要某 kind 存在"
    IfParams { condition, requirement: Box<ToolRequirement> },               // "本工具参数满足 X 时才要求 Y"
    InputParam { kind, param },                                              // "某 kind 工具必须有可见参数 param"
}
```

语义例子:`IfParams` 是"如果我(run_terminal_command)的参数 `enabled_background=true`,则要求必须有 KillTaskAction kind 工具存在"。`finalize` 时对每个工具 eval 它的 `requires_expr`,不满足就报 `Requirements unsatisfied` 拒绝构建。这是"工具之间的完整性约束",声明式、不写过程式 if。

**session_mode(types/session_mode.rs)** 是 closed enum:

```rust
pub enum SessionMode { Default, Plan, Ask }
// 未知 id 解析回 Default(向前兼容,新 mode 不 brick 老客户端)
```

配合 `PermissionMode`(config.rs):`Default / AcceptEdits / Auto / DontAsk / BypassPermissions / Plan`。"Plan mode toolset" 是个独立 toolset,注释明确:"Enforces read-only at the toolset: the agent may inspect the repo and keep a todo list, but `search_replace` (file edits) and `run_terminal_command` (shell) are both omitted so it cannot mutate the workspace." —— **权限靠工具集裁剪硬保证,不是靠 prompt 拜托**。

**persistence(persistence.rs)**:`ResourcesPersistence` 后台 debounce 写盘,mpsc channel 把序列化状态发给后台 task,原子 rename 落盘。`save()` 非阻塞,`flush()` 优雅关闭时调。load 时反序列化喂回 `Resources::load_from`。这是工具状态(todo 列表、skill 状态)跨会话恢复的管道。

**retry(retry.rs)**:`BackoffConfig { max_retries, base_delay_ms, max_delay_ms }`,`execute_with_backoff` 指数退避 + `on_retry` 回调。每个工具可声明自己的 retry config(`ToolRetryConfig`)。

### 对应 story-lifecycle

story-lifecycle 启动 CLI agent 时,权限边界目前靠 **prompt 约束**(给 CLI 的 prompt 里写"你是 Resolver 只读")。设计文档 STATE-MAP 里写"Resolver 只读,Decider 纯函数,Handler 唯一可改 DB",这是状态机角色规则。

### 具体怎么借鉴

1. **权限从 prompt 约束升级成工具集裁剪**。Grok 的 plan-mode toolset 是最值得抄的:read-only 保证**靠不注册 edit/shell 工具**实现,模型根本没能力越权。story-lifecycle 的 Resolver 角色对应一个 `resolver_toolset`(只给 read/grep/list 工具,不给 launch_cli 改 DB 的工具);Handler 角色给全工具集。这样即使 prompt 被模型忽略,权限边界还在。

2. **ToolRequirement 声明式约束用于 stage 组合**。比如 verify stage 声明 `requires = ToolKind("test_runner")` —— 如果该 stage 的 adapter 没测试能力,finalize 阶段就拒绝构建而不是跑到一半失败。single-pass profile 声明 `requires = And[Edit, Test, Plan]`,强制单 adapter 全能。

3. **SessionMode 的向前兼容设计**抄一下。story-lifecycle 的 profile 模式(minimal / single-pass / 全阶段)如果未来加新模式,用"未知模式 fallback 到 Default"而不是抛错,老客户端不 brick。

4. **ResourcesPersistence 的 debounce + 原子 rename** 用于 `.story/done/` 状态文件。高频写 done 状态时,后台 debounce 写避免 IO 抖动,原子 rename 防写一半崩溃损坏文件。Python 侧用 `threading.Thread` + `tempfile.replace` 等价。

5. **声明式 retry**。现在 story-lifecycle 的 verify-gate 重试轮转(`_next_adapter_fallback`)是硬编码顺序,Grok 的 `BackoffConfig` + `recovery` 字段是声明式 per-tool 配置。给每个 adapter 配 `retry: {max_retries: 3, base_delay_ms: 1000}`,失败换 adapter 时也按声明式策略。

---

## G. MCP 集成

### Grok 怎么做

**servers.rs** 是 MCP 客户端生命周期核心。每个 MCP server 有状态机:

```rust
pub enum ClientState {
    Empty,                       // 占位/test
    Pending(PendingTransport),   // transport 就绪等握手
    Initializing,                // 握手中(独占 transport,InitGuard 防丢)
    Ready(McpService),           // 握手完成,Arc 引用计数
}

pub enum LivenessCheck { Healthy, TransportClosed, Transient }
```

`McpState` 管 InitProgress(try_start / finish / cancel / mark_handshaking / mark_server_ready / record_init_failure),事件通过 `client_event_tx` 推给 session dispatcher,后者扇出成 ACP `x.ai/mcp/server_status`。配置 diff(`update_configs_diff`)支持热增删 server。

**liveness.rs**:每个 Ready 客户端 spawn 一个 **one-shot 轮询 task**,500ms 间隔,只在"首次观察到 Ready + transport closed"时 emit 一次 `TransportClosed` 事件然后退出。状态机表很明确(Ready+开=继续,Ready+关=emit+退,其他=静默退)。注释解释了为什么用 polling 而不是 `JoinHandle`:rmcp 2.1 的 `RunningService` 不暴露 shutdown future,`is_transport_closed()` 是仅有的信号,polling 避免改 rmcp。Drop → `DropGuard` cancel token,RAII 清理。

**acp_transport.rs**:ACP = **Agent Client Protocol**(不是 Agent Communication Protocol)。这是把**进程内 SDK MCP server**(SDK 的 `@tool` / `create_sdk_mcp_server`)通过 ACP 反向通道(`x.ai/mcp/sdk_call`)暴露成 rmcp transport 的桥。半双工(v1):只桥 client→server 请求 + 响应,server→client 的 notification / `sampling/createMessage` / `roots/list` 不桥。`AcpReverseInvoker` trait 抽象反调用,本 crate 不依赖 ACP gateway 类型。`invoke_timeout` 复用 HTTP 路径的 `tool_timeout_ms`,零 IPC 和 loopback 共一个预算。

**wire.rs**:跨语言 MCP-over-ACP 协议字符串单源:`MCP_CALL="x.ai/mcp/call"`(前向,pager 调 agent 的 MCP 工具)、`MCP_SDK_CALL="x.ai/mcp/sdk_call"`(反向零 IPC)、`MCP_SERVERS` / `MCP_SDK` 能力旗标。注释:"Reference these constants instead of re-typing the literals so the agent and SDK can't drift apart."

**credentials.rs / oauth.rs / oauth_config.rs**:`McpCredentialStore` 存 `$GROK_HOME/mcp_credentials.json`,key 是 `"{server_name}:{server_url}"`,和 xAI 主 auth(`auth.json`)隔离。OAuth 走 rmcp 的 `AuthorizationManager`(RFC 8414+9728 发现、DCR、PKCE),**proactive discovery**(连之前先探,不是 401 后反应)。两层去重:跨进程(文件锁 `$GROK_HOME/mcp_auth_{name}.lock`)+ 进程内(watch channel,只有一 task 跑 flow)。`CREDENTIAL_POLL_INTERVAL=2s`,等其他窗口/进程完成登录。`mcp_http_client.rs` 是 rmcp streamable-HTTP transport 的退避包装(rmcp 自带零退避 SSE 重连)。

**lib.rs** 明确 MCP crate 的两个职责:① 隔离 `rmcp` 2.1 + `reqwest` 0.13 的依赖(其他 crate 还在 0.12);② 拥有 MCP 集成代码。这是依赖卫生的示范。

### 对应 story-lifecycle

story-lifecycle 目前**不支持 MCP**。它的能力扩展靠 context_providers(自定义软 seam)和 adapter enum(硬编码 CLI)。

### 具体怎么借鉴

1. **MCP 支持值得加,但优先级看场景**。story-lifecycle 的 supervisor LLM 做规划,如果它能直接调 MCP 工具(读 GitHub issue / 查 Linear / 检索 Notion),规划质量会提升。短期可先用 MCP Python SDK 包一层,把 MCP server 注册成 supervisor 的 function tool。

2. **状态机模型抄**。MCP server 不是"连上就用",是 Empty→Pending→Initializing→Ready 的状态机 + InitGuard 防握手丢失。story-lifecycle 如果加 MCP,Python 侧实现同样的状态机:`PendingTransport` / `Initializing`(带锁防并发握手)/ `Ready`(Arc 共享)。liveness 用 `asyncio.Task` + 500ms 轮询,emit `TransportClosed` 后交给 dispatcher 决定重启/报 unavailable。

3. **凭证隔离**原则直接抄。MCP OAuth token 存独立文件(`mcp_credentials.json`),不和 story-lifecycle 的 `config.yaml` 混。key 用 `{server_name}:{url}`。

4. **两层 OAuth 去重**。多 story 并发跑时,同一 MCP server 的 OAuth flow 只跑一次:文件锁跨进程 + `asyncio.Event` 进程内。否则两个 story 同时弹两个浏览器 tab 让用户授权,体验崩。

5. **wire 字符串单源**。如果 story-lifecycle 自己定义协议(比如 supervisor 和 CLI 之间的 done 协议),学 wire.rs 把所有 magic string 集中到一个 `wire.py`,带注释说"SDK 和 orchestrator 共享,改这里两边同步"。

6. **依赖隔离思维**。Grok 为 rmcp 单独建 crate 隔离 reqwest 版本。story-lifecycle 如果引入重依赖(比如 LangChain),考虑类似隔离,别污染核心 orchestrator 的依赖树。

---

## H. 插件市场

### Grok 怎么做

`xai-grok-plugin-marketplace` 是独立 crate,11 个文件分工清晰:

- **types.rs**:`MarketplaceRelativePath` 是 newtype,parse 时**拒绝**绝对路径 / `..` / 前缀 / 当前目录组件 / 逃逸 root。这是路径安全的第一道关:

  ```rust
  pub enum MarketplacePathError { Empty, Absolute, ParentComponent, Prefix, CurrentComponent, EscapesRoot }
  pub struct MarketplaceRelativePath(String);
  ```

- **config.rs**:从 `~/.grok/config.toml` 读 `[[marketplace.sources]]`,每个 source 是 `name + (git url | local path) + branch`。
- **git.rs**:git source 的**持久化缓存**,`~/.grok/marketplace-cache/<url-hash>/`,5 分钟 TTL,文件锁(`fs2::FileExt`)防并发同步,`SourceCacheLease` RAII 持锁,Drop 解锁。`SyncMode::UseTtl / Force`。
- **index.rs**:repo 级 marketplace index,文件查找顺序 `.grok-plugin/marketplace.json`(首选)→ `.claude-plugin/marketplace.json`。`MarketplaceIndex { name, description, owner, plugins: Vec<IndexEntry> }`。注释说 index 比文件系统扫描快、有 curated metadata。
- **catalog.rs**:CI 生成的 `plugin-index.json` 组件目录,**展示层增强 only**,失败降级 None 不阻塞 listing。目录优先级同 index。**关键**:`components_for(name, index_sha)` 对 URL 源条目**校验 pinned SHA** —— catalog 条目的 sha 必须等于 index 锁定的 sha,否则当缺席处理(防陈旧数据)。
- **scanner.rs**:两种模式 —— 有 index 用 index,没有就 walk `plugins/*/` + 解析 manifest。`default-skills/` 当虚拟插件扫。
- **install_resolve.rs**:**纯解析逻辑**,无 IO。`MarketplaceRef { name, qualifier }` 解析 `<name>` / `<name>@<qualifier>`(排除 git URL / GitHub 简写 / 本地路径 / Windows 盘符路径)。`resolve_qualified_source` 把 qualifier 映射到 source:`owner/repo`(GitHub)、`local/<slug>`、`git/<slug>`、或 source 注册名。冲突返 `Ambiguous([indices])`。`select_bare_name` 多源同名时**官方源优先**(单一官方副本赢,报其他副本数),否则 Ambiguous。
- **matcher.rs**:纯关键词匹配,无 regex。`KeywordCandidate { name, domains, keywords }`,候选有效关键词 = keywords + domains(去 scheme/www/path)+ name,长关键词优先,ASCII 词边界守卫。draft < 3 字符不匹配。
- **installer.rs**:路由到现有 `InstallRegistry + git_install` pipeline,加 marketplace provenance 到 installed repo 记录。`MarketplaceInstallResult::Installed | AlreadyInstalled`。

**冲突解决三原则**:① qualifier 精确匹配唯一源;② 多源官方优先;③ 都不唯一报 Ambiguous 让用户用 `@qualifier` 消歧。路径安全 + SHA 锁定是信任基础。

### 对应 story-lifecycle

story-lifecycle 的 context_providers(`knowledge/context_providers/`)是插件式软 seam:`config.yaml` 配 `module` + `class`,`importlib.import_module` 动态加载,失败返 None 不阻塞。这和 marketplace 的"展示层失败降级"哲学一致,但 story-lifecycle 没有"市场"概念 —— provider 是本地单实例,没有多源/版本/SHA 锁定。

### 具体怎么借鉴

1. **`MarketplaceRelativePath` 的路径安全**直接抄。context_provider 的 `path` 配置字段(目前直接 `sys.path.insert`)是注入风险点。包一层 `SafeRelativePath` newtype,拒绝 `..` 和绝对路径。Grok 的 6 种错误枚举(`Empty/Absolute/ParentComponent/Prefix/CurrentComponent/EscapesRoot`)值得逐条照搬成 Python。

2. **catalog SHA 锁定**思想用于 knowledge 包。story-lifecycle 消费 `packages/knowledge` 的 schema(scenario/playbook/failure),如果 schema 版本漂移,context_provider 可能解析错。给 knowledge 包的 schema 加版本号 + provider 加载时校验"schema version 匹配",不匹配降级。

3. **官方源优先 + Ambiguous 报错**用于多 provider 冲突。如果用户配了多个 context_provider 都能服务同一 story,选 default transcript miner(官方),其他报 Ambiguous 让用户消歧,而不是静默选第一个。

4. **git 缓存的 TTL + 文件锁**如果 story-lifecycle 未来支持"知识库市场"(团队共享 playbook 仓库),直接抄:5 分钟 TTL 缓存 + 文件锁防并发 clone。

5. **index vs filesystem fallback** 双模式。story-lifecycle 的 knowledge 目录如果有 `index.json`(curated metadata)就用,没有就 walk 目录。比纯 walk 快且元数据准。

6. **provenance 记录**。installer.rs 给每个装的市场插件记 provenance(source name + commit SHA)。story-lifecycle 的 context_provider 加载时也该记"来自哪个 module path + 哪个 config 版本",出问题能追溯。

---

## I. 插件信任与 hooks_adapter

### Grok 怎么做

**trust.rs** 是项目插件信任管理,核心洞察:**项目目录(`.grok/plugins/`)的插件是执行面** —— 克隆的 repo 可能带恶意 hook 脚本或 MCP 命令。

```rust
// 信任粒度:per-plugin-root(不是 per-worktree),信一个插件不信同 repo 其他插件
// 信任 key:plugin root 的 canonical 绝对路径(dunce::canonicalize)
// 信任存储:~/.grok/trusted-plugins(每行一个 canonical path)

pub struct TrustStore { trusted: HashSet<PathBuf>, file_path: PathBuf }

// 未信任插件的行为:
// - Skills 和 agents:**发现并列出**(只元数据)
// - Hooks、MCP servers、scripts:**阻断**
```

`is_config_path_auto_trusted`:config `[plugins].paths` 的条目如果在用户 home 目录下,**自动信任**;否则要显式 `grant_trust`。CLI `--plugin-dir` 始终信任(CliOverride scope)。canonicalize 失败 → 当不信任处理(fail-closed)。

**manifest.rs**:`PluginManifest` 从 `plugin.json` 解析,forward-compatible(**不设** `deny_unknown_fields`,未知字段静默忽略,新版本 manifest 仍能 load)。

```rust
pub struct PluginManifest {
    pub name: String,                    // 必需,kebab-case,最长 64
    pub version: Option<String>,
    pub author: Option<Author>,
    pub skills: Option<PathOrPaths>,     // 组件路径覆盖(补充约定目录)
    pub commands: Option<PathOrPaths>,
    pub agents: Option<PathOrPaths>,
    pub hooks: Option<PathOrInline>,     // 路径或内联 JSON
    pub mcp_servers: Option<PathOrInline>,
    pub lsp_servers: Option<PathOrInline>,
}
```

**路径安全**:`PathOrPaths::resolve(plugin_root)` 过滤掉用 `..` 逃逸 plugin root 的路径(`is_path_contained`),警告并排除。fallback 位置 `plugin.json` → `.grok-plugin/plugin.json` → `.claude-plugin/plugin.json`。没 manifest 也能跑(约定式发现 skills/ agents/ .mcp.json hooks/hooks.json),名字从目录名推。

**hooks_adapter.rs**:桥接插件 hook JSON 和共享 `xai-grok-hooks` runtime,**不是第二个 hooks 引擎**。三步:
1. **预过滤不支持事件**:从 hook JSON 删掉 `SUPPORTED_EVENTS` 之外的 event key(PascalCase + snake_case 都接受:v0 的 `SessionStart/PreToolUse/PostToolUse/SessionEnd`,v2 的 `Notification/Stop/UserPromptSubmit/SubagentStart/SubagentEnd`),避免 parse 失败。
2. **parse_hook_file** 解析过滤后的内容。
3. **注入插件 env vars**:`GROK_PLUGIN_ROOT` / `CLAUDE_PLUGIN_ROOT`(native + 兼容别名)、`GROK_PLUGIN_DATA` / `CLAUDE_PLUGIN_DATA`。**关键**:插件 adapter 对这些 key 拥有所有权,插件作者声明的 `env` 对这些特定 key **必须让位**(防插件故意 pin root 到任意路径破坏契约)。用户声明的非冲突 key 保留。然后 hook 名字加 namespace `plugin/{name}/{hook_name}`,命令路径做 `${CLAUDE_PLUGIN_ROOT}` 替换 + 通用 env 展开。

**discovery.rs / registry.rs**:PluginScope 优先级 `CliOverride(0) > Project(1) > User(2) > ConfigPath(3)`。LoadedPlugin 带 `trusted: bool`(执行操作要信任)、`enabled: bool`(不在 disabled 列表)、所有组件的解析路径 + 计数。PluginId 含 scope label。name 冲突记 `conflict: Option<String>`。

### 对应 story-lifecycle

story-lifecycle 的包间 seam 是**软的**(try/except imports),context_providers 动态加载 module。这相当于 Grok 的"始终信任 + 无 manifest 约定式发现",**没有任何信任管理**。如果 context_provider 的 module 来自不可信源(用户随便 pip install 一个),它能在 story-lifecycle 进程里跑任意代码。

### 具体怎么借鉴

1. **引入 `TrustStore`**。这是 story-lifecycle 当前最大的安全缺口。`context_providers/__init__.py` 的 `_load_provider` 直接 `importlib.import_module(cfg["module"])` —— 等于无脑 exec。加一层:
   - module path 在用户 home 下 → auto-trust(和 Grok 一致)。
   - module path 在 project workspace 下 → 要显式 `~/.story-lifecycle/trusted-providers` 登记。
   - 失败 fail-closed(canonicalize 失败 → 不信任 → 不加载)。
   - 未信任 provider:**只列元数据**(name/description),**不实例化**(对应 Grok 的"hooks/MCP/scripts 阻断")。

2. **ProviderManifest** 抄 plugin.json 概念。给 context_provider 包加一个 `provider.json`:`{name, version, capabilities, requires_db, env_vars}`。forward-compatible(未知字段忽略)。`path`/`class` 字段从绝对路径改成相对 plugin root,带 `..` 逃逸检测。没 manifest 也能跑(约定 `provider.py` + `class Provider`)。

3. **hooks_adapter 的 env 注入所有权**思想。如果 story-lifecycle 的 provider 接受 env 配置,adapter 注入的 `STORY_WORKSPACE` / `STORY_KEY` 等"原生契约 env"必须**覆盖**用户配置的同名 env,防 provider 故意 pin。这条很容易被忽略但是关键防御。

4. **PluginScope 优先级**用于 context_provider 多源:`CliOverride > Project(.story/context_providers/) > User(~/.story-lifecycle/providers/) > ConfigPath(config.yaml)`。同名 provider 高优先级 shadow 低优先级,记 `conflict` 字段。

5. **预过滤不支持事件**思路。如果 story-lifecycle 未来给 provider 暴露 hook(比如 `pre_stage_start` / `post_done`),用白名单过滤 provider 声明的 hook 事件名,未知事件静默跳过 + warning,而不是整个 provider 加载失败。

6. **forward-compatible manifest**。Python 侧用 `dataclass` + 忽略未知字段(`**kwargs` 收集或 pydantic `extra="ignore"`),新版本 provider 在老 story-lifecycle 上不报错。

---

## J. compaction 策略

### Grok 怎么做

`compaction.rs` 比想象中短,因为它是**策略声明**,执行在别处(sampler/session 层):

```rust
pub struct CompactionPolicy {
    pub auto_compact_threshold_percent: u32,  // 默认 85:context 用到 85% 触发
    pub compact_model: Option<String>,         // None=用当前 model;Some=专用压缩 model
    pub memory_flush_enabled: bool,            // 压缩前先跑 memory flush turn
    pub wall_clock_budget_secs: u64,           // 默认 300:单次压缩 wall-clock 预算
    pub two_pass_enabled: bool,                // 默认 false:双 pass 预取
}
```

触发判断在 `Agent::should_auto_compact`:

```rust
pub fn should_auto_compact(&self, total_tokens: u64, context_window: NonZeroU64) -> bool {
    xai_token_estimation::exceeds_threshold(
        total_tokens, context_window.get(),
        self.compaction_policy.auto_compact_threshold_percent as u8,
    )
}
```

即 `usage_percent = total_tokens * 100 / context_window; trigger if usage_percent >= threshold`。测试覆盖:< 阈值 false,> 阈值 true,== 阈值 true(边界含),0 token false,100% 阈值只在满时触发。

**压缩后的 prompt**:`compact_system_prompt()` 返回常量 `"You are an AI coding agent. You operate in a workspace..."` —— 压缩后用极简 prompt,丢弃原 system prompt 的所有工具约定细节(因为历史已经压缩成摘要,模型重新开始)。

**memory_flush_enabled**:压缩前先让模型把重要信息 summarze 进 memory,避免压缩丢关键信息(和 compact 互补)。

**two_pass_enabled**:预取双 pass —— 接近阈值时后台 speculatively 总结历史前缀(pass 1),压缩时总结 NOTE₁ + 最近尾部(pass 2)。默认 false,实验性。

**wall_clock_budget_secs**:单次压缩 generation 超这个预算就 cut + retry,防 reasoning 模型跑飞 token 限制兜不住。

### 对应 story-lifecycle

story-lifecycle 的 `compress_context`(planner.py)逻辑是:`.story/context/` 下超过 4 个 `.md` 文件触发,用 LLM 把历史压成 `compressed.md`,原文件 archive 不删。触发条件是**文件数**(4 个),不是 token 用量。压缩 prompt 是"保留关键决策、约束、已验证结论、未解决问题,去除过程细节(adapter 选择、model 配置)"。

### 具体怎么借鉴

1. **触发条件从文件数改成 token 百分比**。Grok 的 `usage >= 85% context_window` 是精确的;story-lifecycle 的"4 个 md 文件"是粗略代理 —— 4 个 100 字文件和 4 个 10000 字文件天差地别。Python 侧用 tiktoken 估 token,阈值化触发。如果 orchestrator supervisor LLM 的 context window 是 128k,85% = 108k 时压。

2. **声明式 CompactionPolicy dataclass** 抄过来:

   ```python
   @dataclass
   class CompactionPolicy:
       auto_compact_threshold_percent: int = 85
       compact_model: str | None = None       # 用便宜 model 压缩
       memory_flush_enabled: bool = False
       wall_clock_budget_secs: int = 300
       two_pass_enabled: bool = False
   ```

   `compact_model` 用便宜 model(如 haiku)做压缩,省成本。story-lifecycle 当前用 `get_llm()` 默认 model 压,贵。

3. **memory_flush_enabled 思路**用于 knowledge 包。压缩前先把历史里的 scenario/playbook/failure 提取写进 knowledge store,然后压缩对话。这样压缩丢的不是知识(已沉淀),是过程噪音。这和 story-lifecycle 的 story-miner flywheel 哲学一致 —— 压缩和 mining 联动。

4. **wall_clock_budget** 直接抄。压缩 LLM 调用加超时(300s),超了 cut + retry,防 reasoning model 跑飞。story-lifecycle 现在 `llm.invoke(prompt, temperature=0.2)` 没超时。

5. **compact_system_prompt 常量**思路。压缩后如果 supervisor 重新开始,用一段极简 prompt(只说"你是 story-lifecycle 编排器,继续推进当前 story"),而不是把完整的 5KB system prompt + AGENTS.md 重新塞进去 —— 那些已经在压缩摘要里了。

6. **two_pass 实验**。story-lifecycle 如果 context 经常撞上限,开 two_pass:接近阈值时后台先压一遍(pass 1),真触发时只压增量(pass 2),减少单次压缩延迟。默认关,有需求再开。

---

## 横向总结(优先级排序)

按"对 story-lifecycle 的杠杆 / 实施成本"排序,最值得动手的:

| 优先级 | 借鉴点 | 对应节 | 杠杆 |
|---|---|---|---|
| **P0** | TrustStore 给 context_provider 加信任管理 | I | 堵最大安全缺口(当前无脑 importlib) |
| **P0** | 权限靠工具集裁剪而非 prompt 约束 | F | Resolver/Decider/Handler 角色硬保证 |
| **P1** | AgentDefinition/AgentRegistry 解耦 adapter enum | A/E | 治硬编码病,支持第三方 adapter |
| **P1** | ReminderPolicy 分 Nudge/Gate + max_fires 背板 | D | grill-me 直接落地,verify-gate 通用化 |
| **P1** | CompletionRequirement 声明式 | E | 杜绝"规划了不执行" |
| **P2** | PromptContext dataclass + 占位符渲染 | B | 替代正则去重,可调试可持久化 |
| **P2** | AGENTS.md 链式发现 + origin stamping 注入 supervisor | C | 让规划尊重项目约束 |
| **P2** | CompactionPolicy token 阈值 + compact_model | J | 省成本,精确触发 |
| **P3** | 路径安全 SafeRelativePath | H/I | 防注入,6 种错误枚举照搬 |
| **P3** | MCP 状态机 + liveness(如果要支持 MCP) | G | 长期方向,短期可选 |
| **P3** | ToolRequirement 声明式 stage 组合约束 | F | single-pass 全能校验 |

**两个反复出现的设计哲学**(贯穿十节):

1. **声明式 > 过程式**。Grok 用 `Expr<ToolRequirement>` 布尔树、`ToolServerConfig` 配置、`CompactionPolicy` 数据结构表达"系统该怎么 behave",而不是 if-else 散落代码。story-lifecycle 的 `if stage == "verify"` / `if _is_single_stage` 是反面教材。

2. **状态变更走单一同步通道,对外不可变**。`Agent` 构造后不可变,所有变更走 `ToolBridge` 内部锁;`TrustStore` 的 canonicalize-then-check;MCP 的 `ClientState` 状态机 + InitGuard。这和 story-lifecycle 的 "Handler 唯一可改 DB/起线程" 完全同构 —— Grok 在工具层把这条原则贯彻得更彻底,值得对标。
