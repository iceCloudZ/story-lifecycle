# Board-first Planning Session 设计

> 日期：2026-05-27  
> 状态：Design  
> 关联主线：v0.6 Reliability Loop / TUI 交互收敛  
> 相关文档：`docs/design-terminal-entry-lifecycle.md`、`docs/design-foreground-zellij-execution.md`、`docs/design-board-diagnostics-panel.md`、`docs/v0.6-reliability-loop-tasks.md`

## 1. 背景

Story Lifecycle 当前的 session 体验有一个明显矛盾：

- `plan` / `brainstorming` 阶段需要高密度交互，用户需要和 Agent 多轮讨论、选择方案、补充约束。
- `implement` / `test` / `review` 阶段更像后台执行，用户主要需要观察状态、看最近输出、诊断卡住原因。
- 如果所有阶段都要求用户进入 Zellij / terminal session，用户会被迫记住 attach、detach、exit、Ctrl-D 等终端细节，进入和退出都很重。
- 如果所有交互都改成 headless 多轮调用，又会造成上下文重复注入和 token 浪费，并且失去原生 Agent session 的自然对话能力。

因此需要把产品形态从 **session-first** 收敛为 **Board-first**：

```text
默认停留在 Board
Plan 阶段允许进入受控 Planning Session
Plan 完成后必须结构化落盘并回到 Board
后续阶段默认后台执行、TUI 观察、必要时才接管终端
```

核心原则：

```text
用户不应该为了知道发生了什么而进入 session。
只有为了深度协作或手动接管，才进入 session。
```

## 2. 目标

1. 让 `plan` 阶段保留原生 Agent session 的多轮交互能力。
2. 让用户完成设计后可以自然退出，并回到 TUI Board。
3. 让 `plan` 阶段的自由对话最终沉淀为可供后续阶段消费的结构化产物。
4. 让 `implement`、`test`、`review` 不再默认要求用户进入 session。
5. 降低“进入 session 后不知道怎么退出”的产品摩擦。

## 3. 非目标

- P0 不在 Textual 内实现完整交互式 terminal emulator。
- P0 不做所有阶段的多 session tab 内嵌终端。
- P0 不要求 headless 命令支持原生多轮交互。
- P0 不引入 Web 控制台或复杂画布。
- P0 不让 LLM 自动替用户决定高风险 plan 选择。

## 4. 产品形态

### 4.1 默认入口：Board Mode

`story` 默认进入 Board。Board 是用户的常驻工作台，不是 session 启动器。

Board 至少展示：

- Story 列表。
- 当前 Story 阶段。
- 阶段状态：idle / planning / running / waiting_user / stuck / completed / failed。
- 最近事件。
- 右侧诊断摘要。
- 可执行动作。

示意：

```text
┌──────────────────────┬──────────────────────────────┬────────────────────┐
│ Stories              │ STORY-001                    │ Diagnostics        │
├──────────────────────┼──────────────────────────────┼────────────────────┤
│ > STORY-001 planning │ Stage: plan                  │ Stuck: none        │
│   STORY-002 running  │ Spec: not ready              │ Session: live      │
│   STORY-003 stuck    │                              │ Recent output      │
│                      │ [p] Open Planning Session    │ [d] Pack logs      │
│                      │ [v] View plan artifacts      │ [t] Terminal       │
└──────────────────────┴──────────────────────────────┴────────────────────┘
```

### 4.2 Plan 阶段：Planning Session

Plan 阶段允许进入原生 Agent session，因为它本质上是高密度协作阶段。

进入条件：

- Story 当前阶段是 `plan` / `design` / `brainstorming`。
- 当前没有有效 `plan` done。
- 用户显式按键进入，例如 `[p] Open Planning Session`。

进入前 TUI 显示短提示：

```text
Planning Session

你将进入 Agent 的交互式规划会话。
请在会话中完成需求澄清、方案选择和设计文档。

完成后，Agent 必须写入：
  .story/done/{story_key}/plan.json

退出方式：
  输入 exit 或按 Ctrl+D 返回 Story Board。
```

Planning Session 里允许自由对话：

- 用户和 Agent 深聊需求。
- Agent 读取 PRD、代码、项目事实。
- Agent 追问 blocking questions。
- 用户选择方案。
- Agent 生成设计文档。
- Agent 写出 done 文件。

但自由对话结束时必须结构化落盘。

### 4.3 Plan 完成协议

Plan session 完成后必须写：

```text
.story/done/{story_key}/plan.json
```

建议格式：

```json
{
  "status": "completed",
  "spec_path": "docs/specs/STORY-001-design.md",
  "complexity": "M",
  "decision_log_path": ".story/context/STORY-001/plan_decisions.md",
  "open_questions": [],
  "ready_for_implement": true,
  "summary": "已完成分段赔付方案设计，建议按金额区间建模。"
}
```

同时建议写：

```text
.story/context/{story_key}/plan_decisions.md
.story/context/{story_key}/plan_session_summary.md
```

`plan_decisions.md` 用于记录：

- 用户做过的关键选择。
- Agent 的重要假设。
- 被否决的方案。
- 仍需产品确认但不阻塞 implement 的问题。

后续 `implement` 阶段只消费结构化产物，不依赖 Planning Session 的原始聊天上下文。

## 5. 阶段交互策略

| 阶段 | 默认形态 | 是否默认进 session | 用户主要动作 |
|------|----------|--------------------|--------------|
| plan / design / brainstorming | Planning Session | 是，用户显式进入 | 深度讨论、选择方案、确认设计 |
| implement | 后台执行 + Board 观察 | 否 | 看状态、看 recent output、必要时接管 |
| test | 后台执行 + Board 观察 | 否 | 看测试结果、诊断失败 |
| review | Gate / reviewer 输出 + Board 观察 | 否 | 接受风险、重试、查看报告 |
| diagnostics | TUI 右侧面板 + CLI 打包 | 否 | 查看 stuck reason、打包日志 |

结论：

```text
Plan 是协作空间。
Execute 是后台运行时。
Terminal 是接管工具。
Board 是主产品界面。
```

## 6. Profile 分层：minimal 必须真的 minimal

降低心智负担不只来自 TUI，也来自默认工作流的阶段数量。`minimal` 作为默认 profile，应该只承载最小可用闭环，而不是把设计审查、代码审查、对抗循环和质量飞轮都压给新用户。

推荐分层：

| Profile | 阶段 | 目标用户 | 默认体验 |
|---------|------|----------|----------|
| `minimal` | `design -> implement` | 首次使用、快速验证、小需求 | 先把设计聊清楚，再后台实现 |
| `standard` | `design -> implement -> review` | 日常工程任务 | 保留代码审查，但不暴露过多中间状态 |
| `strict` | `design -> review_design -> implement -> review` | 高风险需求、团队协作、上线前审查 | 显式设计审查和代码审查 |
| `swebench` | `design -> implement -> test -> finalize` | benchmark / eval | 面向自动评测和 patch 导出 |

`minimal` 的产品承诺应该是：

```text
设计清楚 -> 实现完成 -> 可诊断
```

而不是：

```text
设计 -> 设计审查 -> 实现 -> 代码审查 -> 质量循环 -> 再进入下一步
```

### 6.1 Minimal Profile 建议

`minimal` 建议收敛为：

```yaml
stages:
  design:
    description: "需求澄清与方案设计"
    review: false
    next_default: [implement]
    expected_outputs:
      - spec_path
      - complexity

  implement:
    description: "编码实现"
    review: false
    next_default: []
    expected_outputs:
      - files_changed
      - summary
```

质量与诊断能力仍然存在，但不作为用户默认可见 stage：

- diagnostics 始终可用。
- done / malformed done 仍然被检测。
- implement 卡住时仍然进入 stuck reason。
- review 可以作为 `standard` / `strict` 的能力，不压在默认路径上。

### 6.2 现有 Profile 收敛建议

当前需要避免根目录 profile 与打包 profile 表达不同的默认体验。v0.6 收敛时应保证：

```text
profiles/minimal.yaml
src/story_lifecycle/profiles/minimal.yaml
```

语义一致。

如果现有 4 阶段版本仍有价值，应该迁移为：

```text
profiles/strict.yaml
src/story_lifecycle/profiles/strict.yaml
```

这样用户看到的默认路径就是最小闭环，高级审查能力通过显式 profile 打开。

### 6.3 与 Planning Session 的关系

`minimal` 缩短后，Planning Session 的价值更清晰：

```text
design:
  用户进入 Planning Session
  把需求、边界、方案和复杂度聊清楚
  产出 spec_path + complexity + decision log

implement:
  Agent 后台实现
  TUI 观察状态和 recent output
  卡住时 diagnostics 介入
```

这条路径保留了 plan 阶段的人机协作，又避免把后续执行阶段变成连续 session 管理。

## 7. 退出体验设计

退出体验必须被产品化，不能依赖用户知道 Zellij 内部快捷键。

### 7.1 Session 内提示

进入 Planning Session 后，启动脚本或 prompt 应持续提醒：

```text
完成设计后请：
1. 写入 `docs/specs/{story_key}-design.md` 设计文档
2. 写入 .story/done/{story_key}/plan.json
3. 输入 exit 或按 Ctrl+D 返回 Story Board
```

### 7.2 TUI 返回语义

TUI attach session 时应以“临时离开 Board”的方式处理：

```text
Board 暂停
-> attach Planning Session
-> 用户 exit / Ctrl-D
-> 返回 Board
-> 自动刷新 story 状态
-> 如果 plan done 存在，展示 spec_path 和 complexity
```

用户不应该手动重新执行 `story` 才能回到 Board。

### 7.3 退出后的状态提示

退出 session 后，Board 根据事实给出明确反馈：

| 事实 | Board 提示 | 下一步 |
|------|------------|--------|
| `plan.json` 存在且解析成功 | Plan completed: spec ready | `[r] Continue implement` |
| `plan.json` 缺失 | Plan session exited without done | `[p] Re-enter planning` / `[d] diagnostics` |
| `plan.json` malformed | Plan done malformed | `[v] view malformed` / `[p] fix in planning` |
| `open_questions` 非空但 `ready_for_implement=true` | Plan ready with open questions | 用户确认后继续 |
| `ready_for_implement=false` | Waiting user decision | 回到 Planning Session |

## 8. Session 命名与生命周期

P0 建议仍以 story 级 session 为主，避免每个 stage 都创建一个独立 session。

推荐：

```text
s-{story_key}
```

Planning Session 是这个 story session 的一个使用模式，而不是单独进程族。

未来如果确实需要阶段隔离，可以扩展：

```text
s-{story_key}-plan
s-{story_key}-implement
s-{story_key}-review
```

但 P0 不默认启用，避免 session 数量膨胀和清理复杂度上升。

## 9. TUI 信息架构

### 9.1 Story 详情区

当前 Story 详情区展示：

- 当前 stage。
- Plan 产物状态。
- `spec_path`。
- `complexity`。
- `ready_for_implement`。
- open questions 数量。

### 9.2 右侧诊断区

右侧诊断区展示：

- session 状态。
- done 状态。
- stuck reason。
- 最近事件。
- recent output。
- 诊断动作。

### 9.3 快捷键建议

| 快捷键 | 动作 |
|--------|------|
| `p` | Open Planning Session |
| `e` | Enter active runtime session / terminal takeover |
| `r` | Continue / retry current stage |
| `v` | View artifacts |
| `d` | Pack diagnostics |
| `Esc` | 关闭弹层 / 回 Board |

`p` 和 `e` 要区分：

- `p` 是 Plan 阶段的协作入口。
- `e` 是运行时接管入口。

## 10. 与现有实现的关系

当前代码已有基础：

- `ttyd.create_session()`：可创建 Zellij session。
- `ttyd.attach_args()`：可 attach 到 session。
- `ttyd.capture_pane()`：可抓取最近屏幕输出。
- `ttyd.resolve_session_state()`：可区分 live / exited / missing / unknown。
- `stage_done_file()`：已有 done 文件路径协议。
- `debug_packet.py` / `diagnostics.py`：已有诊断包基础。
- TUI 已有 session state、stuck reason、Copilot 区域和 attach 逻辑。

因此该设计不是重写执行模型，而是收敛入口语义：

```text
原有 e：进入/观察执行 session
新增或强化 p：进入 Planning Session
Board：退出后自动恢复和刷新
```

## 11. 风险与边界

### 11.1 Windows + Zellij 后台注入风险

已有设计文档指出，Windows 下后台创建 Zellij session 可能出现空 pane / ConPTY 问题。因此 P0 不应依赖“后台 session 等待注入”作为唯一执行路径。

Plan Session 应优先使用前台 attach / foreground handoff，让用户看到真实交互界面。

### 11.2 用户忘记写 done

用户和 Agent 聊完但没有写 `plan.json` 是主要失败模式。

缓解：

- prompt 中强制说明 done 协议。
- 退出后 Board 检测 `plan.json` 缺失并给出明确提示。
- diagnostics 包含 plan session recent output。

### 11.3 Plan 聊天上下文丢失

原生 CLI session 的上下文不一定能被后续阶段读取。

缓解：

- Plan 完成必须写 `plan_decisions.md`。
- `plan.json` 必须给出 `spec_path`、`summary`、`ready_for_implement`。
- 后续阶段只依赖文件产物，不依赖 native session memory。

### 11.4 Session 数量膨胀

每阶段 session 会让产品变复杂。

P0 默认 story 级 session，stage 级 session 只作为后续扩展。

### 11.5 Minimal 过度压缩

如果 `minimal` 过度压缩，可能让用户误以为系统没有 review、test、quality gate 等能力。

缓解：

- Board 上显示当前 profile 和可切换建议。
- `story create --profile strict` 明确提供严格路径。
- 文档中说明 `minimal` 是默认快速路径，不是能力上限。

## 12. 验收标准

P0 验收：

1. 用户在 Board 选择 plan story，能通过明确入口进入 Planning Session。
2. Planning Session 入口提示清楚说明如何完成、如何退出、必须写什么产物。
3. 用户退出 session 后自动回到 Board。
4. Board 能根据 `plan.json` 状态显示 completed / missing / malformed。
5. `plan.json` 解析成功后，Board 能展示 `spec_path`、`complexity`、`open_questions`。
6. `implement` 阶段默认不要求用户进入 session。
7. 卡住时，右侧诊断区能提供 stuck reason 和 diagnostics 打包入口。

8. 默认 `minimal` profile 只展示 `design -> implement` 的最小路径。
9. `strict` profile 保留显式设计审查和代码审查能力。
10. 根目录和包内 `minimal.yaml` 语义一致。

## 13. 建议落地顺序

1. 明确 TUI 中 `p` 与 `e` 的语义边界。
2. 为 Planning Session 增加专用进入提示和 prompt 尾部协议。
3. 退出 session 后刷新 Board，并消费/校验 `plan.json`。
4. 在右侧诊断面板展示 Plan artifact 状态。
5. 将 `minimal` 收敛为 `design -> implement`。
6. 将现有严格流程迁移到 `strict` profile。
7. 保证根目录 profile 和包内 profile 一致。
8. 为 `plan done missing`、`plan done malformed`、`plan completed` 补回归测试。
9. 再考虑 stage tab 和 recent output 增强。

## 14. 一句话总结

```text
Plan 阶段允许进入 session，因为它是深度协作；
Plan 结束必须结构化落盘，因为后续阶段需要确定性上下文；
minimal 默认只保留 design -> implement，因为新用户需要最小闭环；
Plan 之后回到 Board，因为 Story Lifecycle 的主产品形态不是 terminal，而是可观察、可诊断、可推进的 Story Board。
```
