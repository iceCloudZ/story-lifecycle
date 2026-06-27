# 一期 Tier 0:原生数据梳理(native data)— 设计

> 2026-06-27。把 adapter 事后解析 transcript 的顽疾修掉,并为 per-story / per-session token 统计打好结构。
> Hook 推送架构是**二期**(`hooks/`),不在本期。关联探索见 `transcript-signals-ideas.md`。

## 目标
修正三端 adapter 的三类有损问题(时间戳截断、Kimi 丢失败信号、token 错位/缺失),并建立可做 per-story token 聚合的数据结构。

## 范围

### 做
1. **timestamp per-event 全 ISO**(三端):每个 event/token 带自己的时间戳(不只去 `[:10]`,否则 events 共享 session 首 ts,turn 间 gap 算不了)。`sessions.ts` = 首 event 全 ISO。向后兼容现有 `ts[:10]` 切片。
2. **Kimi `isError`**:`adapters/kimi.py` 提取 tool result 的 `isError` → `kind='result', ok=not isError`。解锁 Kimi 的 failure_mode。
3. **`token_usage` 表**(三端,每 turn 一行)+ 扩展 `parse()` 返回 `(meta, events, tokens)`。
4. **清理**:Kimi `usage.record` 不再 emit `kind='think'`(改入 token_usage);`think` 留给 Tier 1 真 reasoning。
5. **`scripts/story_token.py`**:per-story / per-session / 未关联 三层 token 输出。
6. **`tests/test_adapters.py`**(TDD):合成 sanitized fixture。

### 不做(留后续)
- events 表不加列;sessions schema 不动(仅 ts 精度提升)。
- Claude think / compact / todo、Codex/Kimi bonus → Tier 1。
- 完整 stage-token 分析(本期只出 per-story/per-session 聚合)。
- story 关联率提升(I2/UUID,独立工作)。

## 数据结构

### 新表 `token_usage`
```sql
CREATE TABLE token_usage(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sid TEXT, src TEXT, ts TEXT, model TEXT,
  input_tokens INT, output_tokens INT,
  cache_read_tokens INT, cache_creation_tokens INT, reasoning_tokens INT
);
CREATE INDEX idx_tu_sid ON token_usage(sid);
```
- 每 turn 一行,覆盖**所有** session(含无 story 的 —— story 关联只在聚合时 JOIN,不影响采集)。
- 三端字段归一映射(实现时对样本 TDD pin 精确字段名):
  - Claude `message.usage`:`input_tokens` / `cache_read_input_tokens` / `cache_creation_input_tokens` / `output_tokens`
  - Codex `token_count.info.total_token_usage`:`input_tokens` / `cached_input_tokens` / `output_tokens` / `reasoning_output_tokens`
  - Kimi `usage.record`:`inputOther`(+`inputCacheRead`) / `output` / `inputCacheRead` / `inputCacheCreation`

### 不变
events / sessions 表 schema 不动(只改 ts 存全 ISO)。

## 接口变更
- `base.py`:`SourceAdapter.parse(path, sid)` → `(meta, events, tokens)`;基类默认 `tokens=[]`。
- `adapters/{claude,codex,kimi}.py`:返回 tokens;events 逻辑尽量不动。
- `store.py`:SCHEMA 加 `token_usage`;`main()` 接收 tokens 批量插入;删 sid 时连带删其 token 行(与 events 同生命周期);只对入库的 session 插 token(避免孤儿)。

## token 聚合
- per-session(全量):`SELECT sid, SUM(input_tokens), SUM(output_tokens), SUM(cache_read_tokens), SUM(cache_creation_tokens) FROM token_usage GROUP BY sid`
- per-story:`SELECT s.story_id, SUM(t.input_tokens), ... FROM token_usage t JOIN sessions s ON s.sid=t.sid WHERE s.story_id IS NOT NULL GROUP BY s.story_id`
- 未关联:`story_id IS NULL` 的 session token(`story_token.py` 第三层)
- ⚠️ per-story 覆盖率受 `sessions.story_id` 关联率(~18%)限制;提升关联是独立工作。

## 测试(TDD)
`tests/test_adapters.py`,合成 sanitized jsonl fixture,断言:
- 三端:每 event/token 带独立全 ISO ts(不再共享 session 首 ts)。
- Kimi:`isError:true` → `kind='result', ok=0`;`usage.record` → tokens,不再进 `events.think`。
- Claude:assistant `message.usage` → tokens 行(input/output/cache 归一)。
- Codex:`token_count` 事件 → tokens 行。
- 现有 3 测试不回归。

## 迁移
1. 备份 `data/transcripts.db` → `.bak`。
2. 删库;`python -m miner.store`(全量)+ `story_ingest` + `link`。
3. 校验:sessions/events 按 src 计数无异常回退;`token_usage` 行数 ≈ 三端 turn 量级;抽 1 story + 1 未关联 session 跑 `story_token.py` sanity。

## 风险
- `parse()` 接口变更需 base+3adapter+store 同步 → 测试覆盖兜底。
- 三端 token 字段名差异 → 实现时对样本 TDD pin。
- 重建耗时(1027+ 文件)→ 备份兜底。

## 验收
- 全量重建后 `token_usage` 有数据,`story_token.py` 三层输出正常。
- 新测试 + 现有测试全绿。
- timestamp 秒级精度可见(抽 1 session 看 `events.ts` 各异)。
