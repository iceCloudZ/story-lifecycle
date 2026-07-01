> ⚠️ **历史快照（归档于 2026-07）**：描述的架构可能已被后续演进取代。当前架构见 [../../ARCHITECTURE.md](../../ARCHITECTURE.md)。本文件保留作决策记录（ADR），正文未修改。

---

# 设计文档：Agent 模式重构 — 去掉 LangGraph，用 Function Calling 驱动全流程

> **版本**: v1.0  
> **日期**: 2026-06-13  
> **状态**: 待评审  
> **作者**: 项目团队

---

## 1. 背景与动机

### 1.1 项目现状

Story Lifecycle Manager 是一个 AI 编排系统，核心功能是将 TAPD 需求通过多阶段工作流（design → implement → test）自动完成开发。

当前架构使用 LangGraph 构建状态机，节点包括：

```
plan_stage_node       → LLM 生成规划 JSON 文本
execute_and_wait_node → 启动 CLI（claude/codex），等待完成
poll_completion       → 轮询 .story-done/{stage}.json 握手文件
router_node           → LLM 或规则判断下一步（advance/retry/skip/fail）
advance_node          → 推进到下一阶段
```

### 1.2 发现的问题

经过实际使用，我们识别出以下核心问题：

**P1: 编排 LLM 输出冗余，职责错位**

编排 LLM（DeepSeek）被当作"架构师"使用，生成的规划是一篇详细的设计文档（数千字），包含数据模型设计、API 设计、测试策略等。但编排 LLM 不应该做具体设计——这是 CLI（Claude/Codex）的能力。编排 LLM 应该只做"用什么工具、做什么阶段、关注什么要点"的决策。

**P2: 文本输出无法直接执行**

LLM 返回 JSON 文本，系统需要：解析 JSON → 校验字段 → 转换成 CLI 启动参数 → 处理解析失败。中间环节多且脆弱。应该让 LLM 直接"调用工具"（function calling），系统拿到结构化的 tool_calls 即可执行。

**P3: 用户无法控制和干预**

LangGraph 自动跑完整个状态机，用户看不到规划过程，无法在关键节点干预。应该让用户看到 Agent 的决策过程（规划了哪些步骤），确认后才执行。

**P4: LangGraph 增加了不必要的复杂度**

LangGraph 提供了 checkpoint、conditional edge、sub-graph 等复杂机制，但我们的场景本质上是一个简单的"规划 → 执行 → 检查 → 决策"循环。LangGraph 的概念模型（节点、边、状态 TypedDict）反而让流程更难理解和调试。

### 1.3 行业调研

我们调研了行业内成熟的 Agent 编排系统，确认了设计方向：

#### AWS CLI Agent Orchestrator (CAO)

AWS 开源的分层多 Agent 编排框架，核心设计：

- **Hub-and-Spoke 拓扑**：Supervisor Agent 协调多个 Worker Agent，Worker 之间不直接通信
- **三种编排模式**：Handoff（同步等待完成）、Assign（异步并行）、SendMessage（直接通信）
- **Session 隔离**：每个 Worker 运行在独立 tmux session
- **上下文隔离**：Supervisor 只给 Worker 必要的上下文，避免污染

> 参考：https://aws.amazon.com/blogs/opensource/introducing-cli-agent-orchestrator-transforming-developer-cli-tools-into-a-multi-agent-powerhouse/

#### Augment Code: 多 Agent 编排架构分析

对生产级多 Agent 系统的结构化分析，识别出四个原语：

| 原语 | 作用 | 关键数据 |
|------|------|----------|
| 分解 (Decomposition) | 高层目标 → 子任务 DAG | HTN 规划生成依赖图 |
| 路由 (Routing) | 子任务 → Agent 分配 | 路由开销 <50ms，LLM 推理 2-15s |
| 状态 (State) | Agent 边界间传递上下文 | Living spec 是最可靠的状态载体 |
| 恢复 (Recovery) | 检测 → 重试 → 重规划 → 升级 | Schema validation gates 防止错误级联 |

关键结论：**Hub-and-Spoke 拓扑最适合 spec-driven 场景**（我们的场景正是这种）。路由开销相对 LLM 推理可以忽略。

> 参考：https://www.augmentcode.com/guides/multi-agent-orchestration-architecture-guide

#### Anthropic: Claude Code Agent Teams

Anthropic 官方的多 Agent 编排方案：

- 一个 session 做 **team lead**，协调其他 session
- 支持 sub-agent，每个 agent 有专门能力
- 通过共享文件（如 `claude-progress.txt`）做上下文传递

> 参考：https://code.claude.com/docs/en/agent-teams

#### DeepSeek Function Calling

我们使用的编排 LLM（DeepSeek）支持 OpenAI 兼容的 function calling：

- 标准 `tools` + `tool_choice` 参数格式
- 支持 multi-turn（tool role 消息喂回）
- 支持 structured output

> 参考：https://api-docs.deepseek.com/guides/function_calling

---

## 2. 架构设计

### 2.1 核心理念

**一句话总结**：用 DeepSeek 的 Function Calling 驱动一个 Supervisor Agent，替代 LangGraph 状态机。Agent 通过工具调用来规划、执行、监控整个开发生命周期。

### 2.2 目标架构

```
┌────────────────────────────────────────────────────────────────┐
│                     Supervisor Agent                            │
│                     (DeepSeek + Function Calling)               │
│                                                                  │
│  System Prompt:                                                  │
│    "你是开发任务编排 Agent。根据需求信息，用工具规划并执行开发    │
│     流程。你可以调用工具来规划步骤、启动 CLI、检查完成状态。      │
│     规划阶段完成后暂停，等待用户确认后再执行。"                   │
│                                                                  │
│  可用工具:                                                        │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │ plan_step(adapter, stage, focus, done_file)                │ │
│  │   → 规划一个执行步骤，返回确认后由系统执行                   │ │
│  │                                                             │ │
│  │ launch_cli(adapter, stage, focus, workspace)                │ │
│  │   → 立即启动 CLI 执行（执行阶段使用）                        │ │
│  │                                                             │ │
│  │ check_done_file(path)                                       │ │
│  │   → 检查 CLI 是否写入了完成信号文件                           │ │
│  │                                                             │ │
│  │ skip_stage(reason)                                          │ │
│  │   → 跳过当前阶段                                            │ │
│  │                                                             │ │
│  │ mark_complete(summary, files_changed)                       │ │
│  │   → 标记阶段完成，记录产出                                   │ │
│  │                                                             │ │
│  │ mark_failed(error)                                          │ │
│  │   → 标记阶段失败，触发错误处理                               │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                  │
│  Agent 循环:                                                      │
│    while not finished:                                            │
│      response = llm.chat(messages, tools)                         │
│      for tool_call in response.tool_calls:                        │
│        if 需要用户确认 → 暂停，推送 tool_call 到前端               │
│        else → 执行工具，结果喂回 messages                         │
└──────────────────────────────────────────────────────────────────┘
         │                              │
         │ SSE 实时推送                  │ 直接调用
         ▼                              ▼
┌─────────────────┐         ┌──────────────────────┐
│   前端详情页      │         │  Worker Agent (CLI)   │
│                   │         │  Claude / Codex       │
│  结构化步骤卡片    │         │  独立 PTY session     │
│  确认/重新规划    │         │  写 .story-done 文件   │
│  实时终端输出     │         │  完成后 handshake      │
└─────────────────┘         └──────────────────────┘
```

### 2.3 两阶段流程

Agent 的工作分为两个阶段，中间有用户确认点：

**阶段 1：规划（Planning）**

```
Agent 启动 → 调用 plan_step("codex", "design", "需求澄清、方案设计")
            → 调用 plan_step("claude", "implement", "按设计文档编码")
            → 不再调用工具（说完了）
            → 系统暂停，推送 action list 到前端
            → 用户看到结构化步骤卡片，点击「确认并执行」
```

**阶段 2：执行（Execution）**

```
用户确认 → Agent 恢复，遍历 action list
         → 调用 launch_cli("codex", "design", ...)
         → 调用 check_done_file(".story-done/xxx-design.json")
         → CLI 完成，拿到结果
         → 调用 mark_complete(summary, files_changed)
         → 调用 launch_cli("claude", "implement", ...)
         → ... 重复直到所有阶段完成
```

### 2.4 与现有架构的对比

| 组件 | 现在 (LangGraph) | 改造后 (Agent) |
|------|------------------|----------------|
| **规划** | `plan_stage_node` + 文本 JSON | Agent `plan_step` tool call |
| **执行** | `execute_and_wait_node` + LangGraph 边 | Agent `launch_cli` tool call |
| **轮询** | `poll_completion` 节点 | Agent `check_done_file` tool call |
| **路由** | `router_node` + LLM/规则判断 | Agent 自主决策（跳过/重试/失败） |
| **推进** | `advance_node` + 条件边 | Agent 循环自然推进 |
| **状态** | LangGraph `StateGraph` + checkpoint | DB `story` 表 + `context_json` |
| **恢复** | `recover_orphan_stories()` + checkpoint | DB 状态 + Agent 重新读取恢复 |
| **依赖** | `langgraph`, `langchain-core` | 无（直接用 httpx 调 OpenAI API） |

### 2.5 为什么可以去掉 LangGraph

LangGraph 解决的问题是：**复杂 DAG 工作流的状态管理**。但我们的场景不是复杂 DAG：

1. **流程是线性的**：design → implement → test，顺序执行，没有并行分支
2. **决策是简单的**：每步只有 advance/retry/skip/fail 四个选择
3. **状态已经有 DB 管理**：`story` 表存了所有状态，LangGraph checkpoint 是重复存储
4. **恢复机制已有**：`recover_orphan_stories()` 从 DB 恢复，不依赖 checkpoint

去掉 LangGraph 后：
- **减少依赖**：移除 `langgraph` 和 `langchain-core` 两个包
- **降低复杂度**：不需要理解 StateGraph、conditional edge、checkpoint
- **更好的调试**：Agent 循环是普通的 Python while 循环，可以断点调试
- **更灵活**：Agent 可以在任意步骤做任意决策，不受预定义边的约束

---

## 3. 实施方案

### 3.1 LLMClient 加 Tool Calling 支持

**文件**: `src/story_lifecycle/llm_client.py`

在现有 `invoke`/`stream` 方法基础上，新增 `invoke_with_tools`：

```python
def invoke_with_tools(
    self,
    messages: list[dict],       # 完整 messages 历史（支持多轮对话）
    tools: list[dict],           # OpenAI function calling 格式的工具定义
    *,
    tool_choice: str = "auto",   # "auto" | "none" | {"type": "function", "function": {"name": "..."}}
    temperature: float = 0.1,
    timeout: int = 90,
) -> dict:
    """
    调用 LLM 并返回 tool_calls。
    
    返回格式:
    {
        "message": {"role": "assistant", "content": "...", "tool_calls": [...]},
        "tool_calls": [
            {"id": "call_xxx", "type": "function", "function": {"name": "plan_step", "arguments": "{...}"}}
        ],
        "content": "..."  # 纯文本内容（如果有）
    }
    """
```

实现要点：
- `_build_body` 扩展，支持 `tools` 和 `tool_choice` 参数
- 响应解析提取 `choices[0].message.tool_calls`
- 兼容 DeepSeek 的 function calling 格式（与 OpenAI 一致）

### 3.2 定义编排工具集

**文件**: `src/story_lifecycle/orchestrator/agent_tools.py`（新建）

```python
ORCHESTRATOR_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "plan_step",
            "description": "规划一个执行步骤。规划阶段使用，返回后由用户确认。",
            "parameters": {
                "type": "object",
                "properties": {
                    "adapter":   {"type": "string", "enum": ["claude", "codex"], "description": "CLI 工具"},
                    "stage":     {"type": "string", "description": "阶段名称"},
                    "focus":     {"type": "string", "description": "2-3 个关键要点"},
                    "done_file": {"type": "string", "description": "完成信号文件路径"},
                },
                "required": ["adapter", "stage", "focus"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "launch_cli",
            "description": "启动 CLI 工具执行指定阶段。执行阶段使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "adapter":  {"type": "string", "enum": ["claude", "codex"]},
                    "stage":    {"type": "string"},
                    "focus":    {"type": "string"},
                    "prompt":   {"type": "string", "description": "给 CLI 的执行指令"},
                },
                "required": ["adapter", "stage", "focus"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "check_done_file",
            "description": "检查 CLI 是否已完成（检查 .story-done 文件）",
            "parameters": {
                "type": "object",
                "properties": {
                    "path":     {"type": "string", "description": "done file 路径"},
                    "timeout":  {"type": "integer", "description": "超时秒数", "default": 1800},
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "skip_stage",
            "description": "跳过不需要的阶段",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "mark_complete",
            "description": "标记当前阶段完成",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary":      {"type": "string"},
                    "files_changed": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["summary"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "mark_failed",
            "description": "标记当前阶段失败",
            "parameters": {
                "type": "object",
                "properties": {"error": {"type": "string"}},
                "required": ["error"]
            }
        }
    },
]
```

### 3.3 编排 Agent 循环

**文件**: `src/story_lifecycle/orchestrator/planner.py`

新增 `run_orchestrator_agent` 函数，替代整个 LangGraph 状态机：

```python
def run_orchestrator_agent(story_key: str) -> dict:
    """Supervisor Agent：规划 → 确认 → 执行 → 监控 全流程"""
    
    # 1. 加载 story 上下文
    story = db.get_story(story_key)
    messages = [_build_system_prompt(), _build_user_message(story)]
    
    # 2. 规划阶段
    actions = []
    for _ in range(10):  # 最大 10 轮
        resp = llm.invoke_with_tools(messages, ORCHESTRATOR_TOOLS)
        messages.append(resp["message"])
        
        if not resp.get("tool_calls"):
            break
        
        for call in resp["tool_calls"]:
            name = call["function"]["name"]
            args = json.loads(call["function"]["arguments"])
            
            if name == "plan_step":
                actions.append({"action": "launch", **args})
                _notify_frontend("action", call)  # SSE 推送
            
            elif name == "skip_stage":
                actions.append({"action": "skip", **args})
                _notify_frontend("action", call)
            
            # 喂回确认
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": "已记录"})
    
    # 3. 等待用户确认（写入 DB，前端轮询）
    db.update_story(story_key, status="planning", context_json={"actions": actions, "plan_confirmed": False})
    _notify_frontend("done", {"actions": actions})
    
    # === 此时 Agent 暂停，等待用户确认 ===
    # 确认后由 API 端点调用 continue_orchestrator_agent()
    
    return {"status": "planning", "actions": actions}
```

```python
def continue_orchestrator_agent(story_key: str):
    """用户确认后，执行 action list"""
    story = db.get_story(story_key)
    ctx = json.loads(story["context_json"])
    actions = ctx.get("actions", [])
    
    for action in actions:
        if action["action"] == "skip":
            db.log_event(story_key, action["stage"], "skipped", {"reason": action["reason"]})
            continue
        
        if action["action"] == "launch":
            # 启动 CLI
            adapter = get_adapter(action["adapter"])
            workspace = story["workspace"]
            pty.ensure_agent_pty(story_key, adapter.interactive_launch_cmd(), workspace, action["focus"])
            
            # 轮询：同时检查 done file 和 wait file
            done_path = Path(workspace) / action.get("done_file", f".story-done/{story_key}-{action['stage']}.json")
            wait_path = Path(workspace) / f".story-wait/{story_key}-{action['stage']}.json"
            
            while True:
                # 1. CLI 完成了
                if done_path.exists():
                    result = _consume_done_file(done_path)
                    db.log_event(story_key, action["stage"], "completed", result)
                    break
                
                # 2. CLI 等待用户确认（human-in-the-loop）
                if wait_path.exists():
                    question = json.loads(wait_path.read_text())
                    # 推送给前端，等待用户回答
                    _notify_frontend("wait_for_input", question)
                    # 暂停执行，等前端调用 /answer API
                    answer = _wait_for_answer(story_key, action["stage"], timeout=3600)
                    # 把回答写回，CLI 读取后继续
                    answer_path = wait_path.with_suffix(".answer.json")
                    answer_path.write_text(json.dumps({"answer": answer}, ensure_ascii=False))
                    wait_path.unlink()  # 删除 wait 文件
                    continue
                
                time.sleep(2)
    
    db.update_story(story_key, status="completed")
```

### 3.4 Human-in-the-Loop: CLI ↔ Agent ↔ 用户

CLI 执行过程中可能需要用户确认（如方案选择、架构决策、安全操作确认）。
通过文件握手协议实现三方可信通信：

```
CLI (Claude/Codex)                    Agent (编排 LLM)                前端 (用户)
     │                                    │                              │
     │ 执行中遇到需要确认的点              │                              │
     │                                    │                              │
     ├─ 写 .story-wait/{stage}.json ─────►│                              │
     │  {                                 │                              │
     │    "question": "数据库表设计        │                              │
     │     用哪种方案？",                  │                              │
     │    "options": [                     │                              │
     │      "A: 单表",                     │                              │
     │      "B: 分表"                      │                              │
     │    ],                               │                              │
     │    "context": "当前需求涉及..."      │                              │
     │  }                                  │─ SSE 推送 ──────────────────►│
     │                                    │  {type: "wait_for_input",    │
     │                                    │   question, options}         │
     │                                    │                              │
     │                                    │                              │─ 用户看到问题
     │                                    │                              │  选择/输入回答
     │                                    │                              │
     │                                    │◄── POST /api/.../answer ─────│
     │                                    │  {answer: "A: 单表"}         │
     │                                    │                              │
     │◄── .story-wait/{stage}.answer.json─│                              │
     │  {answer: "A: 单表"}               │                              │
     │                                    │                              │
     │ CLI 读取回答，继续执行               │                              │
     │ ...                                │                              │
     │                                    │                              │
     ├─ 写 .story-done/{stage}.json ─────►│                              │
     │  {stage, status, summary}          │─ SSE 推送完成 ──────────────►│
     │                                    │                              │
```

**握手协议细节**：

| 文件 | 写入方 | 读取方 | 内容 |
|------|--------|--------|------|
| `.story-wait/{story}-{stage}.json` | CLI | Agent | 问题、选项、上下文 |
| `.story-wait/{story}-{stage}.answer.json` | Agent | CLI | 用户回答 |
| `.story-done/{story}-{stage}.json` | CLI | Agent | 阶段产出（已有） |

**CLI 侧约定**（注入到 prompt 中）：

```
## 等待用户确认协议
当你需要用户确认某个决策时：
1. 写文件 .story-wait/{story_key}-{stage}.json，包含：
   {"question": "问题内容", "options": ["选项1", "选项2"], "context": "背景说明"}
2. 等待 .story-wait/{story_key}-{stage}.answer.json 出现
3. 读取用户回答，继续执行
```

这个机制让 Agent 成为 CLI 和用户之间的桥梁：
- CLI 不需要直接和用户交互（它在 PTY 里运行）
- Agent 负责中转问题和回答
- 前端提供用户友好的交互界面

### 3.5 API 端点

### 3.4 API 端点

**文件**: `src/story_lifecycle/orchestrator/api.py`

| 端点 | 方法 | 作用 |
|------|------|------|
| `/api/story/{key}/plan/stream` | GET (SSE) | 启动 Agent 规划，实时推送 tool_calls |
| `/api/story/{key}/plan/confirm` | POST | 用户确认规划，开始执行 action list |
| `/api/story/{key}/plan` | GET | 获取当前 action list |
| `/api/story/{key}/plan/regenerate` | POST | 重新规划 |
| `/api/story/{key}/answer` | POST | 用户回答 CLI 的等待确认问题 |
| `/api/story/{key}/wait` | GET | 获取当前 CLI 等待确认的问题 |

### 3.5 前端结构化卡片

**文件**: `frontend/src/pages/StoryDetailPage.tsx`

规划阶段展示：
- EventSource 接收每个 action 事件
- 每个 `plan_step` 渲染为步骤卡片
- 卡片内容：阶段名、adapter 图标、focus 要点、done_file 路径
- `skip_stage` 渲染为灰色删除线

执行阶段展示：
- 每个 action 对应一个终端 tab
- 实时显示 CLI 输出
- 完成后卡片变绿，失败变红

### 3.6 清理 LangGraph

移除的文件/依赖：
- `src/story_lifecycle/orchestrator/graph.py` — 整个文件（StateGraph 定义）
- `src/story_lifecycle/orchestrator/nodes/graph_nodes.py` — LangGraph 节点实现
- `src/story_lifecycle/orchestrator/nodes/state.py` — StoryState TypedDict
- `pyproject.toml` 中的 `langgraph` 和 `langchain-core` 依赖

保留的模块：
- `planner.py` — 改造为 Agent 循环
- `router.py` — 决策逻辑可复用到 Agent system prompt
- `nodes/prompt_renderer.py` — prompt 渲染逻辑
- `nodes/profile_loader.py` — profile 解析
- `adapters/` — CLI adapter 模式不变
- `terminal/` — PTY/ttyd 管理不变
- `db/models.py` — DB 操作不变

---

## 4. 迁移策略

### 4.1 分步迁移

| 步骤 | 内容 | 可独立验证 |
|------|------|------------|
| 1 | `LLMClient.invoke_with_tools` | 单元测试 tool calling |
| 2 | `agent_tools.py` 工具定义 | 验证 DeepSeek 能正确识别工具 |
| 3 | `planner.py` Agent 规划循环 | 端到端：规划 → action list |
| 4 | 前端结构化卡片 | SSE 接收 + 渲染 |
| 5 | Agent 执行循环（launch_cli + poll） | 确认后 CLI 正常启动 |
| 6 | 清理 LangGraph 依赖 | 移除旧代码，全流程回归 |

### 4.2 风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| DeepSeek tool calling 不稳定 | Agent 无法正确调用工具 | 保留 `invoke_structured` 作为 fallback；测试 DeepSeek V3.1 的 tool calling |
| Agent 无限循环 | Token 消耗 | 最大 10 轮硬限制；per-tool-call 超时 |
| CLI 握手文件丢失 | Agent 不知道 CLI 是否完成 | 保留现有 `.story-done` 协议不变；增加超时和重试 |
| 去掉 LangGraph 后恢复机制丢失 | 服务重启后 story 丢失 | DB 已有完整状态；重启后从 DB 恢复 story 状态 |
| 前端改动范围大 | UI 回归 | 分步改，规划阶段先跑通再加执行阶段 |
| CLI 不写 wait 文件 | human-in-the-loop 不生效 | wait 文件是可选机制，CLI 不写就不等待；逐步让 CLI 支持 |
| wait/answer 文件竞争 | CLI 还没读 answer，Agent 就删了 wait | 用 atomic rename；answer 存在才删 wait |

---

## 5. 验证标准

1. **Tool Calling 基础**：`invoke_with_tools` 正确返回 `tool_calls`（不是纯文本）
2. **规划阶段**：Agent 自主决定用 codex 做 design、claude 做 implement
3. **SSE 流式**：每个 tool_call 实时推送到前端
4. **结构化展示**：前端显示步骤卡片（不是 markdown 文本）
5. **用户确认**：确认后 CLI 正常启动
6. **执行阶段**：CLI 完成后写入 done file，系统检测到并推进下一步
7. **去除依赖**：`pip install` 后不包含 langgraph 和 langchain-core
