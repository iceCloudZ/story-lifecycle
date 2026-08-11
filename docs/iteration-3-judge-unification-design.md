# 设计文档：裁判统一 + severity 贯通 + 孤儿清理（迭代 3）

> 版本：v1.0（2026-08-11）｜ 状态：待评审 ｜ 作者：kimi 深入代码考古产出，opencode 执行
> 证据来源：迭代 1/2 报告、replay47_v2_20260810.md、heldout15_20260810.md、full_scan_all_20260810.md + 2026-08-11 代码考古（本文档 §1）
> 前置纪律：judge 三元组（Go 端点 only）、pre-flight 端点检查、沙箱隔离、hc-all 只读、删除操作白名单制 + dry-run（删代码文件同样适用：先列完整清单，逐条确认后删）。

## 1. 背景与问题定义

### 1.1 核心发现：eval 的 gate 回测一直在测生产不用的孤儿裁判

代码里有两个 verify 裁判：

| | 生产裁判（甲） | 孤儿裁判（乙） |
|---|---|---|
| 模块 | `evaluation/stage_completion.py::judge_stage_completion` | `evaluation/unified_gate.py::run_unified_verify_gate` |
| 生产调用方 | `orchestrator/scheduler.py:750` 实际调用 | **无**（stage_completion.py:186 注释自认「design 12 收敛后 run_unified_verify_gate 无调用方」） |
| conformance 通道 | 有（F2：check_conformance → inject_conformance_findings severity 化 → 结构化 conformance_ev 进 prompt，stage_completion.py:287-315） | **无**（prompt 只有 done_summary + files_changed + DB open HIGH findings + playbook，unified_gate.py:374-402） |
| 迭代 1 F1/F2 改动落点 | fail-closed fallback、conformance 质检器 | fallback + `_log_gate_event` 增强（**白改**） |
| eval 回测覆盖 | **从未被回放测过** | gate_replay 167 条、replay47 47 条、heldout15 15 条全部测它 |

后果：

1. 迭代 1 F2 接进生产后，生产裁判从未被端到端回测——目前所有拦截率/误拦率数字（84%、held-out 9/9）是孤儿口径，不代表线上行为。
2. 3db315d4f2 漏拦（align=5 cov=1 放行）与 held-out 4 条「真争议」（align≥4 cov≥3 被拦）都发生在孤儿身上。孤儿的结构化输入里没有 conformance 通道，负面信息只能靠 runner 塞进 done_summary 文本让 LLM 自行解读——两个方向的异常都可能是「测错对象」的伪影。
3. 迭代 2 的 QualityPanel 部分接线到孤儿数据源：`gate-history` API（timeline.py:169）三源合并中，`gate_decision` 事件全仓只有 unified_gate._log_gate_event（unified_gate.py:457）会写。生产跑 story 时该事件源恒空；orchestrator_decision 合并段（timeline.py:204-222）有决策但 `findings: []`、`repair_action: None`——**生产 UI 上看不到 severity findings 明细，迭代 2 G2 只修了一半**。

### 1.2 孤儿普查（2026-08-11 全仓调用方扫描）

| 模块 | 状态 | 证据 | 处置 |
|---|---|---|---|
| `evaluation/unified_gate.py`（18KB） | **孤儿** | 生产零调用方；3 个测试文件引用；eval 回放直接 import | 删除（前置检查见 §3.4） |
| `evaluation/boundary_judge.py`（11.6KB） | **孤儿** | 生产零调用方，仅 test_boundary_judge.py 引用；docstring 声称「unified_gate 并入这里」，结果两者皆孤儿 | 删除 |
| `orchestrator/nodes/` | 空兼容壳 | 仅剩 `__init__.py` 为旧测试 re-export；demo.py 引用的 `nodes.graph_nodes` 已不存在 | 保留壳，修 demo（§3.4） |
| `entry/cli/demo.py` | 疑似坏 | patch 目标 `orchestrator.nodes.graph_nodes.planner` 不存在 | 实测，坏则修或从 CLI 摘除 |
| `engine/graph.py` | **活**（纠正误报） | CLI main.py:216、service/api.py:23、routers/lifecycle.py:19 均调用 `start_story_async` | round 2.5 报告「graph.py 残留死代码」结论**错误**，需在 coverage_20260805.md 加注纠正 |
| quality / semantic / review_feedback / stuck_diagnose / reject_budget | 活 | scheduler/CLI/stage_completion 有真实调用 | 不动 |

### 1.3 议题 2（medium 自判阻断）重新定性

held-out 4 条争议（e4fdff3243/99b052ac90/87f5faa863/2111abcf2b）在生产裁判上可能根本不会出现：生产 judge 看到结构化 `conformance_ev` 明确印「→ OK」（stage_completion.py:305-309），而孤儿只能读文本负面描述。**本迭代不调任何 judge prompt**，先在生产裁判上重测，用数据决定该议题是否还存在。

## 2. 目标与非目标

**目标**：
- G1：eval gate 回放改测生产裁判 `judge_stage_completion`，完整覆盖 F2 conformance 现场执行 + reject budget + fail-closed fallback；
- G2：eval 落盘 severity 化——`v2_rebase.score_linked` 补 `severity_findings` 字段，750 + 2019 旧数据离线回补（零 LLM）；
- G3：47 条 replay_samples + 15 条 held-out 在生产裁判上重测，产出新基线报告（与孤儿口径并列展示但明确标注不可直接比较——被测对象变更等同 judge 三元组变更）；
- G4：生产侧补齐 findings/repair_action 落库，QualityPanel 在生产可见 severity findings（迭代 2 G2 收尾）；
- G5：孤儿清理：unified_gate、boundary_judge 及其专属测试删除；demo.py 实测处置；round 2.5 graph.py 误报纠正。

**非目标**：
- 不调任何 judge/gate prompt（议题 2 挂起等数据）；
- 不改 scheduler/planner/executors 编排逻辑；
- 不改 judge_stage_completion 的判定逻辑本身（G4 只加落库字段，若回放暴露真 bug 另立迭代）；
- 不重跑 v2 基线评分（750 键分数不变，只补 severity 衍生字段）。

## 3. 设计

### 3.1 回放改测生产裁判（保真注入）

改造 `gate_replay.py` 与 replay47/heldout 运行器：不再直接调 `run_unified_verify_gate`，改为构造最小 story 上下文调 `judge_stage_completion`（参数对象见 stage_completion.py:74 JudgeInput）。

注入映射（关键设计决策：**conformance 现场算，不注入预算分**）：

| 生产入参 | 回放注入来源 |
|---|---|
| `done_data.summary` | delivery 的 merge_summary（中性摘要，不塞 findings 文本——杜绝文本锚定） |
| `done_data.files_changed` | delivery.diffstat |
| `done_data.spec_path` | 参照物临时文件（复用 v2_rebase._write_ref_text：spec > prd > story_refs > tapd） |
| `done_data.delivery_diff_path` | delivery.diff 落临时文件（**走 conformance 正式输入通道**） |
| `ctx.conformance_check` | true |
| workspace / STORY_HOME | 沙箱临时目录（沙箱三件套纪律不变） |
| DB | 沙箱新库，reject budget 满血 |

理由：生产路径 conformance 是现场算的（stage_completion.py:289），注入预算分会跳过 F2 本身——而 F2 恰恰是本迭代要验证的对象。代价是每样本多 1 次 LLM 调用（conformance）+ 1 次 judge，47+15 共 ~130 次调用，可接受。v2 落盘分与现场分的差异即 judge 波动的真实度量，逐样本双记（v2 分 / 现场分）。

construct 6 条（HIGH finding / playbook / 缺依赖）需要适配：原构造针对孤儿的 DB open_findings 通道，生产裁判无此入口——HIGH finding 样本改为经 conformance HIGH（inject_conformance_findings 产出的 severity_findings 预置进 judge_ctx 等价物）或直接构造低分 diff。适配方案实施前先 dry-run 2 条验证可行，不可行则 construct 类别标注「口径不适配，本轮跳过」并在报告说明。

### 3.2 severity 化落盘 + 旧数据回补 + 冻结目录修复

1. `v2_rebase.score_linked`（v2_rebase.py:187-197）落盘结构加字段：`severity_findings = inject_conformance_findings(ConformanceResult(**{...}))`——复用核心包函数，不另写规则（单一事实源）；
2. 离线回补脚本（纯确定性，零 LLM）：读 `snapshot_v2_20260806/merge_scores.jsonl`（750）+ `merge_scores_full.jsonl`（2019），对有 conformance_score 的行按同规则计算 `severity_findings` 回填；抽样 10 条与现算结果断言一致；
3. 冻结目录修复：`merge_scores_full.jsonl` 与 `full_scan_summary.json`（8月10-11 日产出）从 `dataset/snapshot_v2_20260806/` 挪至 `dataset/` 根，快照目录恢复 8月6日冻结状态（snapshot_manifest 不变）；
4. scanall 的 `_is_suspected_wrong_link` 等消费方如读 findings，同步支持 severity_findings（保留原 findings 字段兼容）。

### 3.3 生产侧：findings/repair_action 落库（G4，迭代 2 收尾）

`judge_stage_completion` 已有 conf_findings（stage_completion.py:302）与 judge 自产 findings（:413），但 log_decision 未带这些字段，导致 gate-history 合并段（timeline.py:215-216）只能给空数组。改动限定为**落库增强**：

- `log_decision` 调用处把 `findings`（severity 化后的 conf_findings + judge findings 合并）与 `repair_action` 序列化进 decision 记录（action_taken 或新增 detail 字段，按 db 现有 schema 最小改动）；
- timeline.py:204-222 合并段透传这两个字段（UI QualityPanel 已能渲染 findings——迭代 2 前端不动）；
- 判定逻辑、prompt、reject budget 一律不碰。

### 3.4 孤儿处置（删除白名单，逐条 dry-run 后执行）

删除前置检查（全部通过才删）：
1. 全仓 grep 确认无新增生产调用方（含 service/routers、entry/cli、scheduler、engine）；
2. `pytest` 基线先跑绿，记录引用 unified_gate/boundary_judge 的测试清单；
3. QualityPanel 依赖检查：确认 G4 落库增强上线后，gate-history 不再依赖 `gate_decision` 事件源（该事件类型随 unified_gate 删除而绝迹；timeline.py:179 的过滤段保留兼容旧数据）。

删除清单：
- `evaluation/unified_gate.py`、`evaluation/boundary_judge.py`
- `tests/test_unified_gate.py`、`tests/test_boundary_judge.py`、`tests/test_judge_three_decisions.py` 与 `tests/test_verify_providers.py` 中引用 unified_gate 的用例（逐文件核对，只删孤儿专属部分）
- `engine/planner.py:698` 注释中 unified_gate 引用改写为 stage_completion

保留/修复：
- `orchestrator/nodes/__init__.py` 兼容壳保留（旧测试依赖）；
- `demo.py`：先跑 `story demo` 实测；确认坏则最小修复（patch 目标改 engine.planner 真实路径）或从 CLI 帮助中标注 deprecated；
- `results/coverage_20260805.md` 加注：撤回「graph.py 残留死代码」结论，graph.py 为 CLI/API 活入口（误报根因：回放进程未 import graph 路径）。

### 3.5 重测与议题 2 复核

- 47 条 replay_samples + 15 条 held-out 全量重跑（生产裁判口径）；
- 报告双口径并列：孤儿口径（存档）vs 生产裁判口径（新基线），头部注明「被测对象变更，数字不可直接比较」；
- held-out 4 条争议样本单独列出：若全部放行 → 议题 2 关闭；若仍有拦 → 样本明细归档为迭代 4 候选议题（届时才谈 prompt 调整）；
- construct 适配结果如实记录（含跳过情形）。

## 4. SQL / 配置变更

- SQL：无变更（G4 落库增强走现有表字段，若确需加列先停下称量，另报审批）。
- Nacos/配置：无变更。

## 5. 验证方案（验收标准）

- **A1（端点与链路）**：改造后 dry-run 5 条，日志确认 conformance 现场真实执行（出现「conformance 检查: alignment=…」行）且全程 Go 端点（pre-flight 打印 + 跑完 grep api.deepseek.com 0 命中）；
- **A2（47 条）**：完成率 100%（允许 LLM 抖动自动重试，不允许静默跳过）；新拦截率/误拦率落盘；HIGH 语义不丢——construct c1/c2 等价样本仍拦；
- **A3（held-out 15 条）**：数字落盘；4 条争议样本逐条列出判定与理由；
- **A4（severity 回补）**：750 + 2019 全部 conformance 行有 severity_findings；抽 10 条断言与 `inject_conformance_findings` 现算一致；merge_scores_full.jsonl 已挪出冻结目录；
- **A5（孤儿删除回归）**：删除后 `pytest` 全绿；`story serve` 冒烟——UI 创建 story → 详情页 QualityPanel 正常渲染（含 G4 后的 findings 明细，用一条构造 verify 完成事件验证）；
- **A6（报告锚点）**：results/iteration3_YYYYMMDD.md 落盘，含 commit 锚点、双口径对比表、议题 2 结论、construct 适配说明。

## 6. 风险与开放问题

1. **judge_stage_completion 调用复杂度**：它是 15 参数对象 + 深依赖（db/planner 上下文），回放构造最小可行入参可能踩隐含前置——A1 dry-run 先行，踩坑如实记录，不硬闯；
2. **conformance 现场算引入 judge 波动**：同一样本 v2 分与现场分可能不同，报告逐样本双记，不掩饰；
3. **construct 类别可能整类不适配**（孤儿专有注入通道）——接受跳过并在报告标注，不为凑数强行构造；
4. 开放问题（本轮不答）：nodes/ 兼容壳与 demo.py 的长期去留，随下次 CLI 清理另议。
