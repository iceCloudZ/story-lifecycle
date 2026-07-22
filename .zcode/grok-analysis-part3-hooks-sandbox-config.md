# Grok Build 三 crate 源码对 story-lifecycle 的设计借鉴分析

阅读了 `xai-org/grok-build` 的 `xai-grok-hooks`(14 文件)、`xai-grok-sandbox`(8 文件)、`xai-grok-config`(14 文件)全部 Rust 源码,并对照 `story-lifecycle` 的 `AGENTS.md`、`infra/config.py`、`orchestrator/engine/policy_engine.py`、`orchestrator/engine/agent_tools.py`、`orchestrator/engine/claude_stream.py`、`entry/cli/setup.py`、`entry/cli/doctor.py` 做了映射。下面分小节给出"Grok 怎么做 / 对应 story-lifecycle 哪里 / 怎么借鉴"。

---

## A. hooks 事件模型与 deny 语义(event.rs / result.rs / dispatcher.rs)

### Grok 怎么做

事件名是一个显式枚举,分成几组生命周期(`event.rs`):

```rust
pub enum HookEventName {
    // 会话
    SessionStart, SessionEnd,
    Stop, StopFailure,          // turn 结束 vs turn 因 API 错误结束
    // 工具
    PreToolUse, PostToolUse,
    PostToolUseFailure,         // 工具调用抛错
    PermissionDenied,           // 被权限系统拦了
    // 用户/通知
    UserPromptSubmit, Notification,
    // 子 agent
    SubagentStart, SubagentStop, SubagentEnd(=SubagentStop 别名),
    // 压缩
    PreCompact, PostCompact,
}
```

关键设计有三处:

**1. `Stop` 与 `StopFailure` 分开。** `Stop` 是 turn 正常结束(完成/取消/错误都算),`StopFailure` 专门是"turn 因 API 错误结束"。注释明确:`StopFailure` 时 hook 的输出和退出码被忽略——因为上游已经失败了,hook 再 deny 没有意义。这就是把"正常完成"和"上游故障"建模成两个状态,而不是一个布尔。

**2. 只有 `PreToolUse` 是 blocking,其余全是 non-blocking。**

```rust
pub fn is_blocking(&self) -> bool {
    matches!(self, Self::PreToolUse)
}
```

只有 `pre_tool_use` 能 deny;`post_tool_use` / `session_end` 等只是观察点(fire-and-forget)。dispatcher 因此有两个函数:`dispatch_pre_tool_use`(返回 `HookDecision`)和 `dispatch_non_blocking`(返回 `Vec<HookRunResult>`,永不 deny)。

**3. deny 语义 + first-deny-wins + fail-open。** `result.rs`:

```rust
pub enum HookDecision {
    Allow,
    Deny { reason: String, hook_name: String },  // 带原因 + 哪个 hook 拒的
}
```

dispatcher 串行跑所有匹配的 hook,任何一个返回 `Deny` 就短路、阻断工具调用。但 hook **崩溃/超时/输出畸形**走 fail-open——记进 `HookRunResult::Failed` 给 UI 回放,但不阻断。`dispatcher.rs` 的文档注释把这个威胁模型写得很直白:

> Grok runs in protected environments where induced-failure bypass of security hooks is not part of the threat model; the previous fail-closed posture over-blocked innocent tool calls when hooks timed out.

也就是说:**fail-open 是一个产品取舍**(避免误杀),不是安全设计;真正的安全靠沙箱(见 D 节)。

### 对应 story-lifecycle

`story-lifecycle` 目前 **没有 hook 机制**(grep 全树无结果)。但它有两处是 hook 的雏形:
- `orchestrator/engine/policy_engine.py` 的 `GUARDED_RULES` 矩阵 `(GuardedAutonomy, ActionCategory) -> AutonomyLevel`,本质就是一张"事件 × 动作 → allow/confirm/shadow/forbidden"的 deny 表。
- `orchestrator/engine/claude_stream.py` 监听 coding agent 的 stdout,检测 `permission_request` / `elicitation`,调 `supervisor.decide_response()`——这就是一个 `Notification`/`PermissionDenied` 观察点。

### 怎么借鉴

**1. 给编排过程定义一个 `OrchestratorEvent` 枚举,用 Grok 的分组。** story-lifecycle 的阶段是 design→implement→test,直接映射:

```python
class OrchestratorEvent(str, Enum):
    # 会话生命周期
    STAGE_START = "stage_start"        # 对应 SessionStart
    STAGE_END = "stage_end"            # Stop
    STAGE_FAILED = "stage_failed"      # StopFailure —— 关键:和 STAGE_END 分开
    # agent 调用
    PRE_AGENT_INVOKE = "pre_agent_invoke"   # 对应 PreToolUse,唯一 blocking
    POST_AGENT_INVOKE = "post_agent_invoke"
    AGENT_AWAITING = "agent_awaiting"       # claude 的 permission_request
    # 编排决策
    PRE_ROUTING = "pre_routing"             # 跳过/重试/失败前
```

`STAGE_FAILED` 必须独立,正好命中 `AGENTS.md` 的规则"跨系统状态超出 true/false 要建模成 enum"——`STAGE_END` 是正常收尾(可跑清理 hook),`STAGE_FAILED` 是 agent 崩了(清理 hook 该跳过,不该再 deny)。

**2. deny 结果带 `reason + hook_name`,落到 `supervisor_decision` 事件。** story-lifecycle 已经有 `log_decision`(`supervisor` 模块),直接复用。Grok 的 `HookDecision::Deny { reason, hook_name }` 两个字段正是为了让 UI 能说清"谁拦的、为什么",对应 `AGENTS.md` 的"每个非可执行分支必须有可见反馈和日志"。

**3. blocking/non-blocking 二分,复用现有 `AutonomyLevel`。** story-lifecycle 的 `AutonomyLevel(APPLY/CONFIRM/SHADOW/FORBIDDEN)` 已经比 Grok 的二元 `Allow/Deny` 更细。借鉴点是:**只对 `PRE_AGENT_INVOKE` 这一个事件做 deny 决策**(等价 Grok 的"只有 PreToolUse blocking"),其余事件(`POST_AGENT_INVOKE`、`STAGE_END`)只做观察+日志,不阻断。这样 deny 链路短、易测试。

---

## B. hooks 发现与信任(discovery.rs / trust.rs / matcher.rs)

### Grok 怎么做

**发现是分层 + 加性合并。** `discovery.rs` 的 `HookSource` 枚举:

```rust
pub enum HookSource<'a> {
    SettingsFile(&'a Path),  // 单个 settings.json
    Directory(&'a Path),     // ~/.grok/hooks/*.json 目录
}
```

`load_hooks_from_sources(global_sources, project_sources)` 把 global 先加载、project 后加载,加性合并进 `HookRegistry`(按事件类型索引的 `HashMap<HookEventName, Vec<HookSpec>>`)。global hook 名字加前缀 `global/`,project hook 不加前缀,这样能区分来源。registry 是快照——磁盘改动只在下次会话生效。

**信任机制值得注意:trust.rs 已经"退化"成空壳。** 注释明说:

```rust
// Project-hook trust is no longer stored here: the shell's folder-trust store
// (~/.grok/trusted_folders.toml) is the single authority for whether a repo's
// project hooks run (the same gate as repo-local MCP/LSP).
```

也就是说 hook 信任 **复用了文件夹信任**(和 repo-local MCP/LSP 同一道门)。`trust.rs` 只剩:(a) 从 legacy `trusted-hook-projects` 文件做一次性迁移;(b) `disabled-hooks` 黑名单(按 hook 名整行禁用/启用)。没有签名验证 hook 脚本本身——信任的对象是"文件夹",不是"脚本内容"。

**matcher 有一个反直觉但很关键的细节。** `matcher.rs`:

```rust
enum MatcherKind { All, Exact(Vec<String>), Regex(Regex) }
```

判定逻辑:**只含 `[A-Za-z0-9_|]` 的 pattern 是精确匹配(或 `|` 分隔的精确列表),否则才是 regex。** 这是为了避开 `^a|b|c$` 的锚定 bug(naive regex `^a|b|c$` 只锚定首尾,会错误匹配)。而且做 external→Grok 别名展开:`"Bash"` 能匹配 Grok 内部的 `run_terminal_command`(通过 `grok_names_for`)。空白不 trim:`"   "` 是匹配不到任何工具的 regex(不是 match-all),防止把 deny 闸变成 deny-all。

### 对应 story-lifecycle

- `knowledge/context_providers/` 已经是"从文件系统发现 provider"的雏形,类似 Grok 的 `Directory` 源。
- `orchestrator/engine/agent_tools.py` 的 `adapter: enum["claude","codex","kimi"]` 是工具名,但没有匹配层。
- 启动外部 coding agent 的安全边界目前没有"信任哪个 workspace"的闸。

### 怎么借鉴

**1. 信任对象选"文件夹/workspace"而非"脚本",复用 story-lifecycle 的 workspace 概念。** story-lifecycle 已经有 `workspace`(.story 目录所在),可以加一个一次性信任闸:首次在某个 workspace 跑 hook/外部 agent 时,提示用户确认(类似 git 的 "are you sure you want to run scripts from this repo")。存到 `~/.story-lifecycle/trusted_workspaces.yaml`。这比给每个 hook 脚本做签名简单得多,且足够防"clone 一个恶意 repo 就自动跑脚本"。

**2. matcher 如果要做,优先精确匹配,别上 regex。** story-lifecycle 的工具是固定三个 adapter(claude/codex/kimi),用 `Literal["claude","codex","kimi"]` 或精确集合匹配即可,完全不需要 regex。Grok 用 regex 是因为它要兼容各家 CLI 的工具名别名(Bash/Edit/Write...),story-lifecycle 没这个负担。

**3. 全局/项目 hook 加性合并 + 全局加前缀。** 如果将来加 hook,`~/.story-lifecycle/hooks/*.yaml` 是全局,`<workspace>/.story/hooks/*.yaml` 是项目,合并时全局 hook 名前缀 `global/`,便于在 UI 日志里区分"这个 deny 是用户全局策略还是项目配置"。

---

## C. hook 执行器:command + http + 环境变量展开的安全(runner/command.rs / runner/http.rs / env_expand.rs)

### Grok 怎么做

两种 runner:`command`(spawn 子进程,stdin 喂 envelope JSON,读 stdout/stderr)和 `http`(POST envelope 到 URL)。两者返回同样的 `{"decision":"allow"|"deny","reason":"..."}`。

**command runner 的 exit code 语义(`runner_command.rs`):**

```rust
const DENY_EXIT_CODE: i32 = 2;
```

- exit 0 = allow;exit 2 = deny(JSON 的 decision 优先于 exit code);其它非零 = 失败(fail-open)。
- stdout/stderr 截断到 64KB 防内存爆。
- 如果命令含 shell 元字符(空格/管道/`&&`/`$`/`~`)走 `sh -c`,否则直接 exec。
- **spawn 前预检未解析的环境变量**(`find_unresolved_env_vars`):如果命令引用了 `${VAR}` 但该变量在 runner 常量集 / extra_env / 进程环境 / 命令内本地赋值 里都找不到,就**拒绝 spawn**,报 "required env var(s) not set"。理由是:否则 `sh` 会把它展开成空,产生畸形命令、exit 127、fail-closed 给个不透明的错误。提前拦住给模型一个可操作的错误。带 modifier 的(`${VAR:-default}`)不拦,因为用户已经显式处理了未设置情况。

**http runner 的 SSRF 防护(`runner_http.rs`)是这套代码里最值得抄的部分:**

```rust
fn is_blocked_ip(ip: &IpAddr) -> bool {
    match ip {
        IpAddr::V4(v4) => {
            let o = v4.octets();
            if o[0] == 127 { return false; }              // loopback 放行(本地 dev)
            if o[0] == 10 { return true; }                // RFC1918
            if o[0]==172 && (16..=31).contains(&o[1]) { return true; }
            if o[0]==192 && o[1]==168 { return true; }
            if o[0]==169 && o[1]==254 { return true; }    // 云元数据 169.254.169.254!
            if o[0]==100 && (64..=127).contains(&o[1]) { return true; } // CGNAT
            if v4.is_unspecified() { return true; }       // 0.0.0.0
            false
        }
        // IPv6: loopback 放行,其余 link-local/ULA 拦
    }
}
```

校验强制 **HTTPS-only**,且先 DNS 解析再查每个解析出的 IP 是不是私网/元数据地址(CWE-918)。**最关键:`169.254.169.254` 被显式拦截**——这是 AWS/GCP/Azure 的云元数据端点,hook 如果能 POST 到它,就能偷实例凭证。Grok 把整个 git repo 上传 xAI 的隐私争议背景下,这个 IP 拦截尤其重要。

**secret 泄漏防护:`raw_url` vs `url` 的 display/exec 分离(`result.rs`):**

```rust
pub struct HttpInfo {
    pub url: String,           // 展开后的真实 URL,仅用于 SSRF 调试
    pub raw_url: Option<String>, // 配置文件里的原始串,用于给用户看
    ...
}
```

`HttpInfo.url` 的注释极其详尽:它是展开后的真实目标,如果用户用 `${TOKEN}` 注入了 secret,这个字段里就带 secret。**任何要给用户展示的层必须用 `raw_url`**,否则 `?token=ghp_REAL_SECRET` 会泄漏到 scrollback / 日志 / wire DTO。而且 `reqwest::Error::Display` 会自动追加 URL,所以错误处理特意用 `e.without_url()` 剥掉 URL 再自己拼 `log_url`(偏好 raw 形式)。这是 defense-in-depth:既分离字段,又在错误格式化时再剥一次。

**env_expand 的"无损展开"(`env_expand.rs`):** `${VAR}` 解析时,未设置的变量和带 modifier 的形式(`${VAR:-default}`)都**原样保留**,这样:加载时展开是幂等的;运行时才该解析的变量(如 `CLAUDE_PLUGIN_ROOT`)能撑过加载期、在运行时二次展开时解析。还用 per-call 随机 sentinel(`\u{f8ff}__GROK_HOOKS_MASK_<128bit>__\u{f8ff}`)把 modifier 形式从 shellexpand 里"藏"起来再还原,避免固定 sentinel 撞上用户输入。

### 对应 story-lifecycle

story-lifecycle 调外部 LLM API(`infra/llm_client.py`)和外部 coding agent CLI,API key 在 config.yaml 里。如果将来加 HTTP hook(比如"每次 stage 开始 POST 到一个 webhook"),SSRF 和 secret 泄漏是真实风险。

### 怎么借鉴

**1. 如果加任何 HTTP 出站调用(webhook / 远程决策服务),抄 `is_blocked_ip`。** Python 等价:

```python
import ipaddress
def is_blocked_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if addr.is_loopback:
        return False  # 本地 dev 放行
    if addr.is_private or addr.is_link_local or addr.is_unspecified:
        return True
    return False
# 特别硬编码:169.254.169.254(云元数据)单独拦,因为 is_link_local 已覆盖但语义要显式
```

强制 HTTPS,解析 DNS 后逐 IP 校验。`requests`/`httpx` 默认会跟重定向,要禁止或对重定向目标再校验一次。

**2. secret 永远不进日志/UI —— 用 raw/display 分离。** story-lifecycle 的 config 里有 `api_key`、`base_url`(可能带 token)。建议给 config 加一个 `display_value()` 方法:`api_key` 显示成 `sk-***1234`,`base_url` 里的 `?token=` 显示成 `?token=***`。Grok 的教训是:`reqwest::Error` 默认会把 URL 打出来,Python 的 `requests` 异常也会带 URL。所以异常处理要 `str(e).replace(url, "***")` 或用 `e.__class__.__name__` + 自定义 message。

**3. 环境变量展开用"无损 + 运行时二次展开"两段式。** story-lifecycle 的 prompt 模板里如果引用 `${STORY_STAGE}` 这类变量,加载时先无损展开一次,运行时(env 已知)再展开一次。这能避免"加载时 STORY_STAGE 还没设置就展开成空"的 bug。

---

## D. 沙箱的 deny 列表与 profile(deny/mod.rs / deny/glob.rs / profiles.rs / types.rs / child_net.rs)

### Grok 怎么做

**profile 是预设的安全级别。** `profiles.rs`:

```rust
pub enum ProfileName {
    Workspace,   // 默认:只 workspace 可写,全盘可读,网络开
    Devbox,      // 除 /data 外全盘可写(开发机)
    ReadOnly,    // 仅最小可写,网络关
    Strict,      // 严格(只读+网络关)
    Off,         // 关
    Custom(String),
}
```

每个 profile 解析成 `SandboxProfile`:

```rust
pub struct SandboxProfile {
    pub read_only: Vec<PathBuf>,
    pub read_write: Vec<PathBuf>,
    pub deny: Vec<PathBuf>,        // 完全禁止(覆盖 read_only/read_write)
    pub default_read: bool,        // 是否默认全盘可读
    pub restrict_network: bool,
}
```

**deny 的优先级高于 allow。** 这正是 Landlock/Seatbelt 的难点——`deny/mod.rs` 用了大量篇幅解决"deny 如何赢过 allow"。macOS Seatbelt 上,`(deny file-write* ...)` 单独不赢(因为 nono 在 read-allow 和 write-allow 之间插了 platform rule,workspace 的宽 write-allow 在 deny 之后 emit、最后匹配胜出)。解法是 deny 每个**具体的 write 子动作**:

```rust
const SEATBELT_WRITE_DENY_ACTIONS: &[&str] = &[
    "file-write-data", "file-write-create", "file-write-unlink",
    "file-write-mode", "file-write-owner", "file-write-flags",
    "file-write-times", "file-write-setugid",
];
```

这样既挡 overwrite 也挡 rename/unlink(`mv x y && cat y` 的绕过)。还要处理 macOS 的 `/private` firmlink 别名(`/tmp` ↔ `/private/tmp`),否则 deny canonical 形式会被 alias 绕过。

**deny 失败"fail closed":** 如果一个 deny 路径无法表达成 Seatbelt filter(比如含控制字符),直接 `bail!`,让 shell 的 `!is_applied` 闸拒绝启动——而不是"沙箱报告已激活但实际漏了"。`deny/mod.rs` 注释:

> Fail CLOSED: a deny path we can't express ... would otherwise be silently unprotected while the sandbox still reports active.

**deny glob 的跨平台一致性(`deny/glob.rs`):** glob deny(`*.env`、`**/.ssh`)在 macOS 编译成运行时 regex(覆盖启动后新建的文件),在 Linux 启动时展开成具体匹配(bwrap 只能挂载存在的路径)。两者用一个 `validate_deny_glob` 保证 accept/reject 完全一致,拒绝 `{`/`}`/`\`(globset 支持 brace 但 Seatbelt regex 无法忠实复现)。注释强调 parity invariant:

> `validate_deny_glob` accepts/rejects identically on both platforms, and the accepted subset translates the SAME on both.

**项目 profile 不能覆盖全局自定义 profile(`profiles.rs`):**

```rust
/// Project config may **add** new profile names only. It cannot redefine
/// a name already present in the global config — last-write-wins would let
/// a malicious workspace hollow out a user/enterprise custom profile
/// (e.g. empty deny / broad read_write) while keeping the trusted name.
```

`sandbox_profile_conflicts()` 检测这种冲突,`merge_project_profiles` 只用 `entry().or_insert()`(全局已有就不覆盖)。

**网络拦截在子进程层(`child_net.rs`):** 进程级网络是开的(agent 要调 LLM API),子进程网络用 **seccomp BPF 过滤**拦截 `connect/bind/sendto/sendmsg/listen/accept/accept4`:

```rust
let blocked_syscalls: &[i64] = &[
    SYS_connect, SYS_bind, SYS_sendto, SYS_sendmsg,
    SYS_listen, SYS_accept, SYS_accept4,
];
```

返回 `EPERM`。注意:**只拦出站连接类,不拦 socket 创建**——这样 agent 进程能调 API,但它 spawn 的子进程(跑用户代码的 bash)不能联网。这是"父进程开网、子进程断网"的精准切分。

**降级策略(`lib.rs` 的 `apply`):** 沙箱不可用时不 crash,记 `ApplyFailed` 事件继续跑;但 `is_applied()` 反映真实状态,shell 用它决定是否启动。

### 对应 story-lifecycle

story-lifecycle 启动 claude/codex/kimi 这些外部 coding agent(`claude_stream.py` 的 `subprocess.Popen`),agent 会在用户 repo 里跑命令、改文件。目前**没有任何文件系统/网络层面的权限控制**——完全信任 agent CLI 自己的权限系统。`agent_tools.py` 的 `adapter` 枚举只决定调谁,不决定"它能干什么"。

### 怎么借鉴

**1. profile 概念直接对应 story-lifecycle 的 `GuardedAutonomy`。** story-lifecycle 已有 L0-L5 自治级别,policy_engine 已有 `ActionCategory(CODE_MODIFY/DESTRUCTIVE/...)`。可以加一个**文件系统 profile 层**:
- 对应 `Workspace`:agent 只能改 `<workspace>/.story` + 指定源码目录。
- 对应 `ReadOnly`:`design` 阶段(agent 只该读不该写)。
- 对应 `Strict`:test 阶段在隔离目录跑。

但 Python 做 kernel 级沙箱不现实(Landlock/Seatbelt 是 Rust + libc)。**现实可行的等价物**:用 OS 已有的隔离——Windows 上没有好方案,Linux/macOS 可以考虑用 Docker/Podman 容器包一层(给 agent 一个只挂载 workspace 的容器),或者至少用 `--add-dir-ro` / 只读 bind mount。这比 reimplement seccomp 现实。

**2. "deny 优先于 allow"的语义直接抄到 policy_engine。** story-lifecycle 的 `GUARDED_RULES` 矩阵目前是 `(autonomy, category) -> level`,单层。借鉴 Grok 的 deny-wins:**任何一条规则说 FORBIDDEN,最终就是 FORBIDDEN**,即使别的规则说 APPLY。Grok 的 `first_deny_wins` + `allow_then_deny_denies` 测试就是这个语义。对应 story-lifecycle:如果同时命中"用户全局策略禁止 MODEL_SWITCH"和"当前 autonomy 允许",应该禁止。

**3. "项目配置不能覆盖全局受信任 profile"反 hijack。** story-lifecycle 如果加 workspace 级配置(`<workspace>/.story/config.yaml`),要保证它**只能加严、不能放松**用户全局配置。Grok 的 `entry().or_insert()` + conflict 检测是简洁实现。对应 story-lifecycle:workspace config 里把某 autonomy 从 L2 降到 L4(放松)应该被忽略或警告。

**4. 子进程网络拦截的思路:agent 跑用户代码时断网。** story-lifecycle 的 `test` 阶段 agent 会跑测试、可能执行生成代码。如果担心恶意 story 让 test 阶段 `curl` 外泄,可以在 `test` 阶段的 subprocess 上设 `env` + 用 OS 防火墙规则,或至少在 `doctor` 里提示"test 阶段建议在容器里跑"。Grok 的"父开网/子断网"切分值得记住。

---

## E. 配置的签名策略(signed_policy.rs)

### Grok 怎么做

这是三个 crate 里**设计最精巧**的一个。解决的问题:**远程下发的配置(managed config)如何防止被本地篡改/伪造/跨租户重放。**

机制:服务器用 Ed25519 私钥对"配置内容 + 绑定的 principal(team_id 或 deployment_id) + 过期时间"签名,客户端用**编译期内嵌的公钥集**验证。核心函数:

```rust
pub fn verify_signed_payload(
    signed_payload: &str,
    signature_b64: &str,
    trusted_keys: &[(&str, &[u8])],
) -> Result<SignedPayload, SigError> {
    let payload: SignedPayload = serde_json::from_str(signed_payload)?;
    let (_, public_key) = trusted_keys.iter()
        .find(|(id, _)| *id == payload.key_id)   // key 由签名内的 key_id 选
        .ok_or(SigError::UnknownKeyId)?;
    ring::signature::UnparsedPublicKey::new(&ring::signature::ED25519, public_key)
        .verify(signed_payload.as_bytes(), &sig)?;
    Ok(payload)
}
```

**几个关键设计决策:**

**1. 公钥编译期内嵌,不是环境变量。** 注释:`// Compile-time, not an env flag: the local attacker controls their env.` 本地攻击者控制自己的环境变量,所以信任根必须是二进制里烧死的常量。当前 `EMBEDDED_DEPLOYMENT_CONFIG_PUBKEYS = &[]`(空),即"暗发布"——没嵌入 key 时签名验证不激活,行为不变;等 key ship 进二进制才生效。编译期还用 const assert 保证每个 key 是 32 字节、key_id 唯一。

**2. 验签 key 由签名内的 `key_id` 选,不是由外部 hint。** 注释:`// safe to read pre-verification because selection can only land within the trusted set (a forged id either misses or picks a key the signature won't match)`。一个伪造的 key_id 顶多导致验签失败,不会选中错误的 key。

**3. 身份绑定 + 防跨租户重放。** `check_fetch_identity`:deployment 签名的 payload 仅凭签名就信;team 签名的必须匹配当前 active team。`signed_principal_matches` 是 at-rest 检查:另一个租户的 cache 读不到外国数据。当一方未知时宽松(避免 auth.json 读 blip 把 session 搞挂)。

**4. on-disk 字节级匹配——挡的不只是删除,更是就地改。** `check_on_disk_matches`:

```rust
// 签名说该槽位是某内容 → 磁盘必须逐字节一致
// 签名说该槽位空(absent) → 磁盘必须真的空(本地种一个 requirements.toml = 篡改)
```

还检测 `non_regular_file_at`:目录/符号链接/fifo 蹲在槽位上算篡改(no-follow,即使指向字节相同的文件也算)。

**5. fail-closed 是签名 payload 里的 opt-in,不是本地 marker。** `SignedCacheFacts.fail_closed` 从签名字节读,不从可伪造的 marker 读。而且本地环境变量 `GROK_MANAGED_CONFIG_FAIL_CLOSED` **只能加严(强制开),不能放松**——admin 设了 fail_closed,本地 env 关不掉:

```rust
fn resolve_fail_closed_mode(requirements: &toml::Value) -> bool {
    fail_closed_flag(requirements) || env_bool(FAIL_CLOSED_ENV) == Some(true)
    // env 只能 OR 进 true,不能把文件的 true 变 false
}
```

**6. verdict 是一个显式 enum,不是布尔。**

```rust
pub enum SignedVerdict {
    Inactive,           // 暗构建(无内嵌 key)
    NoAuthenticSidecar, // 无 sidecar 或签名不过
    SidecarUnreadable,  // 瞬时 IO 错(非篡改)→ fallback 到 marker
    Trusted,            // 签名有效且绑定本 principal
    Compromised,        // 签名有效但已被改/过期/绑别处 → 拒
}
```

`SidecarUnreadable`(EACCES 这种瞬时错)和 `Compromised`(真篡改)分开——前者 fallback 不拒绝,后者无条件拒绝。这正是 `AGENTS.md` 说的"跨系统状态超出 true/false 要建模成 enum"。

### 对应 story-lifecycle

story-lifecycle 的 config.yaml(`~/.story-lifecycle/config.yaml`)是**纯明文、本地完全可控、无任何完整性校验**。目前威胁模型里这没问题(单机个人工具)。但如果将来有:
- 团队共享配置(远程下发统一 prompt/策略);
- CI 环境里 config 被注入;
- "企业策略要求所有 story 必须 L2 confirm" 这种需要防本地绕过的场景。

那签名策略就有价值。

### 怎么借鉴

**1. 不需要现在就上签名,但 `SignedVerdict` 的 enum 建模思路值得学。** story-lifecycle 加载 config 时,现在 `get_config()` 失败就返回空 dict、YAML 错就返回空——把"文件不存在""YAML 畸形""字段缺失""字段类型错"全压成一个"空"。借鉴 Grok,应该返回一个 enum:

```python
class ConfigLoadResult(Enum):
    OK = auto()
    MISSING = auto()        # 首次运行,引导 setup
    MALFORMED = auto()      # YAML 畸形 → 报错别静默吞
    INCOMPLETE = auto()     # 缺关键字段(api_key)→ doctor 报告
```

这直接服务 `AGENTS.md` 的"每个非可执行分支要有可见反馈"。现在 `get_config` 吞掉 `yaml.YAMLError` 返回空,用户配置写坏了会无声地当成"未配置",难诊断。

**2. "本地 env 只能加严不能放松"的规则。** 如果 story-lifecycle 将来有 `STORY_REQUIRE_CONFIRM=true`(强制所有 apply 走 confirm)这种安全开关,环境变量应该只能 `|| true` 加严,不能把配置里的 true 关成 false。Grok 的 `resolve_fail_closed_mode` 是范本。

**3. 签名如果真要做,公钥必须进包,不是 env。** Python 里可以放进 `story_lifecycle/_trusted_keys.py` 作为常量,用 `cryptography` 库的 Ed25519 验签。配置文件旁放 `.sig` sidecar,加载时验。但这是 over-engineering 除非有明确的远程下发需求。

---

## F. 配置的原子写入与分层覆盖(fs_atomic.rs / config_override.rs / managed_cache.rs / version_overrides.rs)

### Grok 怎么做

**原子写(`fs_atomic.rs`)极简但正确:**

```rust
pub(crate) fn write_atomically(
    final_path: &Path, contents: &str, mode: Option<u32>,
) -> std::io::Result<()> {
    static WRITE_NONCE: AtomicU64 = AtomicU64::new(0);
    let dir = final_path.parent().unwrap_or(Path::new("."));
    let nonce = WRITE_NONCE.fetch_add(1, Ordering::Relaxed);
    let tmp = dir.join(format!("{name}.{pid}.{nonce}.tmp"));  // 唯一名
    let mut options = std::fs::OpenOptions::new();
    options.write(true).create_new(true);                     // 不覆盖已存在
    #[cfg(unix)] if let Some(mode) = mode { options.mode(mode); }  // 创建时就设权限
    let result = options.open(&tmp)
        .and_then(|mut f| f.write_all(contents.as_bytes()))
        .and_then(|()| std::fs::rename(&tmp, final_path));    // 原子 rename
    if result.is_err() { let _ = std::fs::remove_file(&tmp); }
    result
}
```

三个要点:(a) temp 名用 `pid + 全局计数器`保证并发写不撞;(b) `create_new` 不覆盖;(c) 权限在 temp 创建时设(不是写后 chmod),这样最终文件从没以更松权限存在过。写失败清理 temp。

**分层合并(`lib.rs` 顶部明示 6 层,低 → 高):**

```
1. /etc/grok/managed_config.toml        (系统)
2. $GROK_HOME/managed_config.toml       (用户 managed)
3. $GROK_HOME/config.toml               (用户)
4. $GROK_HOME/requirements.toml         (云缓存,签名)
5. /etc/grok/requirements.toml          (系统 requirements)
6. macOS MDM (ai.x.grok, admin-forced)  (企业)
```

合并用 `deep_merge_toml`:table 递归合并、非 table(叶节点/数组)整体替换。

```rust
pub fn deep_merge_toml(base: &mut toml::Value, overrides: &toml::Value) {
    if let (Table(bt), Table(ot)) = (&mut *base, overrides) {
        for (k, v) in ot {
            if let Some(existing) = bt.get_mut(k) { deep_merge_toml(existing, v); }
            else { bt.insert(k.clone(), v.clone()); }
        }
    } else { *base = overrides.clone(); }  // 叶节点:整体替换
}
```

**version_overrides:按 CLI 版本 gate 的补丁。** `version_overrides.rs`:

```toml
[[version_overrides]]
minimum_version = "1.7.0"
[version_overrides.features]
logging = true
```

加载时按当前 CLI 的 semver,把匹配的 override(`minimum_version <= 当前 < maximum_version`)按 `minimum_version` 升序 deep-merge 进去。每层各自先 apply自己的 override,再跨层合并。**失败策略分两档:** 普通加载时 version_overrides 无效 → 跳过该层(soft-fail);但若该层 `fail_closed = true`,启动时校验失败直接退出(见 G 节)。

**campaigns(A/B 测试覆盖,`campaigns.rs`):** 带幂等 id 的 overlay,优先级 `requirements > remote > user > managed > system_managed`(首个 id 胜)。可以 dismiss(按 id),`ids_touching_paths` 能判断某个 campaign 是否影响了某个配置路径(用于判断"dismiss 它会不会影响当前显示的值")。

**managed_cache(`managed_cache.rs`):** 云配置的本地缓存 marker,记录 `synced_at / principal / had_managed_config / had_requirements / fail_closed`。**unsigned、user-writable**——注释强调它只是"刷新提示",不是防篡改手段(防篡改靠签名 + OS 层)。staleness 判定基于 marker 字段而非 mtime。`SyncMarker` 是 struct 而非相邻的几个 bool 参数——注释:`// A struct (destructured without ..) so a new field is a compile error at every writer — three adjacent positional bools would silently transpose.`

### 对应 story-lifecycle

`infra/config.py` 现状:

```python
def save_config(config: dict):
    existing = get_config()
    merged = _merge_config(existing, config)   # 浅合并!
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(yaml.dump(merged, ...), encoding="utf-8")  # 非原子!
```

两个问题:(a) `_merge_config` 是 `dict.update` **浅合并**,嵌套 dict 会被整体覆盖;(b) `write_text` 非原子,写一半进程挂了留半个文件。优先级只有"用户 config.yaml"一层,没有系统/环境变量分层(`setup.py` 里读 `os.environ` 但散落各处)。

### 怎么借鉴

**1. 原子写立即抄(Python 版):**

```python
import os, tempfile
def save_config_atomic(config: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    merged = _merge_config_deep(get_config(), config)
    fd, tmp = tempfile.mkstemp(dir=CONFIG_DIR, prefix=".config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(yaml.dump(merged, allow_unicode=True))
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, CONFIG_FILE)  # 原子 rename
    except Exception:
        os.unlink(tmp); raise
```

`tempfile.mkstemp` + `os.replace` 等价 Grok 的 `create_new + rename`。story-lifecycle 的 config 写得勤(setup 向导、每次 autonomy trace),非原子写迟早出半个文件。

**2. 浅合并改深合并。** `_merge_config` 现在是 `merged = dict(existing); merged.update(updates)`。如果 config 有嵌套(比如 `features: {logging: true, telemetry: false}`),`update` 会整体替换 `features`。改成递归:

```python
def _merge_config_deep(base: dict, updates: dict) -> dict:
    for k, v in updates.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = _merge_config_deep(base[k], v)
        else:
            base[k] = v
    return base
```

**3. 显式的优先级链。** story-lifecycle 现在优先级隐式(config.yaml + 散落的 os.environ)。借鉴 Grok 在 `infra/config.py` 顶部注释明确优先级链:

```
1. 命令行 flag (--api-key)          最高
2. 环境变量 (STORY_API_KEY)
3. workspace config (.story/config.yaml)
4. 用户 config (~/.story-lifecycle/config.yaml)
5. PRESET_PROVIDERS 默认             最低
```

然后写一个 `load_effective_config()` 按这个顺序 deep-merge。环境变量映射规范化(现在 setup.py 里散着读)。

**4. version_overrides 的思路:按 story-lifecycle 自身版本 gate 配置补丁。** 如果将来配置 schema 变了(加了字段、改了默认),可以用类似机制让旧 config 文件在新版 story-lifecycle 上自动迁移,而不是在加载处散着写 `if config.get("new_field") is None`。

---

## G. 配置校验(validation.rs)

### Grok 怎么做

`validation.rs` 校验的对象很窄:**只校验 `requirements.toml` 层的 `fail_closed` + `version_overrides` 组合**。不是泛泛的"schema 校验"。核心逻辑:

```rust
fn validate_requirements_value(v: toml::Value, source) -> Result<(), RequirementsError> {
    let fail_closed = resolve_fail_closed_mode(&v);   // 先读 fail_closed
    let version = xai_grok_version::installed_semver();
    if let Err(e) = apply_version_overrides(&mut v, &version)
        && fail_closed
    {
        return Err(RequirementsError::InvalidVersionOverrides { .. });
    }
    Ok(())
}
```

**关键顺序:** 先读 `fail_closed` 再 apply override——防止一个坏 patch 在加载中途把 `fail_closed` 关掉。`resolve_fail_closed_mode` 是文件 flag `||` env-tighten(env 只能开不能关,见 E 节)。

**两档失败语义明确分:**
- **soft-fail(加载时):** `load_requirements_layer` 里 version_overrides 无效 → 返回 `None`(跳过该层),不报错。
- **fail-closed(启动校验时):** `validate_requirements`(在 `main()` 调一次)里,若该层 `fail_closed=true` 且 override 无效 → 返回 `Err`,二进制退出。

注释点明这两条路径故意独立:

> Re-reads the file independently from `load_requirements_layer`: at startup both run, costing one extra small read per layer. Sharing the parse would couple loader+validator APIs for negligible gain.

即:加载(soft)和校验(fail-closed)读两次文件,不共享解析——为了 API 解耦,代价仅一次小文件读。

**错误类型带 provenance:** `RequirementsError::InvalidVersionOverrides { path, source }`,错误消息含具体文件路径和子错误,不丢上下文。`RequirementsSource` 是 `File(PathBuf) | Mdm` 的 enum——MDM 层没有文件,所以 source 是 label 不是 path,类型上就防止了"对一个没文件的层调 exists()"。

**MDM 层和文件层同等强制:** `validate_requirements` 对 user file、system file、MDM 三个都跑,MDM 用 raw value(fail_closed 保留),保证企业策略和文件策略执行强度一致。

### 对应 story-lifecycle

`entry/cli/doctor.py` 现在校验的是**外部工具是否存在**(claude/codex/git/zellij/ttyd 用 `which` 检测),`has_missing_tools()` 返回布尔。`setup.py` 的 `is_configured()` 只看 `config.get("api_key")` 是否非空。**配置 schema 本身没有校验**——provider 不在 PRESET_PROVIDERS 里、base_url 格式错、model 名拼错,都不会被 doctor/setup 发现,只在运行时炸。

### 怎么借鉴

**1. doctor 加一个"配置自洽"检查块,学 Grok 的窄而精确校验。** 不要做全 schema 通用校验(过度工程),只校验"会导致运行时失败的关键组合"。具体在 `doctor.py` 加:

```python
def check_config_consistency() -> list[str]:
    problems = []
    cfg = get_config()
    provider = cfg.get("provider")
    if provider and provider not in {p["name"] for p in PRESET_PROVIDERS.values()}:
        problems.append(f"provider={provider!r} 不在已知列表,custom 需手填 base_url")
    if provider == "custom" and not cfg.get("base_url"):
        problems.append("provider=custom 但 base_url 为空")
    key = cfg.get("api_key", "")
    if key and not key.startswith(("sk-", "ANTHRO", "GLM")):  # 粗略格式提示
        problems.append("api_key 格式可能不符(仅提示)")
    return problems
```

这比 Grok 校验的还宽,但都是 story-lifecycle 真实会踩的坑。

**2. 错误带文件路径 + 子原因。** doctor 报问题时别说"配置错误",要说"`~/.story-lifecycle/config.yaml` 的 provider='deepseek' 但 base_url 指向 anthropic.com"。Grok 的 `RequirementsError` 带 path + source 是范本。

**3. 两档失败:setup 向导 soft-fail(填错了重新问),doctor/启动 fail-closed(致命配置错就拒启)。** story-lifecycle 的 `story serve` 启动时如果 config 严重不一致(比如 provider 指向不存在的 base_url),应该启动期就报清楚,而不是等到第一次 LLM 调用 404 才发现。可以在 `story serve` 入口加一个 `validate_config_or_exit()`。

**4. 校验和加载分离。** Grok 让 validate 和 load 各读一次文件换取 API 解耦。story-lifecycle 的 `get_config()` 现在既被 setup 用、又被 doctor 用、又被 orchestrator 用,职责混在一起。可以拆:`load_config()`(纯读)、`validate_config(cfg)`(纯函数,返回 problems list)、`effective_config()`(读+合并环境变量)。这符合 `AGENTS.md` 的"Resolver 只读,Decider 纯函数"。

---

## H. 其它值得注意的(campaigns.rs / macos_managed.rs / shell.rs)

### campaigns.rs —— A/B 测试配置

campaigns 是**带幂等 id 的配置 overlay**,优先级 `requirements > remote > user > managed > system_managed`。可以 dismiss(按 id),dismissed id 存磁盘。最巧的是 `ids_touching_paths`:判断"某个 campaign 是否影响某个配置路径",这样 UI 能标注"你看到的这个 `models.default` 值,是 campaign `exp1` 改的,要不要 dismiss"。`config_override.rs` 的 `patch_touches_path` 还处理一个边界:patch 用标量替换整个 table(`models = "oops"`)会抹掉 `models.default`,所以这种 patch 也算 touches 子路径。

**对 story-lifecycle 的借鉴:** 如果将来做 prompt/策略实验(比如"对一半 story 用新 design prompt"),campaign 机制是范本。但更近期的借鉴是 **`patch_touches_path` 的思路用于 autonomy trace**:story-lifecycle 的 `_write_autonomy_trace` 记录决策,如果加一个"标注当前显示的某个值是哪条规则驱动的",可追溯性大增。

### macos_managed.rs —— 企业 MDM 管理

通过 macOS `ai.x.grok` preference domain 读 admin-forced 的 base64 TOML。**只读 admin-forced 值**(本地用户改自己 preference domain 无效),所以不可伪造。**关键:`$VAR`/`${VAR}` 在这一层故意不展开**——因为这是可信的 admin 层,用本地进程环境展开会让"forced check 本要排除的那个用户"反过来影响策略。注释:

> The forced payload is used verbatim — `$VAR`/`${VAR}` are deliberately NOT expanded: this is the trusted, non-forgeable admin layer, and expanding from the local process environment would let the very user the forced check excludes influence the policy.

**对 story-lifecycle 的借鉴:** story-lifecycle 目前没有企业/团队管理需求,这条短期用不上。但"可信层不展开环境变量"是个原则:如果将来加"团队统一 prompt 模板"功能,模板里的变量展开要在受控环境做,不能让本地 env 污染。

### shell.rs —— Windows shell 检测

Windows 上检测 shell 的优先级:`pwsh → powershell.exe → Git Bash → cmd.exe`。**故意优先 PowerShell 而非 Git Bash**,注释理由非常具体:

> MSYS2/Git Bash performs POSIX-to-Windows path translation, mangling every flag starting with `/` (e.g. MSBuild `/t:Build`, cl.exe `/nologo`). This breaks native Windows C++/C#/.NET builds.

`GROK_SHELL` 环境变量可覆盖,结果进程内缓存。

**对 story-lifecycle 的借鉴:** story-lifecycle 在 Windows(`env` 显示 win32 + Git Bash)上跑,`claude_stream.py` 用 `subprocess.Popen` 起 coding agent。如果 agent 内部要起 shell 跑命令,这个 story-lifecycle 控制不了(agent 自己的事)。但 story-lifecycle 自己的脚本(比如 `story doctor --fix` 装 tool、`run_doctor_fix` 跑 winget/brew)如果涉及 shell 选择,值得抄这个优先级——尤其"Git Bash 会把 `/` 开头的 flag 当路径翻译"这个坑,在 Git Bash 环境跑 `pip install` 带 `/` 参数会出诡异错。story-lifecycle 的 `platform_ops.py` / terminal 层可以参考。

---

## 总结:最高 ROI 的借鉴(按优先级)

1. **config 原子写 + 深合并**(`infra/config.py`):立刻可做,防配置损坏,Python 10 行代码。
2. **config 加载返回 enum 而非空 dict**(`infra/config.py`):让"未配置/畸形/缺字段"可区分,直接服务 doctor 和 `AGENTS.md` 的反馈规则。
3. **doctor 加配置自洽检查**(`entry/cli/doctor.py`):窄而精,只查会导致运行时炸的关键组合,带文件路径的错误信息。
4. **deny-wins 语义**(`policy_engine.py`):`GUARDED_RULES` 改成"任一 FORBIDDEN 即 FORBIDDEN",符合"stricter deny takes precedence"。
5. **`OrchestratorEvent` enum + STAGE_FAILED 独立**:给编排过程加观察点时,正常结束和失败结束分开(对应 Grok 的 Stop vs StopFailure)。
6. **SSRF/secret 防护**(任何 HTTP 出站):如果加 webhook,抄 `is_blocked_ip` + raw/display URL 分离。
7. **签名策略/MDM/version_overrides**:短期 over-engineering,仅当出现"远程下发配置""团队统一策略"需求时再回头看 `signed_policy.rs`——它的 enum verdict 建模(`Inactive/NoAuthenticSidecar/SidecarUnreadable/Trusted/Compromised`)即便不上签名也值得学。
