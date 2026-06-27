# hooks/ — Phase 2(未启用)

> 二期占位。当前采集仍走 `miner/adapters/`(解析 transcript)。本目录是 hook 推送架构的起头,代码未接入。

**为什么**:把 ingest 从"事后解析三种格式 transcript(有损/滞后)"翻成"源头 hook 直接吐统一 schema(无损/实时)"。前提已成立:三端(Claude/Codex/Kimi)都已有趋同的 PreToolUse/PostToolUse 等 hook 事件。

**架构**:
```
[Claude/Codex/Kimi hook] → emit.py(stdin JSON → common schema 行) → spool.jsonl → drain → transcripts.db
[adapters] 历史回填 / 无 hook 兜底 ─────────────────────────────────────────────────────────────→ transcripts.db
```
hook 是新的 ingest 来源,**不替换 adapter**。

**边界**:hook 最擅长 tool 调用/结果/ok/会话边界/compact/subagent(正是 adapter 最丢的);thinking 文本、token usage 可能仍需走 transcript。

**落地约束**:emitter 只 append spool、不内联写库(要快、不阻断 agent);emit 即 mask;每端一个几行 hook 配置指向同一 `emit.py`。

**文件**:`emit.py`(骨架,未实现)。

**参考**(二期启动时再细化字段映射、spool 切分、drain 去重、与 adapter 共存规则):
- https://code.claude.com/docs/en/hooks
- https://developers.openai.com/codex/hooks
- https://www.kimi.com/code/docs/en/kimi-code-cli/customization/hooks.html
