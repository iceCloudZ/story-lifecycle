> ⚠️ **历史快照（归档于 2026-07）**：描述的架构可能已被后续演进取代。当前架构见 [../../ARCHITECTURE.md](../../ARCHITECTURE.md)。本文件保留作决策记录（ADR），正文未修改。

---

# SWE-bench Runner Design

Date: 2026-05-25

## Background

Story Lifecycle Manager 的核心价值不是“直接调用一个 coding agent”，而是把 AI 软件交付过程拆成可编排、可观察、可恢复、可人工干预的多阶段工作流。当前系统已经有这些基础能力：

- Story 作为任务单位，落库到 SQLite。
- Profile 定义阶段序列，例如 `design -> implement -> test`。
- Orchestrator 通过 LangGraph 推进阶段。
- AI CLI 通过 adapter 和 terminal/session 执行。
- 阶段完成通过 `.story-done/{story_key}/{stage}.json` 握手。
- Review gate、repair packet、epoch/run guard、workspace lock 等机制逐步解决卡死、重复执行和不可见的问题。

SWE-bench 可以反过来成为这个项目的工程压力测试场。它包含真实 GitHub issue、真实仓库、真实测试和真实 patch 判分，天然会暴露 agent 工作流的脆弱点：

- checkout、依赖、测试命令不稳定；
- issue 上下文不完整；
- repo 搜索成本高；
- agent 没生成 patch 或生成无效 patch；
- review gate 误拦截或漏拦截；
- retry/resume/force-stop 产生重复执行；
- server 重启后 checkpoint/epoch 恢复失败；
- 多 instance 并发时 workspace lock 失效；
- 最终结果无法解释失败原因。

因此，SWE-bench runner 不应该只是“刷 benchmark 的脚本”。它应该成为 Story Lifecycle 的 benchmark adapter，用标准化任务输入和官方 evaluator 来持续验证 agent 工作流的稳定性。

## Problem Statement

用户希望能够一行 CLI 跑 SWE-bench：

```bash
story swebench run --dataset verified --limit 50 --agent claude --evaluate
```

但一行命令背后需要解决几类问题：

1. SWE-bench instance 如何映射成 Story。
2. 每个 instance 的 repo、commit、problem statement、test patch、hints 如何进入 prompt/context。
3. 工作目录如何隔离，避免多个 instance 互相污染。
4. Story Lifecycle 如何生成最终 `model_patch`。
5. 如何导出官方 SWE-bench prediction file。
6. 如何调用官方 harness 评估，而不是自己发明判分逻辑。
7. 如何记录 token、耗时、轮次、失败原因，用于改进 agent 健壮性。
8. 如何支持 smoke/regression/leaderboard 三种预算档位。

## Goals

- 提供一行命令运行 SWE-bench 子集。
- 将 SWE-bench instance 映射为普通 Story，让现有 orchestrator、TUI、review gate、resume 机制继续生效。
- P0 就由确定性脚本 checkout 正确 repo 和 base commit，不让 agent 负责机械 Git 环境准备。
- P0 就引入极简 finalize stage 或等价 finalize step，强制提取干净 patch。
- 生成官方兼容的 `predictions.jsonl`。
- 可选调用官方 `swebench.harness.run_evaluation` 做判分。
- 每次 run 生成稳定目录结构，方便断点恢复和失败分析。
- 把每题 token、耗时、轮次、失败标签纳入 run summary。
- 先支持本地 JSONL 和手动指定 dataset，避免 P0 过度依赖网络下载。

## Non-Goals

- 不在 P0 自己实现 SWE-bench evaluator。
- 不在 P0 做排行榜、云执行、分布式调度。
- 不在 P0 支持所有 SWE-bench 变体。
- 不在 P0 自动优化依赖安装策略。
- 不把 benchmark 逻辑硬编码进 LangGraph 主状态机。
- 不要求第一次实现就全量跑 Lite/Verified。
- 不让 agent 自己负责 `git clone`、`git checkout` 等确定性准备动作。

## User Experience

### One-Line Smoke Run

```bash
story swebench run \
  --dataset lite \
  --limit 5 \
  --agent claude \
  --budget smoke \
  --evaluate
```

语义：

- 加载 SWE-bench Lite 的前 5 个 instance。
- 为每个 instance 创建一个 Story。
- 使用 `swebench` profile 执行。
- 生成 `runs/swebench/<run_id>/predictions.jsonl`。
- 调官方 harness 评估。
- 输出 run summary。

### Local JSONL Run

```bash
story swebench run \
  --instances ./swebench-lite-5.jsonl \
  --run-id smoke-001 \
  --workspace-root ./.story-runs/swebench \
  --agent claude \
  --no-evaluate
```

这个模式用于本地开发和 CI smoke test。它不依赖 Hugging Face dataset 下载，也不强制 Docker evaluator 可用。

### Split Commands

一行命令需要保留，但内部能力应该拆成可恢复的子命令：

```bash
story swebench prepare --instances ./lite.jsonl --run-id r1
story swebench solve --run-id r1 --agent claude
story swebench export --run-id r1
story swebench eval --run-id r1
story swebench summarize --run-id r1
```

`run` 是组合命令：

```text
run = prepare -> solve -> export -> optional eval -> summarize
```

在 `--mode benchmark` 下，`run` 必须保证不会因为人工确认而无限阻塞。任何 stage/gate 如果进入 `wait_confirm`，都要按 `--gate-policy` 转换为自动结果。

## Architecture

### Component Overview

```text
CLI: story swebench ...
  -> benchmarks.swebench.loader
  -> benchmarks.swebench.run_store
  -> benchmarks.swebench.story_adapter
  -> orchestrator.service.create_and_start_story
  -> orchestrator.graph.start_story_async
  -> benchmarks.swebench.predictions
  -> optional official SWE-bench harness
```

新增模块建议：

```text
src/story_lifecycle/benchmarks/
  __init__.py
  swebench.py
  swebench_harness.py

src/story_lifecycle/cli/
  swebench.py

profiles/
  swebench.yaml
```

### Responsibilities

`benchmarks.swebench`

- 定义 `SWEbenchInstance`。
- 从本地 JSONL 或 dataset 加载 instance。
- 创建 run directory。
- 生成 run manifest。
- 把 instance 转成 Story。
- 导出 predictions。
- 写 summary。

`cli.swebench`

- 定义 `story swebench` 命令组。
- 参数解析、用户输出、错误提示。
- 调用 benchmark service，不直接操作 DB 细节。

`swebench_harness`

- 只负责调用官方 evaluator。
- 检查依赖是否存在。
- 捕获 stdout/stderr、退出码和结果路径。
- 不实现 patch 判分。

`profiles/swebench.yaml`

- 定义 benchmark 专用阶段。
- 控制 retry/review/budget。
- 保持中文 prompt 风格，但 context 字段使用 SWE-bench 官方字段名。

## Data Model

### SWEbenchInstance

```python
@dataclass
class SWEbenchInstance:
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    hints_text: str = ""
    test_patch: str = ""
    version: str = ""
    FAIL_TO_PASS: list[str] | None = None
    PASS_TO_PASS: list[str] | None = None
```

字段尽量保留官方命名，减少转换成本。

### Run Manifest

位置：

```text
.story-runs/swebench/{run_id}/manifest.json
```

结构：

```json
{
  "run_id": "smoke-001",
  "dataset": "lite",
  "split": "test",
  "agent": "claude",
  "profile": "swebench",
  "budget": "smoke",
  "created_at": "2026-05-25T10:00:00Z",
  "instances": [
    {
      "instance_id": "django__django-12345",
      "story_key": "django__django-12345",
      "repo": "django/django",
      "base_commit": "abc123",
      "workspace": ".story-runs/swebench/smoke-001/django__django-12345",
      "status": "prepared"
    }
  ]
}
```

### Prediction File

位置：

```text
.story-runs/swebench/{run_id}/predictions.jsonl
```

每行：

```json
{
  "instance_id": "django__django-12345",
  "model_name_or_path": "story-lifecycle-claude",
  "model_patch": "diff --git ..."
}
```

`model_patch` 来源优先级：

1. `.story-done/{story_key}/finalize.json` 中的 `model_patch`
2. `workspace/final.patch`
3. `git diff` against base commit

P0 必须有 finalize 约束，不能把无约束的 `git diff` 直接当成最终结果。`git diff` 只能作为 fallback，且 exporter 必须做 patch noise inspection。

Patch noise inspection 至少检查：

```text
modified_file_count
added_file_count
deleted_file_count
binary_file_count
diff_size_bytes
```

如果发现明显噪音，summary 必须打标签：

```text
patch_too_noisy
```

P0 的 noise rule 可以很保守：

```text
modified_file_count > 20 -> patch_too_noisy
diff_size_bytes > 1MB -> patch_too_noisy
binary_file_count > 0 -> patch_too_noisy
```

这些规则不直接阻止导出 prediction，但必须在 summary 中可见，避免把脏 diff 当作正常 agent 失败。

### Run Summary

位置：

```text
.story-runs/swebench/{run_id}/summary.json
```

结构：

```json
{
  "run_id": "smoke-001",
  "total": 5,
  "prepared": 5,
  "completed": 4,
  "predictions": 4,
  "resolved": 2,
  "failed": 3,
  "token_total": 2500000,
  "duration_seconds": 7200,
  "failures": {
    "test_failure": 2,
    "agent_no_patch": 1
  }
}
```

## Story Mapping

每个 SWE-bench instance 创建一个 Story：

```text
story_key = instance_id
title = first line / compressed problem_statement
profile = swebench
workspace = {workspace_root}/{run_id}/{instance_id}
```

`context_json` 写入：

```json
{
  "benchmark": "swebench",
  "run_id": "smoke-001",
  "instance_id": "django__django-12345",
  "repo": "django/django",
  "base_commit": "abc123",
  "problem_statement": "...",
  "hints_text": "...",
  "test_patch": "...",
  "fail_to_pass": [],
  "pass_to_pass": [],
  "prediction_path": ".story-runs/swebench/smoke-001/predictions.jsonl"
}
```

同时写入一个 PRD 文件，方便现有 prompt 渲染链路复用：

```text
{workspace}/prd/{instance_id}.md
```

内容：

```markdown
# SWE-bench Instance: django__django-12345

## Repository

django/django

## Base Commit

abc123

## Problem Statement

...

## Hints

...

## Test Patch

...
```

## Profile Design

`profiles/swebench.yaml` 建议：

```yaml
name: swebench
cli: claude
adversarial:
  enabled: true
  code_loop:
    enabled: true
    max_rounds: 2
stages:
  design:
    prompt: design
    review: true
    expected_outputs:
      - root_cause
      - target_files
      - test_strategy
  implement:
    prompt: implement
    review: true
    expected_outputs:
      - patch_summary
  test:
    prompt: test
    review: false
    expected_outputs:
      - test_command
      - test_result
  finalize:
    prompt: swebench_finalize
    review: false
    expected_outputs:
      - model_patch
```

P0 必须引入极简 `finalize`。它不需要复杂 review，也不需要额外 evaluator，只做一件事：要求 agent 明确确认最终 patch 边界，并写出可用于 SWE-bench prediction 的 `model_patch` 或 `final.patch`。

`swebench_finalize` prompt 的核心约束：

```text
只包含修复核心逻辑所需的 diff。
不要包含日志、临时文件、本地配置、格式化无关改动、依赖缓存或测试产物。
输出前检查 git diff --stat。
如果 diff 包含无关文件，必须先清理。
```

这样 P0 的 patch 提取不是完全依赖 exporter 猜测，而是形成 agent 明确承诺的阶段产物。

## Execution Flow

### Prepare

```text
load instances
  -> create run_dir
  -> deterministic git clone/fetch
  -> checkout base_commit
  -> verify clean worktree
  -> write PRD markdown
  -> create Story
  -> update context_json
  -> write manifest
```

P0 必须由脚本完成 checkout。原因是 repo clone、commit checkout、clean worktree 校验都是确定性工程动作，不应该消耗 agent token，也不应该让 CLI agent 在网络、权限、命令拼写和工作目录切换中试错。

Prepare 的 checkout contract：

```text
workspace does not exist:
  ensure repo cache exists at ~/.cache/story-lifecycle/swebench/repos/{repo_slug}
  git clone --reference-if-able {cache_repo} https://github.com/{repo}.git {workspace}
  git checkout {base_commit}

workspace exists:
  verify it is the same repo
  fetch if needed
  git checkout {base_commit}
  git clean/reset only inside instance workspace
  verify git status --porcelain is empty
```

Clone cache 是 P0 的强建议实现。SWE-bench 经常一个 repo 对应几十个 instance，如果每个 instance 都从 GitHub 完整 clone，会显著拖慢 prepare，并增加网络失败和 rate limit 风险。

缓存策略：

```text
cache root: ~/.cache/story-lifecycle/swebench/repos/{owner}__{name}

cache missing:
  git clone --mirror https://github.com/{repo}.git {cache_repo}

cache exists:
  git remote update --prune

instance workspace:
  git clone --reference-if-able {cache_repo} https://github.com/{repo}.git {workspace}
  git checkout {base_commit}
```

如果 cache 更新失败，prepare 可以回退到普通 clone，但必须在 manifest 中记录 `checkout_cache_miss` 或 `checkout_cache_failed`，便于分析网络成本。

Cache 更新必须加进程级文件锁。原因是 P1 支持并发 solve/prepare 后，或者用户手动开多个终端跑同一个 run 时，多个 prepare 进程可能同时发现 cache 不存在并执行 `git clone --mirror`，导致目录写入冲突。

推荐实现：

```python
from filelock import FileLock

cache_lock_path = f"{cache_repo}.lock"
with FileLock(cache_lock_path, timeout=300):
    if not cache_repo.exists():
        git_clone_mirror(repo_url, cache_repo)
    else:
        git_remote_update(cache_repo)

git_clone_reference_if_able(cache_repo, repo_url, workspace)
```

锁只保护 shared cache 的创建和更新，不保护每个 instance workspace 的 checkout。instance workspace 是 per-run/per-instance 独立目录，仍由 workspace lock 和 run manifest 管理。

失败时不创建 active story，manifest 记录：

```json
{
  "instance_id": "django__django-12345",
  "status": "checkout_failed",
  "failure_type": "checkout_failure",
  "error": "..."
}
```

任何 destructive git 操作只能作用于 `{workspace_root}/{run_id}/{instance_id}` 目录内，不能对用户当前项目工作区执行。

### Solve

```text
for each manifest instance:
  start_story_async(story_key)
```

P0 默认串行或低并发。并发时必须依赖现有 workspace lock，但每个 instance workspace 独立，因此不会互相阻塞。

每个 Story 必须启动全新的 CLI process/session：

```text
one instance -> one story -> one fresh CLI process/session
```

严禁多个 SWE-bench instance 复用同一个 Claude/Codex CLI 进程。否则前一个 repo、issue、review 结论和失败日志可能污染后一个 instance 的上下文。TUI 可以 attach 到对应 session，但 session 生命周期必须绑定 story/run token。

### Export

```text
for each manifest instance:
  patch = read final.patch or git diff
  inspect patch noise
  write predictions.jsonl row
```

没有 patch 的 instance 仍写空 patch，或者在 summary 中标记 `agent_no_patch`。建议 P0 写空 patch，确保 evaluator 输入完整，同时 summary 中明确记录。

Finalize 和 noise inspection 是两层防守：

```text
finalize stage:
  防守请求。要求 agent 主动清理 diff，只保留核心修复。

exporter noise inspection:
  防守验证。不信任 agent 一定做对，独立检查最终 patch 是否过脏。
```

如果 exporter 触发 `patch_too_noisy`，它不只是 evaluator 风险，也是一条 agent 质量反馈：说明 finalize prompt 约束不够、agent 对 patch 边界理解失败，或实现阶段产生了幻觉/无关改动。该标签应进入 quality flywheel，后续用于改进 finalize prompt 和 review checklist。

### Evaluate

调用官方 SWE-bench harness：

```bash
python -m swebench.harness.run_evaluation \
  --dataset_name SWE-bench/SWE-bench_Verified \
  --predictions_path .story-runs/swebench/{run_id}/predictions.jsonl \
  --run_id {run_id}
```

具体参数跟随当前安装的 `swebench` 包版本，不在代码中假设过多。CLI 应支持：

```bash
story swebench eval --run-id r1 --extra-arg "--max_workers=4"
```

## Budget Design

预算不是 evaluator 的概念，而是 Story Lifecycle 控制 agent 行为的策略。

```text
smoke:
  limit: 5-10 instances
  max_rounds: 1
  max_tokens_per_instance: 200k

standard:
  limit: 50 instances
  max_rounds: 2-3
  max_tokens_per_instance: 800k

leaderboard:
  full dataset
  max_rounds: 3-5
  max_tokens_per_instance: 1.5M-2M
```

Budget 必须在 prepare 阶段落到 run manifest 和 story context 中，而不是只停留在 CLI 参数：

```json
{
  "budget": {
    "name": "smoke",
    "max_rounds": 1,
    "max_review_rounds": 1,
    "max_tokens_per_instance": 200000,
    "timeout_seconds": 1800
  }
}
```

Profile 执行时读取该 budget，并覆盖 `profiles/swebench.yaml` 中的默认值：

```text
budget.smoke.max_rounds
  -> adversarial.code_loop.max_rounds
  -> review gate retry limit
  -> executor timeout/token guard
```

如果当前 executor 还没有 token hard stop，P0 至少要记录 token 估算和 `budget_exceeded` 标签；P1 再接入强制拦截。

每个 instance summary 记录：

```json
{
  "instance_id": "django__django-12345",
  "tokens": {
    "prompt": 420000,
    "completion": 80000,
    "total": 500000
  },
  "rounds": 3,
  "review_rounds": 2,
  "duration_seconds": 1800,
  "failure_type": "test_failure"
}
```

## Failure Taxonomy

SWE-bench runner 的长期价值在于失败归因。建议固定失败标签：

```text
load_failure
checkout_failure
dependency_failure
agent_no_patch
patch_apply_failure
test_failure
review_false_positive
review_false_negative
state_machine_stuck
resume_failure
timeout
evaluator_failure
review_gate_blocked
human_confirmation_required
patch_too_noisy
budget_exceeded
unknown
```

每次 run 的 summary 都按这些标签聚合。这样它能成为 agent regression suite，而不是只看最终 pass rate。

## CLI Design

### Command Group

```bash
story swebench --help
```

子命令：

```text
prepare
solve
export
eval
summarize
run
```

### Run Command

```bash
story swebench run \
  --dataset lite \
  --split test \
  --limit 5 \
  --run-id smoke-001 \
  --workspace-root .story-runs/swebench \
  --agent claude \
  --profile swebench \
  --budget smoke \
  --mode benchmark \
  --evaluate
```

参数：

```text
--instances PATH       local JSONL input; overrides dataset download
--dataset NAME         lite|verified|full or Hugging Face dataset name
--split NAME           default test
--limit N              max instances
--run-id ID            stable run id
--workspace-root PATH  run root
--agent NAME           claude|codex|shell adapter name
--profile NAME         default swebench
--budget NAME          smoke|standard|leaderboard
--mode NAME            benchmark|development
--gate-policy NAME     benchmark mode default: auto_fail; development mode default: wait_confirm
--start / --no-start   whether to start stories after prepare
--evaluate / --no-evaluate
```

## Execution Mode

SWE-bench 跑分和真实开发的交互语义不同，必须显式区分。

### development mode

真实开发默认是 human-in-the-loop：

```text
--mode development
--gate-policy wait_confirm
```

语义：

- review gate 可以进入 `wait_confirm`。
- TUI 展示 gate report 和 allowed actions。
- 用户可以进入 CLI session 查看过程。
- 高风险动作需要人工确认。
- 适合真实业务需求、架构设计、复杂实现。

### benchmark mode

跑分默认是 headless：

```text
--mode benchmark
--gate-policy auto_fail
```

语义：

- 不允许无限等待人工确认。
- 如果 gate 原本要 `wait_confirm`，runner 将其转成确定性结果。
- 默认策略是 `auto_fail`：该 instance 标记失败，failure_type 记录具体原因。
- 可选策略是 `auto_accept_risk`，但只能显式开启，不作为默认。

可选 gate policy：

```text
wait_confirm       development default; block for human input
auto_fail          benchmark default; no human wait, mark instance failed
auto_retry         retry until budget exhausted, then auto_fail
auto_accept_risk   force advance; only for experiments, must be explicit
```

Benchmark mode 的核心目标是可复现。任何需要人拍板的状态都必须被转换成结构化结果，而不是让 run 卡住。

`auto_fail` 的终态必须保留具体原因，不要只写模糊的 failed：

```text
story.status = failed
story.last_error = "review_gate_blocked: <gate reason>"
run_instance.status = failed
run_instance.failure_type = review_gate_blocked
```

如果后续 DB 状态枚举允许更细状态，可以升级为：

```text
story.status = failed:review_gate_blocked
```

但 P0 不要求修改主 DB schema。P0 至少必须在 `last_error`、event_log、run summary 中保留 `review_gate_blocked`，以区分：

```text
agent_no_patch
test_failure
review_gate_blocked
state_machine_stuck
```

Run summary 必须记录 gate policy：

```json
{
  "mode": "benchmark",
  "gate_policy": "auto_fail",
  "blocked_gate_count": 3,
  "failures": {
    "review_gate_blocked": 3
  }
}
```

## TUI Integration

P0 不需要新增 TUI 页面。SWE-bench instance 本质是 Story，因此现有 TUI 已可展示：

- story key = instance id
- current stage
- status
- terminal entry
- review gate
- logs

后续可以增加 run filter：

```text
filter: benchmark=swebench run_id=smoke-001
```

## Version Plan

### 0.5.0 P0: Local Smoke Runner

- `story swebench run --instances local.jsonl --limit N --no-evaluate`
- 创建 run manifest。
- 由脚本 checkout repo，并切到 `base_commit`。
- 使用 clone cache 降低重复 repo 的准备成本。
- 校验 worktree clean 后再创建 active story。
- 创建 Story。
- 写 PRD/context。
- 支持 `--no-start`。
- 引入极简 `finalize` stage 或等价 finalize step。
- 导出 `predictions.jsonl`。
- exporter 执行 patch noise inspection，记录 `patch_too_noisy`。
- 支持 `--mode benchmark|development` 和 `--gate-policy`。
- 不要求官方 harness 必装。

### 0.5.1 P1: Official Harness Integration

- 支持 `--evaluate`。
- 检查 Docker 和 `swebench` 包。
- 捕获 evaluator 输出。
- 生成 `summary.json`。
- 强化 token/budget hard stop。

### 0.5.2 P2: Regression Suite

- 支持固定 subset 文件。
- 失败标签聚合。
- token/cost/rounds 汇总。
- 支持 resume/export/eval 分步重跑。

### 0.6.0 P3: Visible Benchmark Workflow

- TUI run filter。
- 每个 instance 的 review report 链接。
- benchmark dashboard。
- 对比两个 run 的 solve rate、成本、失败类型。

## Test Plan

P0 单测：

- 本地 JSONL 加载。
- `prepare` 生成 manifest。
- instance -> Story 映射写入 context。
- `export` 生成官方格式 `predictions.jsonl`。
- `run --no-evaluate --no-start` 能一行跑完 prepare/export。

P1 单测：

- harness 命令构造正确。
- evaluator 缺失时给出明确错误。
- evaluator 非 0 退出码写入 `evaluator_failure`。

E2E smoke：

```bash
story swebench run --instances tests/fixtures/swebench-one.jsonl --no-start --no-evaluate
```

## Open Decisions

1. P0 是否必须 checkout repo，还是只创建 workspace/Story/context？
   - 决定：P0 必须由脚本 checkout。确定性环境准备不能交给 agent，否则会浪费 token，并引入网络、权限、命令拼写和工作目录错误。

2. `finalize` 是否作为新 stage 引入？
   - 决定：P0 引入极简 finalize stage 或等价 finalize step，约束 agent 输出干净 patch。Exporter 的 noise inspection 作为独立验证层。

3. full dataset 加载是否直接依赖 Hugging Face `datasets`？
   - 决定：P0 只支持 local JSONL；P1 再支持 dataset name。

4. `run` 默认是否 evaluate？
   - 决定：默认 `--no-evaluate`，用户显式传 `--evaluate` 才调用 Docker/harness。

5. benchmark run 是否允许人工确认？
   - 决定：默认不允许。`--mode benchmark` 默认 `--gate-policy auto_fail`，把需要人工确认的 gate 转成结构化失败；真实开发使用 `--mode development --gate-policy wait_confirm`。

## Recommendation

先把 SWE-bench runner 做成一个很薄的 benchmark adapter，而不是一个独立 agent。P0 只保证：

```text
local JSONL -> deterministic checkout -> Story -> finalize -> noise inspection -> predictions.jsonl
```

这一步完成后，Story Lifecycle 的现有状态机、TUI、review gate、epoch/resume 都可以自然参与 benchmark。后续再逐步接入官方 harness、token budget、失败归因和 run dashboard。
