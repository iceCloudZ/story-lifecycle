> ⚠️ **历史快照（归档于 2026-07）**：描述的架构可能已被后续演进取代。当前架构见 [../../ARCHITECTURE.md](../../ARCHITECTURE.md)。本文件保留作决策记录（ADR），正文未修改。

---

# Story Lifecycle 架构：Java 开发者视角

> 写给 10 年 Java 开发者的代码导航指南。用 Java 生态的概念类比，把隐式变显式。

> ⚠️ **本文档基于 LangGraph 时代的架构编写，已于 cb6f9cd (2026-06-13) 过时。** 文中 `plan_stage`/`review_stage`/`interrupt()`/`router_node` 等描述的节点链路、`planner.py` 行号均已失效。当前架构为 Function Calling 模式（`run_orchestrator_agent` + agent_tools 六工具 + `_plan_confirmed` HITL）。本文保留作 LangGraph→FC 迁移的对照参考，**请勿据此理解当前代码**。当前架构见 `docs/design-agent-orchestrator.md`。


## 1. 类型地图：StoryState 在运行时到底长什么样

Python 的 `TypedDict(total=False)` 对 Java 开发者来说是噩梦——所有字段可选，运行时动态注入。下面是它在运行时的**真实形态**，用 Java 表达：

```java
// 等价于 Python 的 StoryState — 一个在 LangGraph 节点间流转的对象
// 注意：所有字段都可能为 null，字段会在运行时被不同节点动态添加

public class StoryState {
    // ===== 初始化时必填 (graph.py:358-378) =====
    String storyKey;        // "FEAT-001"
    String title;           // "Add dark mode"
    String workspace;       // "/path/to/repo"
    String profile;         // "minimal"
    String currentStage;    // "design" → "implement" → "review" → ...
    String status;          // "active" | "paused" | "blocked" | "completed"
    String complexity;      // "S" | "M" | "L"
    Map<String, Object> context;  // 自由 JSON，各阶段产出都往里塞
    int executionCount;     // 当前阶段执行了几次
    String lastError;       // null = 正常，非 null = 有问题
    double stageStartTime;  // epoch seconds

    // ===== 运行时动态注入（没有构造函数保证！）=====
    String planSummary;           // Planner LLM 产出
    String reviewSummary;         // Reviewer LLM 产出
    Double trajectoryScore;       // 0.0-1.0，越低越危险
    Map<String, Object> plan;     // Planner 的结构化输出
    String _nextAction;           // Router 设置："advance"|"retry"|"skip"|"fail"|"wait_confirm"
    List<String> _pendingSubKeys; // 子 story keys
    Map<String, Object> _routerDecision; // Router 的 LLM 原始响应
    String _preRoutedAction;      // 对抗循环预设的路由（跳过 Router）
    int _epoch;                   // 用于取消陈旧线程
    boolean _cancelled;           // 强制停止标记
}
```

**关键区别**：Java 里你会用 Builder 模式或构造函数保证必填字段。Python 里这些保证不存在——你在 `nodes.py` 里看到 `state["_pre_routed_action"] = "wait_confirm"` 就是在运行时往字典里塞一个新 key。追踪一个字段从哪来、在哪变、到哪去，全靠 grep。

**对策**：把 `nodes.py:43-68` 的 `StoryState` 定义打印出来贴屏幕旁边，这就是你的"接口文档"。

## 2. 流程地图：一次完整的 Story 执行

用 Java 的话说，这是一个 **StateGraph ≈ Activiti/BPEL 工作流引擎**。但不是 XML 定义流程，是代码：

```
                         ┌── poll_completion ──┐
                         │  (轮询 .story/done/ │
                         │   {key}/{stage}.json)│
                         └────────┬────────────┘
                                  │
        ┌─────────────────────────┤
        │                         │
   done 文件存在              done 文件不存在
   且 JSON 合法              或会话已死
        │                         │
        ▼                         ▼
   review_stage              router_node
   (LLM 审查产出)            (决定下一步)
        │                         │
        ▼                         ├── advance (推进到下一阶段)
   router_node                    ├── retry   (重试当前阶段)
   (决定下一步)                   ├── skip    (跳过)
        │                         ├── fail    (标记失败)
        ├── advance               └── wait_confirm (暂停等人)
        ├── retry
        ├── fail
        └── wait_confirm
             │
             ▼
        interrupt() ← LangGraph 挂起，等 TUI 按 r 恢复
```

**完整路径跟踪**（`graph.py:214-275` 定义了这 10 个节点）：

```
START
  → plan_stage       (Planner LLM 分析 story 上下文，生成任务书)
  → execute_stage    (启动 AI CLI 在 Zellij session 里执行)
  → poll_completion  (轮询 .story/done/{key}/{stage}.json，最长 30 分钟)
  → review_stage     (Reviewer LLM 审查产出，可能触发对抗循环)
  → router           (决策引擎：advance/retry/skip/fail/wait_confirm)
  → advance          (清理当前阶段，进入下一阶段 → 回到 plan_stage)
  → END              (所有阶段完成)
```

**对照 Java**：如果你用过 Activiti/Camunda，`graph.py:214-275` 的 `buildGraph()` 就是 `bpmn.xml`。`nodes.py` 里每个 `xxx_node()` 函数就是一个 `JavaDelegate.execute()`。

## 3. 决策地图：到底谁在决定 Story 的命运

这是最让人困惑的部分。Java 项目通常是一个 Service 层做所有决策。Story Lifecycle 有 **5 个决策者**，权责分层：

```
                    ┌──────────────────────────────┐
                    │        Router (路由器)         │
                    │  8 条规则 + LLM 回退           │
                    │  决定: advance/retry/skip/     │
                    │        fail/wait_confirm       │
                    │  nodes.py:1392-1558            │
                    └──────────────┬─────────────────┘
                                   │
           ┌───────────────────────┼───────────────────────┐
           │                       │                       │
    ┌──────▼──────┐        ┌──────▼──────┐        ┌──────▼──────┐
    │ Policy Engine│        │    Gate     │        │  Planner    │
    │ (策略引擎)    │        │  (门禁)     │        │  (规划器)    │
    │              │        │             │        │             │
    │ 四态裁决:     │        │ 检查:        │        │ 生成:       │
    │ allow/reject │        │ review轮次  │        │ 阶段任务书   │
    │ needs_confirm│        │ 执行次数    │        │ 拆分建议    │
    │ shadow_only  │        │ 高严重性find│        │ 轨迹评分    │
    │              │        │ ings        │        │             │
    │ policy_engine│        │ gate.py     │        │ planner.py  │
    │ .py          │        │             │        │             │
    └──────────────┘        └──────────────┘       └──────────────┘
                                    │
                            ┌───────▼────────┐
                            │   Reviewer     │
                            │   (审查器)      │
                            │                │
                            │ 审查阶段产出:    │
                            │ pass/revise/fail│
                            │ 产出 findings   │
                            │                │
                            │ planner.py      │
                            │ :146-232        │
                            └────────────────┘
```

**决策优先级**（`router_node` 的判断顺序，`nodes.py:1392-1558`）：

```
1. _pre_routed_action 已设置？ → 直接使用（跳过所有判断）  ← 对抗循环设的
2. review_summary 有"达到重试上限"？ → fail
3. trajectory_score < 0.3？ → fail
4. 没有 last_error？ → advance（快乐路径）
5. 缺少 expected_outputs？ → fail
6. 有 review 说要重试？ → retry（审查驱动）
7. 执行次数超限？ → wait_confirm（人工介入）
8. 以上都不匹配？ → 调 LLM Router 决定  ← 非确定性
```

**记法**：把 Router 想成 `switch-case` + 一个 LLM 作为 `default` 分支。Policy Engine 是额外的校验层（"LLM 说要执行，但我得先检查预算/权限"）。

## 4. 协议地图：Done 文件 = 没有接口定义的 RPC

Java 里服务间通信靠接口（REST API、gRPC、MQ）。Story Lifecycle 里 orchestrator 和 AI CLI 之间通信靠**文件系统**：

```
Orchestrator (Java 等价: Service Layer)
    │
    │  1. 写入 prompt 到 Zellij session 的 stdin
    │  2. AI CLI 在 session 里工作（可能 30 分钟）
    │  3. AI CLI 完成工作后，写 .story/done/{key}/{stage}.json
    │  4. Orchestrator 的 poll_completion_node 轮询发现文件
    │  5. 解析 JSON → 快照 → 删除原文件（标记已消费）
    │
    ▼
AI CLI (Java 等价: 外部 Worker)
```

**这个协议没有 IDL**。等价于两个 Java 服务之间用一个共享目录 + JSON 文件通信，JSON 的 schema 没有 `.proto` / `.avsc` / OpenAPI 定义。

**Done 文件期望的字段**取决于 profile 配置的 `expected_outputs`（`profiles/minimal.yaml`），不是代码里定义的：

```yaml
# profiles/minimal.yaml — 这定义了 done JSON 应该有什么字段
stages:
  design:
    expected_outputs: [spec_path, complexity]
  implement:
    expected_outputs: [files_changed, summary]
```

所以同一个 `design` 阶段，用 `minimal.yaml` 要求 `spec_path` + `complexity`，用 `swebench.yaml` 可能要求别的字段。**没有编译期保证**——如果一个 AI CLI 写了 `{"spec_path": 42, "complexity": "???"}` （类型错误），系统只检查 key 存在，不检查 value 类型。

**对策**：把 `profiles/minimal.yaml` 当 WSDL/IDL 读，把 `nodes.py:168-194` 的 `robust_json_parse()` 当容错反序列化器。

## 5. 对抗循环地图：两个 LLM 互相博弈

这是整个系统最复杂的部分。Java 里你不会有两个线程互相审查对方的产出、打回修改、最多 3 轮。

```
plan_stage 产出任务书
    │
    ▼
review_plan (对抗审查)
    │
    ├── pass → 进入 execute
    ├── revise → 回到 plan_stage 修改（最多 3 轮）
    ├── no_progress → wait_confirm（连续无改善，人来看）
    └── max_rounds → wait_confirm（达到上限）

execute_stage 产出代码
    │
    ▼
review_stage (对抗审查)
    │
    ├── pass → advance
    ├── revise → 回到 execute_stage 修改（最多 3 轮）
    ├── no_progress → wait_confirm
    └── max_rounds → wait_confirm
```

**Java 类比**：这相当于一个代码审查流程，但审查者和被审查者都是 LLM。关键文件：
- `evaluator_loop.py` — 循环控制器（决定是否再来一轮）
- `loop_events.py` — 循环事件记录（用于 TUI 展示循环状态）
- `planner.py:146-232` — Reviewer 实现（`review_stage()` 函数）

## 6. 导航速查表

| 你想知道... | 看这个文件 | Java 等价 |
|------------|-----------|----------|
| 整体流程怎么走 | `graph.py:214-275` | `bpmn.xml` / Activiti 流程定义 |
| 每个节点做什么 | `nodes.py` | `JavaDelegate` 实现类 |
| StoryState 有哪些字段 | `nodes.py:43-68` | 领域对象接口 |
| CLI 怎么启动的 | `tools/base.py` | ProcessBuilder 封装 |
| done 文件路径怎么定的 | `paths.py` | PathResolver |
| 谁决定下一步 | `nodes.py:1392-1558` (router_node) | Router/Dispatcher |
| LLM 怎么被调用的 | `planner.py`, `router.py` | HTTP Client to external service |
| 数据库表结构 | `db/models.py` | DDL / JPA Entity |
| API 端点 | `api.py` | @RestController |
| TUI 面板布局 | `tui.py:1114-1129` | Vue/React 组件树 |
| 配置怎么加载 | `setup.py`, `main.py:63-94` | @Configuration + Environment |
| 测试怎么写 | `tests/e2e/` | 集成测试 |

## 7. 理解路径建议

按这个顺序读，每天一个模块：

**Day 1**：`graph.py` + `nodes.py:43-68`（StoryState 定义）
→ 搞清楚 10 个节点是什么，StoryState 有哪些字段

**Day 2**：`nodes.py` 里的 `plan_stage_node` → `execute_stage_node` → `poll_completion_node`
→ 跟踪一次正常的 stage 执行

**Day 3**：`nodes.py` 里的 `router_node`（1392-1558）
→ 理解 8 条决策规则的优先级

**Day 4**：`paths.py` + `tools/base.py:134-261`（无头执行 + 合成 done）
→ 理解 done 文件协议和文件路径

**Day 5**：`evaluator_loop.py` + `planner.py:146-232`（review_stage）
→ 理解对抗循环

每个模块控制在 300-500 行，不是整个文件。关键是**画图**——把函数调用关系画成时序图，把状态变迁画成状态图。
