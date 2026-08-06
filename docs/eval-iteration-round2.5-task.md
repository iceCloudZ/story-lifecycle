# eval 迭代循环任务：回放覆盖率测量（round 2.5）

> 自包含任务文档。执行者：opencode。写于 2026-08-05，前置：round 1（gate 回测）、round 2（全管线回放 20 样本）已完成。
> 目的：给已有回放补「覆盖率」刻度——四维测量，回答「测到了什么、没测到什么」。本任务**纯测量**，不改 story-lifecycle、不造新样本（样本扩充留给快照 v2）。

## 0. 铁律

- 输入：`dataset/snapshot_20260805/`、`results/gate_replay_20260805.md`、`results/pipeline_replay_20260805.md`、`results/failure_patterns_20260805.md`、`sandbox/` 现有产物。
- LLM 一律 opencode-go（仅 §1 插桩重跑会消耗，走 Go 池）；不得修改 story-lifecycle 核心包；新代码限 `packages/eval/src/eval/`。
- 沙箱纪律同 round 2（STORY_HOME 隔离、AGENTS.md 截断、禁外发禁 git 变更、hc-all 只读）。

## 1. 维度一：代码路径覆盖（coverage.py 插桩重跑）

1. 在 `.venv-monorepo-test` 装 coverage（不装全局）。
2. 用 coverage 插桩重跑两类已有测试，source 限定 `story_lifecycle`：
   - round 2 管线回放：挑 2 条已跑通的 story（1 条 A 类 + 1 条 B 类），同沙箱流程重跑；
   - round 1 gate 回测：全量 167 条重跑（离线、快，LLM 量可接受）。
3. 产出按模块的覆盖率表：`orchestrator/evaluation/`（unified_gate、stage_completion）、`orchestrator/service/`（prd_generator 等）、`engine/`（planner、graph）、`entry/`、`infra/`，行覆盖 + 分支覆盖。
4. **重点回答**（写进报告）：planner/graph 在分段驱动模式下到底被执行了多少（验证「司机盲区」的量化证据）；unified_gate 的 decision 分支（advance/retry/fail）各被执行几次；stage_completion 的 fallback 路径覆盖情况。

## 2. 维度二：场景覆盖矩阵（纯计算，零 LLM）

1. 总体分布：快照 deliveries.jsonl（2019 merge，或 mine 664）按 repo × 月份 × 有无 story_key × ownership 分层统计；stories_matched.jsonl 按 单仓/跨服务 × 参照物类型（spec/prd/story_refs/tapd 描述/link-only 未富化）统计。
2. 样本分布：round 2 的 20 条 + round 1 的 167 条按同样维度统计。
3. 矩阵对比：标出**空格**（总体里有、样本里无的组合），按空格的总占比排序——这就是快照 v2 的选样配额依据。

## 3. 维度三：决策分支覆盖（盘点已有产物，零 LLM）

从 round 1/2 明细 + 沙箱 DB（gate_result/event_log 表）盘点 gate 行为空间的实际触发情况：

- decision ∈ {advance, retry, fail} × verdict ∈ {pass, rework} × repair_action ∈ {retry, swap_approach, insert_rescue_stage, escalate}；
- stage_completion 的 fallback 路径（approve/escalate）。
- 输出触发次数矩阵，从未触发的组合列出——这些是将来要主动构造样本逼出来的分支（如伪造悬而未决 HIGH finding 逼 escalate）。

## 4. 维度四：缺陷模式覆盖（纯映射，零 LLM）

- 拿 `failure_patterns_20260805.md` 的 21 个主题，逐主题标注：round 2 的 20 条样本里有没有对应该用例（有则列 story_key）。
- 无对应用例的主题列出清单 + 每个主题一句「选样建议」（从快照哪批 case 里能挑出代表）。

## 5. 产出与验收

`results/coverage_20260805.md`：

1. 四张表：代码模块覆盖率、场景矩阵（总体/样本/空格）、决策分支触发矩阵、缺陷模式映射表；
2. **缺口清单**：四维合并，按优先级排序（建议权重：决策分支未触发 > 代码核心模块零覆盖 > 缺陷模式无用例 > 场景空格）；
3. 快照 v2 选样建议：每个缺口对应一句可执行的选样/构造规则。

验收：
- 代码覆盖率表可由 coverage 原始数据（落 `results/coverage_data_20260805/`）复算；
- 场景矩阵行数与快照文件行数对得上（ deliveries 2019 / stories_matched 383）；
- 决策分支矩阵的触发次数可在 round 1/2 明细中抽查复核；
- `git status` 核心包零改动、packages/eval 之外零代码改动；
- 回复给出：四维覆盖率摘要 + 缺口 top5 + 报告路径。
