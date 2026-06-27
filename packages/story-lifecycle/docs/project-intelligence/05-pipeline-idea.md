# 项目情报管线思路（待设计）

## 背景

当前 `orchestrator LLM` 主要基于 story 上下文、阶段产出和少量质量数据做计划、审查和路由。它能判断单次执行是否成功，但对真实工程现场的感知仍然有限：

- 不知道项目测试/CI/验证基建是否可靠
- 不知道在线 story、bug、评论、验收标准的完整上下文
- 不知道生产运行状态，例如错误率、慢 SQL、接口耗时
- 不知道业务指标是否异常，以及当前改动是否触及关键业务链路

如果目标是让 Story Lifecycle 更智能，不能只让 LLM 做更多推理，而要让系统持续收集更接近真实工程现场的证据。

## 核心想法

把“项目结构扫描”升级为 **Project Intelligence Pipeline（项目情报管线）**。

管线负责持续收集、标准化、缓存和压缩项目相关信号，再生成可注入 planner / reviewer / router / adversarial loop 的情报包。

```text
Collectors
  -> normalize raw signals
  -> Evidence Store
  -> Project Intelligence Packet
  -> planner / reviewer / router / adversarial loop
```

编排智能来自：

```text
结构化事实 + 历史质量数据 + 在线运行信号 + LLM 策略判断
```

而不是让 LLM 临时猜测仓库或业务状态。

## 信号分层

| 信号层 | 来源 | 例子 | 用途 |
|--------|------|------|------|
| 静态代码信号 | 本地 repo | 语言、框架、测试、CI、依赖、模块结构 | 判断验证能力和代码风险 |
| 在线协作信号 | TAPD/Jira/GitHub/GitLab | story、bug、评论、优先级、验收标准、历史返工 | 判断需求上下文和真实痛点 |
| 运行时技术信号 | 日志/APM/DB/监控 | error rate、慢 SQL、接口耗时、异常堆栈 | 判断生产风险和故障根因 |
| 业务指标信号 | BI/埋点/报表 | 转化率、订单量、活跃、失败率、投诉量 | 判断改动是否影响业务目标 |

## Collector 类型

P0 不需要一次性实现所有 collector，但接口应预留统一形态。

候选 collector：

- `RepoScannerCollector`：扫描语言、框架、测试、lint、CI、目录结构
- `TapdCollector`：读取 story、bug、评论、状态、优先级、验收标准
- `GitHubCollector` / `GitLabCollector`：读取 issue、PR、CI、review、commit 历史
- `LogCollector`：读取错误日志、异常堆栈、关键告警
- `DbSlowQueryCollector`：读取慢 SQL、执行次数、影响表
- `MetricsCollector`：读取接口耗时、错误率、QPS、资源使用
- `BusinessMetricCollector`：读取订单、转化、活跃、失败率等业务指标
- `IncidentCollector`：读取近期故障、回滚、报警记录

## Evidence 模型

所有 collector 输出统一 evidence，避免不同数据源直接污染 planner/reviewer prompt。

```json
{
  "id": "evidence-xxx",
  "source": "repo|tapd|github|logs|db|metrics|bi",
  "type": "test_readiness|story|bug|slow_sql|error_rate|business_metric|incident",
  "title": "...",
  "summary": "...",
  "severity": "high|medium|low",
  "confidence": "high|medium|low",
  "observed_at": "2026-05-24T10:00:00+08:00",
  "expires_at": "2026-05-25T10:00:00+08:00",
  "links": [],
  "raw_ref": "...",
  "tags": ["payment", "order", "sql"]
}
```

原则：

- evidence 保存事实和证据，不保存过度推理
- LLM 只消费压缩后的 packet，不直接消费无限原始数据
- 每条 evidence 要有来源、时间、置信度和过期时间
- 运行时/业务数据必须有 freshness，避免旧数据误导决策

## Project Intelligence Packet

面向 LLM 的压缩输出称为 `Project Intelligence Packet`。

示例：

```text
## Project Intelligence Packet

### Project Structure
- Language: Python
- Frameworks: FastAPI, Click
- Test readiness: L2
- Available commands: ruff check src, pytest
- CI: not detected

### Related Online Context
- TAPD bug HC-123: 用户反馈订单状态展示异常
- Recent comment: 需要兼容历史数据为空的情况

### Runtime Signals
- order API error rate increased in last 24h
- slow SQL detected on order_status query

### Business Signals
- Order success rate dropped 2.1% yesterday

### Risk Assessment
- Risk: high
- Reason: current story touches order module while runtime and business signals are abnormal
- Recommended review strategy: heavy_semantic + targeted verification
- Recommended verification: inspect order_status edge cases, run targeted tests if available
```

## 与现有能力的关系

### 与 Quality Packet 的关系

`Quality Packet` 关注质量闭环：

- open findings
- learned patterns
- verification history

`Project Intelligence Packet` 关注项目现场：

- 项目结构
- 测试/CI 能力
- 在线需求/缺陷上下文
- 运行时风险
- 业务指标风险

两者互补，应一起注入 planner/reviewer。

### 与 adversarial loop 的关系

项目情报管线是 adversarial loop 的前置感知能力。

没有项目情报时，reviewer 只能泛泛地“严格审查”。

有项目情报后，reviewer 可以根据事实调整策略：

```text
项目无 CI
+ pytest 存在但 tests 很少
+ 当前 diff 涉及 order 模块
+ 最近 order API error rate 升高
=> verification_unavailable 时不能轻易 pass
=> 需要重点审查兼容性、回滚风险、历史数据边界
```

### 与 orchestrator LLM 的关系

LLM 不负责低层扫描。职责分工：

```text
Deterministic scanners:
  收集事实、命令、文件、指标、在线数据

Orchestrator LLM:
  解释事实、评估风险、选择 review_strategy、建议验证路径
```

这样可以减少幻觉，并让系统输出可审计。

### 与 Code Agent Probe 的关系

Project Intelligence Pipeline 不只能靠静态 scanner。对于真实项目，启动方式、测试入口、模块边界、发布规则往往散落在 README、脚本、CI、配置和代码约定里，单纯 grep 很容易漏。

因此可以引入受控的 **Project Intelligence Probe**：

```text
Pipeline 生成明确探查问题
  -> code agent 只读探查项目
  -> 输出 facts / hypotheses / evidence
  -> Pipeline 校验并写入 Evidence Store
```

示例任务：

- “找出这个项目的启动命令和测试命令，给出证据路径。”
- “分析 claim 模块的核心入口，不要修改任何文件。”
- “根据 TAPD bug 判断可能关联模块，列出置信度和 evidence。”
- “检查 CI 配置和数据库迁移规则。”

Probe 输出示例：

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
      "type": "related_module",
      "value": "claim calculation likely lives in src/claim/",
      "confidence": 0.72,
      "evidence": ["src/claim/service.py"]
    }
  ],
  "open_questions": []
}
```

约束：

- Probe 默认只读。
- Probe 必须有明确问题和输出 schema。
- 事实必须带 evidence。
- 推断必须带 confidence。
- 写入 Evidence Store 前，系统必须校验路径存在、schema 合法、命令非 destructive。

## 定时感知

项目情报可以按需采集，也可以定时采集。

### On-demand sensing

在关键节点采集：

- story 创建时
- plan 阶段开始前
- review 阶段开始前
- retry 前
- release / deploy 前

优点：简单、成本低、上下文最相关。

### Scheduled sensing

后台定时采集：

- 每 5-15 分钟拉取告警、错误率、慢 SQL
- 每小时拉取在线 bug/story 状态变化
- 每天生成业务指标摘要

优点：系统可以主动发现风险，而不是等 story 出错。

例子：

```text
发现过去 24h order API error rate 升高
+ 当前 story 要改 order 模块
=> plan 阶段标记高风险
=> review 阶段启用更严格策略
=> router 更倾向 wait_confirm 而不是自动 pass
```

## 数据存储设想

P0 可以先复用轻量存储：

- `event_log`：记录 `evidence_collected`、`project_intelligence_built`
- `story.context_json`：保存当前 story 的 `project_intelligence` 摘要
- `.story-knowledge/{story_key}/project-intelligence.json`：保存 story 级情报缓存
- 后续如需跨 story 查询，再考虑新增 `evidence` 表

长期可能需要：

```text
evidence table = 当前有效证据和检索入口
event_log      = 采集、过期、使用、决策的审计轨迹
knowledge      = 面向 LLM 的压缩摘要和长期经验
```

## 对 planner / reviewer / router 的影响

### Planner

planner 不只生成任务书，还要根据情报包输出：

- test readiness
- risk level
- review strategy
- recommended verification
- modules likely affected
- whether to split story

### Reviewer

reviewer 根据情报包自适应审查：

- 测试基建弱：提高语义审查权重
- 运行时信号异常：重点审查相关模块和边界条件
- 业务指标异常：关注业务链路和回滚方案
- 高风险模块：提高 pass 门槛或启用双 reviewer

### Router

router 不只看 last_error，还可以结合风险：

- 高风险 + verification unavailable：倾向 `wait_confirm`
- 低风险 + findings resolved：倾向 `advance`
- 运行时异常未解释：倾向 `retry` 或 `fail`
- 信号过期：要求重新采集

## MVP 范围建议

### P0：静态 + 在线需求信号

目标：先让 orchestrator 看到项目结构和在线需求上下文。

包含：

1. `RepoScannerCollector`
   - 语言/框架识别
   - test/lint/CI 检测
   - test readiness level
2. `TapdCollector` 或现有 source provider 复用
   - story/bug 标题、描述、评论、验收标准
3. `Project Intelligence Packet`
   - 注入 planner/reviewer prompt
4. 事件
   - `evidence_collected`
   - `project_intelligence_built`
5. 存储
   - `context_json.project_intelligence`
   - `.story-knowledge/{story_key}/project-intelligence.json`

### P1：运行时技术信号

包含：

1. 慢 SQL collector
2. error log / APM collector
3. API latency / error rate collector
4. affected modules 与 runtime signals 的关联
5. 风险预警注入 review prompt

### P2：业务信号 + 主动感知

包含：

1. 业务指标 collector
2. scheduled sensing
3. anomaly detection
4. 自动生成风险预警
5. 建议创建 story / 阻断 release / 请求人工确认

## 风险与边界

- 不要把 P0 做成大数据平台；先做 repo + story/bug 上下文闭环
- 在线系统需要权限、限流、脱敏和查询模板
- 运行时和业务指标必须有时间窗口与新鲜度，否则容易误导
- LLM 不应直接访问无限原始数据，应消费压缩 packet
- 任何阻断行为都应给出 evidence 链接和可解释原因
- scheduled sensing 需要成本控制和去重，避免产生噪音

## 状态

待设计。后续可拆成正式设计：

1. Project Intelligence Packet P0 设计
2. Evidence Store 与 Collector 接口设计
3. Runtime/Business Signal Collector 设计
4. Scheduled Sensing 与主动风险预警设计
