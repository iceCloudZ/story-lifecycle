# TUI n/r/e/.done/session 决策逻辑设计

日期：2026-05-24

## 目标

将 TUI 中 `n`/`r`/`e` 的入口行为从分散的 if/else 提取为可单测的纯决策逻辑，统一 `.done` 文件处理，并修复 `e` 键误启动 Claude、Windows 下不可靠 session 创建等问题。

## 新增模块

### `src/story_lifecycle/orchestrator/entry.py`

包含三层抽象：

#### 层 1：`.done` helpers

```python
def stage_done_file(story: dict) -> Path:
    """返回当前 stage 的 .done 文件路径。"""

def has_stage_done(story: dict) -> bool:
    """当前 stage 的 .done 是否存在（不校验内容）。"""

class DoneStatus(Enum):
    OK = "ok"
    CORRUPTED = "corrupted"
    MISSING = "missing"

@dataclass
class DoneValidationResult:
    status: DoneStatus
    data: dict | None = None   # OK 时为解析后的 dict
    error: str | None = None   # CORRUPTED 时为错误信息

def validate_stage_done(story: dict) -> DoneValidationResult:
    """校验 .done 文件。OK 返回解析数据，CORRUPTED 返回错误信息，MISSING 表示不存在。"""
```

`validate_stage_done` 内部复用 `robust_json_parse`。损坏文件返回 `CORRUPTED`，调用方据此提示用户修复或删除，不启动 AI。

#### 层 2：SessionBackend

```python
from typing import Protocol

class SessionBackend(Protocol):
    def is_healthy(self, session_id: str) -> bool: ...
    def attach_foreground(self, session_id: str) -> None: ...
    def launch_independent_terminal(
        self, story_key: str, workspace: str, launch_cmd: str, prompt_file: str
    ) -> None: ...

class TtydSessionBackend:
    """默认实现，封装现有 ttyd 模块调用。"""

    def is_healthy(self, session_id: str) -> bool:
        return ttyd.session_alive(session_id)

    def attach_foreground(self, session_id: str) -> None:
        # TUI 退出后由 run_tui() 执行 subprocess.run(attach_args)
        # 此方法返回 attach_args，实际 attach 由 TUI handler 完成
        ...

    def launch_independent_terminal(self, story_key, workspace, launch_cmd, prompt_file):
        ttyd.launch_cli(story_key, workspace, launch_cmd, prompt_file)
```

TUI 注入 `TtydSessionBackend`，测试用 mock 实现。

`attach_foreground` 的实际语义：TUI 需要先退出 Textual 再运行 `subprocess.run(attach_args)`。所以 `TtydSessionBackend.attach_foreground` 返回 attach 参数，TUI handler 负责实际的退出+subprocess 调用。在 mock 中这个方法只记录调用。

#### 层 3：纯决策函数

```python
class StageEntryState(Enum):
    DONE = "done"                        # .done 存在且有效
    DONE_CORRUPTED = "done_corrupted"    # .done 存在但损坏
    RUNNING_HEALTHY = "running_healthy"  # story 运行中，session 健康
    RUNNING_DEAD = "running_dead"        # story 运行态但 session 不健康，无 .done
    IDLE = "idle"                        # 未运行、无 .done、无 session
    STORY_FINISHED = "story_finished"    # completed/failed/aborted

class StageEntryAction(Enum):
    ATTACH = "attach"                      # attach 到已有 session
    START_OR_RESUME = "start_or_resume"    # 启动/恢复 graph 执行
    PROMPT_PRESS_R = "prompt_press_r"      # 提示用户按 r 启动
    PROMPT_DONE_PRESS_R = "prompt_done_press_r"  # .done 已存在，提示按 r 消费
    PROMPT_FIX_DONE = "prompt_fix_done"    # .done 损坏，提示修复
    NOOP = "noop"                          # 无操作

def resolve_stage_state(
    story: dict,
    backend: SessionBackend,
    is_running: bool,
) -> StageEntryState:
    """根据 story 状态、.done 文件、session 健康度，输出 StageEntryState。

    优先级：story 终态 > .done 状态 > running + session > idle。
    """

def decide_action(
    state: StageEntryState,
    user_action: Literal["e", "r"],
) -> StageEntryAction:
    """纯函数：(state, user_action) → action。覆盖设计文档状态决策表。"""
```

### 状态决策表映射

| StageEntryState | `e` | `r` |
|---|---|---|
| DONE | PROMPT_DONE_PRESS_R | START_OR_RESUME |
| DONE_CORRUPTED | PROMPT_FIX_DONE | PROMPT_FIX_DONE |
| RUNNING_HEALTHY | ATTACH | NOOP（不重复启动） |
| RUNNING_DEAD | PROMPT_PRESS_R | START_OR_RESUME |
| IDLE | PROMPT_PRESS_R | START_OR_RESUME |
| STORY_FINISHED | NOOP | NOOP |

`.done` 优先级高于 running/session：即使 session dead，只要 .done 有效，状态为 `DONE`。

## TUI 修改

### `action_enter_terminal(e)`

```
1. resolve_stage_state(story, backend, is_running)
2. decide_action(state, "e")
3. switch action:
   ATTACH → 退出 Textual，attach 到 session
   PROMPT_DONE_PRESS_R → detail panel 显示 "Stage 已完成，按 r 继续推进"
   PROMPT_PRESS_R → detail panel 显示 "没有运行中的 session，按 r 启动执行"
   PROMPT_FIX_DONE → detail panel 显示 ".done 文件损坏：{error}，请修复或删除"
   NOOP → 无操作
```

`e` 永不启动 graph、不创建空 session、不调用 `create_session`。

### `action_resume_story(r)`

```
1. resolve_stage_state(story, backend, is_running)
2. decide_action(state, "r")
3. switch action:
   START_OR_RESUME → update status=active, start_story_async(key)
   NOOP → 无操作（已在运行或终态）
   PROMPT_FIX_DONE → detail panel 显示损坏信息
```

### `_startup_sweep` / `watchdog_check`

- 用 `has_stage_done(story)` 替换内联的 `Path(ws) / ".story-done" / key / f"{stage}.json"` + `.exists()`
- 消费 `.done` 前用 `validate_stage_done` 校验，损坏的不触发 graph

### Windows 执行链路

- `e` 不再调用 `ttyd.create_session`（不创建空 Zellij pane）
- `r` 的 `START_OR_RESUME` 走 `start_story_async` → graph nodes → `launch_cli`，用独立终端启动 AI CLI
- 不依赖 `zellij attach --create-background` 启动执行用 AI session

## 测试

### `tests/test_entry_decisions.py`

覆盖决策表所有行：

1. `resolve_stage_state` 测试：
   - story completed → STORY_FINISHED
   - .done 有效 → DONE
   - .done 损坏 → DONE_CORRUPTED
   - running + session healthy → RUNNING_HEALTHY
   - running + session dead + 无 .done → RUNNING_DEAD
   - 未运行 + 无 .done → IDLE

2. `decide_action` 测试：
   - 6 state × 2 action = 12 个组合，对照决策表验证

3. `.done` helper 测试：
   - 正常 JSON → OK + data
   - 损坏 JSON → CORRUPTED + error
   - 文件不存在 → MISSING
   - markdown 包裹的 JSON → OK

4. Mock `SessionBackend`，不依赖真实 Zellij/tmux

## 不做的事

- `e` 不自动启动 Claude
- `e` 不自动消费 .done
- Windows 下不用 `zellij attach --create-background` 创建执行用 AI session
- 不引入超出测试需要的 backend 抽象
- 不改无关 UI/样式
