# grok-build 两个 crate 源码阅读 → story-lifecycle 借鉴分析

> 阅读对象:`xai-grok-memory`(15 文件全读)与 `xai-grok-telemetry`(19 文件重点读)。每个小节给出 Grok 的实现要点、对应 story-lifecycle 的问题、可操作的改法。

---

## A. 混合检索的完整流水线

**Grok 怎么做**(`search.rs` + `index.rs` + `embedding.rs` + `mmr.rs`)

`hybrid_search` 是一条 8 步管线,核心在于"分数怎么合、向量挂了怎么降级"。

**第 1 步:FTS5 永远在线,并补一轮 evergreen 召回。** 关键设计——session 日志体积大会把 global/workspace 挤出候选集,所以除了全量 FTS,再单独按 source 跑一次 FTS 补召回:

```rust
let mut fts_results = index.search_fts(query, candidate_limit).unwrap_or_default();
let evergreen = index
    .search_fts_by_sources(query, candidate_limit, &["global", "workspace"])
    .unwrap_or_default();
// 去重后并入 fts_results
```

**第 2 步:FTS 查询先过 stop word。** `search_fts` 不直接把 query 丢给 FTS5,而是 `extract_keywords` 去停用词后用 `OR` 拼(`query_expansion.rs`)。这样"那个我们讨论过的 API 问题"不会让"那个/我们/的问题"参与 BM25 计分。当全部词都是停用词时返回空,触发纯向量降级。

**第 3 步:分数归一化——FTS 用相对归一化,向量用绝对归一化。** 这是全篇最值得抄的细节。FTS5 的 rank 是负数(越负越好),用 min/max 相对归一到 [0,1]:

```rust
let normalized = 1.0 - (r.rank - min_rank) / range;  // 最好=1.0
```

向量距离则**故意不用相对归一化**,而是用绝对尺度 `similarity = 1 - distance/2.0`。注释解释得很直白:高维向量有"测度集中"现象,候选往往挤在一个窄带里,相对归一化(`1 - d/max_d`)会把分数压到接近 0:

```rust
const MAX_L2_DISTANCE: f64 = 2.0;  // 两个单位向量 L2 距离的理论上限 sqrt(4)
for (chunk_id, distance) in &vec_results {
    let similarity = (1.0 - (*distance as f64 / MAX_L2_DISTANCE)).clamp(0.0, 1.0);
    vec_scores.insert(chunk_id.clone(), similarity);
}
```

**第 4 步:三态合分,FTS-only 不被向量拖累。** 关键坑:如果用 `text_weight*fts + vector_weight*vec` 统一公式,当一个 chunk 只有 FTS 命中(没向量)时,它会被乘上 `text_weight=0.3` 直接掉到 0.3 以下,过不了 `min_score=0.35`。Grok 分三种情况:

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

**第 5 步:降级。** 向量不可用(sqlite-vec 没加载 / embed 失败)时,`vec_available=false`,query_embedding=None,直接走 FTS-only,`text_weight` 实质为 1.0。embedding API 还有指数退避重试(429/5xx,3 次,1s/2s/4s)。

**第 6 步:最终的 raw_score = base × decay × source_weight × access_boost。** 见 B 节和 C 节。

**第 7 步:MMR 去冗余**(`mmr.rs`),见下文。

**第 8 步:truncate 到 max_results。**

**对应 story-lifecycle 的哪个问题**

`packages/knowledge` 痛点之一:"检索要混合关键词 + 向量,向量挂了要能降级"。如果 knowledge 现在是纯关键词或纯向量,这正是要补的。

**具体怎么借鉴**

1. **存储用 SQLite + FTS5**(contentless 虚表,见 `schema.rs`:`CREATE VIRTUAL TABLE chunks_fts USING fts5(text, content='')`),向量用 `sqlite-vec`(或 Python 侧 `sqlite-vec` / `chromadb`)。一份库同时承载结构化字段 + BM25 + KNN,部署零依赖。
2. **向量距离用绝对归一化**(cosine → `1 - (1-cos)/2` 或直接 `1 - L2/2`),不要按批次 max 归一化。这是高维检索最容易踩的坑。
3. **三态合分**逻辑直接照抄,否则 FTS-only 的知识条目(scenario 里没被 embed 的)会系统性失分。
4. **query 先过停用词**再喂 BM25,避免口语化 query 被噪声词主导。
5. **降级做成显式分支**:向量层包一层 try/except,失败时 warn 并把 `text_weight` 提到 1.0,而不是整体报错。
6. **补一轮 evergreen 召回**:scenario/playbook(不衰减的长期知识)单独按 source 跑一次 FTS 再合并,防止被大量 failure 挤掉。

---

## B. evergreen vs decaying 二分

**Grok 怎么做**(`search.rs`)

时间衰减的开关由 source 决定,逻辑极简但设计明确:

```rust
fn is_evergreen_source(source: &str) -> bool {
    matches!(source, "global" | "workspace")  // 人工策展的 MEMORY.md
}
// session 自动生成的会话日志 → 衰减

fn temporal_decay_multiplier(source, created_at, now_secs, half_life_days: Option<f64>) -> f64 {
    let Some(half_life) = half_life_days else { return 1.0; };  // None=关闭衰减
    if is_evergreen_source(source) { return 1.0; }              // evergreen 永不衰减
    if half_life <= 0.0 { return 1.0; }
    let age_days = ((now_secs - created_at.max(0)) as f64 / 86400.0).max(0.0);
    let lambda = f64::ln(2.0) / half_life;                      // 半衰期模型
    (-lambda * age_days).exp()                                   // e^(-λ·age)
}
```

几个工程细节:
- **半衰期模型**(`λ = ln2/half_life`),不是线性衰减。30 天半衰期下,30 天前的 chunk 乘 0.5,60 天前乘 0.25。
- **future created_at 钳到 0**(时钟偏移保护)。
- **不设 age 上限**:2 年的 chunk 在 30 天半衰期下 ≈ 6e-8,自然落在任何 `min_score` 之下,不用特判。
- `half_life=None` 或 `<=0` 全局关闭衰减(测试/调参用)。

最终分数里衰减只是乘子之一:`raw_score = base × decay × source_weight × access_boost`。

**对应 story-lifecycle 的哪个问题**

knowledge 三类知识里,"scenario/playbook 长期有效,failure 随时间失效"。这正是 evergreen/decaying 二分。

**具体怎么借鉴**

1. 给 knowledge 的三类加一个 `decays: bool` 维度(或直接按 type 推断):scenario / playbook → `is_evergreen=True`,failure → `False`。
2. failure 的 `created_at` 用它被记录的时间戳;检索时套半衰期乘子。建议 failure 用较短半衰期(比如 14–30 天,因为"这个库的某个 bug 怎么修"过几个月库都换了)。
3. **半衰期做成 per-type 可配**,而不是全局一个值。scenario 可能压根不衰减;failure 衰减快;playbook 中等。
4. 不要做"硬过期删除"(会丢历史),而是软衰减——`min_score` 自然把它们筛掉,Grok 的做法是让老 chunk 的分趋近 0 而不是 DELETE。
5. 注意 Grok 把"是否衰减"绑在 source 上而非内容上,简化了判断。knowledge 可以同理:衰减属性是 schema 级常量,不在检索时推断。

---

## C. 内容过滤(is_content_free / is_structurally_empty)

**Grok 怎么做**(`search.rs` + `text_utils.rs`)

要过滤掉两类"空架子":(1) 纯标题/注释/空白组成的 chunk;(2) 自动生成的 MEMORY.md 模板桩。

```rust
fn is_content_free(text: &str, source: &str) -> bool {
    is_structurally_empty(text)
        || (is_evergreen_source(source) && super::dream::is_scaffold_template(text))
}
```

**结构空判定 `is_structurally_empty`** 最值得抄——它处理"HTML 注释跨 chunk 边界被切开"的坑:

```rust
fn is_structurally_empty(text: &str) -> bool {
    if !text.contains("<!--") { return lines_are_scaffolding(text); }  // 快路径
    // 剥掉可能跨行的 HTML 注释再扫
    let mut without_comments = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(start) = rest.find("<!--") {
        match rest[start + 4..].find("-->") {
            Some(end) => { /* 剥掉这一段 */ }
            None => {
                // 未闭合注释:把剩余当字面文本保留
                // —— 防止注释被 chunk 边界切开后误判为空
                without_comments.push_str(rest); rest = ""; break;
            }
        }
    }
    lines_are_scaffolding(&without_comments)
}
```

`lines_are_scaffolding`:每一条非空行要么是空行要么是 ATX 标题(`#`/`##`),否则视为有内容。注意几个边界:
- `#hashtag`(无空格)不是标题,算内容。
- **blockquote(`>`)不剥**——是真实用户内容(注释里明确)。
- setext 标题下划线(`=====`)、代码块(````)算内容。
- scaffold 模板判定**只对 evergreen source 生效**——session 里引用了模板短语的不该被误杀。

`is_scaffold_template`(`dream.rs`):短(<500 字节 trim 后)且含特定标记串("Auto-populated by dream consolidation" 等)才算桩;大文件即使残留标记串也不算(避免误杀已经填了内容的模板)。

**对应 story-lifecycle 的哪个问题**

"大量 boilerplate 噪音要过滤"(story-miner ingest)。比如 transcript 里的权限提示、工具调用的 boilerplate、空 section 标题。

**具体怎么借鉴**

1. 在 ingest 时跑一个 `is_structurally_empty` 等价函数,剥 HTML 注释(注意未闭合分支)后判断是否只剩标题/空白,是则丢弃。
2. **过滤在检索时做而不是只在索引时做**(Grok 注释强调:`Filter at search time (not index time) so already-indexed stubs are excluded without requiring a reindex`)——因为存量数据可能已经入库,检索时兜底过滤更稳。knowledge 两个地方都可以加。
3. 定义一组"模板桩标记串"列表(scenario/playbook/failure 各自的空模板文案),`< 500 字节且含标记`才判桩,防止把已经填充的长内容误杀。
4. blockquote / 代码块 / list 当作真实内容保留(别过度过滤)。
5. scaffold 判定按 source 范围限定:只对"可能来自模板的来源"判桩,引用了模板短语的真实笔记要保留。

---

## D. dream 机制

**Grok 怎么做**(`dream.rs` + `dream_lock.rs`)

dream 是**后台知识固化**:把近期 session 日志喂给 LLM,合成成结构化 markdown 写进 workspace MEMORY.md,然后删掉已消化的 session 文件。本质就是"定期把会话经验提炼成长期知识"。

**三道门(最便宜的先查)**(`check_dream_gates`):
1. config 开关 `dream.enabled`
2. 时间门:距上次固化 ≥ `min_hours`
3. 数量门:自上次固化以来的 session 数 ≥ `min_sessions`(当前 session 排除)

**prompt 设计**(`DREAM_SYSTEM_PROMPT`)非常值得抄,核心指令是 5 个动词:
- **Merge**:把相关信息合并成主题摘要
- **Resolve**:矛盾以最新为准(旧的被推翻就删)
- **Convert**:相对日期("昨天")→ 绝对日期
- **Discard**:寒暄、meta、工具输出噪声、消息计数、"下一步"section、已在 global 的偏好、session 元数据
- **Preserve**:决策、理由、架构、偏好、问题/解法对
- 没东西可存就回 `NO_REPLY`

**输入构建**(`build_dream_user_message`):
- 先放 existing memory(若非 scaffold),让模型 merge 而不是覆盖。
- 硬上限 32K 字符,超了就停(剩下的 session 留给下次 dream,**不在 processed_stems 里,所以不会被清理**)——这个区分很关键:`processed_stems` 精确记录"实际读了的",清理只清这些。
- existing memory 上限 16K(32K 的一半),按 char boundary 截。

**输出质检**(`process_dream_response`)四道关:
1. 空白 → 丢
2. `NO_REPLY`(大小写/分隔符无关,`is_no_reply` 把非字母数字全剥掉比 "noreply")→ 丢
3. **必须有 markdown 标题**(`has_markdown_headers`)→ 否则丢(强制结构化输出)
4. 超 16K 截断

**清理**(`clean_processed_sessions`):只删 processed 的、且 5 分钟内没被改过的(防止删掉正在写的并发 session)。返回真正删掉的 stem 列表,调用方据此清索引。

**并发协调**(`dream_lock.rs`):PID-based lock 文件 + mtime,write-then-verify 抢锁,stale(PID 死或超时)可抢占,失败 rollback。注释诚实地说"best-effort,不是互斥",因为 dream 幂等。

**对应 story-lifecycle 的哪个问题**

knowledge 的经验沉淀——跨会话经验怎么存。"把每个 story 跑完的会话经验,定期提炼成 scenario/playbook/failure"几乎就是 dream 的定义。

**具体怎么借鉴**

1. **抄 prompt 的 5 动词结构**(Merge/Resolve/Convert/Discard/Preserve),换成 knowledge 三类:`Preserve` 决策/理由 → scenario;`Preserve` 问题/解法对 → playbook 或 failure;`Discard` 列表基本可直接复用(寒暄、meta、工具噪声、计数、"下一步")。
2. **抄三道门**:时间门 + 数量门 + 开关。story-lifecycle 可以按"已完成的 story 数"而不是 session 数触发,比如每攒 5 个完成的 story 跑一次提炼。
3. **抄输入上限 + processed 精确追踪**:不要一次性把所有 transcript 塞进去,超上限的留到下次,清理只清实际处理的——避免"提了一半却把原始数据全删了"。
4. **抄输出质检三件套**:非空 + 非 NO_REPLY + 必须有结构(对 knowledge 就是"必须有 scenario/playbook/failure 的必填字段"),否则丢弃。
5. **抄"existing memory 先入 prompt"**:让模型 merge 而不是覆盖(对应 knowledge 的"同主题 scenario 合并去重")。
6. **NO_REPLY 机制**:LLM 觉得没价值时显式放弃,不要硬凑。knowledge 提炼同样需要——不是每个 story 都值得沉淀。
7. **is_scaffold_template 的"短 + 含标记才算桩"**:knowledge 的空 scenario 模板同理,避免把填好的长内容误判为模板。
8. 并发:story-lifecycle 单机 Python 可能用不上文件锁,但如果用后台 worker 定时跑 dream,需要一个"上次跑的时间 + 是否正在跑"的状态记录(数据库一行即可,不用 PID 锁那么重)。

> 限制:Grok 的 dream 是**整体覆盖** workspace MEMORY.md(`write_long_term` 是 `fs::write` 全量覆盖),不是增量 append。knowledge 如果要保留已有条目,要么靠 prompt 让模型 merge,要么改成增量写——这点要明确,否则会丢历史。

---

## E. chunker 的切分策略

**Grok 怎么做**(`chunker.rs`)

四级降级切分,核心原则是"不跨 markdown 语义边界":

1. **整体 ≤ max_chunk_chars → 整个文件一个 chunk**
2. **按 `##`(及更深)标题切 section**
3. **section 太大 → 按段落(`\n\n`)切**
4. **段落还太大 → 按行切**

切分时维护一个 header_stack(弹掉同级及更深的),给每个 chunk **拼上祖先标题上下文**:

```rust
fn add_header_context(context: &str, text: &str) -> String {
    if context.is_empty() { text.to_string() }
    else { format!("[Context: {context}]\n\n{text}") }
}
// 例:[Context: ## Parent] 前缀,让子 chunk 自包含
```

段落切分有 **overlap**:`chunk_overlap_chars` 个尾部字符带到下一个 chunk,保证 embedding 连续性。字符数当 token 代理(`chars/4 ≈ tokens`)。

**reindex 增量**(`index.rs::reindex_file`):用 `chunk_hash`(blake3)比对,未变的 chunk 跳过,变的更新+删旧 FTS 条目(contentless FTS5 删要带原文)+ 删旧向量(等重新 embed)。整个操作包在一个 transaction 里。

**header_level 判定**细节:`#hashtag`(无空格)不算标题,`###`(无文本)算。

**对应 story-lifecycle 的哪个问题**

story-miner 的 transcript 切分(长会话怎么切才能被检索/嵌入)。

**具体怎么借鉴**

1. **四级降级结构直接抄**,但 markdown 检测换成适配 transcript 的结构:transcript 通常按 turn(用户/助手轮次)分段,可以"先按 turn 切,turn 太大按段落,段落太大按行"。
2. **祖先上下文前缀**对 transcript 同样重要——一个助手的代码块,脱离"用户问的是什么 story 的什么阶段"就没意义。可以给每个 chunk 前缀 `[Context: story=XXX phase=design]`。
3. **overlap** 在 embedding 检索场景必要,否则边界处的语义断掉。Python 侧 `chunk_overlap_chars` 大概设 100–200。
4. **blake3 内容哈希做增量 reindex**:transcript 没变就不重新 embed(省钱)。Python 侧用 `hashlib.blake2b` 即可。
5. **原子事务**:chunk 增删要在一次 DB 事务里,避免半切状态。FTS5 contentless 表删条目要带原文(Grok 注释强调)。
6. chunk_id 格式 `{path}:{index}` 简单可靠,Python 侧照用。
7. 字符数代理 token 的假设对中文不准(中文 1 字符 ≈ 1 token,不是 0.25),knowledge 如果含中文要把 `max_chunk_chars` 调小或按真实 token 计。

---

## F. telemetry 的脱敏(redact)

**Grok 怎么做**(`redact_common.rs` + `external/redact.rs` + `external/schema.rs`)

Grok 的脱敏是**多层 fail-closed**架构,核心理念写在 `external/redact.rs` 顶部:**"Dropping telemetry on a schema bug is acceptable; leaking is not."**(宁可丢遥测也不能泄漏)。

**第 1 层:闭集 typed schema。** 外发属性 key 不是字符串而是 `ExternalKey` enum,编译器枚举所有可能外发的字段。加新 variant 会触发一个 `const _: () = assert!(ALL_KEYS.len() == COUNT)` 编译期完整性检查。

**第 2 层:gate(内容闸门)。** 两个 gate:`UserPrompts`、`ToolDetails`。只有当对应配置 `otel_log_user_prompts`/`otel_log_tool_details` 开启时,prompt 原文 / tool 参数 / file_path / skill.name / plugin_name / plugin_version 才外发。gate→key 映射:

```rust
fn gate_for_key(key: &str) -> Option<Gate> {
    match key {
        "prompt" => Some(Gate::UserPrompts),
        "tool_parameters" | "file_path" | "skill.name" | "plugin_name" | "plugin_version"
            => Some(Gate::ToolDetails),
        _ => None,
    }
}
```

**第 3 层:emit 时 secret-scrub。** `redact_common::redact_owned` = 先 secret-shape 脱敏(调 `xai_grok_secrets::redact_secrets`,识别 API key、token 等模式)再 user-path 脱敏:

```rust
pub(crate) fn redact_owned(input: &str) -> Option<String> {
    let secrets = xai_grok_secrets::redact_secrets(input);          // 第一遍:密钥形状
    match xai_grok_secrets::redact_user_paths(secrets.as_ref()) {   // 第二遍:用户路径
        Cow::Owned(paths) => Some(paths),
        Cow::Borrowed(_) => match secrets {
            Cow::Owned(s) => Some(s),
            Cow::Borrowed(_) => None,   // 都没变 → 返回 None(无需替换)
        },
    }
}
```

> 注:`redact_secrets` / `redact_user_paths` 实现在 `xai-grok-secrets` crate(不在本次阅读范围),从测试用例 `sk-CANARY...` 看,它至少识别 `sk-` 前缀的 API key 形状。

**第 4 层:export 时 fail-closed 校验**(`RedactingLogExporter`)。即使前几层漏了,导出包装器对每条记录逐字段检查:
- body 非空 → 丢(外部记录 body 必须空,event.name 是身份)
- key 不在 allowlist → 丢
- gated key 但 gate 关 → 丢
- **string 值仍含 secret 形状(`redact_owned().is_some()` 为真说明还能脱敏)→ 丢**
- 非 scalar(bytes/list/map)→ 丢

```rust
AnyValue::String(s) => {
    if crate::redact_common::redact_owned(s.as_str()).is_some() {
        return false;  // 还能脱敏却没脱 → 整条丢
    }
}
```

**第 5 层:URL 只留 origin。** `url_origin("https://x/v1/logs?token=XXX")` → `https://x`,path/query 全剥(因为可能带用户内容)。

**额外:sanitize 枚举值。** `tool_name` 不直接透传自由文本,而是闭集归约:内置工具名透传、含 `__` 的(MCP `server__tool`)→ `"mcp_tool"`、其他 → `"custom_tool"`。`screen_mode`、`client_identifier` 同理,未知值一律 collapse 成 `"other"`。

**对应 story-lifecycle 的哪个问题**

LLM audit 的 prompt 里可能有 API key / token,目前"没有脱敏机制"。这是最直接的痛点。

**具体怎么借鉴**

1. **先做 secret-shape 正则脱敏**(第 3 层),在 `infra/llm_client.py` 的记录出口加一道:
   - `sk-[A-Za-z0-9]{20,}`(OpenAI/xAI 风格 key)
   - `Bearer\s+\S+`、`Authorization:.*` 头
   - `gh[ps]_[A-Za-z0-9]{36}`(GitHub token)
   - `xox[baprs]-...`(Slack)、`AKIA[0-9A-Z]{16}`(AWS)
   - 通用高熵串(`[A-Za-z0-9_-]{32,}`)谨慎用,误杀率高
   - 替换成 `[REDACTED:api_key]` 这类带类型标记的占位符,审计时还能看到"这里有个 key"
2. **gate 机制**:audit 落库时,给"是否存 prompt/response 原文"一个开关(环境变量 / 配置),默认开或关看你的合规需求。tool_calls 的参数也可以单独 gate(可能含文件路径、命令)。
3. **fail-closed**:脱敏函数如果"还能再脱"(说明第一次没脱干净),宁可标记可疑也不要放过——但 Python 侧不必像 Rust 那么激进地整条丢,可以记录"脱敏后仍可疑"标记 + 脱敏后存储。
4. **sanitize 枚举字段**:story_id / phase / model_name 这种结构化字段原样存;agent_name、tool_name 这种自由文本做归约(未知 → `"other"`),避免高基数 + 避免把任意字符串落库。
5. **URL 只存 origin**:prompt 里如果嵌了带 token 的 URL,剥掉 path/query。
6. 不需要抄 Grok 那套 OTLP 闭集 enum(那是强类型语言的优势),Python 侧用一个 `ALLOWED_FIELDS` 白名单 + dataclass / pydantic schema 即可达到类似效果。

---

## G. telemetry 的采样与截断

**Grok 怎么做**(`external/truncate.rs` + `config.rs`)

**截断分三档**(`truncate.rs`,常量贴了"customer-pipeline 对齐值"):

| 对象 | 上限 | 超了怎么办 |
|---|---|---|
| 普通字符串属性 | 512 字符(char 计数) | 取前 128 字符 + `…[truncated]` |
| gated prompt / 内容文本 | 60 KB | 按 char boundary 截 + marker |
| gated tool 参数 JSON | 4 KB / 深度 2 / 每集合 20 项 | 结构化降维后再序列化截断 |
| file_extension | 10 字符 | 直接 cap |

关键实现细节:
- **按 char 计数不按 byte**,保证非 ASCII 的"长度"语义稳定(`s.chars().count()`)。
- **UTF-8 安全截断**:`floor_char_boundary` 往前回退到 char 边界,不切断多字节字符。

```rust
fn floor_char_boundary(s: &str, max_bytes: usize) -> usize {
    let mut idx = max_bytes;
    while idx > 0 && !s.is_char_boundary(idx) { idx -= 1; }
    idx
}
```

- **JSON 降维**(`reduce_tool_input`):深度 >2 的 object/array 塌缩成 `"{object:N}"` / `"[array:N]"`(保留计数),每层只留前 20 项,内部字符串照 `truncate_value` 截。最后整体超 4KB 再截。

**关于"采样"**:需要注意——`config.rs` 里**没有基于概率的采样**。Grok 的"采样控制"是:
1. **模式分级**(`TelemetryMode`):`Disabled`(默认)/`SessionMetrics`(只有生命周期元数据,无内容)/`Enabled`(全量)。这是粗粒度的"采不采",不是"采百分之几"。
2. **gate**(F 节):prompt/tool 细节是否落库由 admin 通过 `otel_log_user_prompts` 等环境变量钉死。
3. 真正的"采样日志"是另一个东西——`sampling_log.rs` 把 LLM 的 **逐 token 采样概率**写到 `sampling.jsonl`(调试用,`--log-sampling` 开),不是遥测采样。

所以 Grok 的存储成本控制主要靠:**gate(要不要内容)+ 截断(内容多长)+ 模式分级(要不要遥测)**,而不是随机丢事件。

**unified_log 的轮转**(`unified_log.rs`)是存储侧的兜底:5MB 上限,超了 `trim_file` 砍掉前一半(在半点后找第一个换行,不切行),用"写临时文件 + rename"防崩溃丢日志。append 时在锁内检查并 trim。

**对应 story-lifecycle 的哪个问题**

LLM audit 的存储成本控制——全量记录 prompt/response 长期会爆。

**具体怎么借鉴**

1. **三档截断常量直接定**:
   - prompt/response:存全文但 cap(比如 32KB,超过截断 + marker),或只存前 N + 尾 N + 中间省略。
   - reasoning_content:往往很长,单独更激进 cap(比如 8KB)。
   - tool_calls 参数:JSON 降维(深度限 2,每数组 20 项,长串截),不要原样存大 JSON。
2. **char 计数 + UTF-8 安全边界**截断(Python `text[:N]` 按 code point 已经安全,但要注意 emoji 等组合字符)。
3. **不要做随机采样,做分级**:
   - 默认档:只记 token 数 / duration / error / model / story_id(元数据,便宜)。
   - 详细档:加 prompt/response(截断后)。
   - 完整档:加 reasoning(截断后)。
   - 用开关切,而不是丢一部分请求。
4. **轮转/保留策略**:audit 表加 TTL 或按条数/大小轮转(参考 `trim_file`:超阈值删旧一半,在行边界切)。Python 侧可以用 SQLite + 定期 DELETE WHERE ts < ? 或 partition by day。
5. **真正缺的是"按需采样"**:如果想控制 LLM 调用监控的量,可以对"成功且 duration 正常"的请求只记元数据,对"慢请求 / error / 高 token"的请求记全量——这是异常驱动的采样,比随机采样更有审计价值。

---

## H. telemetry 的事件分类与归因

**Grok 怎么做**(`events.rs` + `session_ctx.rs` + `enums.rs`)

**事件分类**:`events.rs` 是一个 ~100 个 typed event struct 的目录,每个 struct 通过 `telemetry_event!` 宏绑定一个 `NAME` 字符串和(可选)一个 `external_record` mapper。事件按生命周期分组:
- 会话:`SessionNew` / `SessionEnded`
- Turn:`PromptSubmitted` / `TurnCompleted` / `PromptLatency`
- 模型:`ModelResponseReceived`(tokens/duration/stop_reason)/ `ApiError` / `RateLimitHit`
- 工具:`ToolCallCompleted`
- 记忆:`MemoryFlushed`、`MemorySearch`、`MemoryReindex` 等(`memory_telemetry.rs`)
- 压缩:`CompactionTriggered` / `CompactionCompleted`(`CompactionScope` RAII 配对发)
- 子代理:`SubagentLaunched` / `SubagentCompleted`

**双 sink 设计**(`session_ctx.rs`):一个事件两个出口,独立 gate:
- **外部 OTEL 流**(`external::emit`):由 `external::is_active()` 控制,独立于产品模式。
- **产品/Mixpanel**(`client::track`):由 `TelemetryMode::Enabled` 控制。
- `log_event` 总是先 fan-out 到外部,再判内部 gate;`log_event_dual` 让两个 sink 互斥(避免 ZDR 下重复计 session.count)。

**归因机制——task-local context**(`session_ctx.rs`):这是和 story-lifecycle 最可比的部分。Grok 用 `tokio::task_local!`:

```rust
tokio::task_local! {
    static TELEMETRY_CTX: Arc<TelemetryCtx>;
}
// TelemetryCtx = { session_id, prompt_index: Arc<Mutex<usize>>, prompt_id: Arc<Mutex<Option<String>>> }

pub async fn with_session_ctx(ctx: TelemetryCtx, fut: F) -> F::Output {
    let span = session_span(&ctx.session_id);  // 同时起一个 tracing span
    TELEMETRY_CTX.scope(Arc::new(ctx), fut.instrument(span)).await
}
```

`log_event` 在调用点**同步快照** ctx(注释强调 "snapshotted synchronously by log_event at call time to avoid racing with turn increments"):

```rust
let ctx_snapshot = TELEMETRY_CTX.try_with(|c| {
    (c.session_id.clone(), c.prompt_index.try_lock().map(|g| *g as u32).ok())
}).ok();
```

注意几个细节:
- `prompt_index` 锁用 `try_lock`,抢不到就 `turn_number=None`(非阻塞,不让 emit 卡住)。
- **per-prompt correlation UUID**(`prompt_id`):每个 turn 开始 `begin_prompt_id()` 轮转一个新 UUID,作为 `prompt.id` 把同一 prompt 的多个事件(Apirequest / turn_completed / tool calls)串联起来。
- `session_id` 同时挂在一个 `tracing::info_span!("session", session_id=...)` 上,debug-log 路由器按这个字段分流。
- emit 实际在 `tokio::spawn` 的后台任务里跑(不阻塞调用方),ctx 快照值随任务带走。

**对应 story-lifecycle 的哪个问题**

"跨 story 的 trace 归因靠 ContextVar"——Grok 用 task-local,Python 用 contextvars.ContextVar,本质同构。但 Grok 的归因维度更细(session_id + turn_number + prompt_id 三层)。

**具体怎么借鉴**

1. **三层 correlation id**:
   - `story_id`(≈ session_id):整个 story 的所有 LLM 调用串起来。
   - `phase` / `step`(≈ turn_number):design/implement/test 哪一阶段,甚至每阶段的第几次调用。
   - `request_id`(≈ prompt_id):单次 HTTP 调用的 UUID,把 prompt/response/reasoning/tool_calls 关联——这正是 audit 表的主键需求。
2. **task-local / ContextVar 快照在调用点取**:不要在记录时再读 ContextVar(可能已经被改),像 Grok 那样在 emit 入口同步快照。Python 的 `contextvars.copy_context()` 或在 `llm_client.py` 调用入口显式取当前 story_id/phase 存进局部变量再写记录。
3. **非阻塞降级**:拿不到 context(`try_with` 返回 None)就记 `story_id=None`/`phase=unknown`,不要抛异常卡住 LLM 调用主路径。审计是旁路,不能影响主流程。
4. **后台异步落库**:Grok 的 `tokio::spawn` 思路——audit 写库可以丢到后台 task/线程,主请求不等它。Python 侧用 `asyncio.create_task` 或 queue+worker,但要注意 await 点的 ContextVar 继承。
5. **双 sink / 分级**:audit 可以分"实时简要事件"(token/duration/error 立刻落库做监控)和"完整内容"(异步落 blob 存储做调试),两者独立开关。
6. **typed event struct**:Python 用 dataclass / pydantic 定义事件类型(`PromptSubmitted`、`ModelResponseReceived`、`ToolCallCompleted`、`ApiError`),避免字典散字段。每个事件明确字段白名单(呼应 F 节的闭集思路)。

---

## I. prompt_timing

**Grok 怎么做**(`prompt_timing.rs` + `events.rs::PromptLatency`)

非常精简的一个模块,测的是**一个 turn 的各阶段耗时分解**:

```rust
pub struct PromptTiming {
    turn_start: Instant,
    mcp_wait_ms: u64,
    tool_collection_ms: u64,
}

impl PromptTiming {
    pub fn start() -> Self { Self { turn_start: Instant::now(), mcp_wait_ms: 0, tool_collection_ms: 0 } }

    pub fn record_tool_prep(&mut self, mcp_wait_ms: u64, total_prep_ms: u64) {
        self.mcp_wait_ms = mcp_wait_ms;
        self.tool_collection_ms = total_prep_ms.saturating_sub(mcp_wait_ms);  // 去重
    }

    pub fn emit(self, model_call_ms: u64, turn_index: u32, ...) {
        let total_ms = self.turn_start.elapsed().as_millis() as u64;
        let pre_model_ms = total_ms.saturating_sub(model_call_ms);  // 模型调用前的所有耗时
        log_event(PromptLatency {
            total_ms, mcp_wait_ms, tool_collection_ms, model_call_ms, pre_model_ms, ...
        });
    }
}
```

记录的 5 个耗时维度:
- `total_ms`:整个 turn(从 turn_start 到结束)
- `mcp_wait_ms`:等 MCP server 初始化
- `tool_collection_ms`:收集可用工具(总 prep 减 MCP 等待)
- `model_call_ms`:LLM 调用本身(入参,调用方测)
- `pre_model_ms`:模型调用前的一切(`total - model_call`,反推)

注意设计:`tool_collection_ms` 和 `pre_model_ms` 都是**反推/差值**算出来的,不是每个都单独打点——减少埋点数量,且保证 `total = mcp_wait + tool_collection + model_call + 其他` 大致守恒(`pre_model_ms` 是个兜底差值)。

**对应 story-lifecycle 的哪个问题**

性能观测——story-lifecycle 每个 story 调 coding agent,需要知道时间花在哪:是 LLM 调用慢,还是 prompt 装配慢,还是工具/检索慢。

**具体怎么借鉴**

1. 在 story 推进的每个 phase(design/implement/test)用一个类似的 `PhaseTiming`:
   - `phase_start`
   - `context_assembly_ms`:拼 prompt(context_providers 检索 knowledge + 历史)耗时——这对应 Grok 的 `pre_model_ms`,story-lifecycle 里这块可能很重(知识检索 + transcript 压缩)。
   - `llm_call_ms`:HTTP 调用净耗时。
   - `tool_execution_ms`:如果 agent 回 tool_calls,执行工具的耗时。
   - `total_ms`:整 phase。
2. **反推差值减少埋点**:`context_assembly_ms = total - llm_call - tool_exec`,只在几个关键边界打 `time.perf_counter()`,其余算出来。Python 的 `time.perf_counter()` 精度足够。
3. **把 phase/timing 和 audit 表关联**:audit 记录已经有 duration,把 `llm_call_ms` 落进去;再加一个 phase 级 timing 事件记录 `context_assembly_ms` 和 `total_ms`——这样能回答"为什么这个 story 慢":是检索慢还是 LLM 慢还是工具慢。
4. **MCP 等待可类比**:story-lifecycle 如果有 context_providers 的初始化(向量库加载、索引构建),单独记一个 init_ms,首次慢但后续快。
5. 这是 story-lifecycle 目前 audit 缺的一块:有 duration 但没有"duration 的分解"。补上分解后,前端审计 UI 能直接定位瓶颈阶段。

---

## 跨小节的几个总体观察

1. **fail-closed 优先**:Grok 在遥测隐私上一致地选择"宁可丢数据也不泄漏"(redact 整条丢、schema 闭集、gate 钉死)。story-lifecycle 的 audit 应同理——脱敏失败时宁可标记可疑/截断,也不要把含 key 的原文落库。
2. **闭集 + 编译期/启动期 pin**:Grok 大量用 `const _: () = assert!(...)` 和 allowlist pin test 防止 schema 漂移。Python 没有编译期,但可以用 pydantic 模型 + 启动时校验白名单完整性达到类似效果。
3. **降级是显式分支不是异常**:向量挂了→FTS-only;embedding 失败→重试→跳过;context 拿不到→记 None。story-lifecycle 的 audit 作为旁路系统,绝不能因为自己挂了影响主 LLM 调用。
4. **存储用 SQLite 全家桶**:一份 SQLite 同时跑结构化表 + FTS5 + sqlite-vec,Grok 的 memory crate 验证了这条路对中等规模知识库完全够用,knowledge 包可考虑从"向量库 + 关键词库分离"收敛到单 SQLite。
5. **dream 是最有结构化借鉴价值的部分**:它的 prompt 设计(5 动词)、三道门、输入上限 + processed 追踪、输出质检三件套,几乎可以 1:1 映射到 knowledge 的"定期从完成 story 提炼 scenario/playbook/failure"流程。
