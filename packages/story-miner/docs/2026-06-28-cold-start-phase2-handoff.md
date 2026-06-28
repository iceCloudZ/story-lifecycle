# 冷启动 phase 2 — 闭环飞轮（handoff）

> 日期：2026-06-28 · 分支 `feat/bug-story-graph`（phase 1 结果轴已 done）
> 设计依据：`2026-06-28-cold-start-mining-brainstorm.md`（点 3 注入 / 点 6 门禁 / 结果轴设计）
> **进度总管：主窗口 Claude**——跟踪 5 个 brief 状态、验收产物、协调冲突、做最后的 full-auto 首跑集成 + 飞轮回报实测。

phase 1（结果轴）挖出了 outcome 知识（哪些 feature 易出 bug + 磁铁代码 11/11）。phase 2 = **闭环**：补过程知识 → 接通注入 → 首跑实测 → 门禁 → 深化 → 硬关联。

## 共享上下文

- corpus 口径：`custom_field_25=赵子豪` ≈ 150 story（≈ story.db 147）；TAPD workspace `44381896`，访问经 `~/.claude/scripts/cli_tapd.py`。
- 已有产物（`packages/story-miner/scripts/out/`，gitignore 本地）：`bug_story_graph.json`（bug↔story + severity/time）、`story_task_types.json`（150→12 类）、`story_commits.json`+`story_commits_inferred.json`+`known_magnet_commits.json`（代码）、`bug_iteration_links.json`（weak sprint）、`outcome_knowledge.md`（聚合）。
- bug↔story 走**反向 `get_related_bugs`**（正向 story_id 全空）。hc-all 是 multi-repo（17 子仓），commit 在子项目。
- 12 类 task_type 词表已确认（见设计文档）。
- 全自动 infra 已知坑：`claude -p` 跑完 done file 不退出、miner 可能被 legacy 包 shadow（见记忆 `real-e2e-flywheel-green`）。

---

## 并行图（派活用）

**第一批（4 个，互不冲突，可同时派）**：
- **F** 过程轴挖矿 → story-miner/scripts
- **I** 结果轴二期 → story-miner/scripts（新分析脚本）
- **J** 硬关联 for 新需求 → story-lifecycle profiles + git hook
- **H** 门禁 → story-lifecycle quality.py/gate.py/verify.md

**G（Point3 注入 + 首跑）= 关键路径**：
- **G-build**（context_providers + `{knowledge_context}` 槽）可与第一批并行，但 `prompt_renderer.py` 和 `profiles/` 要和 H、J 错开（见冲突）。
- **G-run**（full-auto 首跑 + A/B）= **集大成，最后跑**，等 G-build + F（过程知识）齐。

**冲突点（协调）**：
- G ↔ H 都在 story-lifecycle：G 加 `{knowledge_context}` 槽；H 用**已有** `{quality_checklist}` 槽（`prompt_renderer.py:352`）+ 在 quality.py/gate.py 干活。→ H 别碰 prompt_renderer vars_map，错开即可。
- G ↔ J 都可能动 profiles：G 加 knowledge 配置、J 改 branch_rule。→ 不同 key，错开。
- F、I 在 story-miner 不同脚本，互不撞。

---

## Brief F — 过程轴 cold-start 挖矿

- **目标**：产出 per-task_type 的 playbook/failure（"怎么干"），补全飞轮知识的过程半边。
- **范围**：`packages/story-miner/scripts/`（generate_playbooks.py / failure_mode.py 等）。**对齐 12 类词表**（用 C 的 `story_task_types.json` 打标，不再用旧 7-theme）。
- **依赖**：transcripts.db（hc-all）+ story_task_types.json。独立。
- **产出**：`<ws>/.story/knowledge/playbooks/{task_type}.md` + `failures/`（advisory 知识，喂点 3）。
- **验收**：12 个 playbook（稀疏的标盲区，呼应发散点 5）；failure 按 task_type 分；抽检 1-2 份人审有用。
- **坑**：unbound session（~80%）也要挖（发散点 4）。

## Brief G — Point 3 注入接通 + full-auto 首跑（🔑 关键路径）

- **目标**：建 `knowledge_context` 槽 + provider（设计见点 3：渲染文件→注入引用→AI 按需读），然后 full-auto 首跑新需求 + with/without 注入 A/B。
- **范围**：`packages/story-lifecycle/.../context_providers/`（新 `KnowledgeContextProvider`）+ `prompt_renderer.py`（加 `{knowledge_context}` 槽）+ design/build 模板加槽 + 一个真实新需求跑通。
- **依赖**：knowledge_context 内容 = outcome（已 done）+ 过程 playbook（F，可先最小版只用 outcome）。run 需 full-auto infra（已知坑，先 `story doctor` + 修 claude-p-不退出 / miner-shadow）。
- **产出**：provider + 槽 + 首跑结果 + A/B 三层数据（给了/读了/有没有效）。
- **验收**：新需求跑通 design→build→verify；`{knowledge_context}` 注入了 task_type→路径清单；A/B 有 baseline vs 注入对比。
- **注意**：这是飞轮回报的第一次实测——**最小版优先**（先只注 outcome，过程 playbook 后补），早拿信号。

## Brief H — Point 6 门禁落地

- **目标**：verify 阶段 failure-checklist + gate 执行层（severity HIGH→block，repair round）。
- **范围**：`quality.py`（`build_quality_checklist` 已存在，喂 mined failures + outcome severity）、`verify.md`（`{quality_checklist}` 槽已存在）、`gate.py`/`evaluator_loop.py`（现 inert，接进 verify 路径）、profile 开 quality。
- **依赖**：outcome severity（done）+ failures（F 理想，可先用 findings/learned_pattern）。
- **产出**：verify 跑 checklist，HIGH 失败 block + repair round（反思式，复用 repair_packet）。
- **验收**：一个 story 的 verify 能 block HIGH-severity 项、触发 repair、到 max_retries 升级人工。LLM-judge 用**换模型**（自增强偏见）。

## Brief I — 结果轴二期（深化）

- **目标**：过程↔结果相关 + cycle-time + code-survival + bug 自己的 fix-commit。
- **范围**：`packages/story-miner/scripts/` 新分析脚本。
- **依赖**：数据齐（bug_story_graph + story_commits + inferred magnets）。独立。
- **产出**：① bug 磁铁代码 vs 它们的 bug → "bug-prone 代码模式"（治种子偏见金子）；② cycle-time（bug created→resolved）按 task_type；③ code-survival/churn；④ bug→fix-commit（一期 B 是 story→commit，bug 修复 commit 用 E 的 time+语义法）。
- **验收**：per-task_type bug-prone 代码模式 ≥1 条可解读；cycle-time 基线；抽检。

## Brief J — 硬关联 for 新需求（merge 策略已定：merge commit）

- **目标**：今后新 story/bug 天然 hard 关联，不用事后推断。
- **策略（已定）**：**merge commit**（沿用 GitLab MR 现状）→ 分支名带 story_key 即够（merge commit 自动记分支名，`git log --grep <story_key>` 可查）。
- **范围**：`profiles/`（minimal/strict）`branch_rule` 加 `{story_key}`（现 `feature/{author}/{summary}_{date}`）；`story doctor` 加 linkage-health（% hard / 孤儿分支）；commit-msg hook **可选加分**（非必需）。
- **依赖**：无（merge 已定）。独立。
- **产出**：branch_rule 带 story_key + doctor linkage-health 报告 + 可选 commit-msg hook。
- **验收**：新 story 分支名含 story_key；`git log --grep <story_key>` 命中 merge commit；doctor 报新工作 % hard。

---

## 我怎么管进度

- 每个 brief 派出去后，我记状态（派发中/产出/已验）。
- 产物回来 → 我**验收**（像 phase 1 的 A/B/C/D/E：核覆盖率/质量/真实性 + 抽检）。
- 协调冲突（G↔H prompt_renderer、G↔J profiles）。
- **G-run 我亲自做**（full-auto 首跑 + A/B + 飞轮回报实测）——这是集大成，也是项目核心命题第一次有答案。
- 全部完成后合 main。
