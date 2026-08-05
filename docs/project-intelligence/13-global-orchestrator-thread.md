# 全局编排线程：三套调度机制归一（设计 13）

> **状态**：已实施（2026-08-05，commit 见下文）
> **实施记录**：
> - 新建 `orchestrator/abc.py`（StageExecutor / DecisionHandler / PromptBuilder 抽象类）、`orchestrator/executors.py`（Interactive/Automatic 执行器）、`orchestrator/handlers.py`（决策三分支）、`orchestrator/prompts.py`（PromptBuilder 子类）、`orchestrator/scheduler.py`（OrchestratorThread 全局编排线程）
> - 删除旧机制：`consume_orphan_artifacts` / `consume_orphan_done`（graph.py）、`find_ready_interactive_stories` / `resume_ready_interactive_stories` / `order_ready_stories`（graph.py）、`_watch_interactive_done_files`（api.py lifespan）、driver poll 循环（planner.py continue_orchestrator_agent 的 1446 行体）
> - `continue_orchestrator_agent` 保留为同步驱动入口（CLI/swebench/测试），内部驱动同一套 executors/handlers/judge 机制（非第二套调度逻辑）
> - `start_story_async` 改为「通知编排线程」（serve 场景只标 active；CLI 无 serve 时回退同步驱动）
> - 验证：全量测试 1377 passed（基线 1331，新增 46 个设计测试，零丢失）
> **先例**：延续 story-lifecycle 归一化传统（`192a9cfd` 统一 spawn env / `c1428454` 统一 artifact 检查 / `e61b5f4d` 统一 sid 捕获 / `2d795770` 删老 gate 管线）
> **行业对照**：[微软 Agent Framework Orchestrator](https://learn.microsoft.com/en-us/azure/architecture/ai-ml/guide/ai-agent-design-patterns) / [OpenAI Runner Loop](https://arize.com/blog/orchestrator-worker-agents-a-practical-comparison-of-common-agent-frameworks/)——所有主流框架都用单一中央编排器，不用 per-task driver

---

## 问题：三套调度机制并存

story-lifecycle 当前有**三套独立的调度机制**管理 PTY 和 stage 推进，职责重叠、行为不一致：

| 机制 | 触发方式 | 职责 | 问题 |
|------|---------|------|------|
| **driver**（`continue_orchestrator_agent`） | `start_story_async` → 线程池 submit | 遍历 actions + spawn PTY + poll + judge + retry/pause/advance | 全自动模式专用；半自动手动 spawn 不走这条 |
| **orphan 认领**（`consume_orphan_artifacts`） | `GET /story` 副作用（前端轮询触发） | 被动发现 artifacts 落地 + 标记完成 | 不 judge / 不 retry / 无条件标记完成 |
| **async watcher**（`_watch_interactive_done_files`） | serve 启动 + 每 1s 轮询 | 查 done.json 存在的 active story → resume driver | 只看 done.json 不看 artifacts；跟 orphan 重复 |

**归一化前例**（本仓库已做过 4 次同类重构）：

| commit | 归一化什么 | 模式 |
|--------|-----------|------|
| `192a9cfd` | 三条 spawn 路径各自手写 env → 抽 `build_story_spawn_env` | 多路径 → 单函数 |
| `c1428454` | 6 处 artifact 检查散在 4 文件 → 抽 `resolve_artifact_paths` | 多检查点 → 单口径 |
| `e61b5f4d` | 三种 sid 捕获各写各的 → 抽 `arm_sid_capture` 策略执行器 | 多策略 → 单执行器 |
| `2d795770` | 老 verify-gate 管线被 unified_gate 替代后删死代码 | 多管线 → 单管线 |

本次：**三套调度机制 → 全局编排线程**。

---

## 目标：一个全局编排线程

**一个 daemon 线程**（serve 启动时起，serve 停时止），负责**所有 story 的所有 PTY 的生命周期管理**。开子线程做具体的 judge 等耗时操作。

### 设计参考

所有主流 agent 框架都用单一中央编排器：
- **OpenAI Agents SDK**：Runner Loop 驱动 tool call 到完成
- **CrewAI**：Manager Agent 规划、委派、验证
- **AutoGen**：GroupChat Manager 选下一个 speaker
- **LangGraph**：Execution Graph 遍历节点和边
- **微软 Agent Framework**：Sequential/Concurrent Orchestrator

**没有一个框架用 per-task driver 线程。** 编排器在一个循环里：委派 → 收集结果 → 更新状态 → 决定下一步。

---

## 全局编排线程的循环

```
orchestrator_loop（daemon thread，每 poll_interval 秒一轮）:
  for story in active_stories（status=active 且 intake_state=ready）:
    ctx = load_context(story)
    stage = story.current_stage
    actions = ctx["_agent_actions"]
    
    # 1. 当前 stage 有 PTY 在跑？
    pty = get_pty(story_key, stage)
    
    if pty and pty.alive:
      # PTY 活着 → poll artifacts
      if artifacts_ready(stage):
        # 2. 成果物落地 → 子线程 judge（不阻塞主循环）
        submit_judge_task(story_key, stage, done_data)
        mark_stage_judging(story_key, stage)  # 防重复 judge
      else:
        continue  # 还在跑，等下一轮
    
    elif pty and not pty.alive:
      # PTY 死了
      if artifacts_ready(stage):
        submit_judge_task(story_key, stage, done_data)
      elif already_judged(stage):
        continue  # 已判过
      else:
        # PTY 死了没产出 → pause（等人介入）
        pause_story(story_key, "PTY died without artifacts")
    
    elif no_pty and stage_needs_spawn(stage, actions, ctx):
      # 3. 没有 PTY 但 stage 需要执行 → spawn
      #    （全自动模式自动 spawn；半自动模式等人点）
      if auto_mode(story):
        spawn_stage_pty(story_key, stage, actions)
      # 半自动：不 spawn，等人点「启动 CLI」
      # PTY 起来后下一轮自动发现
    
    # 4. judge 结果处理（子线程写结果，主循环读）
    if stage_judged(story_key, stage):
      decision = get_judge_result(story_key, stage)
      handle_decision(story_key, stage, decision, ctx, actions)
```

### `handle_decision`——三个 quality 分支

```
handle_decision(story_key, stage, decision, ctx, actions):
  
  if decision.quality == "reject":
    # 半自动：pause + 前端显示 reject 原因，用户手动重做
    # 全自动：插 retry action，下一轮 spawn 重做
    if auto_mode:
      insert_retry_action(story_key, stage, decision.reason)
      clear_stage_judging(story_key, stage)
    else:
      pause_story(story_key, f"judge reject: {decision.reason}")
    return
  
  if decision.quality == "escalate":
    pause_story(story_key, f"judge escalate: {decision.reason}")
    return
  
  # approve
  ctx["_completed_stages"].append(stage)
  save_summary(story_key, stage, decision.summary)
  
  # lifecycle 推进
  if decision.lifecycle_target:
    advance_lifecycle_to_target(story_key, ctx, decision.lifecycle_target)
    if paused_for_confirm: return  # 遇 ui_button 停住
  
  # 找下一个 stage
  next_stage = find_next_stage(actions, ctx["_completed_stages"])
  if next_stage:
    ctx["current_stage"] = next_stage
    save_context(story_key, ctx)
    # 下一轮主循环会 spawn（全自动）或等人点（半自动）
  else:
    # 所有 stage 完成 → lifecycle 推进到下一状态
    # 或 story 完成
    handle_all_stages_done(story_key, ctx)
  
  clear_stage_judging(story_key, stage)
```

---

## 子线程：judge_task

```
judge_task(story_key, stage, done_data):  # 在子线程里跑
  decision = judge_stage_completion(
    story_key, stage, workspace, ctx,
    lifecycle_state, done_data,
    cumulative_outputs, adapter,
    story_states, artifacts,
  )
  save_judge_result(story_key, stage, decision)  # 主循环下一轮读
```

judge 放子线程是因为它调 LLM（~10-30s），不能阻塞主循环（主循环要管所有 story）。

---

## 删掉的机制

| 删什么 | 为什么 |
|--------|--------|
| `consume_orphan_artifacts`（graph.py） | 全局编排线程 poll artifacts 替代 |
| `consume_orphan_done`（GET /story 副作用） | 同上 |
| `_watch_interactive_done_files`（api.py lifespan） | 全局编排线程替代 |
| `find_ready_interactive_stories`（graph.py） | 同上 |
| `resume_ready_interactive_stories`（graph.py） | 同上 |
| driver 的 poll 循环（planner.py:1611-2266） | 全局编排线程 poll + judge 替代 |
| `continue_orchestrator_agent` 的 action 遍历 | 并入 `handle_decision` 的 next_stage 逻辑 |

## 保留的机制

| 保留什么 | 为什么 |
|---------|--------|
| `start_story_async` | 改成「通知全局编排线程这个 story 要开始」（标 active，编排线程下一轮发现它） |
| `_spawn_story_agent_pty`（api.py） | spawn 逻辑保留，被编排线程和 /sessions/spawn 共用 |
| `/sessions/spawn`（手动起 PTY） | 保留，起完 PTY 注册到 `_ptys`，编排线程自动发现 |
| `judge_stage_completion`（stage_completion.py） | 核心裁判逻辑不变 |
| `advance_lifecycle_to_target`（stage_completion.py） | lifecycle 推进逻辑不变 |
| PTY try/finally 释放（设计 12 改动 2） | 保留，spawn 的 PTY 释放不变 |
| ThreadPoolExecutor | 保留给 judge 子线程用 |

---

## 并发安全

- **主循环单线程**：编排循环只在一个线程里跑，所有 DB 读写不需要锁（单写者）
- **judge 子线程**：只读 story 数据 + 写 judge_result（独立 key，不跟主循环冲突）
- **`/sessions/spawn`**：HTTP 请求线程，只写 `_ptys` 注册表（已有 `_lock`）
- **`/advance`、`/skip`、`/lifecycle/advance`**：HTTP 请求线程，改 ctx/DB。跟主循环可能竞争——用「标记 + 下一轮检查」模式：HTTP 请求只改标记（如 `_stage_gate.awaiting_confirm=False`），主循环下一轮读到标记后执行推进

---

## 跟现有 API 的交互

| API | 现在 | 改后 |
|-----|------|------|
| `POST /start` | 规划 + `start_story_async`（submit driver） | 规划 + 标 active（编排线程下一轮发现） |
| `PUT /advance` | `start_story_async`（重启 driver） | 清 confirm 标记（编排线程下一轮继续） |
| `PUT /skip/{stage}` | `start_story_async` | 标 skip（编排线程跳过该 stage） |
| `POST /sessions/spawn` | 起裸 PTY | 不变（起 PTY，编排线程自动发现） |
| `POST /lifecycle/advance` | 推进 lifecycle + `start_story_async` | 推进 lifecycle（编排线程下一轮继续 stages） |
| `GET /story` | 触发 `consume_orphan_artifacts` | 去掉副作用（只读） |

---

## 实施方式：一步到位

**不搞过渡阶段。** 一个 commit 完成全部改动：建全局编排线程 + 删旧机制 + 清理坏味道。

---

## 代码质量要求

这次重构不只是「搬代码到新文件」，必须**清理坏味道、抽设计模式、建抽象类**。当前的代码债：

| 坏味道 | 现状 | 目标 |
|--------|------|------|
| **巨型函数** | `continue_orchestrator_agent` = 1813 行（planner.py:896-末尾） | 拆成 < 50 行的小函数，职责单一 |
| **上帝文件** | `api.py` = 4843 行（spawn + prompt + API + intake + lifecycle 全混） | 按职责拆分（spawn/prompt/api 分开） |
| **无抽象层** | 只有 BaseModel 数据结构，没有 ABC/Protocol/Interface | 建抽象类定义契约 |
| **重复逻辑** | spawn env 三处手写（已部分修）、artifact 检查散落（已部分修）、prompt 构建多处 | 统一到抽象类的实现 |

### 必须建的抽象类

```python
# orchestrator/abc.py（新建）

from abc import ABC, abstractmethod
from typing import Optional

class StageExecutor(ABC):
    """stage 执行器抽象——定义 spawn PTY / poll artifacts / 判断完成的契约。
    
    编排线程通过这个接口操作 stage，不关心具体是半自动(手动 spawn)还是全自动(自动 spawn)。
    子类：
    - InteractiveStageExecutor: 半自动模式（等人点「启动 CLI」，编排线程只 poll+judge）
    - AutomaticStageExecutor: 全自动模式（编排线程自动 spawn + poll + judge）
    """

    @abstractmethod
    def get_pty(self, story_key: str, stage: str):
        """获取当前 stage 的 PTY（可能 None=未 spawn）。"""

    @abstractmethod
    def spawn(self, story_key: str, stage: str, action: dict) -> str:
        """spawn PTY for stage，返回 session_id。"""

    @abstractmethod
    def is_artifacts_ready(self, story_key: str, stage: str) -> bool:
        """stage 的成果物是否全部落地。"""


class DecisionHandler(ABC):
    """judge 决策处理抽象——定义 approve/reject/escalate 三分支的契约。
    
    编排线程调 judge 拿到决策后，通过这个接口执行副作用。
    子类：
    - InteractiveDecisionHandler: 半自动（reject→pause等人，approve→推进）
    - AutomaticDecisionHandler: 全自动（reject→插retry重做，approve→推进+spawn next）
    """

    @abstractmethod
    def handle_approve(self, story_key: str, stage: str, decision: dict, ctx: dict, actions: list) -> bool:
        """approve: 写 _completed_stages + lifecycle 推进 + summary。返回是否 paused_for_confirm。"""

    @abstractmethod
    def handle_reject(self, story_key: str, stage: str, decision: dict, ctx: dict, actions: list) -> None:
        """reject: 半自动→pause；全自动→插 retry action。"""

    @abstractmethod
    def handle_escalate(self, story_key: str, stage: str, decision: dict, ctx: dict) -> None:
        """escalate: pause 等人。"""


class PromptBuilder(ABC):
    """stage prompt 构建抽象——统一 _build_cli_prompt / _build_stage_launch_prompt /
    _build_interactive_stage_prompt 三处重复的 prompt 构建。
    
    子类按 stage 类型（design/build/verify）或 profile 类型提供具体实现。
    """

    @abstractmethod
    def build(self, story_key: str, stage: str, workspace: str, ctx: dict, action: dict) -> str:
        """构建 stage 的 CLI prompt。"""
```

### 设计模式

| 模式 | 应用场景 |
|------|---------|
| **Strategy** | `StageExecutor`（半自动 vs 全自动的 spawn/poll 策略）+ `DecisionHandler`（reject 处理策略） |
| **Template Method** | 编排线程的 `orchestrator_loop` 是模板，`StageExecutor` / `DecisionHandler` 是可替换步骤 |
| **Observer** | stage 完成 / judge 结果 通过 DB 标记通知（编排线程轮询读，类似观察者） |
| **Factory** | 根据 story 的 profile（minimal/realtest/single-pass）创建对应的 `StageExecutor` + `DecisionHandler` |

### 坏味道清理清单

**planner.py（2708 行 → 拆分）**：
- `continue_orchestrator_agent`（1813 行）→ **删**，逻辑分散到 `scheduler.py` + `StageExecutor` + `DecisionHandler`
- `run_orchestrator_agent`（规划逻辑，~200 行）→ 保留，它是规划阶段的入口
- `_build_cli_prompt` / prompt 构建 → 抽到 `PromptBuilder` 子类
- `_register_stage_outputs` / `_auto_commit_worktrees` → 保留（纯函数，可复用）
- orphan 认领逻辑（`_completed_stages` 认领）→ **删**（编排线程替代）

**api.py（4843 行 → 拆分）**：
- `_spawn_story_agent_pty`（~200 行）→ 移到 `StageExecutor` 子类
- `_ensure_story_agent_pty` → **删**（跟 `_spawn_story_agent_pty` 重复）
- `_build_stage_launch_prompt` / `_build_interactive_stage_prompt` → 抽到 `PromptBuilder`
- API endpoint（GET/POST/PUT）→ 保留在 api.py（但去掉 `start_story_async` 调用）
- `_watch_interactive_done_files` → **删**

**graph.py（646 行 → 瘦身）**：
- `run_story` / `start_story_async` / `resume_story_async` → **删**（编排线程替代）
- `consume_orphan_artifacts` / `consume_orphan_done` → **删**
- `find_ready_interactive_stories` / `resume_ready_interactive_stories` / `order_ready_stories` → **删**
- 保留：`is_story_running`（编排线程判断是否需要管）、`force_stop_story`（紧急停止）、DB/epoch 辅助函数

---

## 文件组织（改后）

```
orchestrator/
├── abc.py                    # 新建：StageExecutor / DecisionHandler / PromptBuilder 抽象类
├── scheduler.py              # 新建：OrchestratorThread + orchestrator_loop（编排线程）
├── executors.py              # 新建：InteractiveStageExecutor / AutomaticStageExecutor
├── handlers.py               # 新建：InteractiveDecisionHandler / AutomaticDecisionHandler
├── prompts.py                # 新建：PromptBuilder 子类（从 planner.py + api.py 抽出）
├── engine/
│   ├── planner.py            # 瘦身：只留 run_orchestrator_agent（规划）+ 纯函数辅助
│   ├── graph.py              # 瘦身：只留 claim/epoch + DB 辅助
│   └── profile_loader.py     # 不变
├── evaluation/
│   ├── stage_completion.py   # 不变（judge_stage_completion + advance_lifecycle_to_target）
│   ├── boundary_judge.py     # 逐步废弃（功能并入 stage_completion）
│   └── unified_gate.py       # 不变（verify 质量门 + 外部 provider）
├── service/
│   └── api.py                # 瘦身：只留 API endpoint，去掉 spawn/prompt/调度逻辑
└── workspace/
    └── ...                   # 不变
```

---

## 编排线程的完整伪代码（含抽象类调用）

```python
# scheduler.py

class OrchestratorThread(threading.Thread):
    """全局编排线程（daemon）。一个实例，serve 启动时起，serve 停时止。"""

    def __init__(self, poll_interval: float = 5.0):
        super().__init__(daemon=True, name="orchestrator")
        self._poll_interval = poll_interval
        self._executor_pool = ThreadPoolExecutor(max_workers=4)  # judge 子线程
        self._judging: set[str] = set()  # 正在 judge 的 story_key:stage

    def run(self):
        while True:
            try:
                self._tick()
            except Exception:
                log.exception("orchestrator tick failed (non-fatal, continuing)")
            time.sleep(self._poll_interval)

    def _tick(self):
        """一轮轮询：遍历所有 active story。"""
        for story in db.list_active_stories():
            if story.get("intake_state") != "ready":
                continue
            story_key = story["story_key"]
            ctx = load_ctx(story)
            stage = story.get("current_stage", "")
            if not stage:
                continue

            executor = self._resolve_executor(story, ctx)  # Factory: 按 profile 创建
            handler = self._resolve_handler(story, ctx)
            judge_key = f"{story_key}:{stage}"

            # 1. 已在 judge → 跳过（等子线程写结果）
            if judge_key in self._judging:
                # 检查 judge 是否完成
                result = db.get_pending_judge_result(story_key, stage)
                if result:
                    self._judging.discard(judge_key)
                    handler.handle_decision(story_key, stage, result, ctx, actions)
                continue

            # 2. PTY 在跑 → poll artifacts
            pty = executor.get_pty(story_key, stage)
            if pty and pty.alive:
                if executor.is_artifacts_ready(story_key, stage):
                    done_data = read_done_data(story_key, stage, workspace)
                    self._submit_judge(story_key, stage, done_data, ctx, executor)
                continue

            # 3. PTY 死了 → 看 artifacts
            if pty and not pty.alive:
                if executor.is_artifacts_ready(story_key, stage):
                    done_data = read_done_data(story_key, stage, workspace)
                    self._submit_judge(story_key, stage, done_data, ctx, executor)
                else:
                    # PTY 死了没产出 → pause
                    sm_pause(story_key, error=f"PTY died without artifacts for {stage}")
                continue

            # 4. 没有 PTY → executor 决定是否 spawn
            #    InteractiveExecutor: 不 spawn（等人点）
            #    AutomaticExecutor: 自动 spawn
            executor.maybe_spawn(story_key, stage, ctx)

    def _submit_judge(self, story_key, stage, done_data, ctx, executor):
        """submit judge 到子线程池。"""
        judge_key = f"{story_key}:{stage}"
        self._judging.add(judge_key)
        self._executor_pool.submit(self._judge_task, story_key, stage, done_data, ctx)

    def _judge_task(self, story_key, stage, done_data, ctx):
        """子线程：调 judge_stage_completion，写结果到 DB。"""
        try:
            decision = judge_stage_completion(...)
            db.save_pending_judge_result(story_key, stage, decision)
        except Exception:
            log.exception("[%s] judge failed for %s", story_key, stage)
            db.save_pending_judge_result(story_key, stage, {
                "quality": "approve",  # fallback
                "lifecycle_target": None,
                "summary": "",
                "reason": "judge failed, fallback approve",
            })
```

---

## 验证计划

### 分步验证策略

这次重构**必须分步验证**，每完成一个模块就跑对应测试，不能一口气改完再测。以下是执行 AI 应该遵循的验证节奏：

#### Step 0：基线（改之前）

```bash
# 先确认现有测试全过（建立基线）
cd D:/github/story-lifecycle
python -m pytest packages/story-lifecycle/tests/ -x -q --tb=short 2>&1 | tail -5
# 记录 pass 数（当前 ~1300），重构后不能低于这个数
```

#### Step 1：抽象类（abc.py）

**改完 abc.py 就测，不等其他文件。**

```bash
# 测试抽象类能实例化子类、不能实例化自身
python -m pytest packages/story-lifecycle/tests/test_scheduler_abc.py -x -v 2>&1
```

测试文件 `tests/test_scheduler_abc.py` 要覆盖：
```python
class TestAbcContracts:
    def test_stage_executor_cannot_instantiate_directly(self):
        """ABC 不能实例化"""
        with pytest.raises(TypeError):
            StageExecutor()

    def test_stage_executor_subclass_must_implement_all(self):
        """缺一个方法就 TypeError"""
        class Incomplete(StageExecutor): pass
        with pytest.raises(TypeError):
            Incomplete()

    def test_decision_handler_three_branches_exist(self):
        """三个 handle 方法都在接口里"""
        assert hasattr(DecisionHandler, 'handle_approve')
        assert hasattr(DecisionHandler, 'handle_reject')
        assert hasattr(DecisionHandler, 'handle_escalate')

    def test_prompt_builder_build_signature(self):
        """build 方法签名正确"""
        import inspect
        sig = inspect.signature(PromptBuilder.build)
        assert 'story_key' in sig.parameters
        assert 'stage' in sig.parameters
```

#### Step 2：PromptBuilder（prompts.py）

**从 planner.py + api.py 抽出 prompt 构建，立刻验证 prompt 内容不变。**

```bash
python -m pytest packages/story-lifecycle/tests/test_prompts.py -x -v 2>&1
```

测试要点：
```python
class TestPromptBuilder:
    def test_design_prompt_contains_prd(self, tmp_story):
        """design prompt 包含 PRD 内容"""
        builder = DesignPromptBuilder()
        prompt = builder.build(story_key, "design", workspace, ctx, action)
        assert "提现门槛" in prompt  # PRD 内容

    def test_build_prompt_contains_project_lines(self, tmp_story):
        """build prompt 包含项目 worktree 信息"""
        builder = BuildPromptBuilder()
        prompt = builder.build(story_key, "build", workspace, ctx, action)
        assert "hc-order" in prompt  # 项目信息

    def test_prompt_matches_old_output(self, tmp_story):
        """新 builder 输出 == 老 _build_cli_prompt 输出（回归保护）"""
        old_prompt = old_build_cli_prompt(story_key, ...)  # 改之前的函数
        new_prompt = builder.build(story_key, ...)
        assert old_prompt == new_prompt  # 或核心段落一致
```

#### Step 3：StageExecutor（executors.py）

**spawn + poll 逻辑抽到 executor，验证 spawn 能起 PTY + poll 能判 artifacts。**

```bash
python -m pytest packages/story-lifecycle/tests/test_executors.py -x -v 2>&1
```

测试要点：
```python
class TestInteractiveStageExecutor:
    def test_get_pty_returns_none_when_not_spawned(self, tmp_story):
        """没 spawn 时 get_pty 返回 None"""
        executor = InteractiveStageExecutor()
        assert executor.get_pty(story_key, "design") is None

    def test_is_artifacts_ready_false_when_no_spec(self, tmp_story):
        """spec.md 不存在时 False"""
        assert executor.is_artifacts_ready(story_key, "design") is False

    def test_is_artifacts_ready_true_when_spec_landed(self, tmp_story_with_spec):
        """spec.md 存在时 True"""
        assert executor.is_artifacts_ready(story_key, "design") is True

    def test_maybe_spawn_does_nothing_in_interactive(self, tmp_story):
        """半自动模式 maybe_spawn 不 spawn"""
        executor = InteractiveStageExecutor()
        executor.maybe_spawn(story_key, "design", ctx)
        assert executor.get_pty(story_key, "design") is None

class TestAutomaticStageExecutor:
    def test_maybe_spawn_spawns_pty(self, tmp_story, mock_pty):
        """全自动模式 maybe_spawn 起 PTY"""
        executor = AutomaticStageExecutor()
        executor.maybe_spawn(story_key, "design", ctx)
        assert executor.get_pty(story_key, "design") is not None
```

#### Step 4：DecisionHandler（handlers.py）

**approve/reject/escalate 三分支，每个都要独立测。不需要真跑编排线程。**

```bash
python -m pytest packages/story-lifecycle/tests/test_handlers.py -x -v 2>&1
```

测试要点：
```python
class TestInteractiveDecisionHandler:
    def test_approve_writes_completed_stages(self, tmp_story):
        handler = InteractiveDecisionHandler()
        decision = {"quality": "approve", "lifecycle_target": None, "summary": "ok"}
        handler.handle_approve(story_key, "design", decision, ctx, actions)
        ctx = reload_ctx(story_key)
        assert "design" in ctx["_completed_stages"]

    def test_approve_writes_summary(self, tmp_story):
        handler = InteractiveDecisionHandler()
        decision = {"quality": "approve", "summary": "本轮完成了调研"}
        handler.handle_approve(story_key, "design", decision, ctx, actions)
        session = db.get_session(story_key, "design", "claude")
        assert "本轮完成了调研" in (session.get("completion_summary") or "")

    def test_reject_pauses_story(self, tmp_story):
        handler = InteractiveDecisionHandler()
        decision = {"quality": "reject", "reason": "spec 不完整"}
        handler.handle_reject(story_key, "design", decision, ctx, actions)
        story = db.get_story(story_key)
        assert story["status"] == "paused"

    def test_escalate_pauses_story(self, tmp_story):
        handler = InteractiveDecisionHandler()
        decision = {"quality": "escalate", "reason": "超限"}
        handler.handle_escalate(story_key, "design", decision, ctx)
        story = db.get_story(story_key)
        assert story["status"] == "paused"

class TestAutomaticDecisionHandler:
    def test_reject_inserts_retry_action(self, tmp_story):
        """全自动 reject 插 retry action（半自动不插）"""
        handler = AutomaticDecisionHandler()
        decision = {"quality": "reject", "reason": "spec 不完整"}
        handler.handle_reject(story_key, "design", decision, ctx, actions)
        ctx = reload_ctx(story_key)
        # actions 里应该多了一个 design retry
        design_actions = [a for a in ctx["_agent_actions"] if a.get("stage") == "design"]
        assert len(design_actions) == 2  # 原始 + retry

    def test_approve_advances_to_next_stage(self, tmp_story):
        """approve 后 current_stage 推进到 build"""
        handler = AutomaticDecisionHandler()
        decision = {"quality": "approve", "lifecycle_target": None, "summary": "ok"}
        handler.handle_approve(story_key, "design", decision, ctx, actions)
        story = db.get_story(story_key)
        assert story["current_stage"] == "build"
```

#### Step 5：OrchestratorThread（scheduler.py）

**编排线程的主循环，用 mock executor + mock handler 测调度逻辑（不真起 PTY/LLM）。**

```bash
python -m pytest packages/story-lifecycle/tests/test_scheduler.py -x -v 2>&1
```

测试要点：
```python
class TestOrchestratorTick:
    def test_tick_skips_non_ready_story(self, tmp_story_candidate):
        """candidate 状态的 story 不被编排"""
        orchestrator._tick()
        assert no_events_for(story_key)

    def test_tick_polls_alive_pty_artifacts(self, tmp_story, mock_alive_pty, mock_no_artifacts):
        """PTY 活着 + 没 artifacts → 不 judge"""
        orchestrator._tick()
        assert not judging(story_key)

    def test_tick_judges_when_artifacts_ready(self, tmp_story, mock_dead_pty, mock_artifacts_ready):
        """PTY 死了 + artifacts ready → submit judge"""
        orchestrator._tick()
        assert judging(story_key)

    def test_tick_skips_already_judging(self, tmp_story, mock_judging):
        """已在 judge → 跳过"""
        orchestrator._tick()
        assert judge_submitted_once(story_key)  # 不重复 submit

    def test_tick_does_not_spawn_in_interactive(self, tmp_story, mock_no_pty):
        """半自动 + 没 PTY → 不 spawn"""
        orchestrator._tick()
        assert no_spawn_called(story_key)

    def test_tick_spawns_in_automatic(self, tmp_story_auto, mock_no_pty):
        """全自动 + 没 PTY → spawn"""
        orchestrator._tick()
        assert spawn_called(story_key)

class TestOrchestratorCrashRecovery:
    def test_tick_survives_exception(self, tmp_story, mock_executor_raises):
        """executor 抛异常 → 不崩，继续"""
        orchestrator._tick()  # 不抛
        orchestrator._tick()  # 下一轮正常

class TestJudgeTask:
    def test_judge_writes_result_to_db(self, tmp_story, mock_llm):
        """子线程 judge 完写结果到 DB"""
        orchestrator._judge_task(story_key, "design", done_data, ctx)
        result = db.get_pending_judge_result(story_key, "design")
        assert result["quality"] in ("approve", "reject", "escalate")

    def test_judge_fallback_on_llm_failure(self, tmp_story, mock_llm_failure):
        """LLM 挂了 → fallback approve"""
        orchestrator._judge_task(story_key, "design", done_data, ctx)
        result = db.get_pending_judge_result(story_key, "design")
        assert result["quality"] == "approve"
```

#### Step 6：集成（替换旧机制 + 全量回归）

**删旧代码 + 接 API + 跑全量测试。**

```bash
# 1. 全量回归（核心指标）
python -m pytest packages/story-lifecycle/tests/ -x -q 2>&1 | tail -5
# pass 数不能低于 Step 0 基线

# 2. 重点回归（调度相关）
python -m pytest packages/story-lifecycle/tests/ -k "state_machine or spawn or pty or execution or orphan or lifecycle or stage_completion or boundary" -v 2>&1 | tail -10

# 3. 被删函数的引用检查（确保没有遗漏调用点）
grep -rn "consume_orphan\|start_story_async\|continue_orchestrator_agent\|_watch_interactive" packages/story-lifecycle/src/ 2>/dev/null
# 期望：只在注释/文档里出现，不在运行代码里
```

#### Step 7：E2E（真跑 serve + story）

```bash
# 重启 serve（带 LLM key）
# 用 UI 跑一个真实 story（如 1068018）：
#   intake → start → 编排线程自动 spawn design → judge → 推进
#   或半自动：手动 spawn → judge → 推进

# 验证点：
# 1. serve 日志有 orchestrator tick（每 5s 一轮）
# 2. spawn 后编排线程发现 PTY
# 3. artifacts ready 后编排线程 submit judge
# 4. judge 结果写入 DB（orchestrator_decision 表）
# 5. approve 后 _completed_stages 写入 + lifecycle 推进
# 6. reject 后 pause + UI 显示原因
# 7. UI TerminalTab 显示 summary
```

### 测试基础设施要求

执行 AI 需要创建/补充的测试 fixture：

```python
# conftest.py 补充

@pytest.fixture
def tmp_story(tmp_path):
    """创建一个临时 story（minimal profile, design stage, 有 _agent_actions）。"""
    # 用内存 DB 或 tmp DB
    # 插入 story + context_json（含 _agent_actions 3 个 launch）
    # 返回 story_key

@pytest.fixture
def tmp_story_with_spec(tmp_story):
    """在 tmp_story 基础上写一个 spec.md（模拟 design 产出）。"""
    # 在 evidence 目录写 spec.md
    # 返回 story_key

@pytest.fixture
def tmp_story_auto(tmp_story):
    """改成全自动 profile（realtest/single-pass）。"""

@pytest.fixture
def mock_alive_pty(monkeypatch):
    """mock 一个活的 PTY（pty.alive = True）。"""

@pytest.fixture
def mock_dead_pty(monkeypatch):
    """mock 一个死的 PTY（pty.alive = False）。"""

@pytest.fixture
def mock_llm(monkeypatch):
    """mock judge_stage_completion 的 LLM 调用（返回固定 decision）。"""

@pytest.fixture
def mock_llm_failure(monkeypatch):
    """mock LLM 调用抛异常（测 fallback）。"""
```

---

## 风险

| 风险 | 缓解 |
|------|------|
| 主循环单线程瓶颈（story 多时 poll 间隔变长） | poll_interval 5s + judge 放子线程不阻塞 |
| 编排线程崩溃 = 全部 story 停 | daemon thread + try/except 兜底 + 日志告警 |
| 跟 HTTP 请求竞争 ctx | HTTP 只改标记，编排线程读标记执行（不直接竞争 DB 写） |
| 抽象类过度设计 | 只在有实际多态的地方抽（半自动 vs 全自动确实需要）；prompt 构建确实重复需要抽 |
| 删 driver 后全自动模式的 reject retry 丢失 | DecisionHandler.handle_reject 在全自动模式插 retry action |

---

## 关键文件清单

| 文件 | 改动 |
|------|------|
| `orchestrator/abc.py` | **新建**：StageExecutor / DecisionHandler / PromptBuilder 抽象类 |
| `orchestrator/scheduler.py` | **新建**：OrchestratorThread + orchestrator_loop |
| `orchestrator/executors.py` | **新建**：InteractiveStageExecutor / AutomaticStageExecutor |
| `orchestrator/handlers.py` | **新建**：InteractiveDecisionHandler / AutomaticDecisionHandler |
| `orchestrator/prompts.py` | **新建**：PromptBuilder 子类（从 planner.py + api.py 抽出） |
| `orchestrator/service/api.py` | 瘦身：去掉 spawn/prompt/调度逻辑，只留 API endpoint |
| `orchestrator/engine/graph.py` | 瘦身：删 run_story/start_story_async/consume_orphan 等，留 claim/epoch |
| `orchestrator/engine/planner.py` | 瘦身：删 continue_orchestrator_agent，留 run_orchestrator_agent + 纯函数 |
| `infra/terminal/pty.py` | 不变 |
| `evaluation/stage_completion.py` | 不变 |
