# Orchestration Framework Selection Note

> ⚠️ **历史快照（LangGraph 时代设计，已过时）**：本文描述的编排架构（LangGraph 状态机 / plan_stage / review_stage / run_plan_loop / router 等）已于 cb6f9cd (2026-06-13) 被 Function Calling 模式取代，相应代码已删除或不再接入主流程。本文保留作架构演进决策记录（ADR），**请勿据此理解当前代码**。当前架构见 `design-agent-orchestrator.md`。


story-lifecycle 编排层的技术选型分析，记录 2026 年 6 月评估时的决策依据与反思。

## 项目约束

| 约束 | 说明 |
|------|------|
| 单机部署 | `pip install story-lifecycle` 即用，不依赖外部服务 |
| Python 原生 | 3.10+，不引入其他语言运行时 |
| 状态持久化 | 进程崩溃/重启后能恢复未完成的 Story |
| Interrupt/Resume | wait_confirm 需要暂停线程，人工确认后恢复 |
| 多供应商 | 支持 DeepSeek / Qwen / OpenAI / 任意 OpenAI 兼容 API |
| 精细控制 | Repair Packet、Gate Decision、Finding lifecycle 等自定义逻辑 |

## 候选方案

### 1. LangGraph (当前选择)

**做了什么**：

- `StateGraph(StoryState)` 定义 11 节点 + 5 条条件边
- `SqliteSaver` 做 checkpoint，进程重启后 `recover_orphan_stories()` 恢复
- `interrupt()` 实现 wait_confirm 挂起/恢复
- `ThreadPoolExecutor` 并发运行多个 Story，每个 Story 一个 `thread_id`

**用得好的部分**：

- checkpoint + interrupt/resume 开箱即用，MVP 阶段快速验证
- conditional edges 声明式路由，`graph.py` 一眼能看懂整个拓扑
- SQLite 嵌入式，零运维成本

**用得不够好的部分**：

- 图拓扑偏线性（plan → execute → poll → review → router），retry 是唯一回环，没有 fan-out/fan-in
- 大量状态在 LangGraph 外管理：`_running_stories`、`_workspace_locks`、`_story_epochs` 等全是用 `threading.Lock` 手动管理的 in-memory 状态
- 子任务拆分（`_delegate_subtasks`）用 ThreadPoolExecutor + interrupt 手动实现，本应使用 LangGraph 的 `Send` API
- 对抗循环（`run_plan_loop`）是 while 循环 + 函数调用，本应建模为子图
- 依赖链较重（langchain-core + langchain ecosystem）

### 2. Temporal

**优势**：

- **Durable Execution**：Workflow 状态由 Temporal Server 管理，跨进程、跨机器、跨重启都能恢复
- **Child Workflow**：原生 fan-out，每个子 workflow 独立持久化、独立重试
- **Signal/Query**：比 interrupt/resume 更强大，支持外部系统主动通知 workflow
- **企业级可靠性**：自动重试、超时、取消传播、版本兼容

**不选的原因**：

- 需要 Temporal Server（独立部署），违背 `pip install` 即用的产品定位
- 学习曲线高（Workflow determinism 约束、Activity vs Workflow 边界）
- 对单机场景 overkill

**适用场景**：如果未来需要多机分布式编排（如 K8s 集群中多个 AI Agent 并行），Temporal 是升级路径。

### 3. OpenAI Agents SDK

**优势**：

- `asyncio.gather(agent1.run(), agent2.run())` 实现 fan-out
- Agent-as-tool 模式清晰
- OpenAI 生态原生支持（function calling、structured output）

**不选的原因**：

- 没有 interrupt/resume — 无法暂停等人工确认
- 没有"子图"概念 — 对抗循环只能用 while 循环模拟
- 绑定 OpenAI 生态，不便于支持 DeepSeek/Qwen 等多供应商
- 状态管理需要自己实现（无 checkpoint）

### 4. CrewAI

**优势**：

- 多 agent 协作开箱即用，原型速度最快
- `human_input=True` 支持简单的人工介入

**不选的原因**：

- 黑盒太重 — Repair Packet、Gate Decision、Finding lifecycle 等精细控制难以实现
- `human_input=True` 只支持同步阻塞，不支持异步 pause/resume
- 状态管理不透明，自定义 merge/fan-in 困难

### 5. 手写 FSM

```python
def run_story(story_key):
    state = load_state_from_db(story_key)
    while state.status not in ("completed", "blocked"):
        state = plan_stage(state)
        state = execute_stage(state)
        state = poll_completion(state)
        state = review_stage(state)
        action = router(state)
        match action:
            case "advance": state = advance(state)
            case "retry":   continue
            case "fail":    break
            case "wait":    wait_for_human(state)  # threading.Event
```

**优势**：零依赖、完全可控、线性逻辑一目了然。

**不选的原因**：

- interrupt/resume 需要自己实现序列化 + 状态恢复，本质上是重造 LangGraph 的 checkpoint
- fan-out 需要自己管理线程池 + 结果收集 + 错误传播
- 到最后会写成另一个框架

## 全维度对比

```
                    LangGraph    Temporal     OpenAI SDK    CrewAI     手写 FSM
                    ─────────    ────────     ──────────    ───────    ────────
Python 原生          ✓            ✓            ✓             ✓          ✓
状态持久化           SQLite       Server DB    无             无         自己写
Fan-out/fan-in      Send API     Child WF     asyncio        有限       自己写
子图嵌套             ✓            ✓            ✗              弱         自己写
Interrupt/resume    ✓            ✓            ✗              仅同步     自己写
跨进程/分布式        ✗            ✓            ✗              ✗          ✗
运维复杂度           零(SQLite)   需要Server    零             零         零
学习曲线             中           高            低             低         低
依赖链               langchain    独立          openai         独立       无
精细控制力           高           高            中             低         最高
```

## 改进方向

当前代码中 LangGraph 利用率约 20%。如果重构，三个方向可以提升到 ~70%：

### 1. 子任务拆分改用 Send API（fan-out/fan-in）

现状（手动）：

```python
# subtask_delegate.py
def _delegate_subtasks(state, plan):
    for sub in plan["subtasks"]:
        db.upsert_story(sub_key, ...)     # 手动建子任务
    state["_pending_sub_keys"] = keys
    interrupt(...)                         # 手动挂起

# graph.py 尾部手动启动
for sub_key in result["_pending_sub_keys"]:
    start_story_async(sub_key)             # 手动投线程池
```

改进（LangGraph 原生）：

```python
from langgraph.types import Send

def plan_stage_node(state):
    plan = planner.plan_stage(state, cfg, adapters)
    if plan.get("split"):
        return [
            Send("execute_subtask", {**state, "story_key": sub_key, "plan": sub})
            for sub in plan["subtasks"]
        ]

def merge_subtasks(states: list[StoryState]) -> StoryState:
    merged = states[0].copy()
    merged["sub_results"] = [{"key": s["story_key"], "status": s["status"]} for s in states]
    return merged
```

好处：不需要 ThreadPoolExecutor、不需要 interrupt + Watchdog、checkpoint 自动覆盖。

### 2. 对抗循环改用子图

现状（函数调用）：

```python
# evaluator_loop.py
def run_plan_loop(state, adv_config, adapters):
    for round_num in range(1, max_rounds + 1):
        plan = planner.plan_stage(loop_state, cfg, adapters)   # 普通函数
        review = planner.review_plan(loop_state, plan, cfg)    # 普通函数
        if quality == "pass":
            return LoopResult(decision="pass", ...)
```

改进（子图）：

```python
adversarial_graph = StateGraph(StoryState)
adversarial_graph.add_node("planner", planner_node)
adversarial_graph.add_node("reviewer", reviewer_node)
adversarial_graph.add_conditional_edges("reviewer", route_review, {
    "pass": END,
    "revise": "planner",
    "no_progress": END,    # interrupt
    "max_rounds": END,     # interrupt
})
adversarial_graph.add_edge("planner", "reviewer")

# 作为子图嵌入主图
main_graph.add_node("adversarial_plan", adversarial_graph.compile(
    checkpointer=SqliteSaver(conn)
))
```

好处：每轮 checkpoint 独立持久化，崩溃恢复粒度更细。

### 3. 减少 in-memory 状态

现状（大量模块级全局变量）：

```python
# graph.py
_running_stories: dict[str, int] = {}
_workspace_locks: dict[str, dict] = {}
_story_epochs: dict[str, int] = {}
_status_lock = threading.Lock()
_plan_done: dict[str, tuple] = {}
```

改进方向：

- `_running_stories` / `_story_epochs` → 合并到 StoryState 或 DB
- `_workspace_locks` → 改用文件锁（跨进程安全）
- `_plan_done` / `_plan_activity` → 改用 LangGraph 的 streaming callback

## 结论

LangGraph 对当前场景是合理选择，但有改进空间。核心论点：

1. **Temporal 功能最强但运维成本不符合产品定位**
2. **OpenAI Agents SDK 缺少 interrupt/resume 和子图，且绑定单一供应商**
3. **CrewAI 黑盒太重，精细控制受限**
4. **手写 FSM 最终会重造 LangGraph 的 checkpoint + interrupt**
5. **LangGraph 的改进方向是更充分使用 Send API 和子图，减少手动状态管理**

## 参考资料

- [LangGraph: Building from First Principles](https://www.langchain.com/blog/building-langgraph)
- [LangGraph Multi-Agent Workflows](https://www.langchain.com/blog/langgraph-multi-agent-workflows)
- [Parallel Agents with OpenAI Agents SDK](https://developers.openai.com/cookbook/examples/agents_sdk/parallel_agents)
- [Temporal for AI](https://temporal.io/solutions/ai)
- [Temporal + OpenAI Agents SDK Integration](https://temporal.io/blog/announcing-openai-agents-sdk-integration)
- [6 Python AI Agent Frameworks Compared](https://pub.towardsai.net/i-compared-6-python-ai-agent-frameworks-so-you-dont-have-to-langgraph-vs-crewai-vs-pydanticai-vs-d8a5e6e43262)
- [LangGraph vs CrewAI vs OpenAI Agents SDK 2026](https://techsy.io/en/blog/langgraph-vs-crewai-vs-openai-agents-sdk)
- [Best Multi-Agent Frameworks 2026](https://gurusup.com/blog/best-multi-agent-frameworks-2026)
