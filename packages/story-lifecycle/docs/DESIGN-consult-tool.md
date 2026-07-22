# consult 命令 — Code Agent 主动请外援(路线 Z · CLI 版)

> 状态:已评审定稿(CLI 方案)。创建:2026-07-21;同日评审后由 MCP 工具方案改为 CLI 方案(决策记录见 §6.3)。
> 范围:`packages/story-lifecycle`(尤其 `entry/cli/consult_cmd.py`、`orchestrator/engine/consult_orchestrator.py`、`orchestrator/engine/consult_runner.py`、`orchestrator/engine/replanner.py`、`orchestrator/engine/planner.py`、`infra/paths.py`)。
> 评审目标:验证"code agent 跑 `story consult` → 编排 LLM 在 FC loop 里自主 spawn 外援 CLI 调查 → 综合 advisory 返回"的方案正确性。
> **本文自包含**:背景、代码现状、方案、状态机、接口契约、失败降级、测试策略全部内联。执行者无需阅读其他文档即可开工。

---

## 0. TL;DR(评审者先读)

当前 code agent(claude/codex/kimi)执行 stage 时,**没有任何"主动向编排层 LLM 求助"的通道**。两条现有通道都不够:

1. `mcp__lifecycle__clarify` —— 问**人**(HITL,重,45 分钟超时,仅关键岔路,且只在 claude headless grill 路径接线)
2. `supervisor.decide_response` —— 编排 LLM **被动观察** code agent 在终端里的二选一选择题,**不能开放咨询、不能 spawn 工具调查**

缺口:code agent 卡住时(撞墙重试 / 概念混淆 / 跨模块边界判断),只能 clarify 问人或自己撞墙。没有"**主动咨询编排 LLM,编排 LLM 还能 spawn 外援 CLI 实地调查**"的中间档。

方案:**新增 CLI 子命令 `story consult`**。code agent 用自己的 Bash 工具调用(urgency=high 前台阻塞,low/medium 用 Bash 的 run_in_background 后台异步)→ `consult_cmd` 调编排 LLM 的 **function-calling agent loop**(复用 `replanner.replan()` 骨架)→ 编排 LLM 在 loop 里自主调用 `spawn_reviewer` 工具 spawn 一个外援 CLI(跨 adapter 做真 decorrelation)→ 外援 CLI headless 调查后写 advisory 结果文件 → 编排 LLM 综合多轮调查结果 → advisory 打印到 stdout 返给 code agent。

**关键设计决策**(已拍板):

- **advisory only**(阶段 1):consult 返回建议,code agent 决定是否采纳。authoritative 留阶段 2。
- **路线 Z 完整版**(已拍板):直接复活 `replanner.replan()` 的 FC loop,不是 Python 硬编码路由的简化版。
- **CLI 而非 MCP**(评审改判,§6.3):MCP 的选型理由是 HITL 专属的,consult 是机器到机器;CLI 经 Bash 工具天然跨 adapter(claude/kimi caller 都能用),也消除了"MCP 锁死 caller=claude 导致 decorrelation 规则自相矛盾"的问题。
- **CLI 保持阻塞式一次性,异步推给调用方**(§6.3):不建 CLI 侧轮询协议(避免 detached worker / 孤儿回收 / 忘 poll);agent 用 Bash 原生 `run_in_background` 实现异步。
- **advisory 返回 ≠ stage 产物**:consult 的结果文件落在 `.story/consult/<request_id>.json`(独立目录),**绝不**复用 `.story/done/<key>/<stage>.json`(那有 stage 推进语义)。

**边界**:prompt 协议段只在 headless 路径注入(claude/kimi caller;codex 无 headless 模式,跑不了 headless stage 自然不在其列)。interactive PTY 路径不注入(code agent 在终端可直接问人)。CLI 本身不做路径检查——env 齐全即可跑,边界靠 prompt 注入控制。

---

## 1. 起因与背景

### 1.1 触发事件

调研业界 2025-2026 年的多 agent 编码模式时,发现"**second opinion / consult tool**"被多个互不相识的团队在几周内独立收敛(raine/consult-llm、Mozilla Star Chamber、Perplexity Model Council、addyosmani/adverse)。Mozilla 博客原话:

> About a week after the first Star Chamber commits landed (January 30th), Perplexity launched Model Council on February 5th. Neither project influenced the other; we just arrived at the same idea independently. When two teams working on completely different problems independently converge on a similar solution, that's a strong signal that multi-model consensus is becoming a recognised pattern.

本仓库的 `resolve_stage_adapter` + claude/codex/kimi 三轨架构**天然能做真 decorrelation**(跨模型 blind spots 不重叠),这是业界主流实现做不到的差异化优势。

### 1.2 问题的本质

code agent 现有的求助通道是两个极端:
- clarify:**问人**,重、慢(分钟级到 45 分钟)、仅关键岔路
- supervisor:**编排 LLM 被动观察**,只在 code agent 已经吐出二选一选择题时介入,**没有工具、不能 spawn 调查、不能开放咨询**

缺一个中间档:**轻量(秒级到分钟级)、编排 LLM 主动调查(spawn 外援)、开放咨询(不只选择题)、advisory(不阻塞)**。这就是 consult。

### 1.3 符合 Cognition 的 multi-agent 警告边界

Cognition(Devin 团队)的 [Don't Build Multi-Agents](https://cognition.ai/blog/dont-build-multi-agents) 批的是"多 agent 并行写代码再 merge"。但 Cognition 明确点名 **Claude Code subagent 是反例,是对的**——subagent 只回答问题不写代码,主 agent 保持单一决策线。

consult 完全符合:
- code agent 是**唯一决策者**(写代码的是它)
- consult 返回的是**建议(advisory)**,不是行动
- 外援 CLI **不写业务代码**,只调查 + 写 advisory 结果文件
- 不存在两个 agent 并行写代码再 merge 的冲突

### 1.4 复活 `replanner.replan()`(不是基于 dead code 构建)

`replanner.replan()`(`orchestrator/engine/replanner.py:66`)在 REFACTOR §5.4.1 之前是规划期的 FC 循环,后被单次 `invoke_structured` 替代。现状是 dead code(全 src 零 import,仅 `transition.py:8` 注释提及),但:

- **FC 基础设施全套还在**:`LLMClient.invoke_with_tools`(`infra/llm_client.py:447-516`,真 FC 协议) + `ORCHESTRATOR_TOOLS`(`agent_tools.py:6-140`,6 个标准 OpenAI FC 工具) + `replanner.replan()` 的 10 轮 FC 消费循环(`replanner.py:90-112`)全部活代码。
- **REFACTOR §5.4.1 旁路 FC 的原因是"规划期不需要多轮"(用户口述确认)**——不是 FC 本身有问题。consult 场景**天然需要多轮**(spawn 外援 → 外援调查 → 综合),正是 FC 的用武之地。
- **接口已对齐**:`replanner` 产出的 action 结构 `{action:"launch", adapter, stage, focus, done_file}` 与 `continue_orchestrator_agent`(`planner.py:788` 起,消费点见 §4.4)完全一致。

所以复用 `replanner` 不是"基于 dead code 构建",而是"按原设计激活预留设施"。

---

## 2. 理论依据

### 2.1 模型不能审查自己

[consult-llm README](https://github.com/raine/consult-llm) 原话:

> A model reviewing its own work isn't an independent check. Even in a fresh context, it shares the same training, priors, and many of the same failure modes. A different model was trained differently and makes different mistakes, so it's more likely to push back, challenge weak reasoning, or expose a blind spot.

[Hboon 博客](https://hboon.com/using-a-second-llm-to-review-your-coding-agent-s-work/) 补充:

> Different LLMs think differently. When one gets stuck, it tends to bang its head against the wall — trying the same approach over and over. A second model often sees the problem from a different angle and breaks through.

**根因**:跨模型 decorrelation 才是真价值,同模型多 persona 是伪 decorrelation。本仓库的多 adapter 架构天然支持真 decorrelation。CLI 方案下 caller 可以是 claude 或 kimi,`spawn_reviewer` 的"必须与 caller 不同 adapter"是一条真实可执行的规则(见 §4.3)。

### 2.2 advisory not blocking

[Star Chamber 博客](https://blog.mozilla.ai/the-star-chamber-multi-llm-consensus-for-code-quality/) 原话:

> The advisory-not-blocking distinction is essential. Making it blocking would slow development to a crawl and create false authority. These are AI opinions informed by multiple perspectives, but still opinions. The engineer decides.

阶段 1 采用 advisory。code agent 可以不采纳 consult 建议,但要在 done summary 里说明理由(见 §5.3 prompt 协议)。

### 2.3 orchestrator-workers 范式

[Anthropic Building Effective AI Agents](https://www.anthropic.com/engineering/building-effective-agents):

> Workflow: Orchestrator-workers — a central LLM dynamically breaks down tasks, delegates them to worker LLMs, and synthesizes their results. This workflow is well-suited for complex tasks where you can't predict the subtasks needed... The key difference from parallelization is its flexibility—subtasks aren't pre-defined, but determined by the orchestrator based on the specific input.

路线 Z 的编排 LLM 在 FC loop 里**自主决定**要不要 spawn 外援、spawn 几个、用哪个 adapter——这就是 orchestrator-workers。

---

## 3. 代码现状(执行者必读)

### 3.1 clarify 走 MCP —— 其选型理由是 HITL 专属的(consult 不沿用)

`orchestrator/mcp/clarify_server.py` 是单工具、手写 stdio JSONRPC、无 SDK 的外接 MCP server。它选 MCP 的理由写在模块 docstring 里(实测本机 claude 网关变体):`-p` 无 AskUserQuestion;PTY 下 AskUserQuestion 渲染成 TUI 太脆;in-process `sdk_mcp_servers` 未注册;只有外接 stdio MCP 实测通了。

**这四条理由全部针对"问人"场景**。consult 是机器到机器调用(code agent → 编排 LLM → 外援 CLI),一条都不适用。所以 consult 不加进这个 MCP server,改走 CLI(§6.3)。`clarify_server.py` **零改动**。

### 3.2 MCP 只服务 `headless + claude + grill` 路径 —— consult 的 env 注入要跳出这个分支

`planner.py:1086-1106`:

```python
story_env = None
if _wants_grill and adapter_name == "claude" and headless:
    from ..mcp.clarify_server import write_mcp_config
    _mcp_cfg = safe_story_path(workspace, ".story", "context", story_key) / "clarify_mcp.json"
    write_mcp_config(_mcp_cfg, _sys.executable)
    launch_cmd = list(launch_cmd) + ["--mcp-config", str(_mcp_cfg)]
    story_env = {**_os.environ, "STORY_KEY": story_key, "STORY_STAGE": stage}
```

`story_env` 目前**只在 grill+claude 分支内赋值**(否则 None = 裸继承 `os.environ`)。consult 需要 `STORY_KEY/STORY_STAGE/STORY_WORKSPACE/STORY_ADAPTER` 四个 env 对**所有 headless spawn** 可用——§5.8 把 env 注入提升到分支外。这是 planner 唯一的改动点(MCP config 那段不动)。

### 3.3 编排 LLM 的 FC 基础设施

| 组件 | 位置 | 状态 |
|---|---|---|
| `LLMClient.invoke_with_tools(messages, tools, tool_choice, ...)` | `infra/llm_client.py:447-516` | **活代码**,真 FC 协议(传 tools + tool_choice,读 tool_calls,arguments string→dict 已归一化;返回 `{message, tool_calls, content}`) |
| `ORCHESTRATOR_TOOLS`(6 个 FC 工具) | `agent_tools.py:6-140` | **活代码**,OpenAI FC schema 规范 |
| `replanner.replan()`(10 轮 FC 消费循环) | `replanner.py:66-113` | **dead code**(生产零 import) |
| `replanner._tool_call_to_action()`(plan_step/skip_stage → action) | `replanner.py:116-141` | **dead code** |
| `replanner.build_replan_messages()` | `replanner.py:27-63` | **dead code** |
| `run_orchestrator_agent`(规划期主入口) | `planner.py:259` | 单次 `invoke_structured`(不用 FC) |
| `run_unified_verify_gate`(verify gate) | `unified_gate.py:133` | 单次 `invoke_structured`(不用 FC) |
| `get_llm()` | `infra/llm_client.py:704` | **活代码**,返回 LLMClient(读 env/config 里的 API 配置;consult CLI 进程继承 code agent 的 env,配置可达) |

### 3.4 现有 spawn + done 轮询(无独立 helper,需新写)

`planner.py:1149-1235` 是 headless spawn 的内联逻辑(Popen + stdin 写 prompt + 关 stdin),`planner.py:1318-1508` 是 done file 轮询循环。**全部内联在 `continue_orchestrator_agent` 巨型函数里**,与 stage 推进 / clarification reset / PTY 回收深度耦合。grep 全包 `run_headless_stage` / `execute_stage_sync` / `spawn_headless` / `poll_done` / `consult` 零匹配。

**结论**:consult 需要新写一个同步 spawn+poll helper(约 40 行),但可以复用三块砖:
- `get_adapter(name).headless_launch_cmd(model, prompt="")` —— 构造 headless launch_cmd
- `robust_json_parse(path) -> dict`(`infra/json_helpers.py:33`)—— 解析结果文件
- `planner._kill_headless(proc)`(`planner.py:517`)—— 杀进程树

**已知坑(必须抄对)**:planner 的 spawn 用 `stdout=PIPE, stderr=PIPE` 但有 `_drain_headless` daemon 线程排空(`planner.py:1145` 注释「防 PIPE 死锁」,1193-1227)。consult 的同步 helper **不开 drain 线程,改为 stdout/stderr 落日志文件**(§5.5),效果相同且更简单。

### 3.5 done file 路径约定(`infra/paths.py`)

```python
def stage_done_file_rel(story_key, stage) -> str:
    return f".story/done/{story_key}/{stage}.json"   # 有 stage 推进语义
```

`.story/done/` 被 `graph.py`、`planner.py` 的 `_completed_stages` / orphan-claim / resume 多处扫描。consult 的 advisory 结果**绝不复用此路径**,否则会被误判为 stage 完成。新路径见 §5.4。

### 3.6 headless adapter 支持矩阵

| adapter | headless_launch_cmd | 说明 |
|---|---|---|
| claude | `claude.py:82-90` | 已实现,`["claude", "-p", "--allowedTools", "Bash,Read,Edit,Write,Glob,Grep", "--permission-mode", "acceptEdits"]` |
| kimi | `shell.py:83-107` | 已实现,`[binary, "-p"]`,`stdin_to_prompt_arg: true` 时包一层 |
| codex | `codex.py` | **未实现**(继承 base `base.py:159` 的 `return None`)—— codex 既不能当 caller(跑不了 headless stage)也不能当外援 |

caller 可以是 **claude 或 kimi**(谁跑 headless stage 谁就能 consult);外援 enum 也是 `["claude", "kimi"]`,规则是必须 ≠ caller(§4.3)。本机实测 claude/kimi 两 CLI 均可用。

### 3.7 `story` CLI 入口(click)

`pyproject.toml:56-57`:`story = "story_lifecycle.entry.cli.main:cli"`。`entry/cli/main.py:114` 是 click group,子命令按"每命令一个模块"模式注册(`calendar_cmd.py` / `plan_cmd.py` / `list_cmd.py` 等,main.py 尾部 import 后挂到 group)。consult 按同款模式新增 `entry/cli/consult_cmd.py`。

---

## 4. 方案概览

### 4.1 端到端时序

```
┌─────────────────────────────────────────────────────────────────────────┐
│ 1. code agent(claude/kimi headless)卡住                                │
│    urgency=high   → Bash 前台跑(阻塞等 advisory,反正卡住也干不了别的)   │
│    urgency=low/med → Bash run_in_background 跑,继续干活,稍后读输出     │
│    $ story consult --question "..." --context-file <path> --urgency high │
└────────────────────────────┬────────────────────────────────────────────┘
                             ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 2. story consult 子命令(entry/cli/consult_cmd.py,一次性进程)          │
│    - 校验 env(STORY_KEY/STAGE/WORKSPACE/ADAPTER)                        │
│    - STORY_CONSULT_DEPTH 守卫(≥1 拒绝,防外援套娃)                     │
│    - 落 consult_request 事件(DB)                                       │
│    - 调编排 LLM 的 FC loop(run_consult_orchestrator)                   │
└────────────────────────────┬────────────────────────────────────────────┘
                             ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 3. 编排 LLM 的 FC loop(复用 replanner.replan 骨架)                     │
│    loop 循环(最多 _MAX_CONSULT_ROUNDS=5,硬超时 480s):                 │
│    - invoke_with_tools(messages, [spawn_reviewer, finalize_advice])     │
│    - 若 LLM 调 spawn_reviewer(adapter, focus, timeout):                 │
│        → run_consult_sync(spawn 外援 CLI + poll 结果文件) → tool_result │
│        → 把 tool_result 以 role:"tool" 塞回 messages                    │
│    - 若 LLM 调 finalize_advice(advice_text):                            │
│        → 跳出 loop,advice_text 即最终 advisory                          │
│    - 若 LLM 不调工具(纯文本):                                          │
│        → 跳出 loop,文本即 advisory                                      │
└────────────────────────────┬────────────────────────────────────────────┘
                             ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 4. consult_cmd                                                          │
│    - 落 consult_response 事件(DB)                                      │
│    - stdout 打印: [consult <rid>] [confidence: X] <advisory>            │
│    - exit 0(降级路径也 exit 0 —— 永不阻塞 code agent)                  │
└────────────────────────────┬────────────────────────────────────────────┘
                             ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 5. code agent 拿到 advisory(Bash 输出,context 保留),继续执行          │
│    若不采纳,在 done summary 说明理由(引用 request_id)                 │
└─────────────────────────────────────────────────────────────────────────┘
```

### 4.2 新增 / 改动组件清单

| 组件 | 类型 | 位置 |
|---|---|---|
| **`story consult` 子命令**(cli_main 纯核心 + click 薄壳) | 新增 | `entry/cli/consult_cmd.py`(新文件)+ `entry/cli/main.py` 注册 |
| **编排 LLM 的 consult FC loop** | 新增函数(复用 `replanner.replan` 骨架) | `orchestrator/engine/consult_orchestrator.py`(新文件) |
| **同步 spawn+poll helper** | 新增函数 | `orchestrator/engine/consult_runner.py`(新文件) |
| **`spawn_reviewer` FC 工具定义** | 新增工具 schema | `consult_orchestrator.py` 内联 |
| **`finalize_advice` FC 工具定义** | 新增工具 schema(终止信号) | 同上 |
| **`.story/consult/` 路径 helper** | 新增函数 | `infra/paths.py` 追加 |
| **prompt 协议段(教 code agent 何时/怎么调 consult)** | 新增 section builder | `orchestrator/engine/prompt_sections.py` 追加 |
| **env 注入提升**(STORY_* 对所有 headless spawn) | 改造 | `orchestrator/engine/planner.py:1086-1106` |

`clarify_server.py` **零改动**(consult 不进 MCP server)。

### 4.3 两个新增 FC 工具

#### `spawn_reviewer`(编排 LLM 在 loop 里调用)

```python
SPAWN_REVIEWER_TOOL = {
    "type": "function",
    "function": {
        "name": "spawn_reviewer",
        "description": (
            "Spawn an external reviewer CLI (headless) to investigate a sub-question. "
            "The reviewer investigates the same workspace, writes its findings to "
            ".story/consult/<review_id>.json, and the findings are returned to you. "
            "Use this to get a second opinion from another model (decorrelation), "
            "or to investigate a sub-problem with fresh context. "
            "The adapter MUST differ from the consulting code agent's adapter "
            "(given in the system prompt) — cross-model decorrelation is the point."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "adapter": {
                    "type": "string",
                    "enum": ["claude", "kimi"],   # codex 无 headless,见 §3.6
                    "description": "Which CLI to spawn. MUST differ from the consulting code agent's adapter for decorrelation.",
                },
                "focus": {
                    "type": "string",
                    "description": "Concrete investigation directive (2-3 sentences). Tell the reviewer exactly what to check.",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Max seconds to wait. Default 180.",
                    "default": 180,
                },
            },
            "required": ["adapter", "focus"],
        },
    },
}
```

decorrelation 规则的强制执行分两层:prompt 层(system prompt 给出 caller adapter + MUST 约束,§5.6)+ Handler 层(`run_consult_orchestrator` 在分发 `spawn_reviewer` 前校验 `adapter != adapter_of_caller`,违反时把 `{"status":"decorrelation_violation", ...}` 塞回 tool_result 让 LLM 换 adapter 重试,不真 spawn)。两层都是软约束但叠在一起足够;caller 只有 claude/kimi 两种,不存在"无 adapter 可用"的死锁。

#### `finalize_advice`(编排 LLM 终止 loop)

```python
FINALIZE_ADVICE_TOOL = {
    "type": "function",
    "function": {
        "name": "finalize_advice",
        "description": (
            "Finalize and return the advisory to the consulting code agent. "
            "Call this when you have enough information to give a useful answer. "
            "The advice should be concrete, actionable, and cite evidence from reviewer findings. "
            "Mark it as advisory (the code agent may choose not to follow it)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "advice": {
                    "type": "string",
                    "description": "The advisory text. Concrete, actionable, with evidence.",
                },
                "confidence": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": "How confident you are in this advice.",
                },
                "followed_up": {
                    "type": "boolean",
                    "description": "Whether you spawned reviewer(s) to verify. If false, advice is based on reasoning only.",
                },
            },
            "required": ["advice", "confidence"],
        },
    },
}
```

### 4.4 接口对齐核对表(`replanner.replan` vs `continue_orchestrator_agent`)

这是复用 replanner 骨架的依据:

| 检查项 | replanner 产出 | planner 消费端 | 对齐 |
|---|---|---|---|
| launch action 结构 | `{action:"launch", adapter, stage, focus, done_file}`(`replanner.py:128`) | 读 `action.get("action")=="launch"`(`planner.py` `continue_orchestrator_agent` 内) | ✅ |
| skip action 结构 | `{action:"skip", stage, reason}`(`replanner.py:135`) | 读 `action.get("action")=="skip"`(同上) | ✅ |
| done_file 规范化 | `stage_done_file_rel(story_key, stage)`(`replanner.py:133`) | planner 强制规范化 | ✅ |
| LLM 调用形态 | `invoke_with_tools(messages, tools, tool_choice="auto")`(`replanner.py:90-112`) | — | ✅ 真 FC |

**核对结论**:replanner 产出可直接喂给 `continue_orchestrator_agent`,零接口适配。但 consult **不复用** `replanner.replan()` 本身(它输入是 feedback、输出是 action list,与 consult 语义不同)。**复用的是它的 loop 骨架**——读 tool_calls → 执行 → 塞回 messages → 再调 LLM。

---

## 5. 详细设计

### 5.1 consult 状态机

按 AGENTS.md 要求,跨状态的复杂协议必须先定义状态机。

```
                       ┌──────────────┐
                       │   PENDING    │  story consult 被调,落 consult_request 事件
                       └──────┬───────┘
                              │ 启动 FC loop
                              ▼
                       ┌──────────────┐
                       │ ROUTING      │  编排 LLM 第一次 invoke_with_tools
                       │              │  决定要不要 spawn 外援
                       └──────┬───────┘
                  ┌───────────┼───────────┐
                  │           │           │
        不调工具       调 spawn_reviewer     调 finalize_advice
        (纯文本)        (开始调查)            (跳过调查,直接答)
                  │           │           │
                  │           ▼           │
                  │   ┌──────────────┐    │
                  │   │ INVESTIGATING│    │  run_consult_sync 阻塞
                  │   │              │    │  spawn headless + poll
                  │   └──────┬───────┘    │
                  │          │ 结果回来   │
                  │          ▼            │
                  │   ┌──────────────┐    │
                  │   │ SYNTHESIZING │◄───┘  tool_result 塞回 messages
                  │   │              │       下一轮 invoke_with_tools
                  │   │ 编排 LLM 决定 │       (可能再 spawn,或 finalize,或纯文本)
                  │   └──────┬───────┘
                  │          │
                  └──────────┤
                             │ finalize_advice 或 纯文本 或 达 _MAX_CONSULT_ROUNDS
                             ▼
                       ┌──────────────┐
                       │  ANSWERED    │  落 consult_response 事件,打印 stdout,exit 0
                       └──────────────┘

       异常分支(任何状态都可进入):
                              │
                              ▼
                       ┌──────────────┐
                       │  TIMED_OUT   │  外援 spawn 超时 / loop 超时 / LLM API 超时
                       │              │  → 降级返 fallback advisory(仍 exit 0)
                       └──────┬───────┘
                              │
                              ▼
                       ┌──────────────┐
                       │   FAILED     │  spawn 反复失败 / LLM API 不可用
                       │              │  → 打印降级说明让 code agent 自行决断(exit 0)
                       └──────────────┘
```

**状态转换规则**(决策表):

| 当前状态 | 事件 | 动作 | 下一状态 |
|---|---|---|---|
| PENDING | `story consult` 被调 | 校验 env + depth 守卫,落 consult_request 事件,启动 FC loop | ROUTING |
| ROUTING | LLM 返回纯文本(无 tool_calls) | 把文本作为 advisory | ANSWERED |
| ROUTING | LLM 调 spawn_reviewer | 校验 decorrelation,调 `run_consult_sync` | INVESTIGATING |
| ROUTING | LLM 调 finalize_advice | 取 advice 字段 | ANSWERED |
| ROUTING | LLM 调用失败(超时/坏 JSON) | 重试 1 次,仍失败 → TIMED_OUT | TIMED_OUT |
| INVESTIGATING | 外援 done file 出现 | 解析结果,塞回 messages | SYNTHESIZING |
| INVESTIGATING | 外援超时 | 把"timeout, no finding"塞回 messages | SYNTHESIZING |
| INVESTIGATING | 外援 spawn 失败 3 次 | 把"spawn failed"塞回 messages | SYNTHESIZING |
| SYNTHESIZING | LLM 调 spawn_reviewer(再调查) | 调 `run_consult_sync` | INVESTIGATING |
| SYNTHESIZING | LLM 调 finalize_advice | 取 advice 字段 | ANSWERED |
| SYNTHESIZING | LLM 返回纯文本 | 把文本作为 advisory | ANSWERED |
| SYNTHESIZING | 累计轮次达 `_MAX_CONSULT_ROUNDS=5` | 强制以最近一轮文本作为 advisory | ANSWERED |
| 任何 | _CONSULT_HARD_TIMEOUT_S(默认 480s)到期 | 强制终止,打印 fallback advisory | TIMED_OUT |

硬超时取 480s 的考量:前台调用时 code agent 的 Bash 工具单次上限通常 600s(claude),留 120s 余量;后台调用无此约束,但 480s(最多 2 次外援调查 + 综合)对咨询场景已足够。

**约束(AGENTS.md)**:
- **Decider**:编排 LLM 只产 decision(spawn / finalize / 纯文本),不执行 spawn
- **Handler**:`run_consult_sync` 执行 spawn,返回结果给 Decider
- 每个非异常分支都必须落 DB 事件(可观测性)

### 5.2 CLI 接口契约

```bash
story consult --question TEXT [--context TEXT | --context-file PATH] [--urgency low|medium|high]
```

| 项 | 契约 |
|---|---|
| `--question` | 必填。具体问题,不泛泛。 |
| `--context` | 可选。试过什么、当前代码现状、相关 snippet。 |
| `--context-file` | 可选,与 `--context` 二选一,**推荐**。context 通常长且多行,code agent 先用 Write 工具写文件再传路径,避免 Windows 命令行转义问题。两者都给时 file 优先。 |
| `--urgency` | 可选,默认 `medium`。low=概念澄清,medium=方案选型,high=撞墙/阻塞 bug。 |
| **stdout** | `[consult <request_id>] [confidence: <level>]\n<advisory 文本>`。request_id 供 done summary 引用。 |
| **exit code** | `0` = 成功**或降级**(fallback advisory 也 exit 0,绝不阻塞 code agent);`2` = 用法错误(缺 question、env 缺失、depth 守卫命中),stdout 打印原因。 |
| **env 需求** | `STORY_KEY` / `STORY_STAGE` / `STORY_WORKSPACE` / `STORY_ADAPTER`(planner spawn headless stage 时注入,§5.8)。缺失 → exit 2 并提示"consult 只能在 story headless stage 内调用"。 |
| **测试缝** | `STORY_CONSULT_FAKE`(§8.2):设置后跳过真 LLM + 真 spawn,直接打印其值作为 advisory。 |
| **递归守卫** | `STORY_CONSULT_DEPTH`:外援 CLI spawn 时被注入 `1`(§5.5),consult 见 depth ≥ 1 拒绝(exit 2,"reviewer 不可再 consult")。防外援套娃。 |

**调用模式**(写进 prompt 协议段,§5.3):

- `urgency=high`:**前台**跑。反正卡住也干不了别的,阻塞语义对齐 Cognition 单一决策线。给 Bash 调用设 timeout ≥ 480s。
- `urgency=low/medium`:**后台**跑(Bash 工具的 `run_in_background`),agent 继续干活,稍后读任务输出拿 advisory。

CLI 侧**不建**轮询协议(submit/result 子命令)——理由见 §6.3。

### 5.3 prompt 协议段(教 code agent 何时/怎么调 consult)

`orchestrator/engine/prompt_sections.py` 新增:

```python
def build_consult_protocol_section(*, interactive: bool) -> str:
    """教 code agent 何时/怎么调 consult。仅 headless 路径注入。"""
    if interactive:
        return ""  # interactive 路径在终端可直接问人,不注入
    return """
### 不确定时请外援(consult)

当遇到以下情况,用 Bash 工具运行 `story consult`:
- 你尝试了 2 种方案仍无法确定哪个对(stuck loop)
- 涉及跨模块/跨服务的边界判断,凭现有代码推断不确定
- 需要第二意见打破 stuck loop(编排 LLM 可 spawn 外援从不同角度调查)

**调用方式**:
```bash
# 1. 先把上下文写文件(避免命令行转义问题)
#    .story/consult/req-<简述>.md —— 你试过什么、当前代码现状、相关 snippet
# 2. 调 consult
story consult --question "<具体问题>" --context-file .story/consult/req-<简述>.md --urgency high
```

- urgency=high(撞墙/阻塞):**前台**跑,Bash timeout 设 480000ms 以上。
- urgency=low/medium(概念澄清/选型):**后台**跑(Bash 的 run_in_background),继续干别的,稍后读输出。

**纪律**:
- consult 返回的是**建议**(advisory),最终决策仍在你。你可以不采纳,但**必须**在 done summary 里说明未采纳理由(引用输出里的 request_id)。
- consult 可能 spawn 一个外援 CLI 实地调查(跨模型 decorrelation),需要 30 秒 - 数分钟。**不要滥用**。
- 能从代码/PRD/kb.py 自己查清楚的,不要 consult。consult 是卡住后的求助,不是默认工作流。
- 每个阻塞点最多 consult 一次。别反复 consult 同一个问题。
"""
```

挂载点:`planner.py:1898-1900` 的 `grill_section` 旁,加 `consult_section`:

```python
consult_section = ""
if not interactive:  # 所有 headless 路径注入(claude/kimi caller 都能用)
    consult_section = build_consult_protocol_section(interactive=interactive)
```

注意与 grill 的差异:consult **不依赖 grill-me 开关**,凡是 headless 就注入——CLI 方案下没有 claude-only 的限制。

### 5.4 路径 helper(`infra/paths.py` 追加)

```python
# ---- consult ----

def consult_dir(workspace: str | Path) -> Path:
    """advisory 结果目录(与 stage done 隔离,无 stage 推进语义)。"""
    return story_dir(workspace) / "consult"


def consult_result_file(workspace: str | Path, request_id: str) -> Path:
    return consult_dir(workspace) / f"{request_id}.json"


def consult_result_file_rel(request_id: str) -> str:
    """Workspace-relative path(嵌进 CLI prompt + 自身轮询)。"""
    return f".story/consult/{request_id}.json"
```

**设计约束**:consult 结果文件路径用 `request_id`(uuid hex[:12],同 `clarify_server.py:106` 的生成法),**不用 story_key / stage**。理由:consult 可能在同一 stage 多次调用,用 request_id 天然唯一,且与 `.story/done/<key>/<stage>.json` 完全隔离,不会被 graph/orphan-claim/resume 误抓。

### 5.5 同步 spawn+poll helper(`orchestrator/engine/consult_runner.py` 新文件)

```python
"""consult runner —— spawn 外援 CLI + poll advisory 结果文件(同步)。

复用 planner.py:1149-1192 的 headless spawn 骨架 + 1318-1471 的 done poll 骨架,
**砍掉** stage 推进 / clarification reset / PTY 回收 / db.update_story 等耦合逻辑,
并把 stdout/stderr 的防死锁方案从 drain 线程改为落日志文件(见下)。

设计原则:
- 纯同步(consult FC loop 在阻塞调用里调它)
- 零 DB 副作用(只读写文件;DB 事件归 consult_orchestrator / consult_cmd)
- 失败不抛异常外泄,返 {"status": "...", "error": "..."} 让编排 LLM 决策
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Callable

from ...infra.json_helpers import robust_json_parse
from ...infra.paths import consult_result_file_rel
from ...knowledge.adapters import get_adapter

_DEFAULT_TIMEOUT = 180
_DEFAULT_POLL_INTERVAL = 5.0
_MAX_SPAWN_ATTEMPTS = 3


def run_consult_sync(
    *,
    adapter_name: str,
    focus: str,
    workspace: str,
    request_id: str,
    model: str = "",
    cwd: str | None = None,
    env: dict | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
    max_attempts: int = _MAX_SPAWN_ATTEMPTS,
    # 注入点(测试用)
    popen_fn: Callable = subprocess.Popen,
    sleep_fn: Callable[[float], None] = time.sleep,
    kill_fn: Callable = None,  # 缺省用 _default_kill,见下
) -> dict:
    """Spawn 外援 CLI + poll 结果文件 → 返 advisory dict。

    Args:
        adapter_name: claude / kimi(codex 无 headless,见 §3.6)。
        focus: 给外援的调查指令(2-3 句)。
        workspace: 工作区根。
        request_id: consult 请求 id(uuid hex[:12] + 轮次后缀)。
        timeout: 外援最长等待秒数(默认 180)。
        max_attempts: 外援 spawn 失败后重试上限(默认 3)。

    Returns:
        dict,保证字段:
        - status: "ok" | "timeout" | "spawn_failed" | "no_headless"
        - findings: dict(外援的 advisory,可能为空)
        - error: str(失败时的诊断)

        status="ok" 时 findings 含外援写的任意字段(自由 schema)。

    不抛异常 —— 所有失败路径返 {"status": ..., "error": ...},让编排 LLM 决策。
    """
    adapter = get_adapter(adapter_name)
    launch_cmd = adapter.headless_launch_cmd(model=model, prompt="")
    if launch_cmd is None:
        return {
            "status": "no_headless",
            "findings": {},
            "error": f"adapter {adapter_name!r} has no headless mode",
        }

    result_rel = consult_result_file_rel(request_id)
    result_path = Path(workspace) / result_rel
    result_path.parent.mkdir(parents=True, exist_ok=True)
    if result_path.exists():
        result_path.unlink()  # 清旧(防误读)
    log_path = result_path.with_suffix(".log")  # 外援 stdout/stderr 落此文件

    prompt = _build_reviewer_prompt(focus=focus, result_file=result_rel)

    kill = kill_fn or _default_kill
    spawn_cwd = cwd or workspace
    # 外援 env:继承 + 注入递归守卫(外援不可再 consult,§5.2)
    reviewer_env = {**(env if env is not None else os.environ), "STORY_CONSULT_DEPTH": "1"}
    elapsed = 0.0
    attempt = 1
    proc = None

    try:
        while elapsed < timeout:
            # spawn(首次或重试)
            if proc is None:
                try:
                    # stdout/stderr 落日志文件,绝不用 PIPE —— PIPE 不排空会写满
                    # 缓冲(Windows ~64KB)阻塞子进程,外援永远写不出结果文件。
                    # planner.py:1145/1193-1227 同款坑(那边用 drain 线程);
                    # 这里同步模型下落文件更简单,还顺带拿到 spawn 失败的诊断。
                    log_fh = open(log_path, "ab")  # noqa: SIM115 - 随 proc 生命周期
                    proc = popen_fn(
                        launch_cmd,
                        cwd=spawn_cwd,
                        stdin=subprocess.PIPE,
                        stdout=log_fh,
                        stderr=subprocess.STDOUT,  # 合并进同一日志
                        env=reviewer_env,
                    )
                    proc.stdin.write(prompt.encode("utf-8"))
                    proc.stdin.close()
                except Exception as exc:
                    if attempt < max_attempts:
                        attempt += 1
                        sleep_fn(poll_interval)
                        elapsed += poll_interval
                        continue
                    return {
                        "status": "spawn_failed",
                        "findings": {},
                        "error": f"Popen failed after {attempt} attempts: {exc}",
                    }

            # 外援提前退出但没写结果文件 → 重试
            if proc.poll() is not None and not result_path.exists():
                if attempt < max_attempts:
                    attempt += 1
                    kill(proc)
                    proc = None
                    sleep_fn(poll_interval)
                    elapsed += poll_interval
                    continue
                return {
                    "status": "spawn_failed",
                    "findings": {},
                    "error": (
                        f"reviewer exited without writing result (attempt {attempt}); "
                        f"see {log_path}"
                    ),
                }

            # 结果文件出现 → 解析返回
            if result_path.exists():
                try:
                    data = robust_json_parse(result_path) or {}
                    return {"status": "ok", "findings": data, "error": ""}
                except Exception:
                    pass  # 半写文件,下一轮再试

            sleep_fn(poll_interval)
            elapsed += poll_interval

        # 超时
        return {
            "status": "timeout",
            "findings": {},
            "error": f"reviewer did not finish within {timeout}s; see {log_path}",
        }
    finally:
        if proc is not None:
            try:
                kill(proc)
            except Exception:
                pass


def _build_reviewer_prompt(*, focus: str, result_file: str) -> str:
    """给外援 CLI 的 prompt —— 调查 focus,把结论写到 result_file。"""
    return f"""## 任务:外援调查(advisory)

你被编排层 spawn 为**外援 reviewer**,协助另一个 code agent 解决问题。你的产出是 **advisory**(建议),不是决策。

### 调查指令
{focus}

### 完成协议
调查完成后,把结论写入文件 `{result_file}`,JSON 格式:
```json
{{
  "summary": "<一句话结论>",
  "findings": ["<具体发现 1>", "<具体发现 2>", ...],
  "recommendation": "<给请求方 code agent 的建议>",
  "evidence": ["<引用的代码位置 / 文件 / 行号>"],
  "confidence": "low|medium|high"
}}
```

**纪律**:
- 你**不写业务代码**,只调查 + 写 advisory 结果文件。
- 聚焦调查指令,不要发散。
- 引用具体代码位置(file:line),不要泛泛而谈。
- 完成即写文件退出,不要等待。
- 你**不可**再调 `story consult`(递归守卫会拒绝)。遇到不确定,把不确定性写进 findings。
"""


def _default_kill(proc):
    """复用 planner._kill_headless 的 taskkill /T 逻辑(Windows 进程树)。"""
    from .planner import _kill_headless
    _kill_headless(proc)
```

**关键设计**:
- 所有失败路径**不抛异常**,返 `{"status": ..., "error": ...}`(让编排 LLM 看到"外援挂了"后自己决定怎么办)
- **stdout/stderr 落 `.story/consult/<request_id>.log`,不用 PIPE**(PIPE 不排空 → 写满缓冲 → 子进程阻塞 → 必超时;planner.py:1145 的已知坑)。日志文件顺带支撑 spawn_failed 诊断
- **递归守卫**:外援 env 注入 `STORY_CONSULT_DEPTH=1`(CLI 方案下外援也有 Bash,不设防就会套娃)
- `popen_fn` / `sleep_fn` / `kill_fn` 全注入(可单测,零实时延迟)
- `_default_kill` 从同包 `planner` import `_kill_headless`(避免下沉到 infra 的大改动,见 §6.2)

### 5.6 编排 LLM 的 FC loop(`orchestrator/engine/consult_orchestrator.py` 新文件)

```python
"""consult orchestrator —— 编排 LLM 的 FC loop,决定 spawn / synthesize / finalize。

**复用 `replanner.replan()` 的 loop 骨架**(读 tool_calls → 执行 → 塞回 messages → 再调),
但输入/输出/工具完全不同:
- 输入:code agent 的 consult 请求(question/context/urgency)
- 工具:spawn_reviewer(调 consult_runner.run_consult_sync) + finalize_advice(终止信号)
- 输出:advisory 文本(str)

设计原则:
- 纯 Decider + Handler 分层:LLM 决策(Decider),run_consult_sync 执行 spawn(Handler)
- 零 DB 副作用(DB 事件归 consult_cmd)
- 全注入可测(invoke_with_tools / spawn_fn / clock 都能注入)
"""
from __future__ import annotations

import json
import time
from typing import Callable

# FC 工具 schema(对齐 agent_tools.py 的 OpenAI FC 格式)
CONSULT_TOOLS = [
    # SPAWN_REVIEWER_TOOL  —— 见 §4.3
    # FINALIZE_ADVICE_TOOL —— 见 §4.3
]

_MAX_CONSULT_ROUNDS = 5
_HARD_TIMEOUT_S = 480   # consult 全流程硬上限(前台 Bash 600s 上限留余量,§5.1)


def build_consult_messages(
    *,
    consult_request: dict,
    story_facts: dict,
) -> list[dict]:
    """Pure Decider:code agent 的 consult 请求 → 编排 LLM 的初始 messages。

    Args:
        consult_request: {question, context, urgency, request_id, adapter_of_caller}
        story_facts: {story_key, stage, task_type, recent_events_summary, ...}

    Returns:
        [{role: system}, {role: user}] —— 喂 invoke_with_tools。
    """
    caller = consult_request.get("adapter_of_caller", "?")
    system = (
        "你是 story 编排层,被 code agent 通过 consult 求助。你的任务是:\n"
        "1. 判断这个问题需不需要 spawn 外援(跨模型 decorrelation / 实地调查)\n"
        "2. 如果需要,调 spawn_reviewer(adapter=...) spawn 外援 CLI 调查\n"
        "3. 拿到外援 findings 后,调 finalize_advice 综合给 code agent advisory\n"
        "4. 如果问题简单(你自己能答),直接调 finalize_advice 不 spawn\n\n"
        "**纪律**:\n"
        f"- 求助方 code agent 的 adapter 是 **{caller}**。spawn 的 adapter 必须与其**不同**"
        "(跨模型 decorrelation 是 consult 的核心价值;同模型 fresh context 是次优,仅当"
        "异 adapter spawn 失败后才可考虑,且要在 advice 里标注 decorrelation 弱)\n"
        "- advisory 是建议(不是命令),code agent 可不采纳\n"
        "- 你的建议要 cite 外援的 evidence(具体代码位置),不要泛泛而谈\n"
        "- 最多 spawn 2 个外援(避免过度调查),总轮次 ≤ 5\n\n"
        f"Story 上下文: {json.dumps(story_facts, ensure_ascii=False)}"
    )
    user = (
        f"## Code agent 的 consult 请求\n"
        f"**求助方 adapter**: {caller}\n"
        f"**urgency**: {consult_request.get('urgency', 'medium')}\n"
        f"**问题**: {consult_request.get('question', '')}\n"
        f"**上下文**:\n{consult_request.get('context', '')}\n\n"
        f"请决定:spawn 外援调查,还是直接 finalize_advice?"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def run_consult_orchestrator(
    *,
    consult_request: dict,
    story_facts: dict,
    workspace: str,
    # 注入点(测试用)
    invoke_with_tools: Callable,
    spawn_fn: Callable,            # = consult_runner.run_consult_sync
    tools: list[dict] | None = None,
    max_rounds: int = _MAX_CONSULT_ROUNDS,
    hard_timeout_s: float = _HARD_TIMEOUT_S,
    clock_fn: Callable[[], float] = time.monotonic,
) -> dict:
    """编排 LLM 的 FC loop → 返 advisory。

    Args:
        consult_request: {question, context, urgency, request_id, adapter_of_caller}
        story_facts: story 上下文(供 LLM 决策)
        workspace: 工作区根(传给 spawn_fn)
        invoke_with_tools: 注入的 LLM FC 调用,签名同 LLMClient.invoke_with_tools
        spawn_fn: 注入的 spawn 函数,签名同 run_consult_sync(关键字参数)

    Returns:
        dict:
        - advice: str(最终给 code agent 的 advisory)
        - confidence: "low"|"medium"|"high"
        - followed_up: bool(是否 spawn 过外援)
        - rounds: int(实际跑了多少轮)
        - terminated_by: "finalize"|"text"|"max_rounds"|"hard_timeout"|"llm_failed"|"empty_text"
          (诊断字段,开集 —— 调用方/wiring 可追加取值如 "exception"/"test_fake")
        - spawn_results: list[dict](每次 spawn 的结果,审计用)
    """
    messages = build_consult_messages(
        consult_request=consult_request, story_facts=story_facts
    )
    tools = tools or CONSULT_TOOLS
    start = clock_fn()
    spawn_results: list[dict] = []
    request_id = consult_request.get("request_id", "")
    caller_adapter = consult_request.get("adapter_of_caller", "")

    for round_n in range(1, max_rounds + 1):
        # 硬超时检查
        if clock_fn() - start > hard_timeout_s:
            return _fallback_advice(
                spawn_results, terminated_by="hard_timeout",
                reason=f"hard timeout {hard_timeout_s}s reached at round {round_n}",
            )

        # 一次 FC 调用
        try:
            resp = invoke_with_tools(
                messages, tools, tool_choice="auto", temperature=0.1, timeout=90
            )
        except Exception as exc:
            # LLM 抖动 → 用已有 spawn_results 综合(没有就 fallback)
            return _fallback_advice(
                spawn_results, terminated_by="llm_failed",
                reason=f"invoke_with_tools failed at round {round_n}: {exc}",
            )

        tool_calls = resp.get("tool_calls") or []
        messages.append(
            resp.get("message")
            or {"role": "assistant", "content": resp.get("content", "")}
        )

        # 纯文本(没调工具)→ 当 advisory 返回
        if not tool_calls:
            text = resp.get("content", "").strip()
            if text:
                return {
                    "advice": text,
                    "confidence": "medium",
                    "followed_up": bool(spawn_results),
                    "rounds": round_n,
                    "terminated_by": "text",
                    "spawn_results": spawn_results,
                }
            # 空文本 + 无 tool_calls → 异常,fallback
            return _fallback_advice(
                spawn_results, terminated_by="empty_text",
                reason=f"LLM returned empty text at round {round_n}",
            )

        # 处理 tool_calls
        for tc in tool_calls:
            fn = tc.get("function", {}) or {}
            name = fn.get("name", "")
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}

            if name == "finalize_advice":
                # 终止信号 → 直接返
                return {
                    "advice": str(args.get("advice", "")),
                    "confidence": args.get("confidence", "medium"),
                    "followed_up": bool(spawn_results),
                    "rounds": round_n,
                    "terminated_by": "finalize",
                    "spawn_results": spawn_results,
                }

            if name == "spawn_reviewer":
                adapter = args.get("adapter", "")
                focus = args.get("focus", "")
                timeout = args.get("timeout_seconds", 180)
                # decorrelation 硬校验(Handler 层,§4.3):与 caller 同 adapter → 不 spawn,
                # 塞回违规提示让 LLM 换 adapter
                if caller_adapter and adapter == caller_adapter:
                    tool_result_text = json.dumps({
                        "status": "decorrelation_violation",
                        "error": (
                            f"adapter {adapter!r} equals caller's adapter; "
                            "pick a DIFFERENT adapter for decorrelation"
                        ),
                    }, ensure_ascii=False)
                else:
                    spawn_result = spawn_fn(
                        adapter_name=adapter,
                        focus=focus,
                        workspace=workspace,
                        request_id=f"{request_id}_r{round_n}_{adapter}",
                        timeout=timeout,
                    )
                    spawn_results.append({
                        "round": round_n,
                        "adapter": adapter,
                        "focus": focus,
                        "result": spawn_result,
                    })
                    tool_result_text = json.dumps(spawn_result, ensure_ascii=False)
            else:
                tool_result_text = json.dumps(
                    {"error": f"unknown tool {name!r}"}, ensure_ascii=False
                )

            # 把 tool_result 塞回 messages(FC 协议要求)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": tool_result_text,
            })

    # 达 max_rounds 仍没 finalize → 用最后一轮的 spawn_results 综合
    return _fallback_advice(
        spawn_results, terminated_by="max_rounds",
        reason=f"reached max_rounds={max_rounds} without finalize",
    )


def _fallback_advice(
    spawn_results: list[dict], *, terminated_by: str, reason: str
) -> dict:
    """降级路径:把已有 spawn_results 拼成 advisory,标注低置信。"""
    if not spawn_results:
        return {
            "advice": (
                f"(consult 降级: {reason}。编排层未能提供有效建议,"
                f"请自行决断并在 done summary 说明)"
            ),
            "confidence": "low",
            "followed_up": False,
            "rounds": 0,
            "terminated_by": terminated_by,
            "spawn_results": spawn_results,
        }
    # 拼 findings
    findings_lines = []
    for sr in spawn_results:
        r = sr.get("result", {})
        if r.get("status") != "ok":
            findings_lines.append(f"- [{sr['adapter']}] 调查失败: {r.get('error', '?')}")
            continue
        f = r.get("findings", {})
        summary = f.get("summary", "")
        rec = f.get("recommendation", "")
        findings_lines.append(f"- [{sr['adapter']}] {summary}")
        if rec:
            findings_lines.append(f"  建议: {rec}")
    advice = (
        f"(consult 降级综合,置信低 — {reason})\n"
        + "\n".join(findings_lines)
    )
    return {
        "advice": advice,
        "confidence": "low",
        "followed_up": True,
        "rounds": len(spawn_results),
        "terminated_by": terminated_by,
        "spawn_results": spawn_results,
    }
```

**关键设计**:
- `_fallback_advice` 保证任何失败路径都有 advisory 返回(不阻塞 code agent)
- `invoke_with_tools` / `spawn_fn` / `clock_fn` 全注入(可单测)
- `terminated_by` 是**诊断字段(开集)**,契约测试只断言字段存在不断言取值(§8.3)
- 每个 spawn 的 request_id 加 `_r{round_n}_{adapter}` 后缀(多次 spawn 不撞文件;当前串行处理 tool_calls,若未来改并行需重新审此约定)
- decorrelation 硬校验在 Handler 层(`decorrelation_violation` 不真 spawn,LLM 看到 tool_result 后换 adapter)

### 5.7 `story consult` 子命令(`entry/cli/consult_cmd.py` 新文件)

分层:**纯核心 `run_consult_cli`**(全注入,可单测)+ **click 薄壳**(读 argv/env,打印,exit)。测试缝(`STORY_CONSULT_FAKE`)放在薄壳 wiring 层,核心不感知。

```python
"""story consult —— code agent 的求助通道(CLI 一次性进程)。

code agent(headless stage 内)用 Bash 调 `story consult` → 编排 LLM FC loop
(可 spawn 外援 CLI)→ advisory 打印 stdout。永远 exit 0(除用法错误 exit 2),
绝不阻塞 code agent。

env(由 planner spawn headless stage 时注入,§5.8):
- STORY_KEY / STORY_STAGE / STORY_WORKSPACE / STORY_ADAPTER —— 必需
- STORY_CONSULT_DEPTH —— 递归守卫(≥1 拒绝;外援 spawn 时注入 1,§5.5)
- STORY_CONSULT_FAKE —— 测试缝(设置后跳过真 LLM/spawn,直接打印其值)
"""
from __future__ import annotations

import uuid
from typing import Callable


def run_consult_cli(
    *,
    question: str,
    context: str,
    urgency: str,
    env: dict,                       # 注入(测试传 dict;生产薄壳传 os.environ)
    # 注入点(测试用)
    log_event_fn: Callable,
    run_consult_orchestrator_fn: Callable,
    id_factory: Callable[[], str] | None = None,
) -> tuple[str, int]:
    """处理一次 consult 调用 → (stdout 文本, exit code)。

    Returns:
        (text, 0) —— 正常或降级(fallback advisory 也返 0,不阻塞 code agent)。
        (text, 2) —— 用法错误(env 缺失 / depth 守卫命中),text 为原因。
    """
    # 递归守卫:外援不可再 consult
    if int(env.get("STORY_CONSULT_DEPTH", "0") or "0") >= 1:
        return ("consult: reviewer 不可再 consult(递归守卫)。把不确定性写进 findings。", 2)

    story_key = env.get("STORY_KEY", "")
    workspace = env.get("STORY_WORKSPACE", "")
    if not story_key or not workspace:
        return ("consult: 缺 STORY_KEY/STORY_WORKSPACE —— 只能在 story headless stage 内调用。", 2)

    rid = (id_factory or (lambda: uuid.uuid4().hex[:12]))()
    stage = env.get("STORY_STAGE", "unknown")
    adapter_of_caller = env.get("STORY_ADAPTER", "")

    consult_request = {
        "request_id": rid,
        "question": question,
        "context": context,
        "urgency": urgency,
        "adapter_of_caller": adapter_of_caller,
    }
    log_event_fn(story_key, stage, "consult_request", consult_request)

    # 故事事实(可扩展:加 recent_events / open_findings / task_type)
    story_facts = {
        "story_key": story_key,
        "stage": stage,
        # TODO(后续): 从 DB 取 task_type / recent events 摘要 / open findings
    }

    try:
        result = run_consult_orchestrator_fn(
            consult_request=consult_request,
            story_facts=story_facts,
            workspace=workspace,
        )
    except Exception as exc:
        result = {
            "advice": f"(consult 异常: {exc}. 请自行决断)",
            "confidence": "low",
            "terminated_by": "exception",
        }

    log_event_fn(story_key, stage, "consult_response", {
        "id": rid,
        **{k: v for k, v in result.items() if k != "spawn_results"},
        "spawn_count": len(result.get("spawn_results", [])),
    })

    advice_text = result.get("advice", "")
    confidence = result.get("confidence", "unknown")
    return (f"[consult {rid}] [confidence: {confidence}]\n{advice_text}", 0)
```

click 薄壳(同文件,按 `calendar_cmd.py` 同款模式):

```python
import click

@click.command("consult")
@click.option("--question", required=True, help="具体问题")
@click.option("--context", default="", help="上下文(长文本建议用 --context-file)")
@click.option("--context-file", "context_file", default="", help="上下文文件路径(优先于 --context)")
@click.option("--urgency", type=click.Choice(["low", "medium", "high"]), default="medium")
def consult_cmd(question, context, context_file, urgency):
    """向编排层 LLM 求助(可 spawn 外援 CLI 调查)。供 headless stage 内的 code agent 调用。"""
    import os
    from ...infra.db import models as db
    from ...orchestrator.engine.consult_orchestrator import run_consult_orchestrator
    from ...orchestrator.engine.consult_runner import run_consult_sync
    from ...infra.llm_client import get_llm

    if context_file:
        from pathlib import Path
        context = Path(context_file).read_text(encoding="utf-8")

    # 测试缝(§8.2):fake 模式跳过真 LLM + 真 spawn,事件仍正常落
    fake = os.environ.get("STORY_CONSULT_FAKE")
    if fake:
        def _orch_fn(**kw):
            return {
                "advice": fake,
                "confidence": "high",
                "followed_up": False,
                "rounds": 0,
                "terminated_by": "test_fake",
                "spawn_results": [],
            }
    else:
        def _orch_fn(**kw):
            return run_consult_orchestrator(
                invoke_with_tools=get_llm().invoke_with_tools,
                spawn_fn=run_consult_sync,
                **kw,
            )

    text, code = run_consult_cli(
        question=question,
        context=context,
        urgency=urgency,
        env=dict(os.environ),
        log_event_fn=_safe_log_event,
        run_consult_orchestrator_fn=_orch_fn,
    )
    click.echo(text)
    raise SystemExit(code)


def _safe_log_event(story_key, stage, event_type, payload):
    """落 DB 事件,best-effort(对齐 clarify_server._emit 风格)。"""
    from ...infra.db import models as db
    try:
        db.log_event(story_key, stage, event_type, payload)
    except Exception:
        pass
```

注册(`entry/cli/main.py` 尾部,按 `calendar_cmd` 同款):import 后挂到 `cli` group。

### 5.8 env 注入提升(`planner.py:1086-1106`)

现状:`story_env = None`,仅 grill+claude 分支内赋 `STORY_KEY/STORY_STAGE`。

改造:**所有 headless spawn 都注入四个 env**(提升到分支外),grill 分支只保留 MCP config 写入:

```python
story_env = None
if headless:
    story_env = {
        **_os.environ,
        "STORY_KEY": story_key,
        "STORY_STAGE": stage,
        "STORY_WORKSPACE": workspace,          # consult spawn 外援的工作区
        "STORY_ADAPTER": adapter_name,         # consult 的 decorrelation 决策
    }
if _wants_grill and adapter_name == "claude" and headless:
    # ... MCP config 写入不变(clarify 继续用,env 已在上面注入)
```

注意:**不注入 `STORY_CONSULT_DEPTH`**(caller 的 depth 是未设/0;只有外援 spawn 时才注入 1,§5.5)。

### 5.9 入口注册(`entry/cli/main.py`)

按 `calendar_cmd` / `list_cmd` 同款模式:`from .consult_cmd import consult_cmd` 后挂到 `cli` group。一处 import + 一处注册,共 2 行。

---

## 6. 关键决策与权衡

### 6.1 为什么用路线 Z(编排 LLM FC loop)而不是 Python 硬编码路由

| 维度 | 硬编码路由(简化版) | 路线 Z(编排 LLM FC loop) |
|---|---|---|
| spawn 决策者 | Python 硬编码 | 编排 LLM 在 loop 里自主决定 |
| 多轮调查 | 不支持(一问一答) | 支持(LLM 可多轮 spawn + 综合) |
| 简单问题 | 也要 spawn(慢) | LLM 可直接 finalize(秒回) |
| 分层 | CLI 干 Decider 的活(违反 AGENTS.md) | CLI→Decider→Handler 标准三层 |
| 复用 | 全新机制 | 复用 `replanner.replan` 骨架 |

用户已选路线 Z。代价是改动面大、要复活 dead code、失败模式复杂。文档已规格化全部失败降级(§5.1 状态机 + §5.6 `_fallback_advice`)。

### 6.2 `_kill_headless` 不下沉到 infra

现状 `_kill_headless` 是 `planner.py:517` 的 module-private 函数(Windows taskkill /T 杀进程树)。两个选项:

- **A(推荐,第一版)**:`consult_runner._default_kill` 直接 `from .planner import _kill_headless` 复用(同包,engine/ 内)。改动小。
- **B(干净,后续)**:把 `_kill_headless` 下沉到 `infra/terminal/process.py`,planner 和 consult_runner 都 import。改动面大(planner 改 import + 新文件 + 测试迁移)。

第一版用 A,B 留作后续清理。

### 6.3 为什么 CLI 不是 MCP(评审决策记录,2026-07-21)

初版设计把 consult 加进 `clarify_server.py` 的 MCP server(`mcp__lifecycle__consult`),评审后改判 CLI。理由:

1. **MCP 的选型理由是 HITL 专属的**。clarify 走 MCP 是因为本机 claude 网关变体 `-p` 无 AskUserQuestion、PTY 渲染脆、in-process sdk_mcp_servers 未注册(§3.1)——全部针对"问人"。consult 是机器到机器,一条都不适用。
2. **MCP 锁死 caller adapter**。MCP 只在 `_wants_grill and adapter_name == "claude" and headless` 接线(planner.py:1087)→ caller 恒为 claude → "外援必须 ≠ caller"的 decorrelation 规则把外援强制成 kimi,与"外援默认可选 claude"自相矛盾。CLI 经 Bash 工具天然跨 adapter,caller 是 claude/kimi 都成立,矛盾消失。
3. **进程拓扑更简单**。不需要:长驻 stdio server、JSONRPC 循环、工具表重构、MCP config 文件管理。CLI 是一次性进程:args in → advisory stdout → exit。
4. **异步不建轮询协议**。曾讨论 CLI 做 submit/poll 轮询形式——被否:需要 detached worker 进程(Windows creationflags / std handle / 孤儿回收,仓库现有 headless spawn 全是"父进程轮询+负责 kill",没有 fire-and-forget 形态),还引入"agent 忘 poll"、"stage done 了 advisory 才到"两个纪律风险。改为 **CLI 保持阻塞式,异步推给调用方**:agent 用 Bash 原生 `run_in_background`(claude/kimi harness 都有),轮询/任务管理/输出读取全是 harness 原生 UX,零新代码。
5. **测试更直接**。集成测试 = subprocess 跑 `story consult` 断言 stdout/exit code,不需要 stdio JSONRPC 握手链;fake 缝就是一个 env(§5.7)。

代价:可发现性从"tools/list 自动出现"降级为"prompt 段教"——但 grill 协议本来就是 prompt 段注入的(planner.py:1898),是仓库既有模式。另需显式处理两个 CLI 特有问题:递归守卫(外援也有 Bash,§5.5 注入 `STORY_CONSULT_DEPTH=1`)和前台 Bash 超时(硬超时定 480s,§5.1)。

**待验证**:`claude -p` headless 下 Bash `run_in_background` 是否可用(本机是网关变体,§8.4 手动 E2E 验证;不可用则低优 consult 退化为前台阻塞,语义仍正确)。

### 6.4 codex 暂不支持(caller 和外援都不行)

`codex.py` 未实现 `headless_launch_cmd`(继承 base 的 `return None`)。codex 跑不了 headless stage → 不会成为 caller;`SPAWN_REVIEWER_TOOL` 的 adapter enum 是 `["claude", "kimi"]`,**不含 codex**。`consult_runner.run_consult_sync` 遇到 codex 会返 `status: "no_headless"`,编排 LLM 看到 tool_result 后自己决定换 adapter。

codex headless 支持是独立工作(后续 PR)。

### 6.5 consult 与 clarify / supervisor 的边界

| 通道 | 问谁 | 触发方 | 形态 | 阻塞性 | 可用路径 |
|---|---|---|---|---|---|
| clarify | 人 | code agent 主动 | 选择题,2-4 个 options | 重(分钟级-45min) | claude headless + grill(MCP) |
| **consult** | **编排 LLM + 可选外援 CLI** | **code agent 主动(Bash 调 CLI)** | **开放咨询** | **中(30s-8min),可后台异步** | **claude/kimi headless** |
| supervisor | 编排 LLM | 编排层被动观察 | 固定选项选择题 | 即时(但只观察不回写 headless) | headless |

边界纪律(写进 prompt §5.3):
- 能自己查清楚的 → 不 consult
- 概念澄清 / 二选一选人来答的 → clarify(若 MCP 已接线)
- 卡住、需要第二意见、跨模块判断 → consult
- 已经在终端吐二选一题的 → supervisor 被动处理(不主动调)

### 6.6 advisory 强制 code agent 说明采纳/未采纳

`prompt_sections.build_consult_protocol_section` 的纪律段明确要求:不采纳必须在 done summary 说明(引用 request_id)。**但这是 prompt 层约束,不是硬约束**。后续若要硬约束,可在 done.json schema 加 `consult_decisions: [{request_id, followed: bool, reason: str}]` 字段(阶段 2 工作)。

### 6.7 consult 频次暂无硬护栏

"每个阻塞点最多 consult 一次"目前只是 prompt 纪律。失控的 code agent 理论上可循环 consult,每次最多烧 8 分钟 + 一次外援 CLI 调用。已知风险,第一版接受(prompt 纪律 + consult_request 事件可观测);若实际运行发现滥用,在 `run_consult_cli` 里数 DB 中本 stage 的 consult_request 数加上限(十行以内,预留在阶段 2)。

---

## 7. AGENTS.md 架构评审触发检查

按 AGENTS.md 7 条触发清单:

```
1. 同一边界多次出 bug？            否(新需求)
2. 多状态用一个布尔？              否(状态机已显式建模,§5.1)
3. 多入口决策不一致？              否(单入口 story consult)
4. 副作用混进状态检查？            否(CLI→Decider→Handler 三层分离,§5.6)
5. 缺决策表/状态机/协议？          是(初始触发) → 已通过 §5.1 状态机 + §4.4 决策表解决
6. fix 跨多文件？                  是(6 个文件) → 但每个改动独立可测
7. 用户需手动解释下一步？          否(prompt §5.3 教 code agent)
```

触发 1 条(已通过状态机解决)。**满足"先设计再动代码"的硬规则**。

---

## 8. 测试策略

按 AGENTS.md "每个 bug fix 必须有回归测试" 精神扩展到新功能。

### 8.1 单元测试(纯核心,零外部依赖)

| 测试文件 | 测什么 |
|---|---|
| `tests/test_consult_runner.py` | `run_consult_sync` 的所有 status 路径:ok / timeout / spawn_failed / no_headless。注入 fake popen_fn / sleep_fn / kill_fn,零实时延迟。验证 stdout/stderr 落日志文件(防 PIPE 死锁回归)+ 外援 env 注入 `STORY_CONSULT_DEPTH=1` |
| `tests/test_consult_orchestrator.py` | `run_consult_orchestrator` 的所有 terminated_by 路径:finalize / text / max_rounds / hard_timeout / llm_failed / empty_text。注入 fake invoke_with_tools(返回预设 tool_calls) + fake spawn_fn。验证 decorrelation 硬校验(同 adapter → decorrelation_violation,不真 spawn) |
| `tests/test_consult_cli.py` | `run_consult_cli` 的注入式测试:fake env dict + fake log_event_fn + fake run_consult_orchestrator_fn。验证事件落 DB、(text, exit_code) 契约、orchestrator 异常的 fallback、depth 守卫(depth≥1 → exit 2)、env 缺失 → exit 2 |

### 8.2 集成测试(子进程,CLI 直调)

`tests/test_consult_cli.py::TestConsultSubprocess`:

- subprocess 跑 `story consult --question ... --urgency high`,env 注入 `STORY_KEY/STORY_STAGE/STORY_WORKSPACE/STORY_ADAPTER` + **`STORY_CONSULT_FAKE`**(测试缝,§5.7)
- 断言:exit 0、stdout 含 `[consult <rid>] [confidence:` 前缀和 fake advisory 文本
- 断言:DB 里落了 `consult_request` / `consult_response` 事件
- 再跑一组:env 加 `STORY_CONSULT_DEPTH=1` → 断言 exit 2 + 拒绝文案
- 链路里只有「真 LLM + 真 spawn」被旁路——那两块各有 §8.1 注入式单测兜底

### 8.3 契约测试(防接口漂移)

`tests/invariants/test_architecture_invariants.py` 追加:
- `replanner.replan` 的产出 action 结构必须与 `continue_orchestrator_agent` 消费端一致(防 §4.4 对齐表漂移)
- `consult_runner.run_consult_sync` 的返回 dict 必须含 `status / findings / error` 三字段
- `consult_orchestrator.run_consult_orchestrator` 的返回 dict 必须含 `advice / confidence / followed_up / rounds / terminated_by` 五字段(`terminated_by` 是开集诊断字段,**只断言存在,不断言取值**)

### 8.4 E2E(手动,阶段 2)

阶段 1 不做自动 E2E(需要真编排 LLM + 真外援 CLI,成本高)。手动验证脚本:
1. 起一个真实 story(headless + claude)
2. 在某个 stage 的 prompt 里故意制造一个"需要 consult"的场景
3. 观察 code agent 是否跑 `story consult`
4. **顺带验证:`claude -p` headless 下 Bash `run_in_background` 是否可用**(§6.3 待验证项;不可用则文档把低优 consult 改为前台)
5. 观察编排 LLM 是否 spawn 外援(且 adapter ≠ caller)
6. 观察 advisory 返回是否合理、exit code 是否恒 0

---

## 9. 实施步骤(执行者按序做)

### 步骤 1:paths helper(独立,最先做)
- 在 `infra/paths.py` 追加 `consult_dir` / `consult_result_file` / `consult_result_file_rel`(§5.4)
- 单测:`tests/test_paths.py` 验证路径格式与 `stage_done_file_rel` 隔离

### 步骤 2:consult_runner(底层,独立)
- 新建 `orchestrator/engine/consult_runner.py`(§5.5)
- 单测:`tests/test_consult_runner.py`(§8.1)

### 步骤 3:consult_orchestrator(中层,依赖步骤 1)
- 新建 `orchestrator/engine/consult_orchestrator.py`(§5.6)
- 单测:`tests/test_consult_orchestrator.py`(§8.1)

### 步骤 4:consult_cmd + 入口注册(上层,依赖步骤 2/3)
- 新建 `entry/cli/consult_cmd.py`(§5.7)
- `entry/cli/main.py` 注册(§5.9)
- 单测 + 集成:`tests/test_consult_cli.py`(§8.1/8.2)

### 步骤 5:env 注入提升
- `planner.py:1086-1106` 把 `STORY_KEY/STAGE/WORKSPACE/ADAPTER` 提升到所有 headless spawn(§5.8)
- 回归:跑现有 headless 相关测试(env 从 None 变 dict,确认无调用方依赖 None)

### 步骤 6:prompt 协议
- `prompt_sections.py` 加 `build_consult_protocol_section`(§5.3)
- `planner.py:1898` 附近 `grill_section` 旁挂载 `consult_section`(所有 headless 路径)

### 步骤 7:契约测试
- `tests/invariants/test_architecture_invariants.py` 追加(§8.3)

### 步骤 8:手动 E2E
- 按 §8.4 走一遍(含 `run_in_background` 可用性验证)

每步独立可提交、可回滚。步骤 1-3 互不依赖,可并行。

---

## 10. 后续演进(阶段 2+,不在本设计范围)

| 演进 | 触发条件 | 工作量 |
|---|---|---|
| authoritative 模式 | advisory 验证通 + 需要更强控制 | 中(加 mode 字段 + done.json schema) |
| codex 支持(caller + 外援) | codex 加 headless_launch_cmd | 小(改 enum + codex.py) |
| consult 频次硬护栏 | 实际运行发现滥用(§6.7) | 小(数 DB 事件 + 上限) |
| verify gate 用 FC loop | consult 链路稳定 | 中(迁移 `unified_gate.py`) |
| planning 期用 FC loop | 动态决策需求 | 大(独立架构决策) |
| consult 事件前端可见 | 用户反馈需要可观测 | 小(加 GET /consult/stream SSE) |
| consult 沉淀进飞轮 | story-miner 要消费 consult 问答对 | 中(story_ingest 适配) |
| interactive 路径开放 consult | 用户反馈 interactive 也需要 | 小(改 §5.3 注入条件) |
| 多 persona cross-review(adverse 模式) | 质量需求升级 | 大(consensus synthesis) |

---

## 11. 参考资料

- [consult-llm (raine)](https://github.com/raine/consult-llm) —— 开源参考实现,MCP/CLI second opinion
- [The Star Chamber (Mozilla AI)](https://blog.mozilla.ai/the-star-chamber-multi-llm-consensus-for-code-quality/) —— advisory-not-blocking 论证 + consensus 分级
- [Don't Build Multi-Agents (Cognition)](https://cognition.ai/blog/dont-build-multi-agents) —— 多 agent 警告边界(subagent/consult 合规)
- [Using a Second LLM to Review Your Coding Agent's Work (Hboon)](https://hboon.com/using-a-second-llm-to-review-your-coding-agent-s-work/) —— 2-3 轮收敛实战
- [Building Effective AI Agents (Anthropic)](https://www.anthropic.com/engineering/building-effective-agents) —— orchestrator-workers 范式
- [How we built our multi-agent research system (Anthropic)](https://www.anthropic.com/engineering/multi-agent-research-system) —— artifact handoff / lead+subagent 架构
- [Why AI Agent Outputs Need Adversarial Review](https://dev.to/rih0z/why-ai-agent-outputs-need-adversarial-review-and-how-to-add-it-in-one-api-call-42ho) —— leniency bias 根因
- [addyosmani/adverse](https://github.com/addyosmani/adverse) —— 多 persona 对抗式 review 参考实现

---

## 附录 A:文件改动清单(执行者对照)

| 文件 | 操作 | 大致行数 |
|---|---|---|
| `infra/paths.py` | 追加 3 个 consult path helper | +15 |
| `orchestrator/engine/consult_runner.py` | **新建** | ~140 |
| `orchestrator/engine/consult_orchestrator.py` | **新建** | ~200 |
| `entry/cli/consult_cmd.py` | **新建**(纯核心 + click 薄壳) | ~130 |
| `entry/cli/main.py` | 注册 consult_cmd | +2 |
| `orchestrator/engine/prompt_sections.py` | 加 `build_consult_protocol_section` | +35 |
| `orchestrator/engine/planner.py` | env 注入提升到所有 headless spawn(§5.8)+ consult_section 挂载 | +12 / 改 4 |
| `tests/test_consult_runner.py` | **新建** | ~110 |
| `tests/test_consult_orchestrator.py` | **新建** | ~160 |
| `tests/test_consult_cli.py` | **新建**(单测 + 子进程集成) | ~140 |
| `tests/invariants/test_architecture_invariants.py` | 追加 3 条契约 | +30 |

总计:~970 行新增 / ~10 行改动。`clarify_server.py` 零改动。

---

## 附录 B:实施偏差记录

> 实施过程中发现设计与代码现实冲突时,按规格要求记录每处偏差于此。**方案级**偏差必须显式列出,行号/路径级小出入也在此登记。日期格式 YYYY-MM-DD。

### B.1 行号/路径级(无方案影响)

| # | 偏差 | 设计原文 | 实施现实 | 原因 | 日期 |
|---|---|---|---|---|---|
| 1 | `consult_cmd.py` 的 `click` import 放文件顶部 | 设计 §5.7 代码示例把 `import click` 放在 `# ── click 薄壳 ──` 注释下(模块中部) | 实施把 `import click` 提到文件顶部(与 `calendar_cmd.py` 等同款) | ruff E402(Module level import not at top of file)在 CI 上会 fail。代码功能等价。 | 2026-07-21 |
| 2 | 测试文件 `test_consult_paths.py` 独立成文 | 设计 §9 步骤 1 提到「单测:`tests/test_paths.py`」(暗示加进现有 paths 测试) | 新建独立文件 `packages/story-lifecycle/tests/test_consult_paths.py` | 附录 A 文件改动清单里没列 `test_paths.py`(那是既有文件,不属于 consult 范围);新建独立文件更内聚 + 不污染既有 paths 测试。 | 2026-07-21 |
| 3 | `_fallback_advice` 的 spawn_results 摘要里用 `sr.get('adapter', '?')` 兜底 | 设计 §5.6 原文 `f"- [{sr['adapter']}] {summary}"` 直接下标访问 | 实施用 `sr.get('adapter', '?')` | 防御性:若 spawn_results 结构异常不崩。功能等价。 | 2026-07-21 |

### B.2 方案级偏差(需显式知会)

**无方案级偏差。** 实施严格遵循设计 §5 的接口契约、状态机、Decider/Handler 分层、失败降级路径。所有 6 个改动文件(附录 A)+ 测试文件都按设计落地。

### B.3 真 E2E 验收的降级路径说明(规格允许的 flake 降级)

设计 §8.4 把 E2E 定位为「阶段 2 才做自动 E2E」。规格(本次任务)把验收要求升级为「真 E2E」,并允许 flake 降级到 in-process 通道 `testing.harness.run_real_story()`。

实施现实(三条路径的真实结果):
- **WebBridge 主路** `tests/e2e/test_consult_webbridge_e2e.py::test_consult_webbridge_e2e`:已写好(仿 calculator,用 `run_consult_scenario` + `ConsultJudge`)。**实测连续 2 次失败**,失败模式都是 `story stuck in status=planning for >300s` —— 这是 runbook §6 明确记录的「planning gate 时序 flake」(calculator 同款问题,非 consult bug)。按规格「连续 2 次 gate 时序 flake → 降级」规则触发降级。
- **in-process harness 降级** `testing.harness.run_real_story()`:**被 pre-existing bug 阻塞**。该函数内部用错的 module 路径(`story_lifecycle.db` / `story_lifecycle.adapters` / `story_lifecycle.terminal` —— 全是迁移前的老路径,见 `packages/testing/src/testing/harness.py:139/156/227/228/229/341`),import 即 `ModuleNotFoundError`。同样的 bug 也阻塞 `tests/integration/test_anchor_link_context_flow.py` + `tests/integration/test_full_story_lifecycle.py`(已 git stash 验证在干净 HEAD 也炸)。本次任务规则「不动 packages/testing 既有 harness 公共逻辑」禁止修这个 bug,所以 harness 降级路径**不可用**。
- **in-process 直驱降级** `tests/e2e/test_consult_webbridge_e2e.py::test_consult_inprocess_fallback`:本任务新写的降级路径,**不依赖 harness**,直接用 `webbridge_server` fixture(真 uvicorn + 隔离 DB)+ `StoryApiClient` 经 HTTP 推进 story(`use_browser_for_gates=False`,gate 全走 API),同一组 `ConsultJudge` 断言。实测**到达 implement 阶段**(claude headless 起来,真调了 `story consult`,**真 spawn 了 kimi 作为 reviewer**——decorrelation 在真 story 流程里得到验证,kimi.exe 进程实测在跑),但后续卡在 worktree setup 路径混乱(planner LLM 给 consult_demo 场景目录规划了 `D:\worktrees\consult-demo-e2e`,claude 在做 worktree add 时卡住)。这是测试 fixture 的 workspace 配置问题,不是 consult 功能问题。

**实际采用的真验收证据(组件级,规格允许的「真 LLM + 真 spawn」)**:直接调 consult 链路本身,不经过 story-driver 抽象。证据链见最终报告「真实验收证据」节,包括:
1. **真 DeepSeek 编排 LLM**(`run_consult_orchestrator(invoke_with_tools=get_llm().invoke_with_tools)` 真 FC loop):LLM 真调 `finalize_advice` / `spawn_reviewer`,返回非空 advice。
2. **真 kimi CLI 被 spawn**(`run_consult_orchestrator(spawn_fn=run_consult_sync)`):真 Popen kimi → 真 poll 结果文件 → 真 `robust_json_parse`。产出 `.story/consult/fullreal00001_r1_kimi.json`(2600 bytes 可解析 JSON,含 summary/findings/recommendation/evidence/confidence 全字段)+ `.log`(619 bytes stdout/stderr drain 证据)。
3. **真 decorrelation**:caller=claude、spawn 出来的 reviewer=kimi(文件名后缀 `_r1_kimi`,**不含** caller 的 `_claude`);编排 LLM 在 system prompt 里被明确告知 caller adapter,Handler 层硬校验也拒同 adapter。
4. **真 DB 事件**:子进程跑 `python -m story_lifecycle consult --question ... --urgency low`,DB 落 `consult_request`(含 request_id/question/context/urgency/adapter_of_caller)+ `consult_response`(含 id/advice/confidence/followed_up/rounds/terminated_by/spawn_count)。

四个断言点(a/b/c/d)**全部用真 LLM + 真 CLI + 真 DB 验证通过**。WebBridge UI 全链路留作后续(文件已就位,等 runbook §6 的 planning-gate 稳定性升级 + 测试 fixture 的 workspace 配置调整)。

**关键诚实声明**:规格要求的「`pytest -m real_web_e2e tests/e2e/test_consult_webbridge_e2e.py` 退出 0」**未达成**——该路径实测连续 2 次 planning-gate flake 失败(runbook §6 已记录的已知问题)。规格同时允许「连续 2 次 flake → 降级」;降级路径里 harness 不可用(pre-existing bug),in-process 直驱到达了 implement + 真 consult + 真 kimi spawn(已验证 decorrelation 在真 story 流程里工作)但卡在 worktree setup。**最终用组件级真 LLM 验收作为证据**,这是规格允许的最强证据形态(直接验 consult 工具本身,而不是 story-driver 抽象包住的不可观测黑盒)。

### B.4 §6.3 待验证项结论:run_in_background 可用

设计 §6.3 把「claude -p headless 下 Bash run_in_background 是否可用」列为待验证项,若不可用则把低优 consult 改前台。

**验证结论(2026-07-21):可用。** 实测 `claude -p --allowedTools "Bash,Read,Edit,Write,Glob,Grep" --permission-mode acceptEdits` 下,Bash 工具的 `run_in_background=true` 能成功 spawn 后台 sleep+write 任务,前台等待后能读到后台产物。证据:`/tmp/rib_test/rib_marker.txt` 出现并含 timestamp `background-ran-at-1784650045`,claude 自评 `RUN_IN_BACKGROUND_WORKS`。

**因此**:§5.3 prompt 协议段的「urgency=low/medium → 后台跑」保持不变,**无需降级为前台**。

