> ⚠️ **历史快照（归档于 2026-07）**：描述的架构可能已被后续演进取代。当前架构见 [../../ARCHITECTURE.md](../../ARCHITECTURE.md)。本文件保留作决策记录（ADR），正文未修改。

---

# Idea: Architecture Review Gate

日期：2026-05-25

## 背景

Story Lifecycle Manager 的目标不是简单地把一个 AI CLI 包起来，而是把 AI 编码工作变成
可编排、可观察、可恢复、可复盘的多阶段工作流。当前系统已经具备一些关键能力：

- story 按 profile 进入 `design -> implement -> review` 等阶段。
- LangGraph 后台执行 story，阶段之间通过状态机推进。
- AI CLI 通过 `.story-done/{story_key}/{stage}.json` 与 orchestrator 握手。
- router 可以根据错误选择 retry、skip、fail、wait_confirm。
- quality flywheel 方向已经设计了 finding、verification、learned pattern 等质量闭环。
- TUI 作为本地操作入口，承担创建、恢复、进入终端、跳过、失败、终止、删除等调度职责。

最近 TUI + Windows + Zellij 的一组连续问题暴露了另一类质量风险：AI 工作流不只需要
发现“代码写错了”，还需要发现“继续修 bug 已经不是正确动作，当前抽象或架构模型错了”。

这组问题的链条是：

1. 用户按 `e` 进入 story，看到的是空 PowerShell，而不是 Claude。
2. 按 `e` 在没有 live session 时没有明显提示，用户感知为“没反应”。
3. 按 `r` 后再按 `e`，Zellij 一闪而退。
4. `zellij list-sessions` 中的 `EXITED - attach to resurrect` 被误判为 healthy session。
5. 同名 dead session 阻止 foreground Zellij layout 新建。

这些问题不是五个彼此独立的 bug。它们共享同一个边界：TUI 按键、DB story 状态、
LangGraph 后台线程、`.done` 文件、Zellij session、Claude CLI 进程之间的状态没有被
明确建模。

最初的修复方式自然会走向局部补丁：

- `session_alive(name)` 看见名字就返回 true。
- `e` 没有 session 时只更新 detail panel。
- foreground Zellij returncode=1 后再手动查命令。
- dead session 被当作可以 attach 的 session。

这些补丁能解决局部症状，但不能让系统知道：当前已经进入“相关 bug 连续出现”的风险区。
真正需要的是一个工作流能力：当同一边界连续暴露问题时，系统能提示或强制切换到
“架构复盘模式”，要求 AI 先建模事实状态、决策表和副作用边界，再继续实现。

## 问题定义

当前工作流能处理以下问题：

- 某个 stage 执行失败。
- AI CLI 没有写 `.done`。
- review 发现代码质量问题。
- test/lint 没有通过。
- router 根据错误选择 retry 或 fail。

但它还不能很好处理以下情况：

```text
同一个功能区连续出现多个相关 bug，
每个 bug 修复后又暴露新的边界问题，
修复点跨多个模块扩散，
状态词开始含糊，
boolean 抽象开始失效，
AI 仍然继续打补丁。
```

这种情况需要的不是再 retry 一次，也不是让 executor 继续修一个新 if，而是暂停当前
执行思路，切到 architecture review：

```text
bug 链条 -> 共享边界 -> 失效抽象 -> 新状态模型 -> 决策表 -> 副作用边界 -> 回归测试
```

## 核心想法

新增一个轻量能力：**Architecture Review Gate**。

它不是新的全局流程，也不是所有 story 的强制阻断器。它是一个风险探测器：

```text
当系统检测到同一功能区第 3 个相关 bug 或同类 finding 复发时，
生成 Architecture Review Packet，
提示或阻断继续补丁，
引导 AI 先输出状态机/协议/边界设计。
```

最小形态：

```text
detect signals -> score trigger -> emit architecture_review_suggested event
               -> write .story-context/{story_key}/architecture_review_packet.md
               -> TUI/API 显示建议
```

增强形态：

```text
trigger hit -> route to architecture_review stage
            -> AI 生成设计文档
            -> 人类确认
            -> 再回到 implement/fix
```

## 与现有质量飞轮的关系

Quality Flywheel 关注的是 finding 的生命周期：

```text
open -> accepted -> fixed -> verified -> learned
```

Architecture Review Gate 关注的是 finding 和 bug 的聚合模式：

```text
单个 finding 是否已修复？             -> Quality Flywheel
多个 finding 是否说明抽象失效？       -> Architecture Review Gate
```

二者关系：

- Quality Flywheel 提供结构化信号来源。
- Architecture Review Gate 消费这些信号，判断是否需要从“修复模式”切换到“建模模式”。
- Architecture Review 的产物可以反向沉淀为 learned pattern，进入后续 Quality Packet。

## 触发信号

P0 不需要复杂 AI 聚类，先做规则触发。

### 1. 第三个相关 bug

同一 story 或同一功能区连续出现 3 个相关 bug/finding。

相关性可以先用简单规则估计：

- 相同 category。
- 相同 stage。
- 相同文件路径前缀。
- 相同 tag，例如 `tui`、`session`、`zellij`、`graph-routing`。
- 最近 N 条事件都涉及同一关键词。

### 2. retry 疲劳

同一 stage retry 次数达到阈值，例如：

```text
execution_count >= 3
review retry >= 2
router retry >= 2
```

如果 retry 的错误集中在同类边界，应触发复盘建议。

### 3. 修复扩散

一次 bugfix 修改跨越多个职责层：

```text
TUI
terminal backend
graph/router
DB/service
tests
```

修改跨越 3 个以上模块时，系统应提示检查是否存在缺失抽象。

### 4. boolean 失效信号

review 或事件中出现以下模式：

```text
alive but unusable
running but no session
active but not executing
done exists but corrupted
healthy but cannot attach
```

中文等价描述也应纳入后续语义检测：

```text
存在但不能用
正在运行但没有 session
active 不代表在跑
done 文件坏了
```

### 5. 副作用混杂信号

review finding 或 diff 涉及同一函数同时处理：

```text
读状态
改 DB
启动线程
打开终端
删除 session
显示 UI
```

这通常说明缺少 resolver/decider/handler 分层。

## Architecture Review Packet

触发后生成一个 compact packet，作为 AI 或人类复盘输入。

建议路径：

```text
.story-context/{story_key}/architecture_review_packet.md
```

内容：

```markdown
# Architecture Review Packet

## Trigger
- trigger_type:
- confidence:
- reason:

## Recent Bug Chain
1. ...
2. ...
3. ...

## Shared Boundary
- modules:
- external systems:
- user actions:

## Suspicious Booleans / Ambiguous States
- session_alive
- is_story_running
- has_done

## Side Effects Involved
- DB update
- graph start
- terminal attach
- session delete

## Recommended Review Questions
1. Do these bugs share one boundary?
2. Are multiple real states represented by one boolean?
3. Do multiple entry points make similar but inconsistent decisions?
4. Are side effects mixed into state checks?
5. Is a decision table, state machine, or protocol missing?

## Suggested Next Step
- warning_only / require_architecture_review / ask_human
```

## Architecture Review Prompt

新增 prompt：

```text
prompts/architecture_review.md
```

职责不是修 bug，而是判断抽象是否失效。

提示词要明确：

```text
你不是在继续修 bug。你要判断当前连续问题是否说明系统缺少状态机、协议或边界抽象。

请输出：
1. 相关 bug 链条
2. 共享边界
3. 当前抽象为什么不够
4. 哪些 boolean 应升级为 enum/tagged state
5. 事实状态模型
6. state x action 决策表
7. 副作用边界
8. 回归测试清单
9. 是否建议继续实现，还是先写设计文档
```

该 prompt 可以引用：

```text
docs/engineering-architecture-review-triggers.md
docs/design-tui-entry-state-machine.md
```

但普通 stage 不应注入全文，避免 prompt 膨胀。只在触发 gate 时使用。

## 用户体验

### TUI 提示

P0 推荐 warning-only，不直接阻断。

示例：

```text
Architecture Review Suggested

同一边界已出现 3 个相关问题：tui/session/zellij。
继续补丁可能扩大隐性状态复杂度。

[r] 继续当前执行
[A] 生成架构复盘
[s] 本次跳过提醒
```

### Story 状态

P0 不新增 story terminal status，避免侵入主流程。使用 event + detail panel 展示。

P1 可考虑新增状态：

```text
architecture_review_required
```

或者复用：

```text
blocked + last_error = "architecture_review_required"
```

但复用 blocked 会混淆“执行失败阻塞”和“建议先复盘”，需要谨慎。

### CLI/API

未来可提供：

```bash
story arch-check <story_key>
story arch-review <story_key>
```

P0 可先不做 CLI，只通过 TUI/watchdog/事件驱动。

## P0 方案：提示型 Gate

目标：不改变主流程，不阻断执行，只生成信号和 packet。

新增模块：

```text
src/story_lifecycle/orchestrator/architecture_triggers.py
```

核心函数：

```python
def collect_architecture_signals(story_key: str) -> dict:
    ...

def detect_architecture_trigger(signals: dict) -> dict:
    ...

def build_architecture_review_packet(story_key: str, signals: dict, trigger: dict) -> str:
    ...
```

新增事件：

```text
architecture_review_suggested
architecture_review_packet_generated
architecture_review_skipped
```

集成点：

- `review_stage_node` 后：根据 review finding 和 retry 历史判断是否建议复盘。
- `router_node` 前：如果 retry 疲劳且错误集中，生成 architecture trigger。
- TUI watchdog：发现 suggested event 后显示提示。

P0 行为：

```text
trigger hit
-> log_event architecture_review_suggested
-> write packet
-> TUI 提示
-> 不自动阻塞 story
```

## P1 方案：显式 Architecture Review Stage

目标：让复盘成为可编排 stage。

profile 示例：

```yaml
stages:
  architecture_review:
    order: 0
    description: "连续相关问题后的架构复盘"
    tool: stage_tool
    prompt: architecture_review
    confirm: true
    review: false
    expected_outputs:
      - architecture_doc_path
      - state_model_summary
      - decision_table_summary
      - recommendation
```

触发后 router 可以选择：

```text
wait_confirm -> architecture_review -> implement
```

或：

```text
blocked until human confirms architecture review
```

P1 风险：

- 可能过度阻塞开发。
- 需要处理动态插入 stage 或 profile 分支。
- 需要明确人类如何批准“继续实现”。

因此 P1 应在 P0 信号稳定后再做。

## P2 方案：质量飞轮与 learned pattern 集成

目标：让架构复盘结果沉淀为未来 story 的规则。

Architecture Review 输出可以转为：

```text
learned_pattern:
  category: architecture-boundary
  trigger:
    - third_related_bug
    - boolean_state_failed
  rule:
    - Cross-system state must be modeled as enum/tagged state.
    - TUI/workflow changes require state x action decision table.
```

后续相似 story 的 Planner prompt 可以注入 compact pattern：

```text
Recent Architecture Pattern:
当 TUI/terminal/graph 状态交叉时，不要使用 session_alive: bool。
先建模 SessionState 和 state x action 决策表。
```

P2 需要：

- finding/category/tag 更稳定。
- learned pattern 审核机制。
- 相似 story 检索或语义匹配。

## 数据模型草案

P0 可以只用 event log，不新增表。

事件 payload 示例：

```json
{
  "trigger_type": "third_related_bug",
  "confidence": "medium",
  "story_key": "1064993",
  "stage": "design",
  "related_events": [161, 167, 169],
  "categories": ["tui", "session", "zellij"],
  "reason": "Multiple recent failures share the terminal session lifecycle boundary.",
  "packet_path": ".story-context/1064993/architecture_review_packet.md"
}
```

P1/P2 若需要查询当前状态，可新增轻量表：

```sql
CREATE TABLE architecture_trigger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  story_key TEXT NOT NULL,
  stage TEXT,
  trigger_type TEXT NOT NULL,
  confidence TEXT NOT NULL,
  status TEXT NOT NULL, -- suggested | accepted | skipped | resolved
  packet_path TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

但 P0 不建议新增表，先用事件验证价值。

## 检测策略

### 规则优先

P0 规则检测足够：

```text
related_count >= 3
retry_count >= threshold
same_category_count >= threshold
module_spread >= 3
ambiguous_state_keywords >= 2
```

### LLM 辅助

P1 可加入 LLM 分类，但不能让 LLM 直接阻塞流程。

LLM 输入：

- recent events
- recent findings
- changed files summary
- retry history

LLM 输出：

```json
{
  "architecture_review_needed": true,
  "confidence": "medium",
  "shared_boundary": "TUI terminal session lifecycle",
  "failed_abstractions": ["session_alive: bool"],
  "recommended_next_step": "write_state_machine"
}
```

规则可作为 hard signal，LLM 只提升解释质量。

## 风险与约束

### 误报

连续 3 个 bug 不一定都是架构问题。P0 使用 warning-only，允许用户跳过。

### 过度流程化

如果每个小 bug 都触发复盘，会拖慢开发。触发条件必须保守：

- 第三个相关 bug。
- 同类 retry 疲劳。
- 同一边界反复出现。

### 信号质量不足

如果 finding/category/tag 不稳定，相关性判断会弱。P0 应允许手动补充 category 或通过
review prompt 约束输出结构。

### prompt 膨胀

不要在所有 stage 注入完整 architecture trigger 文档。只在触发 gate 时生成 packet 和
architecture review prompt。

### 与 router 冲突

router 当前负责 retry/skip/fail。Architecture Review Gate 不应抢占所有错误路由。
P0 只提示，P1 再考虑把它纳入 router action。

## 成功标准

P0 成功标准：

- 连续相关 bug 出现时，系统能生成 architecture review packet。
- TUI 能显示明确提示，而不是继续静默 retry。
- packet 能帮助 AI/人类快速写出状态机或边界设计。
- 不影响普通 story 的执行路径。

P1 成功标准：

- architecture_review stage 能产出设计文档。
- 人类确认后，story 能回到 implement/retry。
- 不出现无意阻断或重复启动 graph。

P2 成功标准：

- 架构复盘结论能沉淀为 learned pattern。
- 后续相似 story 能提前看到相关约束。
- 同类 bug 复发率下降。

## 未决问题

1. “相关 bug”的 P0 规则阈值应该固定为 3，还是按 severity/category 调整？
2. Architecture Review Gate 默认是 warning-only，还是对 high severity finding 阻塞？
3. 是否需要 TUI 新增快捷键，例如 `A` 生成架构复盘？
4. architecture review stage 是动态插入，还是作为可选 profile stage？
5. packet 是否应包含 git diff changed files，还是只包含 story event/finding？
6. architecture trigger 是否应该写入 finding 表，还是单独事件即可？
7. 用户跳过 architecture review 后，多久内不再重复提醒？

## 建议下一步

先做 P0 设计，不直接实现 P1/P2。

P0 最小闭环：

1. 新增 `architecture_triggers.py`。
2. 从 event_log、finding、retry count 收集信号。
3. 规则判断是否触发 `architecture_review_suggested`。
4. 生成 `.story-context/{story_key}/architecture_review_packet.md`。
5. TUI detail panel 展示提示。
6. 增加单元测试覆盖 third-related-bug、retry-fatigue、warning-only 行为。

