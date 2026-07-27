# Real-Story 测试跟踪

跑真实 story 穿过 story-lifecycle 编排器的人工测试记录。**每次跑测一份详情**,本文件是总表索引。

## 怎么用

1. **跑测前**:复制 `_TEMPLATE.md` → 建 `RUN-<story-key简写>-<YYYYMMDD>.md`,在本表加一行
2. **跑测中**:边跑边填详情(监控见 `run-real-story-test` skill)
3. **跑测后**:填「发现清单」+「本次沉淀」,总表状态列更新
4. **攒 skill**:详情里的 skill 候选,复现 ≥2-3 次后用 skill-creator 固化

完整操作流程见 `.claude/skills/run-real-story-test/SKILL.md`。

## 跑测索引

| 日期 | Story | Profile | 模式 | 状态 | 关键发现 | 详情 |
|---|---|---|---|---|---|---|
| _待填_ | _待填_ | _待填_ | _待填_ | _待填_ | _待填_ | _待填_ |

**状态取值**:进行中 / 已完成 / 已中止 / 失败
**模式取值**:新建需求 / 接手中途需求

## Skill 候选池(跨次汇总)

> 从各详情的「本次沉淀」提炼。**复现 ≥2-3 次才值得固化**。固化后从这移除,标 [已固化] + skill 名。

| 候选 | 触发场景 | 来源 | 复现次数 | 状态 |
|---|---|---|---|---|
| _暂无_ | | | | |

## 历史跑测参考(本套跟踪机制建立前)

这些跑测发生在 `test-runs/` 目录建立之前,没有按本模板记录,但它们的发现已沉淀进代码/文档:

- **`tapd-1144381896001065488`**(2026-07-10):全自动 FC 流程人工走查 → 10 个 bug,记录在 `docs/BUGLOG-fullauto-walkthrough-20260710.md`。这是本跟踪机制的雏形。
- **`tapd-1144381896001067713`**(2026-07-26):老版本跑的 story,作为新版本对比基线。
- **`local-amountraise-rerun`**(2026-07-27):新版本重跑同需求,暴露 `done_data` UnboundLocalError(已修 `e612ec3b`),沉淀出 AGENTS.md「Story execution entry」+ ARCHITECTURE.md「PTY 监控 + adapter recovery」。
