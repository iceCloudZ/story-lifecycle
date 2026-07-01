# 双飞轮：用户项目知识飞轮与 Story Lifecycle 引擎飞轮

> ⚠️ 历史快照（ISS-008, 2026-07）：本文描述的 dual-flywheel 实现
> (`orchestrator/flywheel/` 的 domain/engine/promotion) 已被删除——它是未接线
> 的设计，被更简单的活 quality flywheel（`db.models` 的 Finding/LearnedPattern +
> `seeds`/`seed_pipeline`/`quality`/`service`）取代。本文保留作设计决策记录。

> 状态：Idea 阶段  
> 日期：2026-05-26  
> 背景：用户本地拥有真实业务资产（`hc-all` 代码库、PRD、设计文档、TAPD story/bug、生产运行信息、用户反馈等），Story Lifecycle 本身也在通过 SWE-bench、headless、Zellij、router、review gate 等机制持续进化。两类资产、目标和风险不同，应该设计成两个隔离但可互相反哺的飞轮。

## 1. 背景

Story Lifecycle 最初解决的是“把一个 story 按设计、实现、测试、评审等阶段稳定跑完”的编排问题。这个阶段的核心能力是流程控制：渲染 prompt、启动 AI CLI、等待 done 信号、路由 retry/fail/advance、记录 stage log。它更像一个执行引擎，目标是把单次任务跑通。

随着 headless、SWE-bench、Zellij、review gate、finalize gate 逐步加入，系统开始出现第二层诉求：每一次运行不应该只是完成一次任务，还应该沉淀出下一次能用的经验。一次失败的 SWE-bench run，可能暴露 router 决策不准、prompt contract 不清、artifact gate 太弱、retry 策略无效；一次成功的修复，也可能说明某个 stage 拆分、某个检查工具、某类 review feedback 是有效的。也就是说，Story Lifecycle 不只是需要“执行”，还需要“从执行结果里学习”。

与此同时，用户本地已经有一批比公开 benchmark 更有价值的真实工程资产：`hc-all` 代码库、PRD、设计文档、接口文档、TAPD story/bug、线上运行信息、用户反馈、发布记录、回滚记录和历史 hotfix 经验。这些资产记录的是一个真实业务系统长期演化后的上下文。它们不是通用软件工程知识，而是“这个组织、这个业务、这些服务、这些生产约束”下的工程知识。

因此，当前系统面对的学习来源实际有两类。

第一类是真实业务项目资产。它们能回答：

- `hc-all` 里哪些服务负责哪些业务边界。
- PRD 和设计文档里的业务规则最终落在哪些接口、表、字段、消息和状态机上。
- TAPD story/bug 常见地对应哪些服务、模块、历史缺陷和测试入口。
- 哪些改动必须检查 Nacos、DDL、灰度、日志、监控、回滚和发布窗口。
- 生产日志、监控告警、用户反馈中的某类症状通常对应哪些根因。
- 哪些用户反馈代表真实业务阻塞，哪些只是体验优化或长期需求。

这类知识的目标是让 AI 越来越懂用户自己的业务系统。它服务的是 `hc-*` 这类本地项目中的需求拆解、代码定位、风险识别、测试建议、上线验证和事故复盘。

第二类是 Story Lifecycle 自己的运行 trace。系统在执行 story、SWE-bench、headless run、Zellij run、review gate、router retry、finalize patch gate 时，会产生大量可学习信号：prompt_context、execute、route_decision、dod_check、review finding、repair packet、patch outcome、eval score。这些 trace 能回答：

- 哪种 stage 拆分更有效。
- router 应该何时 retry、fail、rollback、wait_confirm。
- prompt contract 怎么写更稳定。
- headless 和 Zellij 哪些执行边界容易失效。
- finalize 是否真的产出可评估 patch。
- 哪些 review feedback 能提高下一轮修复率。
- 哪些 pattern 应该从文本建议升级成执行约束或工具。

这类知识的目标是让 Story Lifecycle 这个“AI 软件工程编排器”本身越来越强。它服务的是更稳的协议、更准的路由、更好的 profile、更强的 gate、更有效的工具和更高的 SWE-bench / 真实项目成功率。

关键问题在于：这两类知识都很有价值，但不能混在一个池子里。

如果把 `hc-all` 的业务经验直接写成全局 engine rule，可能污染其他项目。例如“某个服务的某个字段必须查某个 Nacos key”只对当前业务域成立，不应该影响 SWE-bench 或其他代码库。如果把 TAPD 中某类业务 bug 的修复套路当成通用策略，也可能让 AI 在无关项目里过拟合错误上下文。

反过来，如果把 SWE-bench 中学到的通用执行策略直接覆盖业务项目，也可能忽略生产变更的风险。例如 benchmark 里可以更激进地尝试 patch、回滚、重跑测试；但生产 hotfix 必须考虑灰度、日志、监控、回滚、用户影响和发布节奏。一个对 benchmark 有利的策略，不一定可以无条件用于真实业务系统。

所以这里的核心设计判断是：系统需要两个相互隔离、但能在验证后互相反哺的飞轮。

一个是用户项目知识飞轮，负责吸收本地业务资产，让 AI 越来越懂具体项目；另一个是 Story Lifecycle 引擎飞轮，负责吸收运行 trace 和 eval 结果，让编排器越来越会驱动 AI 做软件工程。两者不共享原始敏感数据，不共享默认激活范围，只共享经过脱敏、抽象、验证后的 pattern、constraint 和 micro-tool。

因此，需要设计两个飞轮：

```text
Domain Engineering Flywheel
  让 AI 越来越懂用户本地业务项目

Engine Improvement Flywheel
  让 Story Lifecycle 越来越会编排 AI 做软件工程
```

它们应该隔离原始数据、隔离激活范围，但允许通过“脱敏、抽象、验证后的 pattern / constraint / tool”互相反哺。

## 2. 为什么是两个飞轮

### 2.1 目标不同

用户项目飞轮的成功标准是业务效果：

- TAPD bug 修得更快。
- PRD 到设计再到实现更稳定。
- 生产问题定位更准。
- 用户反馈能更快转成可执行 story。
- 发布风险更低。

Story Lifecycle 引擎飞轮的成功标准是编排能力：

- SWE-bench resolve rate 提升。
- router 决策更准。
- retry 更少无效循环。
- headless/Zellij 链路更稳定。
- prompt contract 更少歧义。
- review gate 更能发现真实问题。

### 2.2 数据敏感度不同

用户项目资产可能包含：

- 内部业务逻辑。
- 客户信息。
- 生产日志。
- 用户反馈。
- TAPD 私有需求。
- 线上配置。

这类数据不能直接进入全局 engine 训练或通用 pattern。

引擎飞轮数据通常是：

- 运行 trace。
- SWE-bench instance。
- router decision。
- stage outcome。
- benchmark score。

它可以更容易脱敏和泛化。

### 2.3 生命周期不同

业务项目知识会随业务变化而变化。例如某个服务迁移、某个字段废弃、某个流程改造后，旧 pattern 可能失效。

引擎知识更偏长期。例如“finalize 必须有 artifact gate”“synthetic 不能跨阶段污染”“router retry 多次应切换策略”这类规则具有更强泛化性。

### 2.4 激活范围不同

业务 pattern 应该只在匹配项目、repo、模块、TAPD 类型、环境时启用。

引擎 pattern 可以作为 Story Lifecycle 默认行为或 profile 级行为启用。

## 3. 两个飞轮的定义

## 3.1 Domain Engineering Flywheel

目标：让 AI 越来越懂用户的本地业务系统。

输入资产：

- `hc-all` 代码库。
- PRD 和设计文档。
- TAPD story / bug / 评论 / 状态。
- 生产日志、监控、告警、运行指标。
- 用户反馈。
- 发布记录、回滚记录、事故复盘。
- 历史 story 执行 trace。

循环：

```text
业务资产
-> Story/Bug 执行
-> 代码变更 + 测试 + 发布/验证结果
-> 用户反馈 / 生产信号
-> 业务域 pattern / constraint / tool
-> 下一次更懂 hc-* 项目
```

产出资产：

- domain pattern：业务域经验。
- domain constraint：项目级执行约束。
- domain micro-tool：项目级工具。
- domain knowledge graph：服务、接口、字段、配置、数据流关系。
- incident memory：线上问题和修复经验。

例子：

```text
TAPD bug 标题包含“邮件审批状态不一致”
-> 优先检查 hc-email-gov 服务
-> 同时检查审批流状态枚举和消息回调日志
-> 若涉及生产问题，必须先查询生产日志再改代码
```

这条经验只应在相关 hc-* 项目、相关模块、相关 bug 类型下启用。

Domain 飞轮必须补齐自己的 Eval 北极星。Engine 飞轮有 SWE-bench 作为相对明确的二元反馈，但业务项目没有天然的公开判分器。TAPD bug 关闭不一定代表修得好，PRD 评审通过也不一定代表实现没有遗漏。因此 Domain trace 必须绑定更接近生产真实结果的终态信号：

- Deployment as Eval：story 最终发布到生产，且观察窗口内无回滚、无 P0/P1 告警、无同类用户投诉，才可标记为 domain_resolved。
- Rollback as Eval：发布后回滚、触发高优告警、出现同类投诉或二次 hotfix，应标记为 domain_failed 或 domain_regressed。
- Review Gate as Eval：人工在 review gate 中明确 pass/skip 才算强通过；accept risk 只能记录为有风险通过，不能作为高置信成功样本。
- Verification as Eval：接口回归、监控指标、日志断言、灰度结果、用户验收可以作为辅助分数，而不是替代最终生产信号。

这意味着 Domain 飞轮的训练样本不能只记录“AI 做了什么”，还必须记录“业务世界最后怎么回应”。没有明确终态信号的 trace 只能进入 evidence pool，不能直接晋升为 active pattern。

Domain Eval 还存在时序差异。SWE-bench 的 eval 是同步的，测试跑完就有 resolved/failed；真实业务发布后的结果是异步的，可能需要 24 小时、3 天甚至一周观察窗口。因此 Domain trace 必须有成熟度，而不是只有成功/失败。

```text
Green Trace
  Story 刚完成，只有 completed，没有发布和生产信号。
  只能作为弱参考，不能激活 pattern。

Yellow Trace
  已发布，处于观察期。
  可以作为候选 evidence，但不能作为强正向样本。

Red Trace
  发布后回滚、P0/P1 告警、同类投诉复发或二次 hotfix。
  立刻转化为强负向样本。

Blue Trace
  观察期通过，无回滚、无高优告警、无同类复发。
  可以作为强正向样本。
```

飞轮只能基于 Blue/Red Trace 转动。Green/Yellow Trace 可以用于检索和人工参考，但不能直接驱动自动 promotion。这要求系统具备长周期状态回写能力：story 完成后，后续发布、监控、回滚、reopen、用户反馈仍能回写到同一条 trace。

## 3.2 Engine Improvement Flywheel

目标：让 Story Lifecycle 越来越会编排 AI。

输入资产：

- event_log trace。
- SWE-bench outcome。
- headless/Zellij 执行结果。
- router decision 和 retry 结果。
- review gate finding。
- prompt context 和 stage contract。
- finalize patch gate outcome。

循环：

```text
Run
-> Outcome-labeled Trace
-> DPO / Classifier / Rule Mining
-> Router + Constraints + Micro-tools
-> Better Run
```

产出资产：

- engine pattern：通用软件工程编排经验。
- router preference sample：决策偏好数据。
- prompt contract rule：提示词协议规则。
- execution constraint：执行层安全约束。
- engine micro-tool：通用工具。

例子：

```text
finalize 阶段没有 model_patch 且 git diff 为空
-> 不能将 story 标 completed
-> router 应 fail/export_failed
-> TUI 必须展示 empty_patch
```

这条经验可以泛化到所有 artifact-driven profile。

## 4. 存储边界

统一使用 `.story/`，但内部区分 domain 和 engine。

```text
.story/
  domain/
    knowledge/
    patterns/
    constraints/
    tools/
    incidents/
    tapd/
    production/

  engine/
    traces/
    patterns/
    constraints/
    tools/
    router/
    evals/

  shared/
    abstractions/
    promoted_patterns/
    validated_constraints/
```

原则：

1. 原始业务数据只进入 `domain/`。
2. 原始 engine trace 只进入 `engine/`。
3. `shared/` 只存放脱敏、抽象、验证后的资产。
4. 任何从 `domain` 提升到 `engine/shared` 的内容必须经过脱敏和适用范围收窄。
5. 任何从 `engine` 下发到 `domain` 的策略必须经过项目 profile 选择或显式开启。

## 5. 互相反哺机制

两个飞轮不是完全隔离。它们通过三类资产交换经验。

### 5.1 Pattern Promotion

从 domain 到 engine：

```text
hc-all 多次出现 boolean 表达多状态导致 bug
-> 抽象为 engine architecture trigger:
   “跨系统状态超过 true/false 时必须建模为 enum/tagged state”
```

从 engine 到 domain：

```text
SWE-bench 发现 artifact gate 能防止 completed + empty patch
-> domain release profile 要求 hotfix 必须有验证产物和回滚说明
```

Pattern Promotion 不能从“某个样本看起来有效”直接跳到“全局启用”。特别是 domain -> engine 的提升存在抽象税：抽象不足会泄露业务细节，抽象过度会把特例包装成公理。因此 promotion 必须至少经历三个状态：

```text
proposed -> sandbox_validated -> active
```

其中 `proposed` 只表示候选，不参与默认注入；`sandbox_validated` 表示它通过了小范围验证，可以被显式 profile 选择；`active` 才表示可以进入默认策略或推荐策略。

对于 domain -> engine 的候选，应优先用 SWE-bench 或内部脱敏 fixture 做 smoke validation。例如从 hc-all 中提取出“跨系统状态不应使用 boolean 表达多状态”后，可以选择 3-5 个相似的公开或脱敏实例注入该 pattern，观察 resolve rate、patch size、test regression、review finding 是否恶化。只有不降分或有明确收益时，才允许升级。

对于 engine -> domain 的候选，应经过 domain profile 的生产约束过滤。例如 SWE-bench 中学到的激进 retry 或大范围重构策略，即使在 benchmark 上有效，也不能直接进入生产 hotfix profile。

### 5.2 Constraint Promotion

从 domain 到 engine：

```text
生产 hotfix 中直接修改配置风险高
-> 抽象为 engine constraint:
   “涉及 production config 的 story 必须先进入 review gate”
```

从 engine 到 domain：

```text
headless 经验表明 synthetic 不能跨阶段污染
-> domain story 中 AI 自动生成的生产诊断结论必须 stage-scoped
```

这里的核心原则是“约束比建议更危险，也更有价值”。一条文本 pattern 误导时，AI 仍可能被 review gate 拉回来；一条执行 constraint 一旦启用，会直接改变 action space。因此 constraint promotion 必须保存：

- 触发条件。
- 禁止或强制的动作。
- 适用 profile。
- 例外条件。
- 回滚方式。
- 最近一次验证结果。

### 5.3 Tool Promotion

从 domain 到 engine：

```text
多个 hc-* bug 都需要查询 TAPD + 日志 + Nacos
-> 抽象为通用 incident_triage_tool 候选
```

从 engine 到 domain：

```text
engine 形成 stable patch extractor
-> domain release 也复用 artifact extractor 生成变更摘要
```

Tool Promotion 是飞轮的高级形态。低级形态是把经验写进 prompt，高级形态是把反复出现的动作固化为 micro-tool。

例如 Domain 飞轮发现：每次支付失败排查，Agent 都会重复执行“查 TAPD 上下文、查 Nacos 配置、查生产日志、查对账表、查发布记录”。这不应该长期停留为一段冗长 pattern，而应该生成候选工具：

```text
investigate_payment_failure()
  -> load_tapd_context()
  -> inspect_nacos_config()
  -> query_production_logs()
  -> inspect_reconciliation_tables()
  -> load_recent_releases()
```

工具候选默认只进入 `.story/domain/tools/candidates/`，必须经过人工审查、权限声明和小范围运行验证后，才能进入 domain profile。若工具内部逻辑被抽象成通用 incident triage 流程，才可进入 shared 或 engine。

但是 micro-tool 不能无限暴露给 Agent。Domain 飞轮持续运转后，某个项目可能沉淀出几十个甚至上百个工具；如果全部塞进 Agent tool list，会造成 Tool Choice Degrade，让模型在工具选择上失焦。

因此 P2 的 Tool Promotion 不应直接扩大 Agent 可见工具集，而应引入 Tool Router 或 Meta-tool：

```text
investigate_incident(symptom, module, context)
  -> retrieve candidate micro-tools
  -> rank by repo/module/symptom/outcome evidence
  -> run selected micro-tool with scoped permission
  -> return normalized findings
```

对 Agent 来说，工具列表保持稳定；对系统内部来说，micro-tool 的数量和能力可以随飞轮持续进化。Agent 只知道“我要调查事故”，具体调用哪个项目级工具由 Tool Router 根据上下文和证据选择。

两个飞轮之间也应该形成“免疫系统”。Engine 飞轮负责发现更快的通用策略，Domain 飞轮负责用生产安全约束拦截不适合业务环境的策略。最终被激活的不是单边最优，而是经过效率和安全博弈后的可执行策略。

## 6. 风险控制

### 6.1 业务知识污染 engine

风险：把 hc-all 特定经验错误提升为通用规则。

控制：

- domain pattern 默认只能 domain-scoped。
- promotion 必须要求 abstracted_reason 和 evidence。
- promoted pattern 必须去除服务名、客户名、具体配置值。
- promoted pattern 必须声明适用范围。

### 6.2 Engine 策略误伤业务项目

风险：SWE-bench 学到的 aggressive patch 策略不适合生产 hotfix。

控制：

- engine constraint 默认不自动应用到 domain。
- domain profile 明确选择启用哪些 engine policy。
- 涉及 production 的 story 默认更保守。

### 6.3 生产数据敏感性

风险：生产日志、用户反馈、配置泄漏到全局 trace 或 LLM。

控制：

- production connector 默认只保存摘要和 hash。
- 原始日志只在本地可读，不进入 engine/shared。
- pattern promotion 必须脱敏。

### 6.4 Pattern 半衰期

业务系统和 engine 都会变化。pattern 必须有半衰期：

```json
{
  "pattern_id": "domain-hc-email-001",
  "scope": "domain",
  "repo": "hc-email-gov",
  "evidence_count": 5,
  "last_verified_at": "2026-05-26",
  "half_life_days": 30,
  "status": "active"
}
```

长期未验证、命中后效果变差、代码结构变化过大的 pattern 自动降权。

### 6.5 Domain Eval 信号模糊

风险：Domain 飞轮没有 SWE-bench 这类客观判分器，容易把“看起来完成”误判为“真实有效”。

控制：

- 每条 domain trace 必须区分 `completed`、`released`、`verified`、`regressed`。
- `accept_risk` 不能作为成功样本，只能作为风险样本。
- 发布后观察窗口内的回滚、告警、投诉、二次 hotfix 必须回写 trace。
- 没有明确终态的样本只能作为弱 evidence，不能自动生成 active pattern。

### 6.6 Promotion 过度抽象

风险：LLM 或人工把具体业务经验抽象成过宽规则，导致 engine 或其他项目被误导。

控制：

- promotion 输出必须同时包含 `generalized_rule` 和 `lost_context`。
- shared abstraction 必须声明不适用范围。
- domain -> engine 候选必须经过 smoke validation。
- engine -> domain 候选必须经过 domain production constraint 过滤。

### 6.7 Micro-tool 爆炸

风险：Domain 飞轮沉淀大量 micro-tool 后，如果直接暴露给 Agent，会降低工具选择质量，甚至让 prompt 和 tool schema 膨胀到不可控。

控制：

- Agent 默认只看到少量稳定 meta-tool。
- micro-tool 由 Tool Router 内部检索、排序和调用。
- micro-tool 必须声明 scope、permission、input_schema、output_schema、last_verified_at。
- 低命中、低收益、长期未验证的 micro-tool 自动降权或归档。

### 6.8 Constraint 代码漂移

风险：constraint 比 pattern 更强。一条过期 constraint 可能直接改变执行路径。例如“修改 X 接口必须查 Y Nacos”，如果 Y Nacos 已下线，这条 constraint 会让 Agent 反复寻找不存在的配置。

控制：

- constraint 必须绑定 code_anchor 或 config_key_anchor。
- 执行 constraint 前必须做轻量 pre-flight check。
- anchor 不存在时，不执行该 constraint，并将其状态改为 suspended。
- suspended constraint 必须进入 doctor/report，提示人工确认是否废弃或更新。

## 7. 数据模型草案

### Domain Asset

```json
{
  "asset_id": "tapd-bug-1065520",
  "scope": "domain",
  "source": "tapd",
  "type": "bug",
  "repo": "hc-email-gov",
  "summary": "审批状态不一致",
  "sensitivity": "internal",
  "stored_at": ".story/domain/tapd/1065520.json"
}
```

### Domain Trace Outcome

```json
{
  "trace_id": "domain-hc-payment-20260526-001",
  "scope": "domain",
  "story_source": "tapd",
  "repo": "hc-payment",
  "completed": true,
  "released": true,
  "verified": true,
  "trace_maturity": "blue",
  "observation_window_hours": 24,
  "rollback": false,
  "p0_p1_alert": false,
  "same_issue_reopened": false,
  "outcome_label": "domain_resolved",
  "confidence": "high"
}
```

`trace_maturity` 可选值：

```text
green   completed only
yellow  released and observing
red     regressed / rollback / alert / reopen
blue    observation window passed
```

### Engine Trace

```json
{
  "trace_id": "run-real-3-stage-finalize",
  "scope": "engine",
  "source": "swebench",
  "stage": "finalize",
  "decision_point": "artifact_gate",
  "outcome": {
    "resolved": false,
    "failure_type": "empty_patch"
  }
}
```

### Shared Abstraction

```json
{
  "abstraction_id": "state-model-enum-required",
  "source_scope": "domain",
  "target_scope": "engine",
  "rule": "Cross-system state with more than two real states must be modeled as enum/tagged state, not boolean.",
  "evidence": ["domain-pattern-001", "domain-pattern-004"],
  "sensitivity": "safe",
  "status": "proposed"
}
```

### Promotion Record

```json
{
  "promotion_id": "promote-state-model-001",
  "source_scope": "domain",
  "target_scope": "engine",
  "status": "proposed",
  "generalized_rule": "Cross-system state with more than two real states must be modeled as enum/tagged state.",
  "lost_context": [
    "Original evidence came from hc-all approval and mail status flows.",
    "Original production configs and customer identifiers were removed."
  ],
  "validation": {
    "method": "swebench_smoke",
    "instances": [],
    "result": "pending"
  }
}
```

### Constraint Record

```json
{
  "constraint_id": "domain-hc-payment-nacos-001",
  "scope": "domain",
  "repo": "hc-payment",
  "rule": "When changing payment callback behavior, inspect the related Nacos callback timeout config before implementation.",
  "code_anchor": {
    "path": "src/main/java/com/example/payment/CallbackService.java",
    "symbol": "CallbackService"
  },
  "config_key_anchor": {
    "provider": "nacos",
    "key": "payment.callback.timeout"
  },
  "preflight": {
    "last_checked_at": "2026-05-26",
    "status": "passed"
  },
  "status": "active"
}
```

### Micro-tool Record

```json
{
  "tool_id": "domain-investigate-payment-failure",
  "scope": "domain",
  "repo": "hc-payment",
  "entrypoint": ".story/domain/tools/investigate_payment_failure.py",
  "visible_to_agent": false,
  "routed_by": "investigate_incident",
  "permissions": ["read_tapd", "read_logs", "read_config"],
  "last_verified_at": "2026-05-26",
  "status": "candidate"
}
```

## 8. P0 设计

P0 不做完整自动学习，只做资产分层和 trace 保存。

### P0.1 Domain Asset Index

为用户项目建立本地资产索引：

```text
story domain index
```

索引：

- repo 列表
- PRD / design docs
- TAPD story/bug metadata
- 本地事故/反馈文档
- 生产验证信号的索引位置，不直接复制敏感原文

输出：

```text
.story/domain/index/assets.jsonl
```

### P0.2 Engine Trace Export

复用 SWE-bench gradient flywheel 的 trace samples：

```text
.story/engine/traces/{run_id}/trace_samples.jsonl
```

### P0.3 Shared Promotion Queue

任何 domain -> engine 或 engine -> domain 的反哺，先进入 queue：

```text
.story/shared/promotion_queue.jsonl
```

默认状态为 `proposed`，不自动激活。

### P0.4 Domain Outcome Capture

P0 还需要给 Domain 飞轮留出 outcome 回写位置，否则后续无法判断业务样本是否真的成功。

输出：

```text
.story/domain/outcomes/{story_key}.json
```

P0 只要求人工或外部脚本可写入以下事实，不要求自动打通生产系统：

- 是否发布。
- 是否回滚。
- 是否有 P0/P1 告警。
- 是否有同类 TAPD reopen。
- 是否有用户反馈复发。
- review gate 的最终操作是 pass、accept_risk 还是 reject。
- 当前 trace_maturity 是 green、yellow、red 还是 blue。

## 9. P1 设计

### Domain Retrieval

P0 可以先用 JSONL 保存资产索引，但 P1 的 Domain Retrieval 不能只做精确字段匹配。真实业务知识常常是语义关联的，例如“退款未到账”可能关联到账务流转、支付渠道、对账脚本、消息回调和近期发布记录，而不是靠一个 TAPD 类型字段就能命中。

因此 P1 检索应采用：

```text
结构化过滤 + 语义向量召回 + 证据重排
```

结构化过滤根据：

- repo
- module
- TAPD type
- bug category
- production impact
- changed files

语义向量召回覆盖：

- PRD 段落。
- 设计文档段落。
- TAPD 标题、描述、评论。
- 脱敏生产日志摘要。
- 用户反馈摘要。
- 历史修复摘要。

证据重排需要优先考虑：

- 同 repo / 同模块。
- 近 90 天内验证过。
- outcome_label 为 domain_resolved。
- 与当前 story 的错误症状、字段、接口、服务名相似。

检索结果必须带证据链返回，不能只返回一句结论。

### Constraint Pre-flight

P1 在应用 domain constraint 前必须先做锚点预检：

```text
constraint matched
-> check code_anchor / config_key_anchor
-> anchor exists: apply constraint
-> anchor missing: suspend constraint + report
```

这一步必须发生在 prompt 注入前。否则 Agent 会被一个已失效的生产约束牵引，浪费时间甚至产生错误结论。

### Engine Policy Retrieval

执行任何 story 时，根据：

- profile
- stage
- adapter
- execution mode
- failure type

检索 engine policies。

### Merge Strategy

domain 和 engine 同时命中时，优先级：

```text
safety constraint > domain production constraint > engine execution constraint > domain pattern > engine pattern
```

冲突时不自动合并，进入 wait_confirm 或 review gate。

## 10. P2/P3 方向

### Domain Flywheel P2

- TAPD story/bug 自动聚类。
- 生产日志到 bug root cause 的弱监督关联。
- 用户反馈到模块/服务的自动路由。
- domain micro-tool：查日志、查 Nacos、查 TAPD 关系、查发布记录。
- domain outcome scorer：将发布、回滚、告警、reopen、用户反馈复发合成为业务成功标签。
- trace maturity updater：在 story 完成后的观察窗口内持续回写 green/yellow/red/blue 状态。
- Tool Router / Meta-tool：用稳定工具入口路由大量 domain micro-tool，避免工具列表膨胀。

### Engine Flywheel P2

- Router preference dataset。
- small router classifier。
- prompt contract evaluator。
- execution constraint registry。

### Shared P3

- 将 domain 中反复验证有效的经验提升为 engine policy。
- 将 engine 中验证有效的 generic tool 下发给 domain profile。
- 对 promotion 做 A/B 或 smoke validation。
- 多租户联邦式飞轮：不同团队保留各自 domain 原始数据，只上报脱敏后的 shared abstraction 和验证结果，由 engine 聚合通用策略后再按 profile 下发。

## 11. 与版本路线图的关系

双飞轮不是一个孤立版本功能，也不是和 v0.6、v0.7 重复的第三套飞轮。它更适合作为 v0.9 的治理层，把前面几个版本沉淀的信号纳入同一套边界模型。

```text
v0.6 质量闭环
  finding / review / handoff 级别的局部学习闭环
  产出 learned finding、quality packet、review evidence

v0.7 SWE-bench / Engine 数据飞轮
  Engine Flywheel 的数据层
  产出 engine trace、gradient attribution、preference samples

v0.8 Domain 输入层
  Domain Flywheel 的输入层
  接入 Story Source、TAPD、PRD、Repo Scanner、项目运行信号

v0.9 双飞轮治理层
  定义 domain / engine 的隔离、检索、晋升、冲突仲裁和权限边界
```

因此，v0.7 负责“跑出引擎数据”，v0.8 负责“接入业务输入”，v0.9 负责“把两类飞轮管起来”。v0.9 不应该重新实现 v0.7 的 SWE-bench 分析，也不应该重新实现 v0.8 的 TAPD/PRD 接入，而应该消费它们的结构化结果。

## 12. 决策

1. 设计成两个飞轮，而不是一个混合飞轮。
2. 原始业务资产只进 domain，不直接进 engine。
3. 原始 engine trace 只进 engine，不直接修改 domain。
4. shared 只保存脱敏、抽象、验证后的资产。
5. Domain flywheel 的成功指标是业务交付质量。
6. Engine flywheel 的成功指标是编排能力和 benchmark/eval 质量。
7. 两个飞轮通过 promotion queue 互相反哺。
8. Domain flywheel 必须补齐 Deployment/Review/Verification outcome，不能只看 TAPD 状态。
9. Domain Retrieval 在 P1 应采用结构化过滤 + 语义向量召回，而不是只靠 JSONL 字段匹配。
10. Promotion 必须经过 proposed -> sandbox_validated -> active，不允许直接自动激活。
11. Domain 飞轮只能基于 Blue/Red Trace 自动转动，Green/Yellow Trace 只能作为弱证据。
12. Micro-tool 默认不直接暴露给 Agent，应由 Tool Router / Meta-tool 路由。
13. Constraint 必须绑定 code/config anchor，并在应用前做 pre-flight check。
14. v0.9 的双飞轮是治理层，不替代 v0.6 的质量闭环、v0.7 的 Engine 数据层或 v0.8 的 Domain 输入层。

## 13. 推荐方案

先做 P0：

```text
Domain Asset Index
Engine Trace Export
Shared Promotion Queue
Domain Outcome Capture
```

不要一开始就做自动学习或自动激活。先把资产分层、边界和证据链建好。只要边界清楚，后续无论是 domain knowledge retrieval、engine DPO dataset、micro-tool creation，还是 production feedback loop，都能在同一个双飞轮框架下扩展。
