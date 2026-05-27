# Idea: StoryOS 与 Project Intelligence Control Plane

## 背景

Story Lifecycle 越做越像一个 code agent，但它的核心价值不应该是“再造一个会写代码的 agent”。底层 coding agent 已经由 Claude Code、Codex、Qoder、Gemini CLI 等工具承担。Story Lifecycle 更适合站在它们上面，成为：

```text
code agent 的操作系统 / 控制平面
```

它负责接入需求来源、理解项目上下文、选择执行路径、调度底层 agent、运行测试、收集证据、诊断失败、沉淀经验，并在下一次 Story 中反哺。

更准确的产品定位：

```text
Story Lifecycle is the operating layer for coding agents in real software projects.
```

也就是：不是替代 code agent，而是让 code agent 能在真实项目中稳定、可控、可观测、可复盘地工作。

## 外部命名调研

外部趋势说明几个词已经被大量占用：

1. **Agent OS / Agent Operating System**
   - 这个词已经被很多企业级 agent 平台使用，偏泛化 AI 基础设施。
   - 如果直接叫 Agent OS，容易显得范围过大，也容易和通用 agent 框架混在一起。

2. **AI Coding Agent Orchestrator**
   - 市场上已有多种把多个 coding agent 并行调度、跨 IDE 运行、共享 memory 的工具。
   - 这个词能描述调度，但不能表达 Story Source、Test Source、Project Intelligence、诊断和飞轮。

3. **Agentic Engineering Platform**
   - 这个词更像外部品类名，适合出现在 tagline 或 pitch 中。
   - 但如果作为产品名，会比较泛，缺少 Story Lifecycle 的差异。

4. **Project Brain / Project Memory**
   - 这类词强调项目知识，但太像 RAG/memory 产品。
   - Story Lifecycle 不只是记忆，它还管执行、门禁、诊断、测试和治理。

参考来源：

- LangGraph 强调 stateful workflows、human-in-the-loop 和 durable execution，这说明“可控编排”是 agent 系统落地的核心能力：https://langchain-ai.github.io/langgraph/
- OpenTelemetry 的 tracing/metrics/logs 模型说明 agent 可观测性最好走标准事件和 trace，而不是 UI 私有状态：https://opentelemetry.io/docs/
- 市场上已有多类 AI coding agent orchestration / agentic engineering platform / project context memory 工具，说明单纯叫 orchestrator 或 memory 不够区分。

## 命名结论

建议采用三层命名：

```text
产品名：StoryOS
品类名：Agentic SDLC Control Plane
核心模块：Project Intelligence Layer
```

### 1. 产品名：StoryOS

推荐使用 **StoryOS** 作为产品/愿景名。

理由：

- 承接现有 `Story Lifecycle` 的品牌，不需要另起炉灶。
- “OS” 表达操作系统/运行层/控制平面，但不是泛泛的 Agent OS。
- “Story” 指向真实软件工程入口：需求、Bug、PRD、TAPD、Jira、测试、发布。
- 命名短，可传播，可解释。

一句话解释：

```text
StoryOS is the operating layer for coding agents.
```

中文解释：

```text
StoryOS 是面向真实软件项目的 Code Agent 操作层。
```

### 2. 品类名：Agentic SDLC Control Plane

对外介绍时，不建议只说 “AI coding agent”。建议说：

```text
Agentic SDLC Control Plane
```

中文：

```text
AI 软件工程生命周期控制平面
```

它表达的是：

- SDLC：不是单次代码生成，而是从需求到测试、发布、复盘。
- Control Plane：负责策略、上下文、调度、观测、治理。
- Agentic：底层可以调用多个 agent/tool/model。

### 3. 核心模块：Project Intelligence Layer

建议把“熟悉用户项目”的能力明确命名为：

```text
Project Intelligence Layer
```

中文：

```text
项目智能层
```

它是 StoryOS 的核心壁垒。

它回答这些问题：

- 这个项目怎么启动？
- 哪些测试能验证这个 Story？
- 哪些模块不能随便改？
- TAPD/Jira 里的 Bug 跟哪些代码有关？
- 最近生产日志里有什么异常？
- 团队发布/回滚/灰度规则是什么？
- 当前 Story 应该读哪些 PRD、设计文档、历史缺陷、运行日志？

## 推荐术语表

| 术语 | 用途 | 说明 |
|---|---|---|
| StoryOS | 产品/愿景名 | Code Agent 操作层 |
| Story Lifecycle | 当前项目/开源工具名 | 保持仓库和 CLI 延续 |
| Agentic SDLC Control Plane | 品类定位 | 面向软件工程生命周期的 agent 控制平面 |
| Project Intelligence Layer | 核心模块 | 熟悉用户项目、生成上下文包、连接 source/test/log |
| Story Source | 输入源 | TAPD/Jira/本地 PRD/Bug/用户反馈 |
| Test Source | 验证源 | 单测、集成测试、SWE-bench、CI、线上验收 |
| Runtime Evidence | 运行证据 | event_log、done、terminal、debug packet、diagnostic bundle |
| Governance Layer | 治理层 | review gate、policy、autonomy、risk、audit |
| Flywheel | 学习闭环 | trace -> analyze -> pattern -> constraint/tool -> better run |

## 产品架构

```text
                           StoryOS
          Agentic SDLC Control Plane for Coding Agents

┌──────────────────────────────────────────────────────────┐
│ Story Sources                                             │
│ TAPD / Jira / PRD / Bug / 用户反馈 / 生产告警               │
└───────────────────────┬──────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────┐
│ Project Intelligence Layer                                │
│ repo scan / docs / domain rules / runtime logs / tests     │
│ -> Project Profile                                         │
│ -> Context Packet                                          │
│ -> Test Plan                                               │
│ -> Constraints                                             │
└───────────────────────┬──────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────┐
│ Orchestration Layer                                       │
│ profile / stage graph / planner / router / policy          │
│ design -> implement -> review -> test -> finalize          │
└───────────────────────┬──────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────┐
│ Agent Runtime                                             │
│ Claude Code / Codex / Qoder / Gemini / custom CLI          │
│ terminal / done protocol / zellij / headless               │
└───────────────────────┬──────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────┐
│ Test Sources & Verification                               │
│ unit / integration / CI / SWE-bench / production signal     │
└───────────────────────┬──────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────┐
│ Evidence & Diagnostics                                    │
│ event_log / debug packet / diagnostic bundle / traces       │
└───────────────────────┬──────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────┐
│ Governance & Flywheel                                     │
│ review gate / pattern / policy / project memory / learning  │
└──────────────────────────────────────────────────────────┘
```

## 核心循环

建议把长期产品闭环定义为：

```text
Story Source
  -> Project Intelligence
  -> Orchestrator Plan
  -> Agent Execution
  -> Test Source / Review Gate
  -> Diagnostic Trace
  -> Project Memory
  -> Next Story Better
```

这和“单次代码生成”不同。StoryOS 的复利来自项目级熟悉度：

- 第一次跑：需要用户告诉它怎么启动、怎么测。
- 第十次跑：它知道哪些模块容易炸、哪些测试最有效。
- 第一百次跑：它能根据 Story 类型自动选择上下文、测试和风险策略。

## Project Intelligence Layer

Project Intelligence Layer 是 StoryOS 的护城河。

### 输入资产

| 资产 | 示例 |
|---|---|
| 代码库 | hc-all、单体仓、多服务仓 |
| PRD/设计文档 | 本地 docs、产品文档、历史设计 |
| TAPD/Jira | story、bug、评论、验收标准 |
| Test Source | pytest、maven test、前端 test、CI pipeline |
| 运行信息 | 生产日志、错误码、慢查询、告警 |
| 用户反馈 | 工单、客服反馈、线上问题描述 |
| 团队规则 | 发布窗口、灰度规则、DDL 禁忌、回滚流程 |

### 输出产物

| 产物 | 用途 |
|---|---|
| Project Profile | 项目结构、启动方式、测试方式、模块边界 |
| Context Packet | 当前 Story 应读的最小上下文包 |
| Test Plan | 当前 Story 最有价值的验证路径 |
| Domain Constraints | 业务约束、安全约束、发布约束 |
| Risk Profile | 当前改动风险、影响范围、是否需要人工确认 |
| Tool Hints | 应该使用哪些工具/脚本/命令 |

### 关键设计

Project Intelligence 不能只是 RAG。它要分层：

```text
Fact Layer
  明确事实：文件、模块、测试命令、配置、接口、日志位置

Inference Layer
  推断：这个 bug 可能关联哪些模块，这个 Story 需要哪些测试

Constraint Layer
  约束：哪些文件不能改，哪些操作必须人工确认

Evidence Layer
  证据：为什么推荐这些上下文/测试/约束
```

LLM 可以参与推断，但事实层和约束层必须可审计。

### Project Intelligence Probe

StoryOS 可以调用 code agent 来理解项目上下文，但它必须是受控探查，而不是让 agent 自由探索项目。

推荐命名：

```text
Project Intelligence Probe
```

流程：

```text
StoryOS 提出明确问题
  -> 生成只读任务书和有限上下文包
  -> 调用 code agent 做项目探查
  -> 要求输出结构化 JSON + evidence
  -> StoryOS 校验路径、命令和 schema
  -> 落盘到 Project Profile / Evidence Store
```

适合交给 code agent 的只读探查任务：

- 找出项目启动命令和测试命令，并给出证据文件路径。
- 梳理某个业务模块的核心入口类、配置和测试目录。
- 根据 TAPD bug 推断可能关联模块，并标注置信度和证据。
- 检查 CI 配置、脚本入口、数据库迁移规则、发布规则。
- 总结 README、脚本、配置和目录结构中的工程约束。

输出必须区分 `facts` 和 `hypotheses`：

```json
{
  "facts": [
    {
      "type": "test_command",
      "value": "pytest tests/unit",
      "evidence": ["pyproject.toml", "tests/unit/"]
    }
  ],
  "hypotheses": [
    {
      "type": "module_mapping",
      "value": "claim calculation likely lives in src/claim/",
      "confidence": 0.72,
      "evidence": ["src/claim/service.py"]
    }
  ],
  "open_questions": []
}
```

边界：

1. **只读**：Probe 阶段默认禁止写文件、改配置、运行 destructive 命令。
2. **任务有边界**：不得发出“随便看看项目”这类开放任务。
3. **证据优先**：每个事实必须有文件路径、命令输出或日志片段作为 evidence。
4. **事实/推断分离**：找到的配置是 fact，模块关联判断是 hypothesis。
5. **落盘前校验**：StoryOS 校验路径存在、命令格式合理、JSON schema 合法。
6. **可缓存**：启动命令、测试命令、模块地图进入 Project Profile，不应每个 Story 重新探查。

因此 code agent 不只是 executor，也可以是受控的项目情报采集器：

```text
Code agent can be a controlled project intelligence collector.
```

但这个能力应放在 Project Intelligence Input Layer（v0.8），不应提前塞进 v0.6 的诊断地基。

## Story Source 与 Test Source

### Story Source

Story Source 是需求入口。

P0/P1 已有方向：

- manual story
- TAPD story/bug
- local PRD file

后续应扩展：

- Jira
- GitHub Issues
- GitLab Issues
- 生产告警
- 用户反馈工单

Story Source 不只是拉正文，还要产出结构化输入：

```json
{
  "source": "tapd",
  "source_id": "12345",
  "type": "bug",
  "title": "...",
  "body": "...",
  "acceptance_criteria": [],
  "attachments": [],
  "comments": [],
  "priority": "P1",
  "business_area": "claim"
}
```

### Test Source

Test Source 是验证闭环。

没有 Test Source，系统只能“完成任务”；有 Test Source，系统才能判断“做对了吗”。

Test Source 应支持：

- 从 repo 自动发现测试命令。
- 从项目配置声明测试命令。
- 从 TAPD/PRD 验收标准生成验证 checklist。
- 从 CI 获取失败信息。
- 从生产日志/告警获取异步结果。
- 从 SWE-bench/benchmark 获取引擎级评估。

Test Source 输出：

```json
{
  "test_source": "repo",
  "commands": [
    {"name": "unit", "cmd": "pytest tests/unit", "scope": "fast"},
    {"name": "integration", "cmd": "pytest tests/integration", "scope": "slow"}
  ],
  "selection_reason": "Story touches claim calculation module.",
  "required": true
}
```

## 与 Code Agent 的边界

StoryOS 不做：

- 代码补全
- IDE 内联编辑
- 自己实现 patch generation
- 取代 Claude Code/Codex/Qoder

StoryOS 做：

- 选择什么时候让哪个 agent 做什么。
- 准备上下文包。
- 注入项目约束。
- 收集 done、事件、日志、测试结果。
- 判断继续、重试、回退、暂停、人工确认。
- 生成诊断包。
- 从成功/失败中沉淀项目经验。

边界公式：

```text
Code Agent owns local coding action.
StoryOS owns project-level lifecycle control.
```

## 与现有路线图的关系

StoryOS 不是新方向，而是现有设计的上位收敛。

| 现有设计 | 在 StoryOS 中的位置 |
|---|---|
| Board 右侧诊断面板 | Evidence & Diagnostics |
| Orchestrator Agent | Orchestration Layer |
| 双飞轮 | Governance & Flywheel |
| SWE-bench runner | Engine Test Source / Eval |
| TAPD Source | Story Source |
| Review Gate | Governance Layer |
| Diagnostic Bundle | Runtime Evidence |
| Project Intelligence 双飞轮 | Project Intelligence Layer + Flywheel |

因此 roadmap 不需要推翻，而是可以在 v0.6-v1.0 里加一个统一叙事：

```text
v0.6: diagnostics and decision envelope
v0.7: engine eval and working memory
v0.8: project intelligence input layer
v0.9: dual flywheel and runtime blackboard
v1.0: StoryOS control plane baseline
```

## 命名备选

### 推荐：StoryOS

优点：

- 和 Story Lifecycle 一脉相承。
- 简短，有产品感。
- 能表达“操作层”，但不会泛化成所有 agent 的 OS。

风险：

- OS 一词有扩张感，需要 tagline 收束范围。

建议 tagline：

```text
StoryOS: the operating layer for coding agents.
```

### 备选：LifecycleOS

优点：

- 更强调软件生命周期。
- 比 StoryOS 更抽象。

缺点：

- 较长，不如 StoryOS 好记。

### 备选：ProjectOps Agent

优点：

- 强调项目操作。
- 更偏工程/DevOps。

缺点：

- 容易被理解成运维工具，不够贴近 coding agent。

### 备选：Agentic SDLC

优点：

- 品类感强。
- 适合论文/白皮书/路线图。

缺点：

- 不像产品名。

## 推荐对外表达

短版：

```text
StoryOS is the operating layer for coding agents.
```

中版：

```text
StoryOS connects story sources, project intelligence, coding agents, test sources, diagnostics, and learning flywheels into one controllable SDLC control plane.
```

中文版：

```text
StoryOS 是 Code Agent 的项目级操作层：接入需求、理解项目、编排 Agent、运行验证、打包诊断，并把每次执行沉淀为下一次更好的上下文。
```

更工程化版本：

```text
StoryOS is an Agentic SDLC Control Plane that turns coding agents from isolated executors into governed, observable, project-aware engineering workers.
```

## P0 落地建议

命名确立后，近期不要马上做品牌重构。先把 “StoryOS” 作为愿景写入文档，把代码和 CLI 保持 `story-lifecycle` / `story`。

P0 动作：

1. 在 README 增加一句定位：

```text
Story Lifecycle is evolving into StoryOS, the operating layer for coding agents.
```

2. 在 roadmap 顶部增加命名说明。
3. 在设计文档中统一使用：
   - 产品愿景：StoryOS
   - 当前实现：Story Lifecycle
   - 核心模块：Project Intelligence Layer
4. 不改包名、不改 CLI、不改数据库命名。

这样既能建立方向，又不会引入迁移成本。

## 风险

### 1. OS 叙事过大

应对：所有对外表达都加限定语：

```text
for coding agents
for real software projects
Agentic SDLC Control Plane
```

### 2. 和 code agent 竞争

应对：明确不做底层 coding action，保持 adapter 中立。

### 3. Project Intelligence 变成无限大

应对：先从三类项目资产开始：

1. repo scan
2. story source
3. test source

生产日志、用户反馈、发布规则后续再接。

### 4. 飞轮污染业务

应对：继续沿用双飞轮边界：

- Domain 经验不直接污染 Engine。
- Engine 策略不直接套到生产项目。
- 所有晋升都要有 evidence 和 policy。

## 结论

推荐命名：

```text
StoryOS
```

推荐品类：

```text
Agentic SDLC Control Plane
```

推荐核心模块：

```text
Project Intelligence Layer
```

这组三层命名能同时表达愿景、品类和工程边界：

- StoryOS：产品愿景，Code Agent 操作层。
- Agentic SDLC Control Plane：外部定位，区别于普通 coding agent。
- Project Intelligence Layer：核心壁垒，熟悉用户项目并反哺执行。
