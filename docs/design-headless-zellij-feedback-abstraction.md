# Headless 链路反哺 Zellij 链路的抽象设计

## 背景

SWE-bench headless 调试暴露了一组以前在交互式 Zellij 链路里不容易被稳定复现的问题：

1. `claude -p` headless 模式不会稳定写阶段完成握手文件。
2. LLM 可能只在 stdout 中说明完成，但没有交付机器可验证的阶段产物。
3. `synthetic: true` 如果作为全局 context 字段，会污染后续阶段，让后续 `expected_outputs` 校验失效。
4. Story 可以显示 `completed`，但 `predictions.jsonl` 中 `model_patch` 为空。
5. 交互式 session、Zellij foreground handoff、headless subprocess 属于不同执行方式，但它们应该共享同一套完成协议和验收语义。

最近的修复已经把几个关键点拉回共享路径：

- headless 优先走 subprocess，不再被残留 session 抢走。
- `claude -p` 使用 stdin 传 prompt，并启用 `--permission-mode acceptEdits`。
- synthetic 标记改为 stage-scoped：`_synthetic_{stage}`。
- SWE-bench `finalize` 增加 patch hard gate：必须有 `model_patch` 或有效 `git diff`。
- exporter 遇到空 patch 会把 manifest 标为 `export_failed / empty_patch`。

这些修复说明：headless 链路最有价值的反哺不在“怎么启动 CLI”，而在“怎么定义阶段完成、怎么验证产物、怎么导出可评估结果”。

## 目标

1. 抽象出 headless 和 Zellij 都能复用的协议层能力。
2. 保留 headless 与 Zellij 各自不同的执行层能力。
3. 降低 `nodes.py` 和 `tools/base.py` 的职责密度。
4. 让 SWE-bench、普通 story、未来 evaluator/reviewer loop 都能使用统一的 stage output validation。
5. 避免再次出现 `completed` 但产物为空、done 文件被消费后无法导出、synthetic 状态跨阶段污染等问题。
6. 收敛项目内隐藏目录，避免业务仓库根目录出现多个 Story Lifecycle 专用隐藏文件夹。

## 非目标

1. 不把 headless 的 stdout 合成 done 文件机制强行套到 Zellij 链路。
2. 不重写 LangGraph 拓扑。
3. 不保留 `.story-done` 作为阶段握手协议的主路径；新协议统一使用 `.story/done`。
4. 不在 P0 引入复杂插件系统或 profile-specific class hierarchy。
5. 不恢复后台 `zellij attach --create-background` 作为执行主路径。
6. 不迁移全局 home 目录 `~/.story-lifecycle/`；本设计只收敛项目 workspace 内的隐藏目录。

## 当前职责边界

### 执行层

`BaseTool._launch_in_session()` 负责选择执行方式：

- headless：`subprocess.run()`，stdin 输入 prompt，stdout 合成 done。
- TUI + existing session：向已有健康 session 注入 prompt。
- TUI + Zellij foreground：发 terminal request，由 TUI 把真实终端交给 Zellij。
- fallback：打开独立 terminal。

这层的职责应该止步于“执行 CLI 并尽力产生 done 或错误”。它不应该理解 SWE-bench patch 质量，也不应该决定 story 是否业务完成。

### 握手层

`poll_completion_node()` 是 `.story/done` 的唯一消费者：

- 解析 done JSON。
- 删除 done 文件。
- 将 done 数据 merge 到 `state["context"]`。
- 将 synthetic 标记转成 stage-scoped context：`_synthetic_{stage}`。

这层应该只负责“把执行结果转成状态事实”，不应该做 profile-specific 业务判断。

### 验收层

`advance_node()` 当前承担：

- expected_outputs 校验。
- SWE-bench finalize patch gate。
- DoD gate。
- next stage 推进。
- completed 状态落库。

这里已经开始过载。headless 反哺 Zellij 的主要抽象应落在这一层。

### 导出层

`benchmarks.swebench._read_model_patch()` 和 `export_predictions()` 负责生成 SWE-bench prediction：

- 优先读 `finalize.json.model_patch`。
- 其次读 `final.patch`。
- 再 fallback 到 `git diff` 或 `git diff base_commit`。
- 空 patch 标记 `export_failed / empty_patch`。

这层和 finalize gate 存在重复规则，应抽出共享 patch extractor。

### 项目隐藏目录

当前项目 workspace 中可能出现多个 Story Lifecycle 专用隐藏目录：

- `.story-done/`：阶段完成握手文件。
- `.story-context/`：plan、review、repair packet、quality packet、运行上下文。
- `.story-runs/`：SWE-bench run workspace、manifest、predictions。
- `.story/`：项目级 profile、PRD task 或未来配置入口。

这对业务项目不友好：目录分散、`.gitignore` 维护成本高、用户难以判断哪些文件可删、哪些文件是协议输入。长期形态应收敛为项目根目录只暴露一个隐藏目录：

```text
.story/
  done/
  context/
  runs/
  profiles/
  prompts/
  tmp/
  logs/
```

建议路径映射：

```text
.story-done/{story_key}/{stage}.json
-> .story/done/{story_key}/{stage}.json

.story-context/{story_key}/...
-> .story/context/{story_key}/...

.story-runs/swebench/{run_id}/...
-> .story/runs/swebench/{run_id}/...
```

迁移采用硬切：所有运行时读写统一改到 `.story/`。旧目录只作为人工排查历史数据使用，不再作为 graph、watchdog、TUI、benchmark 的协议入口。

## 抽象方案

### 1. Stage Output Validator

新增一个小型验证模块，例如：

```text
src/story_lifecycle/orchestrator/validation.py
```

核心接口：

```python
@dataclass
class ValidationResult:
    ok: bool
    reason: str = ""
    details: dict = field(default_factory=dict)


def validate_stage_outputs(state: StoryState) -> ValidationResult:
    ...
```

职责：

- 校验 profile `expected_outputs`。
- 理解 stage-scoped synthetic：只允许当前 stage 的 synthetic 影响当前 stage。
- 调用 profile-specific artifact validation，例如 SWE-bench finalize patch gate。
- 返回结构化失败原因，而不是直接改 DB 或推进状态。

`advance_node()` 只消费结果：

```python
result = validate_stage_outputs(state)
if not result.ok:
    state["last_error"] = result.reason
    log_node_error(...)
    return state
```

这样 Zellij 和 headless 都走同一套完成语义。

### 2. Artifact Extractor

新增或抽出共享 patch 提取逻辑，例如：

```text
src/story_lifecycle/benchmarks/artifacts.py
```

核心接口：

```python
@dataclass
class PatchExtractionResult:
    patch: str
    source: str
    reason: str = ""


def extract_model_patch(workspace: Path, story_key: str, context: dict) -> PatchExtractionResult:
    ...
```

提取优先级保持当前行为：

1. `.story/done/{story_key}/finalize.json` 中的 `model_patch`。
2. `workspace/final.patch`。
3. `git diff`。
4. `git diff {base_commit}`，用于 agent 已提交改动的情况。
5. 空结果。

`advance_node` 的 finalize gate 和 `export_predictions()` 必须共用这一个 extractor，避免一个判断有 patch、另一个导出为空。

### 3. Execution Result Normalizer

headless 的 `_synth_done_file()` 当前在 `BaseTool` 内部。短期可以保留，但建议把“stdout 到 done 数据”的解析抽成纯函数：

```python
def normalize_headless_stdout(stdout: str) -> dict:
    ...
```

职责：

- 从 stdout 解析 fenced JSON 或完整 JSON。
- 解析失败时只返回 minimal synthetic data，例如 `{"output": "...", "synthetic": true}`。
- 不写文件，不改 state。
- 不扫描 workspace，不自动发现 `docs/design*.md`，不补 `spec_path`。

`BaseTool._synth_done_file()` 只负责调用 normalizer 并写 done 文件。

这不会反哺 Zellij 的执行方式，但会让 headless 的合成逻辑可测试、可审计。隐式文件发现属于验收层职责：如果 design 阶段缺 `spec_path`，`validate_stage_outputs()` 可以在明确规则下检查 `.story/context` 或 `docs/` 中是否存在可接受的设计产物，并把补全来源写入 validation details。Normalizer 不应该把“猜到的文件”混入 done 事实。

### 4. Workspace Path Registry

新增统一 workspace path helper，例如：

```text
src/story_lifecycle/orchestrator/paths.py
```

核心接口：

```python
def story_dir(workspace: str | Path) -> Path:
    return Path(workspace) / ".story"


def stage_done_file(workspace: str | Path, story_key: str, stage: str) -> Path:
    ...


def story_context_dir(workspace: str | Path, story_key: str) -> Path:
    ...


def swebench_run_dir(workspace_root: str | Path, run_id: str) -> Path:
    ...
```

职责：

- 所有读写统一落到 `.story/` 下。
- 不提供旧路径 fallback，避免新旧协议并存导致状态判断不一致。
- 所有 graph、TUI、watchdog、benchmark、prompt render 不再手拼 `.story-done` / `.story-context` / `.story-runs`。
- 为 consumed done snapshot 提供稳定位置：`.story/context/{story_key}/done/{stage}.json`。

这个抽象和 validation/artifact 抽象是同一类问题：它们都把“协议事实”从执行方式里抽出来，成为 headless 与 Zellij 共享的基础设施。

## 反哺到 Zellij 的改动

### 已经反哺

1. `advance_node` 里的 finalize patch gate 对 headless 和 Zellij 都生效。
2. stage-scoped synthetic 避免跨阶段污染，任何执行方式消费 synthetic done 都不会影响其他阶段。
3. exporter 的 patch fallback 对 headless 和 Zellij 都生效。
4. empty patch 不再静默成功，会写 manifest failure。

### 还应反哺

1. `validate_stage_outputs()` 统一完成判定。
2. `extract_model_patch()` 统一 patch 判断和导出。
3. validation result 写入结构化日志，便于 TUI/Zellij 用户看到“为什么不能 advance”。
4. 保留 done 消费后的调试材料，例如将 consumed done 复制到 `.story/context/{story_key}/done/{stage}.json`，避免 `poll_completion_node` 删除后难以排查。
5. 用 workspace path registry 统一 `.story-done`、`.story-context`、`.story-runs`，一次性硬切到单一 `.story/`。

## 数据流设计

### Headless

```text
execute_stage_node
-> BaseTool._launch_in_session
-> _run_headless
-> claude -p via subprocess
-> normalize_headless_stdout
-> write .story/done/{story_key}/{stage}.json
-> poll_completion_node
-> validate_stage_outputs
-> advance/router
```

### Zellij

```text
execute_stage_node
-> BaseTool._launch_in_session
-> existing session injection OR foreground zellij request
-> agent writes .story/done/{story_key}/{stage}.json
-> poll_completion_node
-> validate_stage_outputs
-> advance/router
```

两条链路只在执行方式不同，进入 `poll_completion_node` 后应完全共享。

所有 prompt 和执行协议都只暴露 `.story/done/{story_key}/{stage}.json`。旧 `.story-done` 不再出现在新 prompt 中。

## 错误处理

### Headless CLI 非 0 退出

`_run_headless()` 保留 return code 和 stderr snippet 到 `last_error`，不合成 done。

### Headless stdout 无 JSON

允许合成 synthetic done，但 synthetic 只影响当前 stage。对于 SWE-bench finalize，即使 synthetic，也必须通过 patch gate。

### Zellij CLI 退出但无 done

继续使用 exit marker/session health 进入 error path。不要尝试从 Zellij stdout 合成 done，因为该通道不稳定。

### Done 快照

`poll_completion_node()` 只有在 done JSON 解析成功后，才复制快照到 `.story/context/{story_key}/done/{stage}.json`，并且复制必须发生在删除源 done 文件之前。

如果解析失败，不复制到成功快照区，也不删除源文件。应将原始损坏文件移动到错误区，例如：

```text
.story/context/{story_key}/done/{stage}.malformed
```

同时设置 `last_error` 并记录 `JSONParseError`。这样用户可以直接检查损坏的原始输出，而不会把 malformed done 误认为已消费成功的阶段产物。

### Finalize 无 patch

不允许 story completed。错误应为：

```text
finalize has no model_patch and no git diff
```

并记录 validation 日志，便于 TUI 展示。

## 测试策略

### 必须补的回归测试

1. Headless 优先级：
   - `_tui_app is None`
   - `ttyd.session_alive()` 返回 true
   - adapter 支持 `headless_launch_cmd`
   - 断言走 `_run_headless()`，不调用 `send_keys()`。

2. Stage-scoped synthetic：
   - design done 包含 `synthetic: true`
   - implement 缺 expected output
   - 断言 implement 不因为 `_synthetic_design` 跳过校验。

3. SWE-bench finalize hard gate：
   - profile 为 `swebench`
   - stage 为 `finalize`
   - `_synthetic_finalize=True`
   - 无 `model_patch` 且无 git diff
   - 断言 `advance_node()` 设置 `last_error`，story 不 completed。

4. Patch extractor 一致性：
   - `extract_model_patch()` 返回 patch 时，finalize gate 通过，export 输出同一份 patch。
   - 返回空 patch 时，finalize gate 阻断，export 标 `export_failed / empty_patch`。

### 可选测试

1. Agent 已 commit 改动时，`git diff base_commit` 能提取 patch。
2. done 文件消费后复制到 `.story/context`，便于调试。
3. Zellij exit marker 无 done 时进入错误路径，不误判 completed。

## 落地步骤

### P0.5：小抽象，不改行为

1. 新增 `ValidationResult` 和 `validate_stage_outputs()`。
2. 将 `advance_node()` 中 expected_outputs 和 SWE-bench finalize gate 挪进去。
3. 新增 `PatchExtractionResult` 和 `extract_model_patch()`。
4. 让 `advance_node()` 和 `export_predictions()` 共用 extractor。
5. 新增 workspace path registry，并一次性替换所有运行时手写路径为 `.story/` 新路径。
6. 补齐上述 4 个必须回归测试。

### P1：提升可观测性

1. 所有运行时读写统一落到 `.story/`：
   - done 写入 `.story/done/`
   - context 写入 `.story/context/`
   - runs 写入 `.story/runs/`
2. `poll_completion_node()` 在解析成功后、删除源 done 前复制快照到 `.story/context/{story_key}/done/{stage}.json`。
   - 解析失败时不写成功快照。
   - 原始损坏 done 移入 `.story/context/{story_key}/done/{stage}.malformed`。
   - malformed 文件保留原始内容，供人工排查。
3. 提供 doctor 交互式清理旧目录：

```text
story doctor paths
```

   doctor 必须先展示旧目录空间占用，只在用户明确输入 `y` 后移动旧目录内容；移动成功后，再二次确认是否删除旧目录。

4. validation failure 写结构化 event：
   - story_key
   - stage
   - validator
   - reason
   - details
5. TUI 展示最近 validation failure。

### P1.5：隐藏目录收敛完成

1. 更新内置 prompt，将 `.story-done/{story_key}/{stage}.json` 改为 `.story/done/{story_key}/{stage}.json`。
2. 更新 docs 和 examples。
3. 新建项目时只生成 `.story/`。
4. `.gitignore` 推荐项只包含 `.story/`。
5. 移除旧路径运行时读取逻辑。

### P2：profile 化 artifact rules

将 SWE-bench finalize patch gate 从硬编码条件：

```python
stage == "finalize" and profile == "swebench"
```

提升为 profile 配置：

```yaml
artifact_gates:
  finalize:
    require_model_patch: true
    allow_git_diff_fallback: true
```

这样未来其他 benchmark 或 release profile 也能复用。

## 决策

1. 共享层只承载协议和验收：done parsing、expected outputs、artifact validation、patch extraction。
2. 执行层保持分离：headless 用 subprocess/stdin/stdout，Zellij 用 foreground handoff/session/exit marker。
3. Synthetic 是执行产物 metadata，不是 story 全局状态。
4. SWE-bench 的 `completed` 必须意味着“有可导出的 patch”，不能只表示 graph 跑到了 END。
5. Exporter 可以做最后防线，但不能替代 finalize gate。
6. 项目 workspace 内只应长期保留一个 Story Lifecycle 隐藏目录：`.story/`。
7. 路径迁移硬切到新目录，旧目录不参与运行时协议。

## 已决策问题

1. Consumed done 快照保留原始 JSON 全量内容。
   - 可以额外保存 hash 作为索引。
   - hash 不能替代原始内容。
   - 原因：agent 可能在 done JSON 中写入非标准字段，例如 `summary`、`notes`、调试信息。丢弃原始字段会影响排查。

2. Artifact gate 在 P0.5 先硬编码服务 SWE-bench，但接口预留 profile 化入参。
   - `validate_stage_outputs(state, profile_config=None)` 可以先用 `if profile == "swebench"` 实现。
   - P2 再把规则外移到 profile YAML。
   - 原因：过早配置化会同时引入 YAML schema 和执行逻辑设计，拖慢 P0.5。

3. `final.patch` 不强制写出，继续允许 git diff fallback，且 fallback 优先级最低。
   - 优先级：done `model_patch` -> `final.patch` -> `git diff` -> `git diff base_commit`。
   - 原因：headless 场景中 agent 可能已经执行 `git commit -a`，工作区干净，只能依赖 `git diff base_commit` 还原 patch。

4. `export_failed` 必须同步回 DB story，至少更新 `last_error`。
   - 只更新 run manifest 会导致 TUI 仍显示 Story `completed`，误导用户。
   - 状态可以选择 `failed:export_failed` 或 `completed_with_errors`，P0.5 至少要写 `last_error="export_failed: empty_patch"`。

5. SWE-bench 必须支持显式 workspace root，默认不应在业务代码目录下生成 `.story/runs`。
   - 大型 benchmark run 应允许 `--workspace-root /tmp/swebench-runs` 或其他外部路径。
   - 在显式 workspace root 下，目录结构仍使用 `.story/runs`，例如 `/tmp/swebench-runs/.story/runs/{run_id}`。
   - 原因：SWE-bench 会 checkout 多个大型 repo，放入业务项目 `.story/runs` 会污染 IDE 索引并快速占用磁盘。

6. `story doctor paths` 提供交互式清理，但必须保守。
   - 扫描并列出旧目录占用空间。
   - 示例提示：`Found legacy .story-done (120MB). Move into .story/done? [y/N]`
   - 只有用户明确输入 `y` 才执行移动。
   - 移动成功后再次确认是否删除旧目录。
   - 绝不自动删除。

## 推荐方案

建议先做 P0.5。

原因：

- 改动小，不影响 LangGraph 拓扑。
- 能把已经证明有效的 headless 修复正式沉淀到共享验收层。
- 能降低 `nodes.py` 和 `benchmarks/swebench.py` 的重复判断。
- 能先建立 path registry，后续收敛隐藏目录时不需要全局搜索替换。
- 能为后续 Zellij 可观测性和 profile 化 artifact gate 留出清晰接口。
