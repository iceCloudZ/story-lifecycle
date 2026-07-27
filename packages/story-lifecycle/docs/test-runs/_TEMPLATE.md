# RUN — <story-key简写> (<YYYY-MM-DD>)

> **复制本文件建新详情**:`cp _TEMPLATE.md RUN-<key简写>-<YYYYMMDD>.md`
> 跑测操作流程见 `.claude/skills/run-real-story-test/SKILL.md`

---

## 配置

| 字段 | 值 |
|---|---|
| Story key | `<完整 key>` |
| 标题 | `<标题>` |
| Profile | `minimal` / `single-pass` / `strict` / ... |
| 模式 | 新建需求 / 接手中途需求 |
| Adapter | claude / kimi / codex / opencode |
| Workspace | `<workspace 路径>` |
| spawn_cwd | `<实际 spawn 的 cwd>(规划 LLM 决定,查 context_json.workspace_path)` |
| 开始时间 | `<YYYY-MM-DD HH:MM>` |
| 结束时间 | `<YYYY-MM-DD HH:MM>` |
| 总耗时 | `<Xh Ym>` |
| 最终状态 | 已完成 / 失败 / 已中止 |
| serve 版本 | `<git commit hash>` |

**接手模式额外字段**(模式=接手中途需求时填):
- 接手说明(seed_context):`<摘要>`
- 已预置成果物:`<story/spec.md 已存在 / git 有改动 / ...>`

**监控日志路径**:
- events.jsonl: `<spawn_cwd>/.story/runs/<story_key>/pty_<stage>/events.jsonl`
- raw.log: `<spawn_cwd>/.story/runs/<story_key>/pty_<stage>/raw.log`

---

## 时间线

> 关键节点的墙钟时间。从 events.jsonl / DB event_log / serve.log 摘。机器休眠过就在备注标。

| 时间 | 事件 | 备注 |
|---|---|---|
| HH:MM | 规划 LLM 开始 / 完成 | workspace_slug=`<slug>` |
| HH:MM | spawn `<adapter>` (stage=`<stage>`) | pid=`<pid>` |
| HH:MM | 首次输出 | events.jsonl 第一条 |
| HH:MM | `stuck_detected` (规则: `<idle/repeated_errors/startup>`) | duration=`<Ns>` |
| HH:MM | 人工介入: `<Esc打断/重跑/改prompt>` | |
| HH:MM | artifact 落地: `<story/spec.md>` | |
| HH:MM | boundary judge: `<approve/reject>` | |
| HH:MM | stage completed | |

---

## 发现清单

> BUGLOG 级。每个问题含:严重度/现象/根因/代码引用 file:line/修复方向。没问题就写"无"。

### 问题 #1 — <标题>

**严重度**:高 / 中 / 低
**类型**:bug / 体验 / 设计疑问 / 性能

**现象**:<观察到什么>

**根因**:
- <代码层面为什么发生>

**代码引用**:
- `<file>:<line>` — <说明>
- `<file>:<line>` — <说明>

**修复方向**:
- <怎么修,或"待评估">

**状态**:未修 / 已修(`<commit>`) / 待评估

---

### 问题 #2 — <标题>

(同上结构)

---

## 人工介入记录

> 每次手动干预:为什么介入、怎么处理、效果。无介入就写"无"。

| 时间 | 触发 | 动作 | 效果 |
|---|---|---|---|
| HH:MM | events.jsonl 显示 `<信号>` | Esc 打断 + 输入 `<纠偏内容>` | claude 调整方向 |

---

## 与预期/历史版本的对比

> 这次跑和上次跑同需求(或预期行为)比,有什么不同。无对比就写"首次跑测"。

- <差异点 1>
- <差异点 2>

---

## 本次沉淀

> 可复用的操作模式 → skill 候选。**单次不算模式,复现 ≥2-3 次才固化**。固化后回总表 README 标 [已固化]。

### Skill 候选

- [候选] `<skill 名>`: `<触发场景>`。来源:本次 `<story-key>`。成熟度:1/3 次复现。
- (无就写"本次无候选")

### 可复用片段

> 不是完整 skill,但值得记下来的操作技巧(命令、判据、排查路径)。

- `<技巧>`

---

## 附件

> 关键日志摘录、截图路径、相关 commit。大段日志贴这,别污染正文。

```
<events.jsonl / serve.log 关键片段>
```
