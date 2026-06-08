# story-lifecycle 编排层重构设计

## Context

story-lifecycle 当前有三个架构问题：

1. **LLM 调用重复**：7 个文件各自手写 httpx 调用 + JSON 解析，_api_config / _parse_llm_json 等函数重复 6+ 次
2. **Layer 1 图过度碎片化**：11 个节点中 retry/skip/fail/wait_confirm 4 个仅做字段赋值，图拓扑复杂但不必要
3. **LangGraph 利用率低**：对抗循环用 while 函数调用，子任务拆分用 ThreadPoolExecutor，未用 Send/sub-graph

重构目标：统一 LLM 调用 → 精简图到 5 节点 → 对抗循环变子图 → 子任务变 fan-out。

---

## Step 1: LLMClient 统一 LLM 调用层

### 新文件

`src/story_lifecycle/llm_client.py` (~200 行)

### 设计

```python
from pydantic import BaseModel, ValidationError
import httpx, json, re, os, time, logging

T = TypeVar("T", bound=BaseModel)

class LLMClient:
    def __init__(self):
        self.api_key = os.environ.get("STORY_LLM_API_KEY", "")
        self.base_url = os.environ.get("STORY_LLM_BASE_URL", "https://api.deepseek.com")
        self.model = os.environ.get("STORY_LLM_MODEL", "deepseek-v4-pro")

    def invoke(self, prompt, *, temperature=0.1, timeout=90, system=None) -> str:
        """返回文本内容"""

    def invoke_json(self, prompt, *, temperature=0.1, timeout=90, system=None) -> dict:
        """返回解析后的 JSON dict"""

    def invoke_structured(self, prompt, model: Type[T], *, temperature=0.1, timeout=90, system=None) -> T:
        """返回 Pydantic 模型实例"""

    def stream(self, prompt, *, on_chunk=None, temperature=0.1) -> str:
        """流式调用，返回完整文本"""

    # 内部方法
    def _request(self, prompt, *, temperature, timeout, system, stream=False)
    def _extract_content(self, body: dict) -> str  # 含 reasoning_content fallback
    @staticmethod
    def _parse_json(content: str) -> dict           # direct → fence → bracket counting

# 全局单例
_client: LLMClient | None = None

def get_llm() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
```

### 要消除的重复代码

| 文件 | 现有函数 | 改造为 |
|------|---------|--------|
| `orchestrator/planner.py` | `_call_llm()`, `_call_llm_for_text()`, `_stream_llm()`, `_api_config()`, `_parse_llm_response()`, `_trace_llm()` | `llm.invoke_json()`, `llm.invoke()`, `llm.stream()` |
| `orchestrator/router.py` | `_llm_route()`, `_get_api_key/base_url/model()`, `_parse_llm_json()`, `_extract_json_object()` | `llm.invoke_json()` |
| `orchestrator/semantic.py` | `_call_semantic_llm()`, `_get_api_key/base_url/model()`, `_parse_llm_json()`, `_extract_json_object()`, `_validate_schema()` | `llm.invoke_structured()` (Pydantic 替代手写 schema) |
| `orchestrator/copilot.py` | `_call_llm()`, `_api_config()`, `_parse_copilot_response()` | `llm.invoke_json()` |
| `orchestrator/review_feedback.py` | `_llm_extract()`, inline env vars, `_parse_llm_json()` | `llm.invoke_json()` |
| `planner/llm.py` | `call_llm()`, `call_llm_json()`, `_api_config()`, `_parse_json_response()`, `_extract_json()` | `llm.invoke()`, `llm.invoke_json()` |

### Pydantic 模型替代手写 schema

```python
# src/story_lifecycle/schemas.py
class PlanResult(BaseModel):
    adapter: str = "claude"
    provider: str | None = None
    model: str | None = None
    skip: bool = False
    split: bool = False
    subtasks: list[Subtask] | None = None
    summary: str
    extra_instructions: str
    reasoning: str
    trajectory_score: float = 0.5

class ReviewResult(BaseModel):
    quality: Literal["pass", "revise", "fail"]
    summary: str
    issues: list[Issue] = []
    suggestions: list[str] = []
    trajectory_score: float = 0.5

class RouteDecision(BaseModel):
    action: Literal["retry", "skip", "fail"]
    reasoning: str
    provider_override: str | None = None

# semantic.py 的 schema 也转 Pydantic
class BugContextResult(BaseModel):
    description: str
    steps_to_reproduce: str = ""
    expected_behavior: str = ""
    actual_behavior: str = ""
    environment: str = ""
    logs: str = ""
    missing_fields: list[str] = []
    confidence: Literal["high", "medium", "low"] = "low"
```

### 追踪统一

LLMClient 内置 `_trace()` 方法，通过 DB 记录所有 LLM 调用的 token/延迟/状态。当前只有 planner.py 和 copilot.py 有追踪，改造后所有调用自动追踪。

### 依赖变更

- 新增：`pydantic`（如果尚未引入）
- 无新增 httpx/langchain 依赖（httpx 已有）
- 删除：无（保持向后兼容，旧函数标记 deprecated）

### 涉及文件

- 新建：`src/story_lifecycle/llm_client.py`, `src/story_lifecycle/schemas.py`
- 修改：`orchestrator/planner.py`, `orchestrator/router.py`, `orchestrator/semantic.py`, `orchestrator/copilot.py`, `orchestrator/review_feedback.py`, `planner/llm.py`, `planner/decomposer.py`, `planner/roadmap.py`, `planner/idea_expander.py`

---

## Step 2: Layer 1 精简到 5 节点

### 改造前 (11 nodes)

```
START → plan_stage → execute_stage → poll_completion → review_stage → router
                                                                        │
                     ┌─────────────────────────────────────────────────┘
                     ├── advance ──→ (plan_stage | END)
                     ├── retry ────→ plan_stage
                     ├── skip_stage → advance → (plan_stage | END)
                     ├── fail_stage → END
                     └── wait_confirm → plan_stage
```

### 改造后 (5 nodes)

```
START → plan_stage → execute_and_wait → review_stage → router
                                                              │
                     ┌────────────────────────────────────────┘
                     ├── advance ──→ (plan_stage | END)
                     ├── retry ────→ plan_stage
                     └── END (fail/completed)
```

### 合并规则

| 旧节点 | 新归属 | 说明 |
|--------|--------|------|
| `execute_stage` | → `execute_and_wait` | 合并 execute + poll |
| `poll_completion` | → `execute_and_wait` | 内部轮询 + interrupt |
| `retry` | → `router` 内部 | `state["last_error"] = None` + 回到 plan_stage |
| `skip_stage` | → `router` 内部 | 填 SKIPPED + 执行 advance 逻辑 |
| `fail_stage` | → `router` 内部 | `state["status"] = "blocked"` → END |
| `wait_confirm` | → `router` 内部 | `interrupt()` 挂起，恢复后回到 plan_stage |
| `advance` | → 保留为独立节点 | 逻辑复杂（validation + DoD + next_stage + source sync），值得独立 |

### 新 build_graph()

```python
def build_graph() -> StateGraph:
    graph = StateGraph(StoryState)
    graph.add_node("plan_stage", plan_stage_node)
    graph.add_node("execute_and_wait", execute_and_wait_node)
    graph.add_node("review_stage", review_stage_node)
    graph.add_node("router", router_node)
    graph.add_node("advance", advance_node)

    graph.add_edge(START, "plan_stage")
    graph.add_conditional_edges("plan_stage", route_after_plan, {
        "execute_and_wait": "execute_and_wait",
        "router": "router",
        "__end__": END,
    })
    graph.add_conditional_edges("execute_and_wait", route_after_execute, {
        "review_stage": "review_stage",
        "router": "router",
    })
    graph.add_edge("review_stage", "router")
    graph.add_conditional_edges("router", route_from_router, {
        "plan_stage": "plan_stage",     # advance / retry / skip / wait_confirm resume
        "advance": "advance",
        "__end__": END,                  # fail
    })
    graph.add_conditional_edges("advance", route_after_advance, {
        "plan_stage": "plan_stage",
        "__end__": END,                  # completed
    })
    return graph
```

### router_node 内部逻辑

```python
def router_node(state: StoryState) -> dict:
    if _is_cancelled(state):
        return {"_next_action": "__end__"}

    # 1. Pre-routed (from adversarial loop exhaustion)
    pre = state.pop("_pre_routed_action", None)
    if pre:
        return _execute_action(state, pre)

    # 2. Rule-based routing (trajectory score, retry fatigue, etc.)
    action, reason = _decide_by_rules(state)
    if action:
        return _execute_action(state, action)

    # 3. LLM routing (unhappy path, no review context)
    decision = get_llm().invoke_structured(prompt, RouteDecision)
    return _execute_action(state, decision.action)

def _execute_action(state, action):
    match action:
        case "advance":
            state.pop("last_error", None)
            return {"_next_action": "advance"}
        case "retry":
            state["last_error"] = None
            return {"_next_action": "plan_stage"}
        case "skip":
            _fill_skipped_outputs(state)
            state.pop("last_error", None)
            return {"_next_action": "advance"}
        case "fail":
            state["status"] = "blocked"
            _notify_fail(state)
            return {"_next_action": "__end__"}
        case "wait_confirm":
            _write_gate_report(state)
            state["status"] = "paused"
            db.update_story(state["story_key"], status="paused", ...)
            interrupt({"reason": "waiting_for_confirmation"})
            # 恢复后
            s = db.get_story(state["story_key"])
            if s and s["status"] == "active":
                state["status"] = "active"
                state["execution_count"] = 0
                override = _get_gate_override(s)
                if override == "accept_risk_advance":
                    return _execute_action(state, "advance")
            return {"_next_action": "plan_stage"}
```

### execute_and_wait_node

```python
def execute_and_wait_node(state: StoryState) -> StoryState:
    stage = state["current_stage"]
    workspace = state["workspace"]
    key = state["story_key"]

    # 幂等检查
    done_file = stage_done_file(workspace, key, stage)
    if done_file.exists():
        state["context"].update(robust_json_parse(done_file))
        done_file.unlink(missing_ok=True)
        return state

    # 渲染 prompt + dispatch tool
    prompt = _build_prompt(state)
    tool = get_tool(state.get("plan", {}).get("tool", "stage_tool"))
    tool.execute(state, {...})

    # 轮询等待（原 poll_completion 逻辑）
    while True:
        if _is_cancelled(state):
            return state
        if done_file.exists():
            data = robust_json_parse(done_file)
            done_file.unlink(missing_ok=True)
            state["context"].update(data)
            ttyd.clear_launch_state(key)
            return state
        # 检查 session/exit 状态...
        interrupt({"reason": "waiting_for_done_file", "stage": stage})
```

### advance_node（基本不变）

保留为独立节点，因为逻辑复杂：validate_stage_outputs + check_dod + resolve_next_stage + source sync + DB 更新。

### StoryState 精简

移除路由专用字段，改用 `_next_action` 统一：

```python
class StoryState(TypedDict, total=False):
    # 业务字段（保留）
    story_key: str
    title: str
    workspace: str
    profile: str
    current_stage: str
    status: str
    complexity: str
    context: dict
    execution_count: int
    last_error: Optional[str]
    stage_start_time: float
    plan_summary: Optional[str]
    review_summary: Optional[str]
    trajectory_score: Optional[float]
    plan: Optional[dict]

    # 路由字段（精简为 2 个）
    _next_action: Optional[str]       # router 的输出，决定下一条边
    _cancelled: bool
    _epoch: int

    # 移除：_pending_sub_keys → Layer 3 处理
    # 移除：_router_decision → 不再持久化 LLM 返回值
    # 移除：_pre_routed_action → 合并进 router 内部逻辑
```

### 涉及文件

- 重写：`orchestrator/graph.py`（build_graph + run_story）
- 重写：`orchestrator/nodes/graph_nodes.py`（合并节点）
- 重写：`orchestrator/nodes/routing.py`（新路由逻辑）
- 修改：`orchestrator/nodes/state.py`（精简 StoryState）
- 不变：`orchestrator/nodes/profile_loader.py`, `orchestrator/nodes/prompt_renderer.py`, `orchestrator/nodes/stage_resolver.py`, `orchestrator/nodes/knowledge.py`, `orchestrator/nodes/json_helpers.py`
- 不变：`orchestrator/validation.py`, `orchestrator/quality.py`, `orchestrator/gate.py`, `orchestrator/notify.py`, `orchestrator/observability.py`, `orchestrator/paths.py`

---

## Step 3: Layer 2 对抗循环子图

### 现状

- Plan Loop：`run_plan_loop()` 是 evaluator_loop.py 中的 while 循环 + 函数调用
- Code Loop：`run_code_review_loop()` 是单次调用，通过 graph retry 实现多轮

### 改造：建模为 LangGraph 子图

```python
# src/story_lifecycle/orchestrator/adversarial_graph.py

def build_adversarial_graph(loop_type: Literal["plan", "code"]) -> CompiledStateGraph:
    """构建对抗循环子图"""
    graph = StateGraph(AdversarialState)

    graph.add_node("planner", adversarial_planner_node)
    graph.add_node("reviewer", adversarial_reviewer_node)
    graph.add_node("judge", judge_node)

    graph.add_edge(START, "planner")
    graph.add_edge("planner", "reviewer")
    graph.add_edge("reviewer", "judge")
    graph.add_conditional_edges("judge", route_judge, {
        "pass": END,
        "revise": "planner",
        "no_progress": END,    # 输出 decision=no_progress
        "max_rounds": END,     # 输出 decision=max_rounds
    })

    return graph.compile(checkpointer=SqliteSaver(conn))


class AdversarialState(TypedDict):
    story_state: dict           # 父图传下来的完整 state
    round: int
    max_rounds: int
    plan: Optional[dict]        # planner 输出
    review: Optional[dict]      # reviewer 输出
    prev_blockers: list[dict]   # 上一轮 blockers（用于 no-progress 检测）
    decision: Optional[str]     # pass / revise / no_progress / max_rounds
```

### 嵌入主图

```python
# plan_stage_node 中
if adv_cfg.plan_loop_enabled(stage):
    adversarial = build_adversarial_graph("plan")
    result = adversarial.invoke(
        {"story_state": state, "round": 0, "max_rounds": adv_cfg.plan_loop.max_rounds, ...},
        config={"configurable": {"thread_id": f"{story_key}:{stage}:plan_loop"}},
    )
    # 根据 result["decision"] 决定后续
```

```python
# review_stage_node 中
if adv_cfg.code_loop_enabled(stage):
    adversarial = build_adversarial_graph("code")
    result = adversarial.invoke(
        {"story_state": state, "round": review_round_count, "max_rounds": adv_cfg.code_loop.max_rounds, ...},
        config={"configurable": {"thread_id": f"{story_key}:{stage}:code_loop"}},
    )
```

### 好处

1. 每轮 checkpoint 独立持久化 — 崩溃恢复粒度更细
2. 子图可视化 — 调试时能看到每轮 planner/reviewer 的中间状态
3. 对抗循环的 token 使用和延迟可以独立追踪

### 涉及文件

- 新建：`orchestrator/adversarial_graph.py`
- 修改：`orchestrator/evaluator_loop.py`（保留 LoopResult / detect_no_progress / build_repair_packet，移除 run_plan_loop / run_code_review_loop）
- 修改：`orchestrator/nodes/graph_nodes.py`（plan_stage_node 和 review_stage_node 调用子图）

---

## Step 4: Layer 3 Fan-out 子任务

### 现状

`_delegate_subtasks()` 创建子任务 DB 记录 → `interrupt()` 挂起父任务 → `_run_story_impl` 尾部手动 `start_story_async()` 投线程池。

### 改造：LangGraph Send API

```python
from langgraph.types import Send

def plan_stage_node(state: StoryState):
    plan = planner.plan_stage(state, cfg, adapters)

    if plan.get("split") and plan.get("subtasks"):
        # Fan-out：为每个子任务生成 Send
        sends = []
        for sub in plan["subtasks"]:
            sub_key = f"{state['story_key']}-{sub['key_suffix']}"
            sub_state = {
                **state,
                "story_key": sub_key,
                "title": sub.get("title", ""),
                "plan": {"extra_instructions": sub.get("summary", ""), ...},
                "status": "blocked" if sub.get("depends_on") else "active",
            }
            sends.append(Send("plan_stage", sub_state))
        return sends  # LangGraph 自动并行执行

    # ... normal plan flow
```

### Fan-in：合并子任务结果

```python
def merge_subtasks(states: list[StoryState]) -> StoryState:
    """所有子任务完成后自动调用"""
    parent_state = states[0]  # 用第一个子任务的基础 state
    results = [
        {"key": s["story_key"], "status": s["status"], "stage": s["current_stage"]}
        for s in states
    ]
    parent_state["subtask_results"] = results
    parent_state["status"] = "active"
    # 如果有子任务失败，标记
    failed = [r for r in results if r["status"] == "blocked"]
    if failed:
        parent_state["last_error"] = f"{len(failed)} subtask(s) failed"
    return parent_state
```

### 依赖处理

```python
# 简单方案：depends_on 的子任务不加入 Send，等前置完成后手动 start
# 复杂方案：在 graph 层面建模为有向无环图 (DAG)
# MVP 先用简单方案
```

### 好处

1. 不需要 ThreadPoolExecutor — LangGraph 调度并行
2. 不需要 interrupt + Watchdog — LangGraph 自动等待 fan-in
3. Checkpoint 覆盖所有子任务

### 涉及文件

- 修改：`orchestrator/nodes/graph_nodes.py`（plan_stage_node 返回 Send）
- 修改：`orchestrator/graph.py`（build_graph 添加 fan-out/fan-in 边）
- 修改：`orchestrator/nodes/subtask_delegate.py`（简化为 Send 参数构建）
- 移除：`graph.py` 中 `_run_story_impl` 尾部的手动 `start_story_async` 逻辑

---

## 实施顺序

```
Step 1: LLMClient        (独立，无依赖)     → 1-2 天
Step 2: Layer 1 精简图    (依赖 Step 1)     → 2-3 天
Step 3: Layer 2 子图      (依赖 Step 2)     → 1-2 天
Step 4: Layer 3 fan-out   (依赖 Step 2)     → 1-2 天
```

每步完成后跑 `story demo` 验证核心流程不变。

## 验证方案

每个 Step 完成后：

1. `pip install -e .` 安装
2. `story doctor` 检查依赖
3. `story demo` 跑完整 demo 流程
4. 确认 plan → execute → review → advance 全链路通过
5. 确认 adversarial loop (strict profile) 的 retry + no_progress 逻辑正常
6. `ruff check src/` 无 lint 错误
