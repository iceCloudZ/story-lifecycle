# 设计文档：verify 质检能力补齐（迭代 1）

> 版本：v1.0（2026-08-05）｜ 状态：待评审 ｜ 作者：eval 体系产出，opencode 执行
> 证据来源：round 1 gate 回测、round 2 全管线回放（`packages/eval/results/gate_replay_20260805.md`、`pipeline_replay_20260805.md`）

## 1. 背景与问题定义

回放体系已证明 story-lifecycle 的 verify 链路存在两个结构性缺陷：

**D1（fail-open 漏洞）**：`orchestrator/evaluation/stage_completion.py` 的 `judge_stage_completion`（:71）LLM 调用一次失败即走 `_fallback_decision`（:257），返回 `quality=approve`——Go 端点抖动时 stage 被静默放行，仅退化为人工确认挂起，无任何标记。回放实测触发 2 次（1067103、1065191）。

**D2（质检员缺位）**：`unified_gate._build_unified_gate_prompt`（unified_gate.py:356）的 evidence 只有 story_key / done_summary / 文件名清单 / 既有 findings / adapter 信息 / 历史 playbook——**既没有 diff 内容，也没有 spec 原文**。gate 从设计上就无法判断「代码是否实现了需求」。干净输入回放下，5 条已知 drift 凭内容拦截 0/5。生产上 verify 依赖实现 agent 自报的 delivery 摘要，运动员兼裁判。

## 2. 目标与非目标

**目标**：
- G1：LLM 基础设施失败时，verify 链路 fail-closed（拦截转人），不留静默放行路径；
- G2：verify 阶段新增独立的「需求↔代码吻合度检查」，产出的 findings 进入 unified_gate 决策，使干净输入下已知 drift 拦截率 0% → ≥80%；
- G3：所有改动可被回放体系验证（replay 通过标准见 §7）。

**非目标**：
- 不改 planner 编排逻辑（并行会话重构中，见 §8）；
- 不改 gate 的决策职责定位（它仍是编排决策层，本设计只给它补证据生产者）；
- 不做 UI 改动。

## 3. F1：stage_completion fail-closed 改造

文件：`orchestrator/evaluation/stage_completion.py`

| 项 | 现状 | 设计 |
|---|---|---|
| 重试 | 无（一次失败即 fallback） | 瞬态错误退避重试 2 次（5s、10s，总上限 60s） |
| 瞬态判定 | 不区分 | read timeout / 连接错误 / HTTP 5xx / 429 判瞬态；4xx（非 429）与解析失败判永久，直接转 fallback |
| fallback 决策 | `quality=approve`（fail-open） | `quality=escalate`（fail-closed），lifecycle 不自动推进，转人工确认 |
| fallback 留痕 | 空摘要 | 摘要前缀 `[FALLBACK]` + 保留原 done_data 摘要 + 记录异常类型到 event_log |

兼容性：调用方（planner 轮询）对 `escalate` 的处理路径已存在（gate 的 escalate repair_action 同语义），不需新增状态机分支。若 planner 当前对 escalate 表现为挂起，属可接受行为（宁可挂起等人，不可静默放行），在报告中注明。

## 4. F2：conformance 质检器（核心新增）

### 4.1 定位与依赖方向

新模块 `orchestrator/evaluation/conformance.py`。judge 逻辑移植自 `packages/eval/src/eval/judges.py` 的 ConformanceScore（经三轮人工校准的 prompt），**移植进核心包而非 import eval**——依赖方向必须是 eval → story_lifecycle，反向不成立。移植时 prompt 原样保留（含「单仓切片不得给 alignment=1」的口径修正），作为核心包的独立组件；eval 侧后续改为 import 核心包实现，保持单一来源。

### 4.2 接口

```python
def check_conformance(
    *,
    story_key: str,
    workspace: str,
    spec_path: str,          # evidence 中的 spec.md；无 spec 时降级用 PRD.md，再无则跳过并记 skip 原因
    diff_text: str | None,   # 见 4.3
    timeout: int = 180,
) -> ConformanceResult      # {alignment, coverage, scope_drift, findings, reference_type, skipped, skip_reason}
```

### 4.3 diff 来源

- **生产路径**：verify 阶段时，实现产物在 story workspace 的 git worktree 里——`git -C <worktree> diff <base>...HEAD`（base 从 story.branches_json 取）；非 git 产物（配置/DDL 文本）按 files_changed 读文件内容拼接。
- **回放路径**：驱动层通过 done_data 注入 `delivery_diff_path`，conformance 优先读它（回放与生产同一代码路径，仅 diff 来源不同）。
- diff 超 30k token 截断（复用 eval scanall 的截断策略：保留 diffstat + 按文件大小降序的关键 diff）。

### 4.4 结果如何进 gate

- **拦截阈值（迭代 1 收尾修订）**：`alignment ≤ 2` **或** `coverage ≤ 2` → 生成 HIGH finding（category=`conformance`，description 含三维分 + 摘要），追加进 unified_gate evidence 的 `open_high_findings`——gate 现有纪律「有 HIGH finding 倾向 fail/escalate」自然生效，**gate 本体零改动**；
  - 语义：管线内 story 完成时点要求**完整交付**——coverage 严重不足（≤2）即视为 drift，即使已实现部分 alignment 较高；
- `alignment = 3` 且 `coverage > 2` → MEDIUM finding（记录不阻断）；
- `alignment ≥ 4` 且 `coverage ≥ 3` → 记录分数到 done_data（`conformance_score` 字段），不产生 finding；
- conformance 自身 LLM 失败 → 适用 F1 同一套瞬态重试 + fail-closed（escalate），不允许跳过检查静默放行。

### 4.5 接入点与配置

- 接入：verify stage 完成判定流程中，`judge_stage_completion` 通过后、`run_unified_verify_gate` 之前执行；结果写入 done_data + findings 队列。
- 配置：profile 的 quality 段新增 `conformance_check: true|false`（默认 true）与 `conformance_alignment_threshold: 2`；eval-replay 类 profile 显式开启。

## 5. 附带修复（小项）

- **P2 spec 落点**：实际产物落 `story/<sid>-<slug>/spec.md`，artifact 契约写 `story/spec.md`——统一为实际落点，修 profile artifacts 声明（不动 story_paths 的既有行为，证据已被 eval 抽取侧依赖）；
- **P3 STORY_HOME**：profile_loader 的项目级 profile 搜索路径硬编码 home，改为优先 `STORY_HOME` env；
- **P4 Go 抖动**：llm_client 层补 read-timeout/5xx 瞬态重试 ×2（与 F1 语义对齐，统一在 llm_client 收口，stage_completion/conformance 不再各自实现）。

## 6. SQL / Nacos / 配置变更

- SQL：无变更（findings 落既有 finding / gate_result 表结构兼容；conformance 分数进 done_data JSON，不加列）。
- 配置：profile YAML 新增 §4.5 两个键（向后兼容，缺省 true/2）。无 Nacos。

## 7. 验证方案（迭代 1 验收标准）

全部基于冻结快照 `snapshot_20260805` + 既有回放设施，LLM 走 opencode-go：

**验收纪律（迭代 1 收尾新增）**：验收标签与 gate **必须同一 judge 版本**（端点 + 模型 + prompt 三元组）。judge 升级（端点切换/模型更换/prompt 修改）后，**验收标签必须同源重评**，禁止用旧标签衡量新 gate——标签迁移不算漏拦/误拦，需在报告中注明。

1. **F2 主验收**：B 类 5 条干净注入重跑 verify（完整 diff、无先验 findings）→ 凭内容拦截 ≥4/5；A 类 5 条 → 误拦 ≤1/5；
2. **F1 主验收**：故障注入测试——把 LLM 端点指向不可达地址跑 1 条 story 的 verify → 必须产出 escalate + event_log 留痕，禁止 approve；
3. **回归**：round 1 gate 回测脚本全量重跑不报错，decision 分布无异常漂移；B/A 类重跑同时跑 coverage.py，conformance 模块行覆盖 ≥80%；
4. **版本锚点**：报告头部记录 story-lifecycle 新 commit、快照名、judge 端点；与 round 2 报告对比出 delta。

## 8. 时序与并行重构协调

并行会话正在重构 planner（拆 scheduler/executors/handlers，planner -1439 行）。本设计**刻意不碰 planner**：F1/F2 的改动面在 `orchestrator/evaluation/` 与 `infra/llm_client.py`，与重构面（engine/planner 拆分）基本正交；唯一交叉点是 planner 对 `judge_stage_completion` 返回值的消费（escalate 语义），实施前需确认重构后的调用点签名未变。若并行重构未收口，实施顺序：先落 evaluation/llm_client 侧改动（可独立验证），planner 消费点确认留在最后。

## 9. 风险

- **judge 过拟合**：conformance judge 与 eval judge 同源，验收样本（B 类）又是 judge 校准过的——可能高估拦截率。缓解：验收报告附每条 finding 原文供人工抽查；快照 v2 扩样时保留未参与校准的 held-out 集。
- **误拦上升**：conformance 引入后，单仓切片类交付（coverage 低但 alignment 高）不触发 HIGH finding（阈值设在 alignment≤2 而非 coverage），已通过阈值设计规避；A 类验收线 ≤1/5 兜底。
- **成本**：每次 verify 多一次 LLM 调用（~30k token 级），走 Go 池；生产环境如接计费端点需评估。
