> # dev-flywheel / story-miner 方向路线图（派发用）

> 把已验证的方向固化成可独立领的任务卡。其他 AI/subagent 领卡推进，主窗口汇总验收。
> **I1–I4 与 M1–M6 已完成**，见 `docs/INTEGRATION.md`、`docs/MIGRATION.md`、`packages/story-miner/scripts/out/i2-i4-verify.md`。

## 如何用本文档
- **领卡前**：先读 `packages/story-miner/CONTEXT.md` 上手（项目结构/db schema/运行方式/已知坑）
- **每卡独立**：标注 `[并行]` 的可同时派给多个 agent；`[依赖]` 需先完成前置
- **产出统一**：分析类产出写到 `packages/story-miner/scripts/out/<task-id>.md`；反哺 skill 的写到对应工作区 `.agents/skills/`；每份含「做法/关键发现/数字/未决问题」
- **约束通用**：只读 db（除明确要改 schema 的卡）；不改 adapter 已验证逻辑；金融 PII 红线（见 CONTEXT.md）
- **运行**：先激活 monorepo venv，再执行脚本
  ```bash
  cd D:/github/story-lifecycle
  source .venv-monorepo-test/Scripts/activate   # Windows Git Bash
  # 或 .venv-monorepo-test/bin/activate         # Linux/macOS
  ```

---

## T1 约束库产品化 `[并行]` `[高ROI]` `[规则表已完成，skill 接入待跟进]`
- **现状**：`constraint.py` 已从真实 user 指令抽 162 条约束（6 主题：分支git/数据库SQL/skill流程/配置/代码质量/部署），散落在对话里没结构化。
- **目标**：把高频约束转成可 lint 检查的规则，挂到 `code-standards-check` skill，新代码自动检查是否违反。
- **输入**：`packages/story-miner/scripts/constraint.py` + 工作区 `.agents/skills/code-standards-check/SKILL.md`
- **步骤**：① 按主题把约束归成规则（每条：规则文本/检查方式：静态grep或语义/严重级）；② 挑可自动 grep 的（如"doc不提交git""不在test直接改"）做成检查项；③ 接入 code-standards-check（加引导，遵循其现有风格）；④ 用最近 commit 验证能否检出。
- **产出**：`packages/story-miner/docs/constraint-rules.md`（规则表，已沉淀 8 条 grep 规则）+ skill 改动 + 验证结果
- **验收**：至少 5 条约束变成可执行检查项，且在样本 commit 上能跑

## T2 债务雷达打磨 `[并行]`
- **现状**：`debt.py` 从代码 diff 抽 TODO/FIXME/HACK，但被自己生成的脚本污染（feasibility_probe.py 自匹配），真实信号（ProxyService.java）被淹没。
- **目标**：加源码白名单，只扫 `.java/.ts/.tsx/.sql`，排除 `.py/.md/tmp`，产出干净债务清单。
- **输入**：`scripts/debt.py` + db
- **步骤**：① debt.py 加扩展名白名单 + 排除 .claude/tmp/scripts 路径；② 重跑；③ 对 top 文件用 codegraph 核验债务是否仍在。
- **产出**：`scripts/out/debt.md`（干净版）+ debt.py 改动
- **验收**：命中全部是真实源码文件，无自生成脚本噪声

## T3 自动复盘产品化 `[已完成]`
- **状态**：`packages/story-miner/scripts/retrospect.py` 已支持单会话、批量 Top5、Story 级三种模式；输出结构化（任务/做了什么/关键决策/踩坑/访问文件/结论）；路径使用 `generate_playbooks.short()`，不再丢首字母。
- **用法**：
  ```bash
  python packages/story-miner/scripts/retrospect.py <sid>
  python packages/story-miner/scripts/retrospect.py
  python packages/story-miner/scripts/retrospect.py --story <story_key>
  ```
- **验收**：给定任一 sid / story_key 能产出可读复盘。

## T4 智能推荐 → 任务上下文包 `[并行]` `[可复用至 I3 provider]`
- **现状**：`recommend.py` 已支持 `--package` 生成 <500 字上下文包。`story-lifecycle` I3 默认使用 `miner.story_context_provider`（按 story_key 聚合历史 session）。
- **目标**：让 `recommend.py --package` 的输出与 `story_context_provider` 互补——`story_context_provider` 服务已走 story 的需求，`recommend.py --package` 服务排查/小需求/无 story 任务。
- **输入**：`packages/story-miner/scripts/recommend.py` + db + `packages/story-miner/playbooks/`
- **步骤**：① 校验 `--package` 输出格式稳定；② 补充"常见踩坑"（failure checklist）和"必看文件"；③ 输出可直接注入 prompt 的 markdown 段；④ 提供一份样本（如"免息清分"）。
- **产出**：`recommend.py` 改动 + 一份样本上下文包
- **验收**：上下文包 <500 字、相关、可读，能直接喂 prompt

## T5 蒸馏脱敏管线做实 `[依赖mask增强]` `[门槛]`
- **现状**：`distill.py` 管线跑通（选轨迹→SFT messages→mask），15 候选，但 mask 只覆盖手机号/邮箱/长数字，金融 PII 不彻底。
- **目标**：做实脱敏 + 批量导出可用的 SFT 语料（parquet/jsonl）。
- **输入**：`scripts/distill.py` + db
- **步骤**：① 扩展 mask：cid（如 9999707）、用户名、idNo、生产 SQL 结果特征；② 放宽候选条件扩到全工作区获数百条；③ 导出 ShareGPT 格式；④ 抽样人工复核脱敏完整性。
- **产出**：distill.py 改动 + 脱敏样本 + 导出语料（本地，不入 git）
- **验收**：抽样 20 条，人工确认无 PII 泄露；导出 ≥100 条
- **红线**：语料绝不入 git；导出前必须人工复核

## T6 工作量预估做实（9 转向） `[并行]`
- **现状**：`predict.py` 证伪了"成败预测"（无标签），但"工作量预估"可粗估（hc-all turns 中位 10/P90 29）。
- **目标**：做实工作量预估——给定任务特征（ws + 任务类型 + 是否触及共享状态），给历史基线 + 区间。
- **输入**：`scripts/predict.py` + db + stories（story 复杂度 S/M/L）
- **步骤**：① 按 ws × 任务类型 × 复杂度 算 turns/tools 基线（中位/P90）；② 输出"预估表"（这类任务大概多少轮/工具）；③ 诚实标注方差大、只能给量级。
- **产出**：`scripts/out/effort-estimate.md`（预估表）+ predict.py 改动
- **验收**：预估表覆盖主要任务类型，每类有中位/P90/样本数

## T7（可选）失败模式 → 避坑检查项 `[已完成]`
- **状态**：`packages/story-miner/docs/failure-checklist.md` 已产出，含失败分布、10 条检查项、`build-check` / `pre-release-review` skill 接入建议。
- **产出**：`packages/story-miner/docs/failure-checklist.md`
- **待跟进**：实际把清单挂到具体工作区 skill（视各项目需要）。

## T8（可选）三端 benchmark 产品化 `[并行]`
- **现状**：`tri_efficiency.py` 有三端效率画像（Claude 工具广度33/Codex 多轮低密度/Kimi 单轮短）。
- **目标**：做同题对比评分卡（同 story/同任务三端 turns/tools/errs/成功 对比）。
- **依赖**：需更多同题三端样本（当前 story 只 18% 关联）

---

## 依赖与并行
- **已完成**：T3、T7；I1–I4 与 M1–M6。
- **可立即并行**（只读 db + 不同输出）：T1（skill 接入部分）、T2、T4、T6
- **有依赖**：T5 需先增强 mask（可由 T5 自己含）；T8 需更多同题样本
- **story-lifecycle 联动**：I3 默认 provider 已启用；T4 的上下文包可作为通用/排查类任务的补充注入源

## 汇总约定
- 每卡完成后产出 md，主窗口按「做法/发现/数字/未决」验收
- 改动 hc-all skill 的卡（T1/T7）：遵循 CONTEXT.md 的 junction 约束（改 .agents、禁同步脚本、Grep 用直接路径）
- 主窗口汇总时：跑一遍各卡产出 + 检查交叉发现（如某任务类型 × 某失败类型）
