> ⚠️ **历史快照（归档于 2026-07）**：描述的架构可能已被后续演进取代。当前架构见 [../../ARCHITECTURE.md](../../ARCHITECTURE.md)。本文件保留作决策记录（ADR），正文未修改。

---

# 设计文档：Board 右侧常驻诊断面板

## 版本记录

| 版本 | 日期 | 说明 |
|---|---|---|
| v0.1 | 2026-05-27 | 从 `idea-board-copilot-diagnostics-panel.md` 收敛为可实施设计 |

## 背景

`story board` 是 Story Lifecycle 的主入口。用户在这里创建 Story、进入终端、恢复执行、跳过阶段、失败 Story、运行 doctor/setup。当前问题是：当 Story 卡住时，用户只能看到有限的状态和 `last_error`，维护者也需要反复让用户补充日志、done 文件、配置、版本和事件记录。

这个设计引入一个右侧常驻诊断面板，并把诊断能力下沉为可复用的核心模块：

```text
Debug Packet / Diagnostic Bundle
  -> story diagnostics CLI
  -> board right diagnostics panel
  -> future Ask Copilot
```

右侧面板是产品形态，诊断核心是工程边界。P0 不做 Copilot 聊天，不默认调用 LLM，不让 LLM 改状态。P0 只做确定性事实、规则解释和一键诊断打包。

## 目标

P0 目标：

1. 新增稳定的 Story Debug Packet 构造函数。
2. 新增 `story diagnostics STORY_KEY` 和 `story diagnostics --global`。
3. 新增诊断包生成能力，默认脱敏并写入 zip。
4. 在 `story board` 右侧新增常驻诊断面板。
5. 面板跟随当前选中 Story，展示摘要、卡住原因、最近事件和诊断动作。
6. TUI 支持 `[p]` 打包当前 Story 诊断、`[P]` 打包全局诊断。
7. 所有诊断包生成写入 `event_log`。

非目标：

1. P0 不做 LLM 对话输入框。
2. P0 不做自动上传或远程发送日志。
3. P0 不包含完整源码、完整业务日志或完整 diff。
4. P0 不让诊断面板直接修改工作流状态。
5. P0 不读取 LangGraph 私有 checkpoint 结构。

## 用户体验

Board 改为左右布局：

```text
┌──────────────────────────────────────────────────────────────────────┐
│ Header                                                               │
├──────────────────────────────────────────────┬───────────────────────┤
│ Left pane                                    │ Diagnostics           │
│ - plan panel                                 │ - selected story      │
│ - story list                                 │ - stuck reason        │
│ - completed section                          │ - recent events       │
│ - detail panel                               │ - diagnostic actions  │
├──────────────────────────────────────────────┴───────────────────────┤
│ Footer                                                               │
└──────────────────────────────────────────────────────────────────────┘
```

右侧面板固定展示当前选中 Story 的诊断摘要：

```text
Diagnostics

STORY-001
status: active
stage: implement

可能卡住：
CLI 已退出，但当前阶段未写 done 文件。

最近事件：
14:21 execute_stage
14:24 terminal_exit
14:24 cli_exited_without_done

[p] package story diagnostics
[P] package global diagnostics
```

窄屏策略：

- 宽度充足时右侧常驻。
- 宽度不足时隐藏右侧面板，只在 footer 提示 `[o] diagnostics`。
- P0 可以先用固定阈值判断，例如终端宽度 `< 120` 隐藏。

## 快捷键

| 快捷键 | P0 行为 |
|---|---|
| `o` | 显示/隐藏右侧诊断面板 |
| `p` | 为当前选中 Story 生成诊断包 |
| `shift+p` | 生成全局诊断包 |
| `d` | 保持现有 detail 行为 |
| `shift+d` | 保持现有 doctor 行为 |

P0 不新增聊天输入快捷键。`ctrl+enter`、`y` 等 Copilot 交互留到后续阶段。

## 架构

### 模块划分

```text
src/story_lifecycle/
  orchestrator/
    debug_packet.py       # 构造稳定 Debug Packet
    diagnostics.py        # 生成诊断包、脱敏、写 zip
  cli/
    diagnostics.py        # story diagnostics 命令
    tui.py                # 右侧诊断面板和快捷键
```

现有 `orchestrator/observability.py::build_debug_response()` 已经提供很多只读诊断数据。P0 不复制一套查询逻辑，而是做一次收敛：

- `debug_packet.py` 提供新的稳定 schema。
- `build_debug_response()` 可以内部调用 `build_debug_packet()`，或反过来由 `debug_packet.py` 复用现有 helper。
- 外部 UI 和 CLI 只依赖 `build_debug_packet()`，避免直接绑定旧 debug response 的字段命名。

### 数据流

TUI 渲染：

```text
selected story
  -> build_debug_packet(story_key)
  -> explain_stuck_reason(packet)
  -> render diagnostics panel
```

Story 级诊断：

```text
story diagnostics STORY_KEY
  -> build_debug_packet(story_key)
  -> collect story DB rows / events / stage logs / gates
  -> collect safe workspace .story files
  -> collect safe terminal/session hints
  -> redact
  -> write .story/diagnostics/{story_key}-{timestamp}.zip
  -> db.log_event(..., "diagnostic_bundle_created", ...)
```

全局诊断：

```text
story diagnostics --global
  -> collect version / python / executable / package location
  -> collect PATH summary
  -> collect config.redacted.yaml
  -> probe story/setup/doctor help
  -> probe package resources
  -> write ~/.story-lifecycle/diagnostics/global-{timestamp}.zip
```

## Debug Packet Schema

P0 使用 dict/dataclass 均可；对外写入 JSON 时使用 snake_case。

```json
{
  "schema_version": 1,
  "generated_at": "2026-05-27T14:20:33+08:00",
  "story": {
    "story_key": "STORY-001",
    "title": "新增分段赔付",
    "status": "active",
    "current_stage": "implement",
    "workspace": "D:\\project",
    "profile": "minimal",
    "execution_count": 1,
    "last_error": ""
  },
  "done_state": {
    "stage": "implement",
    "path": ".story/done/STORY-001/implement.json",
    "exists": false,
    "valid": null,
    "malformed_path": "",
    "snapshot_path": ".story/context/STORY-001/done/implement.json"
  },
  "session_state": {
    "backend": "zellij",
    "session_name": "story-STORY-001",
    "session_alive": false,
    "cli_exit_state": "exited_without_done",
    "stage_started_at": "2026-05-27T14:05:00+08:00",
    "stage_elapsed_seconds": 930
  },
  "terminal_output": {
    "available": true,
    "path": "terminal/recent_output.txt",
    "line_count": 500,
    "truncated": true,
    "missing_reason": ""
  },
  "stuck_reason": {
    "code": "cli_exited_without_done",
    "severity": "warning",
    "message": "CLI 已退出，但当前阶段未写 done 文件。"
  },
  "recent_events": [],
  "recent_stage_logs": [],
  "gate_results": [],
  "file_hints": {
    "story_context_dir": ".story/context/STORY-001",
    "done_dir": ".story/done/STORY-001",
    "graph_error_log": "~/.story-lifecycle/graph_error.log",
    "planner_error_log": "~/.story-lifecycle/planner_error.log"
  }
}
```

### Stuck Reason 规则

P0 先使用确定性规则：

| code | 条件 | severity | message |
|---|---|---|---|
| `none` | 未发现异常 | info | 当前未发现阻塞信号 |
| `missing_config` | 未配置 LLM key 或 provider | error | LLM 配置缺失，请运行 `story setup` |
| `done_malformed` | 当前 done 存在但解析失败 | error | done JSON 损坏，请查看 malformed 文件 |
| `done_waiting` | session alive 且 done 不存在 | info | Agent 正在执行或等待 done 文件 |
| `cli_exited_without_done` | CLI 已退出且 done 不存在 | warning | CLI 已退出，但当前阶段未写 done 文件 |
| `stage_timeout` | session alive、done 不存在、当前 stage 执行时间超过阈值 | warning | 当前阶段运行时间过长，可能陷入等待或长耗时命令 |
| `loop_exhausted` | review/plan 对抗循环达到 max_rounds 或 no-progress | warning | 对抗循环已达到上限，可能需要人工介入 |
| `gate_blocked` | status paused 且存在 gate_decision/last_error | warning | Gate 阻塞，需要处理审查结果 |
| `story_blocked` | status blocked | error | Story 已阻塞，需要人工恢复或失败处理 |
| `waiting_subtasks` | status waiting_subtasks | info | 父 Story 正在等待子任务完成 |

这些规则应放在 `debug_packet.py`，TUI 只消费结果，不重复判断。

`stage_timeout` 的默认阈值 P0 可先写死为 15 分钟，后续再放入 profile。`loop_exhausted` 应优先从 `event_log` 中的 evaluator/review/no-progress 事件判断，而不是由 TUI 重新推理。

## Diagnostic Bundle

### 输出路径

Story 级：

```text
{workspace}/.story/diagnostics/{story_key}-{YYYYMMDD-HHMMSS}.zip
```

全局级：

```text
~/.story-lifecycle/diagnostics/global-{YYYYMMDD-HHMMSS}.zip
```

### Story 级包内容

```text
manifest.json
summary.md
debug_packet.json
story.json
events.jsonl
stage_logs.jsonl
gate_results.jsonl
config.redacted.yaml
environment.txt
done/
  current.json
  current.malformed
  snapshots/
context/
  known_context_files.txt
terminal/
  session_state.json
  recent_output.txt
workspace/
  git_status.txt
  git_diff_stat.txt
```

P0 不要求所有文件都存在；缺失项写入 `manifest.json` 的 `missing` 列表。

`terminal/recent_output.txt` 是 Story 级诊断的关键产物。P0 规则：

- 如果后端是 Zellij，必须尝试采集当前 pane 最近 N 行 scrollback。
- 默认 N=500 行，超过后截断并在 manifest 标记 `truncated=true`。
- 如果当前平台、后端或 session 状态无法采集，必须在 `manifest.json` 里写明 `missing_reason`。
- 如果采集失败，不中断诊断包生成。
- Windows 下如果没有可用终端后端，允许缺失，但 summary.md 必须提示维护者终端输出不可用。

终端输出的优先级高于 `git_diff_stat.txt`。排查 “CLI 退出但没有 done” 时，维护者应首先查看 `terminal/recent_output.txt`。

### Summary 报告

`summary.md` 是面向维护者的人读报告，不是 `debug_packet.json` 的格式化副本。它必须把 P0 规则结论和下一步关注点写清楚。

模板：

```markdown
# 诊断报告: STORY-001

- **状态**: active / implement
- **Workspace**: D:\project
- **卡住原因**: cli_exited_without_done
- **说明**: CLI 已退出，但当前阶段未写 done 文件。

## 最近关键事件

- 14:21 execute_stage
- 14:24 terminal_exit
- 14:24 cli_exited_without_done

## 重点关注

1. 先看 `terminal/recent_output.txt`，确认 Agent 执行命令是否报错。
2. 再看 `debug_packet.json` 的 `done_state`。
3. 如果存在 malformed done，查看 `done/current.malformed`。

## 包内容状态

- terminal/recent_output.txt: present, truncated=true
- done/current.malformed: missing
- config.redacted.yaml: present
```

如果 `stuck_reason.code=stage_timeout`，重点关注应提示检查最近终端输出是否停在长耗时命令、依赖安装或测试命令。若 `loop_exhausted`，重点关注应提示查看 review/evaluator 事件。

### Global 包内容

```text
manifest.json
summary.md
environment.txt
config.redacted.yaml
commands/
  story_help.txt
  story_setup_help.txt
  story_doctor_help.txt
package/
  metadata.json
  resource_probe.json
logs/
  graph_error_tail.log
  planner_error_tail.log
```

### Manifest

```json
{
  "schema_version": 1,
  "bundle_type": "story",
  "story_key": "STORY-001",
  "created_at": "2026-05-27T14:20:33+08:00",
  "story_lifecycle_version": "0.5.8",
  "workspace": "D:\\project",
  "files": [
    {"path": "debug_packet.json", "kind": "json", "redacted": true}
  ],
  "missing": [
    {"path": "terminal/recent_output.txt", "reason": "not available on Windows"}
  ],
  "truncated": [
    {"path": "terminal/recent_output.txt", "line_limit": 500}
  ]
}
```

## 脱敏策略

统一实现 `redact_text()` 和 `redact_mapping()`。

必须脱敏：

- `api_key`
- `token`
- `password`
- `secret`
- `authorization`
- `cookie`
- `STORY_LLM_API_KEY`
- OpenAI/DeepSeek/Anthropic 常见 key 格式

默认不打包：

- `.env`
- 完整源码
- 完整 git diff
- 大型日志目录
- 业务数据库 dump

默认截断：

- 单个文本文件最多 200KB。
- terminal/log tail 最多 500 行。
- event_log 默认最近 200 条。

## CLI 设计

新增命令：

```text
story diagnostics STORY_KEY
story diagnostics --global
```

选项：

| 参数 | 说明 |
|---|---|
| `--output PATH` | 指定输出 zip 路径或目录 |
| `--include-diff` | 显式包含完整 git diff，默认关闭 |
| `--event-limit N` | event_log 条数，默认 200 |
| `--no-zip` | 输出目录而不是 zip，便于本地调试 |

未配置 LLM 时该命令必须可运行。它应加入 `cli/main.py` 的配置检查豁免列表。

输出示例：

```text
Diagnostic bundle created:
D:\project\.story\diagnostics\STORY-001-20260527-142033.zip
```

## TUI 设计

### Compose 结构

当前：

```python
yield Static(id="header-bar")
yield Static(id="plan-panel")
yield VerticalScroll(id="story-list")
yield Static(id="completed-section")
yield Static(id="detail-panel")
yield Static(id="footer-bar")
yield Footer()
```

目标：

```python
yield Static(id="header-bar")
with Horizontal(id="body-row"):
    with Vertical(id="left-pane"):
        yield Static(id="plan-panel")
        yield VerticalScroll(id="story-list")
        yield Static(id="completed-section")
        yield Static(id="detail-panel")
    yield Static(id="diagnostics-panel")
yield Static(id="footer-bar")
yield Footer()
```

### CSS

```css
#body-row {
    height: 1fr;
}

#left-pane {
    width: 1fr;
}

#diagnostics-panel {
    width: 44;
    min-width: 34;
    max-width: 56;
    padding: 1;
    border-left: solid $accent;
    background: $panel;
}

#diagnostics-panel.hidden {
    display: none;
}
```

### 窄屏布局规则

右侧常驻面板不能挤爆左侧 Story 列表。P0 使用确定性宽度规则：

| terminal width | 行为 |
|---|---|
| `< 120` 列 | 默认隐藏 diagnostics panel，左侧占满 |
| `120-159` 列 | diagnostics panel 压缩为 34-38 列 |
| `>= 160` 列 | diagnostics panel 使用 44 列 |

左侧面板必须保留最小可读宽度。建议约束：

```text
left_min_width = 84
diagnostics_min_width = 34
diagnostics_default_width = 44
```

如果 `terminal_width - diagnostics_width < left_min_width`，必须隐藏或压缩 diagnostics panel，而不是继续压缩 Story 列表。

### 渲染函数

新增：

```python
def _render_diagnostics_panel(self) -> None:
    ...

def _render_diagnostics_packet(packet: dict) -> str:
    ...
```

`_render()` 在 full 和非 full 场景都应更新右侧面板，因为 cursor 移动时选中 Story 会变。

### TUI Action

新增：

```python
def action_toggle_diagnostics(self): ...
def action_package_story_diagnostics(self): ...
def action_package_global_diagnostics(self): ...
```

`action_package_story_diagnostics()` 成功后：

- 面板显示 zip 路径。
- `notify()` 提示。
- `db.log_event(story_key, current_stage, "diagnostic_bundle_created", payload)`。

## 与现有代码的关系

| 模块 | 改动 |
|---|---|
| `orchestrator/observability.py` | 可改为复用 `build_debug_packet()`，避免双份 debug schema |
| `orchestrator/entry.py` | 复用 `validate_stage_done()`、`resolve_cli_exit_state()` |
| `orchestrator/paths.py` | 复用 `.story/done`、`.story/context` 路径 |
| `db/models.py` | 新增 stage_log/gate_result 查询 helper，如缺失 |
| `cli/main.py` | 注册 diagnostics 命令，加入配置检查豁免 |
| `cli/tui.py` | 新增右侧布局、渲染和快捷键 |
| `cli/doctor.py` | global diagnostics 可复用 doctor 检查逻辑或捕获输出 |

## 错误处理

| 场景 | 行为 |
|---|---|
| story 不存在 | CLI 返回非 0，TUI 面板显示错误 |
| workspace 不存在 | 仍生成 DB/全局诊断，manifest 标记 workspace missing |
| done 文件损坏 | 不解析内容，原样复制到 `done/current.malformed` |
| zip 写入失败 | 回退到用户级 diagnostics 目录 |
| 单个文件读取失败 | 记录到 manifest missing，不中断整个包 |
| git 命令不可用 | 跳过 git_status/git_diff_stat |
| 未配置 LLM | 不影响 diagnostics |

## 测试策略

单元测试：

1. `build_debug_packet()` 覆盖 active/paused/blocked/waiting_subtasks。
2. malformed done 文件不会被吞掉，packet 中 `done_state.valid=false`。
3. `explain_stuck_reason()` 对主要状态返回稳定 code。
4. `stage_timeout` 在 session alive 且超阈值时触发。
5. `loop_exhausted` 能从 evaluator/review/no-progress 事件触发。
6. `redact_text()` 能脱敏 key/token/password/authorization。
7. `create_story_diagnostics_bundle()` 生成 manifest、summary、debug_packet。
8. `summary.md` 包含 stuck reason、最近事件和重点关注文件。
9. `terminal/recent_output.txt` 可缺失但必须在 manifest 记录原因。
10. `create_global_diagnostics_bundle()` 未配置 LLM 时仍能运行。

TUI 层测试：

1. `_render_diagnostics_packet()` 对空 story、正常 story、blocked story 都能输出。
2. cursor 切换时右侧面板更新。
3. `[p]` 调用 bundler 并展示路径。
4. 终端宽度 `< 120` 时默认隐藏右侧面板。
5. 终端宽度不足以保留左侧最小宽度时不挤压 Story 列表。

集成测试：

1. 构造 CLI exited without done 的 Story，诊断包记录 stuck_reason。
2. 构造 malformed done，诊断包包含 malformed 文件。
3. 构造 stage timeout，右侧面板展示长耗时警告。
4. 构造 loop exhausted，诊断包记录相关 evaluator/review 事件。
5. Zellij 可用时，诊断包包含 `terminal/recent_output.txt`。
6. `story diagnostics --global` 输出 zip 且不包含 API key。

## 落地顺序

1. `orchestrator/debug_packet.py`
2. `cli/diagnostics.py` + `story diagnostics STORY_KEY --no-zip`
3. `summary.md`、redaction、terminal recent output
4. `orchestrator/diagnostics.py` zip bundle
5. 单元测试覆盖 debug packet、stuck reason、redaction、summary、bundle
6. 用一个真实/模拟 Story 跑 `story diagnostics STORY_KEY --no-zip`，确认目录和报告足够回答问题
7. `cli/tui.py` 右侧只读面板
8. TUI `[p]` / `[P]` 接入 bundler
9. 回归 `story setup`、`story doctor`、`story diagnostics --global`

## 后续阶段

P1 Ask Copilot：

- 用户主动提问才调用 LLM。
- 输入只用 redacted Debug Packet。
- 输出结构化 `CopilotResponse`。
- 不自动执行建议。

P2 SuggestedAction：

- Copilot 可以生成待确认动作。
- workflow_state 动作必须确认。
- 所有确认/拒绝写入 event_log。

P3 Policy Engine：

- SuggestedAction 升级为 DecisionEnvelope。
- 接入 autonomy level 和 policy check。
- 右侧面板展示 policy decision。
