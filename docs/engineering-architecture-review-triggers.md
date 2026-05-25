# Architecture Review Triggers

日期：2026-05-25

## 背景

Story Lifecycle Manager 最近在 TUI + Windows + Zellij 的交互链路上连续暴露问题：

1. 按 `e` 进入 story 后看到空 PowerShell，而不是 Claude。
2. 按 `e` 在没有运行 session 时没有明显反馈。
3. 按 `r` 后再按 `e`，Zellij 一闪而退。
4. `zellij list-sessions` 中的 `EXITED - attach to resurrect` 被误判为 live session。
5. 同名 dead session 阻止 foreground Zellij layout 新建。

这些问题表面上是多个独立 bug，但它们共享同一个边界：TUI 按键、DB story 状态、
LangGraph 后台线程、`.done` 完成文件、Zellij session、Claude CLI 进程之间的状态没有
被明确建模。

最初的修复倾向是继续补判断：

- 如果 session 名字存在，就认为 healthy。
- 如果按 `e` 没 session，就更新 detail panel。
- 如果 Zellij foreground 命令闪退，就再查命令。

这些补丁都能局部缓解症状，但不能消除根因。真正的问题是：系统缺少一个明确的状态机，
把“事实状态”“用户动作”“决策结果”和“副作用执行”分开。

这份文档的目的不是复盘某一个 bug，而是沉淀一个工程规则：当连续 bug 指向同一抽象边界时，
团队和 AI 都必须停止惯性补丁，先做架构复盘。

## 问题本质

连续修 bug 时最危险的不是“修得慢”，而是“每个修复都让系统多一个隐藏状态”。

在这次 TUI 事件里，几个概念一开始都被 boolean 化了：

```text
session_alive: bool
is_story_running: bool
has_done: bool
status == active
```

但真实世界不是二值的：

```text
SessionState = live | exited | missing | unknown
DoneState = ok | corrupted | missing
GraphRunState = running | not_running | unknown
StoryStatus = active | paused | blocked | waiting_subtasks | completed | failed | aborted
```

当真实状态比代码抽象更丰富时，继续加 `if` 只会把复杂度藏起来。后续每个 bug 都会在
另一个分支爆出来。

## 触发规则

### 第三个相关 bug 规则

同一功能区连续出现第 3 个相关 bug 时，停止继续直接补丁。必须先做一次架构复盘。

相关 bug 的判断标准：

- 出现在同一个模块或相邻模块。
- 涉及同一条用户路径。
- 共享同一个外部系统边界。
- 修复一个后暴露另一个。
- 修复点开始跨多个文件扩散。

本次例子：

```text
空 PowerShell
-> EXITED session 误判
-> foreground Zellij 闪退
-> e 静默
```

这些都属于 TUI/Zellij/session 生命周期边界，因此第 3 个问题出现时就应该暂停继续补丁。

### 固定复盘问题

架构复盘至少回答以下问题：

```text
1. 这些 bug 是否共享同一个边界？
2. 是否有多个真实状态被一个 boolean 表达？
3. 是否有多个入口在做相似但不一致的判断？
4. 是否有副作用混在状态判断里？
5. 是否缺少决策表、状态机或协议？
6. 修复是否开始跨多个文件扩散？
7. 是否需要人肉解释“现在应该按哪个按钮”？
```

如果 3 个以上答案是“是”，继续打补丁就是高风险。应先写状态机、协议或设计文档。

## 架构问题信号

以下信号出现时，应主动怀疑架构抽象不够，而不是继续局部修复。

### 修复链条异常

```text
修一个症状后立刻暴露另一个症状。
每个修复都在同一条用户路径附近。
每次改动都需要补一个新的特殊分支。
bug 从一个文件扩散到多个文件。
```

### 状态语言开始含糊

危险说法：

```text
“这个 running 是 DB active，还是 executor running？”
“session 存在，但又不能用。”
“healthy 其实不一定能 attach。”
“done 文件有，但可能是坏的。”
“按理说不会发生，但这里先兜底。”
```

这些句子说明当前抽象已经不能准确表达真实状态。

### boolean 开始失效

危险命名：

```text
is_running
session_alive
has_session
is_healthy
has_done
```

如果一个 boolean 需要额外解释，通常应该升级为 enum。

推荐：

```text
SessionState = live | exited | missing | unknown
DoneState = ok | corrupted | missing
GraphRunState = running | not_running | unknown
```

### 多入口逻辑漂移

如果多个入口都在判断相同事实，就容易漂移。

本次例子：

```text
e handler 判断 session
r handler 判断 graph
watchdog 判断 .done
startup sweep 判断 .done
BaseTool 判断 session_alive
poll_completion 判断 session 是否 dead
```

这些判断如果不经过统一 resolver/decider，就会产生不一致行为。

### 副作用和判断混在一起

一个函数同时做两类以上事情时，要警惕：

```text
读状态
改 DB
启动线程
打开终端
删除 session
显示 UI
```

特别是 TUI handler 里边判断状态、边启动 graph、边 attach terminal、边更新 DB，很容易
形成隐式状态机。

## 推荐处理流程

### Bug 1

目标：找根因并修复。

要求：

- 使用系统化调试。
- 写最小回归测试。
- 修复后验证。

允许局部修复，但要记录是否发现抽象问题。

### Bug 2

目标：检查是否出现模式。

要求：

- 对比 Bug 1 和 Bug 2 是否共享边界。
- 搜索是否有重复判断逻辑。
- 若发现同一状态在多个地方被解释，开始考虑抽取 resolver/decider。

仍可局部修复，但要更谨慎。

### Bug 3

目标：停止补丁，做架构复盘。

要求：

- 写一页设计说明或状态机。
- 明确事实状态、归一化状态、用户动作、决策表、副作用边界。
- 得到 review 后再继续实现。

此时不应继续追加新的 `if` 作为主要解决方案。

## Skill 使用建议

skill 不能替代判断，但可以强制流程不失控。

### 第一个 bug

使用 `superpowers:systematic-debugging`。

目标：

- 不猜。
- 找到可复现路径。
- 查清根因。

### 需要改行为时

使用 `superpowers:test-driven-development`。

目标：

- 先写失败测试。
- 再写最小修复。
- 让 bug 变成回归用例。

### 连续相关 bug 出现时

使用 `superpowers:brainstorming` 或直接写设计文档。

目标：

- 从“修这个症状”切换到“当前模型是否错误”。
- 产出状态机、协议或边界设计。

### 设计定稿后

使用 `superpowers:writing-plans`。

目标：

- 把设计拆成可执行实现计划。
- 避免边想边改。

### 完成前

使用 `superpowers:verification-before-completion`。

目标：

- 用测试、lint、手工验证证明结果。
- 不靠主观判断宣布完成。

## AI 协作规则

AI 容易在连续 bug 中进入“补丁惯性”。为了避免这种问题，后续给 AI 的任务可以加入以下规则。

### 交互类改动前

要求 AI 先回答：

```text
1. 这个改动涉及哪些事实状态？
2. 哪些状态来自外部系统？
3. 有没有 dead/unknown/corrupted 这类非 happy path？
4. 用户动作有哪些？
5. 每个不可执行分支有没有用户可见反馈？
6. 有没有后台线程/watchdog 也会做同一件事？
```

### 跨系统状态改动前

要求 AI 先列反例：

```text
列出这个改动可能破坏的 10 个状态组合。
哪些状态现在没有被建模？
哪些状态不能用 boolean 表示？
```

### 第三个相关 bug 出现时

要求 AI 不直接改代码，先输出：

```text
共享根因
当前抽象是否错误
是否需要状态机/协议/边界
继续小修的风险
建议的重构切入点
```

## 文档化要求

当触发架构复盘时，至少写一份短文档，包含：

```text
背景
最近的 bug 链条
共享边界
现有抽象为什么不够
新的状态模型或协议
决策表
副作用边界
测试策略
未决问题
```

文档不需要很长，但必须让其他人或 AI 能 review。

如果文档写不出来，说明问题还没理解清楚，不应该继续实现。

## 工程护栏

建议把以下三条写入项目级工作规则：

```text
1. 同一功能区第 3 个相关 bug，必须先做架构复盘。
2. 跨系统状态禁止只用 boolean，优先 enum 或 tagged state。
3. TUI/CLI/workflow 交互必须先有 state × action 决策表，再实现 handler。
```

额外建议：

- 每个历史 bug 必须有回归测试。
- 每个外部命令失败必须有用户可见反馈和 debug log。
- 每个后台自动推进机制必须有防重入策略。
- 每个删除/中止类动作必须有确认和清理顺序。

## 本次事件的经验

这次最早应该触发架构复盘的信号是：

```text
zellij list-sessions 里有 session 名字，但它是 EXITED。
```

这说明 `session_alive: bool` 已经不是正确抽象。正确方向不是继续补：

```text
if name in list_sessions and not EXITED ...
```

而是升级模型：

```text
SessionState = live | exited | missing | unknown
```

之后再建立：

```text
SessionState × UserAction -> EntryAction
```

这类转折点越早识别，后续补丁越少，系统也越容易测试。

## 判断句

当你或 AI 开始说以下句子时，应立即停下来检查架构：

```text
“这个状态其实不一定代表……”
“这里先特殊处理一下……”
“按理说不会发生，但……”
“这个 session 存在但又不能用……”
“running 是 DB running 还是进程 running？”
“这次只要再加一个判断……”
```

这些不是小心谨慎的表现，而是抽象边界正在失效的信号。

## 结论

避免类似问题不能只靠加强个人意识。意识会疲劳，尤其在连续 debug 和用户催促时，很容易
继续打补丁。

更可靠的做法是把意识固化为：

- 触发器：第 3 个相关 bug 停止补丁。
- 模板：事实状态 -> 归一化状态 -> 用户动作 -> 决策表 -> 副作用。
- 测试：每个 bug 都变成回归测试。
- 文档：架构问题先写状态机或协议。
- review：让人或 AI 先评审设计，再实现。

这套规则的目标不是让每个小 bug 都变成大设计，而是在系统明确出现“抽象失效”信号时，
及时从修症状切换到修模型。

