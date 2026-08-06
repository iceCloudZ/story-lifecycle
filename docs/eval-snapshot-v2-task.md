# 快照 v2 构建任务（Go 重基 + 配额选样 + held-out）

> 自包含任务文档。执行者：opencode。写于 2026-08-06，前置：迭代 1 收口（F1/F2 上线，judge 三元组原则已入 handoff）。
> 目的：产出 `dataset/snapshot_v2_20260806/`——全部按 Go 三元组重基的评分基线 + 按缺口配额选取的回放样本集 + 密封 held-out 集。此后一切 replay/验收/迭代对比以 v2 为准，v1 仅作历史存档。

## 0. 铁律

- **judge 三元组**：一律 `opencode-go`（`https://opencode.ai/zen/go/v1` + OPENCODE_API_KEY）+ `deepseek-v4-flash` + 当前 prompt（conformance.py，迭代 1 移植版，记录其 commit 作 prompt 版本）。**禁止 DeepSeek 官方端点**。放量前先单条验证走 Go。
- v1 快照（`snapshot_20260805/`）只读，不修改不删除；v2 全新目录。
- 断点续跑：所有评分任务可中断重进（merge_scores 机制现成的，跳过已评行）。
- 不得修改 story-lifecycle 核心包；新代码限 `packages/eval/src/eval/`。

## 1. Go 全量重基（最大工作量）

1. **merge conformance 重评**：v1 快照的 664 个 mine merge（含 174 关联 + 无关联仅摘要的），全部按 Go 三元组重评，落 `snapshot_v2_20260806/merge_scores.jsonl`（新文件，不覆盖 v1）。参照物优先级不变（spec > prd > story_refs > tapd 描述）；human_confirmed / human_recalibrated 标记从 v1 stories_matched.jsonl 原样携带，跳过自动规则的铁律保持。
2. **148 baseline 重评**：baseline.py 全量重跑（Go），落 `snapshot_v2_20260806/baseline_v2.json`；同时跑自洽性检查（分差≤1 比例），与 v1 的 88.9% 并列报告（跨三元组对比需标注端点漂移，不做直接优劣结论）。
3. **重生成 full_scan_mine 报告**（v2 版，`results/full_scan_mine_v2_20260806.md`）：新 conf.alignment 基线均分、管线内 vs 管线外、按 repo/月份，头部注明「v2 = Go 三元组重基，与 v1 数字不可直接比较」。

## 2. 配额选样（回放样本集 v2，目标 ~50 条）

写入 `snapshot_v2_20260806/replay_samples.jsonl`（每条含 story_key/tapd_id、类别、选取理由、期望行为）：

1. **失败主题配额（≥21 条）**：按 `results/failure_patterns_20260805.md` 的 21 个主题，每主题选 1 条代表 case（优先该主题 findings 最典型的 merge），期望行为 = gate 应拦且 finding 命中主题。
2. **决策分支构造样本（6 条）**：主动构造逼出未触发分支——
   - 伪造悬而未决 HIGH finding ×2 → 期望 fail/escalate；
   - 注入 playbook「换 adapter 成功」记录 ×2 → 期望 swap_approach；
   - 模拟缺依赖 done_data ×2 → 期望 insert_rescue_stage。
   构造方式：改 done_data/DB 种子数据，不改被测代码；构造过程脚本化落 `packages/eval/src/eval/v2_construct.py` 可复现。
3. **A/B/C/D 刷新（~15 条）**：按 v2 新标签重选（A=align≥4 且 cov≥4；B=align≤2 或 cov≤2；C=story_refs 富化；D=跨服务），与 v1 样本去重，B 类全部用 v2 新标签。
4. **场景空格补样（~8 条）**：round 2.5 场景矩阵的空格——无评分×单仓（252 池）选 6、跨服务×无评分（28 池）选 2。

## 3. held-out 密封集

- 从 v2 有关联 merge 中随机抽 15 条（固定随机种子 42，可复现），要求：从未参与任何人工校准、从未进入任何验收样本集（v1 的 21 条校准、A/B/C/D、167 条 gate 回测全排除）。
- 落 `snapshot_v2_20260806/held_out.jsonl` + manifest 注明「密封，仅阶段验收用，迭代期间禁止用于调优」。
- 用 v2 三元组预评分数落盘但不进入任何训练/校准材料。

## 4. manifest 与验收

`snapshot_v2_20260806/snapshot_manifest.md`：冻结时间、judge 三元组（端点/模型/prompt commit）、各文件行数、v2 新基线均分、样本集构成（类别×数量）、held-out 种子与排除规则、与 v1 的差异说明。

验收（逐项自证）：
1. merge_scores.jsonl = 664 行，error 行 ≤2% 且列出清单；LLM 调用日志可证全程 Go 端点（无 api.deepseek.com 请求）；
2. baseline 自洽性数字落盘；replay_samples.jsonl ≥50 条，21 主题全覆盖（逐主题核对）、v1 样本去重可验证；
3. held-out 15 条满足全部排除规则（脚本校验输出）；
4. `git status` 核心包零改动；
5. 回复：v2 新基线均分（vs v1 2.995，标注不可直接比）、自洽性、样本构成表、held-out 清单、报告路径。

## 5. 后续（不在本任务内）

v2 冻结后：round 3（UI 端到端）用 v2 样本驱动的回放数据做读路径验证；迭代 2 起一切验收基于 v2；外环观察以 v2 均分为新基线。
