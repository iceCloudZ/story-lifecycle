# Headless E2E 测试工具设计

## 背景

Story Lifecycle Manager 的核心价值在于把一个 story 按 profile 自动流转：

```text
design -> implement -> test
```

当前真实 E2E 需要启动服务、打开 TUI、进入终端、等待 AI CLI 执行并写入 `.story-done/{story_key}/{stage}.json`。这条链路能验证真实环境，但手动成本高、耗时长、依赖 LLM key、Claude CLI、tmux/ttyd、平台差异和网络状态，不适合作为每次开发后的默认回归。

现有自动化测试已经覆盖了不少函数级和节点级行为：

- `pytest tests` 当前可通过，覆盖 63 个用例。
- `pytest` 裸跑当前会失败，因为会收集 `examples/calculator/tests`，而示例项目里的 `calculator.py` 是预期由 AI 生成的文件。
- 部分测试直接使用默认 `~/.story-lifecycle/story.db`，隔离性不足。
- 缺少一层“完整生命周期自动跑完”的 headless E2E。

## 目标

设计一套可重复、低成本、默认可运行的测试工具，让开发者每次改完代码后能快速验证：

1. story 创建、状态流转、stage 推进是否正确。
2. LangGraph 主链路是否能从 `design` 跑到 `completed`。
3. `.story-done` 握手协议、JSON 解析、上下文写入是否正常。
4. router、advance、retry、fail、skip、sub-story 等关键分支可以通过场景配置覆盖。
5. 测试不依赖真实 AI、tmux、ttyd、网络、用户交互或真实 home 目录数据库。

## 非目标

本设计不替代真实 E2E。真实 E2E 仍然需要少量保留，用于验证 AI CLI、终端复用、prompt 注入、ttyd 页面和跨平台环境。

本设计不要求修改业务 profile 语义，也不要求引入外部服务或新的大型测试框架。

## 方案对比

### 方案 A：继续维护手动 E2E 文档

优点是最接近真实用户路径，缺点是慢、不可重复、难以放进 CI，也无法稳定覆盖错误分支。

结论：只适合作为 smoke/manual 测试，不适合作为默认回归。

### 方案 B：Mock 每个节点做集成测试

直接 mock `plan_stage_node`、`execute_stage_node`、`poll_completion_node` 等节点，可以很快覆盖边界逻辑。

缺点是容易测到实现细节，而没有真正验证 graph 中节点之间的数据衔接、checkpoint/resume 语义和 `.story-done` 协议。

结论：现有测试已经在做这件事，应该保留，但不能作为 E2E 替代。

### 方案 C：Headless Fake Runner E2E

在测试环境中替换真实执行器，让 `execute_stage` 不启动 Claude/tmux，而是根据场景定义写入 `.story-done/{story_key}/{stage}.json`，然后继续走真实的 `poll_completion -> review -> router -> advance` 链路。

优点：

- 快：通常 1-3 秒跑完完整生命周期。
- 稳：不依赖 AI、终端、网络。
- 真：仍然使用真实 graph、真实 DB、真实 profile、真实 done-file 协议。
- 易扩展：新增 YAML/JSON 场景即可覆盖 happy path、缺字段、非法 JSON、retry、skip、sub-story。

推荐采用方案 C，并保留方案 A 的少量真实 smoke。

## 推荐架构

新增一层测试工具，核心由四部分组成：

```text
tests/
  conftest.py
  e2e/
    scenarios/
      happy_path.yaml
      missing_output.yaml
      invalid_done_json.yaml
      sub_story.yaml
    test_headless_lifecycle.py
    helpers.py
```

### 1. 隔离测试环境

每个测试使用临时目录作为 workspace 和 story home。

需要隔离的状态：

- SQLite 业务库：`story.db`
- LangGraph checkpoint：`checkpoint.db`
- workspace 下的 `.story-done/`
- workspace 下的 `.story-context/`
- workspace 下的 `.story-knowledge/`

建议在 pytest fixture 中 patch：

- `story_lifecycle.db.models.get_db_path`
- `story_lifecycle.orchestrator.graph.checkpoint_db`
- 必要时 patch `nodes.STORY_HOME`

这样测试不会读写用户真实的 `~/.story-lifecycle`。

### 2. Fake Stage Tool

实现测试专用 fake execution tool，不进入 `ttyd.create_session()`，不调用 adapter。

行为：

1. 读取当前 `story_key` 和 `current_stage`。
2. 从 scenario 中找到当前 stage 的输出。
3. 写入 `.story-done/{story_key}/{stage}.json`。
4. 增加 `execution_count`。
5. 写入 `execute` event。
6. 返回更新后的 state。

伪代码：

```python
class FakeStageTool:
    def __init__(self, scenario):
        self.scenario = scenario

    def execute(self, state, args):
        key = state["story_key"]
        stage = state["current_stage"]
        next_count = state.get("execution_count", 0) + 1
        payload = self.scenario.stage_payload(stage, execution_index=next_count)

        done_file = Path(state["workspace"]) / ".story-done" / key / f"{stage}.json"
        done_file.parent.mkdir(parents=True, exist_ok=True)
        done_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        db.log_event(
            key,
            stage,
            "execute",
            {"attempt": next_count, "tool": "fake_stage_tool"},
        )
        return {**state, "execution_count": next_count, "last_error": None}
```

测试中通过 patch `story_lifecycle.orchestrator.tools.get_tool` 返回该 fake tool。

Fake tool 可以同步写入 done file，不需要模拟真实 AI 的执行耗时。这个设计刻意让 happy path 走“execute 后 poll 立即读到 done file”的路径，以保持 L2 测试快速稳定；等待 done file 后再 resume 的 interrupt 行为由 L1 节点测试单独覆盖。

Fake tool 返回新 state 字典，不直接 mutate 传入的 `state` 对象，避免和 LangGraph 的状态追踪或后续 reducer 语义产生耦合。

### 3. Scenario DSL

用 YAML 描述 E2E 场景，降低新增用例成本。

示例：

```yaml
story_key: E2E-HAPPY
title: Headless happy path
profile: minimal

stages:
  design:
    done:
      spec_path: docs/spec.md
      complexity: S
      summary: design completed
  implement:
    done:
      implementation_summary: implemented
  test:
    done:
      tests_passed: true

expect:
  status: completed
  final_stage: test
  context:
    spec_path: docs/spec.md
    complexity: S
  events:
    - plan
    - execute
    - review
    - complete
```

异常场景可以扩展字段：

```yaml
stages:
  design:
    raw_done: "not json"

expect:
  status: blocked
  last_error_contains: "Cannot parse JSON"
```

或：

```yaml
stages:
  design:
    done:
      complexity: S

expect:
  status: blocked
  last_error_contains: "Missing expected outputs"
```

需要表达同一个 stage 多次执行时，使用 `executions` 数组按尝试次数定义输出。Fake tool 根据本次执行的 `execution_count + 1` 选择对应 payload；如果执行次数超过数组长度，默认复用最后一个元素或让 scenario loader 抛出配置错误，MVP 推荐抛错以暴露场景定义问题。

```yaml
story_key: E2E-RETRY
title: Review retry then pass
profile: minimal

stages:
  design:
    executions:
      - done:
          spec_path: docs/spec.md
          complexity: S
          summary: first draft with flaw
      - done:
          spec_path: docs/spec.md
          complexity: S
          summary: revised draft
  implement:
    done:
      implementation_summary: implemented
  test:
    done:
      tests_passed: true

reviews:
  design:
    executions:
      - quality: revise
        summary: missing edge cases
        issues:
          - type: missing_tests
            severity: high
            location: docs/spec.md
            description: Edge cases are not covered
      - quality: pass
        summary: design accepted
        issues: []

expect:
  status: completed
  retries:
    design: 1
```

### 4. E2E Runner Helper

封装一个测试 helper，负责：

1. 初始化隔离 DB。
2. 创建临时 workspace。
3. 读取 scenario。
4. 调用 `create_and_start_story()` 创建 story。
5. patch planner、tool、notify、ttyd。
6. 直接调用 `run_story(story_key)` 或 `_run_story_impl(story_key)`。
7. 从 DB 和 event log 断言最终结果。

建议默认禁用真实 planner：

- `planner.is_available()` 返回 `False`。
- 这样 `plan_stage_node` 使用 profile fallback plan。
- review 逻辑在无 planner 时自动跳过或保持轻量。

如果需要测试 review-driven retry，再单独 patch `planner.review_stage()` 返回 `revise/pass/fail`。

## 测试分层

建议把测试分成四层。

### L0：静态检查

默认命令：

```bash
ruff check src tests
```

### L1：单元和节点测试

默认命令：

```bash
pytest tests -m "not e2e_real"
```

当前短期命令可以先使用：

```bash
pytest tests
```

### L2：Headless E2E

默认随 `pytest tests` 运行，要求无网络、无 AI、无 tmux。

覆盖：

- happy path 完整流转。
- done JSON markdown 包裹解析。
- 缺少 expected output。
- 非法 done JSON。
- retry 后成功。
- skip stage。
- sub-story 创建、父 story waiting_subtasks、子 story 完成后恢复。

### L3：真实 E2E

默认不运行，通过显式参数启用：

```bash
pytest tests/e2e_real --run-real-ai
```

或脚本：

```bash
scripts/wsl-test.sh
```

这层验证：

- `story --serve` 服务启动。
- API 创建 story。
- tmux/ttyd session 创建。
- Claude CLI 启动和 prompt 注入。
- AI 写入 `.story-done` 后自动推进。

## 必要代码调整

### pytest 收集范围

在 `pyproject.toml` 中增加：

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
```

这样根目录执行 `pytest` 不会误收集 `examples/calculator/tests`。

如果需要测试示例项目，单独进入 `examples/calculator` 后执行它自己的 pytest。

### 测试 DB fixture

在 `tests/conftest.py` 增加通用 fixture：

```python
@pytest.fixture
def isolated_story_home(tmp_path, monkeypatch):
    story_home = tmp_path / "story-home"
    db_path = story_home / "story.db"
    checkpoint_path = story_home / "checkpoint.db"

    monkeypatch.setattr(db.models, "get_db_path", lambda: db_path)
    monkeypatch.setattr(graph, "checkpoint_db", checkpoint_path)
    monkeypatch.setattr(nodes, "STORY_HOME", story_home)

    db.init_db()
    return story_home
```

现有直接调用 `init_db()` 的测试可以逐步迁移到这个 fixture，避免污染真实数据库。

再增加一个 `autouse=True` 的全局状态 reset fixture，在每个测试前后都清理 graph 模块里的进程内状态。清理动作同时放在 setup 和 teardown 中，避免某个测试中途失败后污染下一个测试。

```python
@pytest.fixture(autouse=True)
def reset_graph_globals():
    graph._running_stories.clear()
    graph._workspace_locks.clear()
    graph._plan_done.clear()
    graph._terminal_opened.clear()
    yield
    graph._running_stories.clear()
    graph._workspace_locks.clear()
    graph._plan_done.clear()
    graph._terminal_opened.clear()
```

L2 E2E 默认不支持 `pytest-xdist` 并发执行。原因是测试会 patch 进程内全局函数、复用 graph 全局状态，并通过文件系统模拟 `.story-done` 协议。CI 中应串行运行 `tests/e2e`；如果未来需要并发，需要把 scenario runner 改成进程隔离或显式实例化 graph/runtime 状态。

### Graph 运行边界

优先在测试中 patch tool，而不是把 fake runner 放进生产 registry。

原因：

- 生产代码不暴露测试专用 adapter。
- 对现有用户无行为变化。
- 测试能精确控制每个 stage 输出。

如果后续需要命令行工具，例如 `story test-scenario scenario.yaml`，再考虑把 fake runner 做成内部 dev command。

## 风险与处理

### LangGraph interrupt 行为

`poll_completion_node` 在没有 done file 时会 `interrupt()`。Fake tool 必须在 `execute_stage` 阶段同步写好 done file，确保 `poll_completion_node` 不进入等待。

对于测试“等待 done file”的行为，可以单独写节点级测试，不放在完整生命周期 E2E 里。

因此，L2 Headless E2E 验证的是“正常完成后立即推进”的主路径，不验证“先 interrupt，之后由 watchdog 或用户动作 resume”的路径。checkpoint/resume 和 interrupt 相关语义应由 L1 节点测试或单独的 resume 集成测试覆盖。

### review 逻辑导致不稳定

默认 headless happy path 让 `planner.is_available()` 为 `False`，避免真实 LLM review。

需要测试 review 分支时，显式 patch `planner.is_available()` 和 `planner.review_stage()`。

### 全局线程池和运行中 story 状态

优先调用同步的 `_run_story_impl()`，避免 `start_story_async()` 的线程不确定性。

如果必须测试 API 自动启动，则 patch `start_story_async()`，先验证 API 层行为；完整异步行为放真实 E2E。

### 全局锁污染

测试开始前和结束后都清理：

- `graph._running_stories`
- `graph._workspace_locks`
- `graph._plan_done`
- `graph._terminal_opened`

该清理应放在 `autouse=True` fixture 中，不依赖单个测试主动调用 cleanup。这样即使某个测试断言失败，也能降低对后续测试的污染。

## 首批用例

建议第一批只做 5 个，先把价值跑通。

1. `happy_path.yaml`
   - `design -> implement -> test -> completed`
   - 验证 context 中有 `spec_path` 和 `complexity`
   - 验证 DB status 为 `completed`

2. `markdown_done_json.yaml`
   - done 文件内容为 markdown fenced JSON
   - 验证 `robust_json_parse()` 在完整 graph 中有效

3. `missing_expected_output.yaml`
   - design 缺少 `spec_path`
   - 验证进入 blocked 或产生明确 last_error

4. `review_retry_then_pass.yaml`
   - 第一次 review 返回 `revise`
   - 第二次返回 `pass`
   - 验证 execution_count 和 retry event
   - 使用 `stages.design.executions` 和 `reviews.design.executions` 表达同一 stage 的多次执行

5. `sub_story_wait_resume.yaml`
   - parent 被拆成 sub-story
   - 子任务 completed 后 parent 恢复 active
   - 验证 parent/child DB 关系

## CI 建议

默认 CI：

```bash
pip install -e ".[dev]"
ruff check src tests
pytest tests
```

真实 E2E 独立 job，手动触发或 nightly：

```bash
pytest tests/e2e_real --run-real-ai --reruns 2
```

真实 E2E job 需要显式检查：

- `STORY_LLM_API_KEY`
- Claude CLI
- tmux
- ttyd
- Unix/WSL 环境

缺少条件时 skip，而不是 fail。

真实 E2E 依赖大模型和终端环境，允许少量非确定性失败。CI 中建议使用 `pytest-rerunfailures` 对 L3 用例失败后自动重跑 2 次；只有连续失败才标记 job 失败。L3 失败时优先保留 server log、tmux capture、`.story-done` 内容和 graph error log，便于排障。

## 交付计划

第一阶段：测试基线修复

- 配置 `pyproject.toml` 的 `testpaths = ["tests"]`。
- 增加隔离 DB fixture。
- 保持 `pytest tests` 通过。

第二阶段：Headless E2E MVP

- 增加 scenario loader。
- 增加 `FakeStageTool`。
- 增加 happy path 用例。
- 增加非法 JSON 和缺字段用例。

第三阶段：复杂分支覆盖

- 增加 review retry 场景。
- 增加 skip/fail/resume 场景。
- 增加 sub-story 场景。

第四阶段：真实 E2E 分离

- 把真实 AI/tmux 测试放到 `tests/e2e_real`。
- 增加 `--run-real-ai` pytest option。
- 更新 `docs/e2e-test.md`，保留手动排障说明。

## Review 重点

请重点 review 以下问题：

1. Fake tool 写 `.story-done` 的方式是否足够接近真实 AI CLI 完成协议。
2. 是否应该通过 patch `get_tool()` 注入 fake tool，还是应该新增正式的 dev/test adapter。
3. 是否需要把 scenario DSL 做成通用命令行工具，还是先只服务 pytest。
4. 对 LangGraph checkpoint 和 interrupt 的测试边界是否清晰。
5. DB、checkpoint、全局线程池、workspace lock 的隔离是否完整。
6. 首批 5 个场景是否足够覆盖当前最高风险路径。
