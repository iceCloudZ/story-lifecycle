# 冷启动 Phase 2 进度汇总（截至当前窗口）

> 汇总人：当前窗口（Kimi）
> 分支：`feat/bug-story-graph`

## 一、已完成的 Brief

### ✅ Brief C — task_type 打标
- **脚本**：`packages/story-miner/scripts/classify_story_task_type.py`
- **产物**：
  - `packages/story-miner/scripts/out/story_task_types.json`（150 条，12 类全命中）
  - `packages/story-miner/scripts/out/story_task_types_summary.md`
- **关键结果**：
  - 12 类分布无盲区
  - 平均 bug 数最高：`marketing`（3.48）> `credit-limit`（2.87）> `order`（2.33）
  - 已尝试写回 `~/.story-lifecycle/story.db.context_json.task_type`，仅 25/150 匹配入库

### ✅ Brief E — 无 branch bug 磁铁 commit 推断
- **脚本**：`packages/story-miner/scripts/infer_bug_magnet_commits.py`
- **产物**：
  - `packages/story-miner/scripts/out/story_commits_inferred.json`
  - `packages/story-miner/scripts/out/known_magnet_commits.json`（3 个有 branch 磁铁的 ground truth）
  - `packages/story-miner/scripts/out/infer_bug_magnet_report.md`
- **关键结果**：
  - 3 个有 branch 磁铁验证 recall：7天免息=1.0，还款shopee=1.0，MGM二期 branch 未 merge
  - 8 个无 branch 磁铁均推断到 merge commit
  - 方法：标题关键词 → 候选 repo → merge commit 搜索 → branch token + commit msg/files 打分 → 提取 feature commits

### ✅ Brief I — 结果轴二期深化
- **脚本**：`packages/story-miner/scripts/result_axis_phase2.py`
- **产物**：
  - `packages/story-miner/scripts/out/result_axis_phase2.json`
  - `packages/story-miner/scripts/out/result_axis_phase2.md`
- **关键结果**：
  - bug-prone 文件：`LimitCenterServiceImpl`（credit-limit）、`LoanSubOrderServiceImpl`/`MgmInvitationServiceImpl`（marketing）
  - cycle-time：credit-limit median 17.5h / marketing 16.1h / fund-flow 8.5h
  - severity：serious 的 median 24.9h，mean 108.2h
  - bug→fix-commit：对 top 11 磁铁的 171 个 bug 给出候选 fix commits

### ✅ Brief G-build — Point 3 注入槽 + provider
- **改动文件**：
  - 新增：`packages/story-lifecycle/src/story_lifecycle/context_providers/knowledge_provider.py`
  - 修改：`packages/story-lifecycle/src/story_lifecycle/context_providers/__init__.py`（新增 `get_knowledge_context`）
  - 修改：`packages/story-lifecycle/src/story_lifecycle/orchestrator/nodes/prompt_renderer.py`（加入 `{knowledge_context}` 槽）
  - 修改：`packages/story-lifecycle/src/story_lifecycle/prompts/design.md`
  - 修改：`packages/story-lifecycle/src/story_lifecycle/prompts/build.md`
  - 修改：`packages/story-lifecycle/src/story_lifecycle/prompts/verify.md`
- **产物**：
  - `packages/story-miner/scripts/out/brief_g_report.md`
- **A/B 信号**（以 `tapd-1144381896001064811` 为例）：
  - design prompt：1896 → 2862 字符（+966）
  - build prompt：1250 → 2216 字符（+966）
  - verify prompt：1487 → 2413 字符（+926）
- **首跑尝试**：
  - 选中 story：`tapd-1144381896001065618`（credit-limit）
  - 操作：`story sync --id 1144381896001065618` + 渲染 design prompt + `claude -p` 后台执行
  - 状态：用户要求停止后台任务，首跑未完成

## 二、尚未完成 / 需主窗口或其他窗口继续

### ⏳ Brief F — 过程轴 cold-start 挖矿
- 目标：per-task_type playbook/failure
- 依赖：transcripts.db + `story_task_types.json`
- 产出预期：`<ws>/.story/knowledge/playbooks/{task_type}.md` + `failures/`

### ⏳ Brief H — Point 6 门禁落地
- 目标：verify 阶段 failure-checklist + HIGH-severity block + repair round
- 范围：`quality.py`、`gate.py`/`evaluator_loop.py`、`verify.md`、profile 开 quality
- 依赖：outcome severity（已有）+ failures（F 理想，可先用 findings/learned_pattern）

### ⏳ Brief J — 硬关联 for 新需求
- 策略已定：merge commit
- 范围：`profiles/` 的 `branch_rule` 加 `{story_key}`；`story doctor` 加 linkage-health
- commit-msg hook 可选

### ⏳ Brief G-run — full-auto 首跑 + A/B
- 已完成 G-build 和 A/B prompt 对比
- 需要继续完成真实新需求的 design→build→verify 首跑，并收集 with/without 注入的效果数据

## 三、关键产物路径速查

```
packages/story-miner/scripts/out/
├── bug_story_graph.json              # A 的产物（已有 severity/time）
├── story_task_types.json             # C 的产物
├── story_task_types_summary.md       # C 的报告
├── known_magnet_commits.json         # E 的 ground truth
├── story_commits_inferred.json       # E 的推断结果
├── infer_bug_magnet_report.md        # E 的报告
├── result_axis_phase2.json           # I 的产物
├── result_axis_phase2.md             # I 的报告
└── brief_g_report.md                 # G-build 的报告

packages/story-lifecycle/src/story_lifecycle/
├── context_providers/
│   ├── __init__.py                   # 加了 get_knowledge_context
│   └── knowledge_provider.py         # 新增
├── orchestrator/nodes/prompt_renderer.py  # 加了 {knowledge_context} 槽
└── prompts/
    ├── design.md                     # 加了 {knowledge_context}
    ├── build.md                      # 加了 {knowledge_context}
    └── verify.md                     # 加了 {knowledge_context}
```

## 四、接手建议

1. **主窗口汇总验收**：把 C/E/I 的产物 join 到 `outcome_knowledge.md`，按 task_type 出最终知识。
2. **G-run 继续**：用 `tapd-1144381896001065618` 或另选一个未处理的 TAPD story，跑 design→build→verify；建议先跑一次 with knowledge_context，再跑一次 without（或同一 story 的 A/B 由不同窗口做）。
3. **冲突协调**：G 已避开 H/J 的改动范围；H 不要碰 `prompt_renderer.py` 的 `vars_map`；J 改 `branch_rule` 时不要用 `knowledge_context` 这个 key。
