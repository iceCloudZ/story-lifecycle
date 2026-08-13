# B 线（50 条全链路回放）迁移 101 交接文档

> 版本：v1.0（2026-08-12）｜ 交接方：本地 opencode/kimi 轨 ｜ 接管方：ZCode（101）
> 背景文档：`docs/iteration-4-pass-accuracy-design.md`（B 线设计）、`docs/eval-journey-20260804-20260812.md` 附录（进展日志）
> 目的：B 线剩余 45 条从本地 Windows 迁到 101 续跑，本地从此退出长跑业务。读这一份即可接管。

## 1. 为什么要迁（30 秒版）

B 线 50 条历史 story 全链路回放（agent 真实跑 design→verify），本地已完成 5 条、剩余 45 条（约 5-15h 长尾）。本地 runner 被 Windows 外部清理**杀了 3 次**，靠手写守护脚本续命——101 有 systemd + ZCode 已验证的 serve 健壮性（5.6h 零崩），长跑任务天然属于那里。

## 2. 当前状态（接管时点）

- **真完成 5 条**：1065587（270s）等，明细在 `results/b_line_20260812.jsonl`
- **剩余 45 条**：断点续跑就绪
- **已知形态分布**（前 5 条）：真完成 200-650s/条；卡闸（declare 缺失）200-550s；薄 PRD 无产出 1 条；长尾 agent 20-60min
- **已修复的坑（不要回退）**：
  1. PRD 短 id bug（`tapd_id[-7:]` 前导 0 → 空 PRD → agent 拒写），已修为 `tapd_id[12:]`
  2. story_refs 网页噪音（钉钉编辑器 UI 词），已加清洗词表（1024105/1023315 仍薄，标注）
  3. confirm-gate 卡死：pipeline_replay 已加 `--auto-advance`（eval 沙箱专用，生产安全网不动）
  4. 看门狗 900s → **3600s**（真干活样本 600-900s+，900 会误杀；机制本身保留防真卡死）

## 3. 运行机制（技术要点）

- **runner**：`packages/eval/src/eval/pipeline_replay.py`——逐条 `run_one(tapd_id, gold_dir, auto_advance=True)`；B 线外层有个 ad-hoc 驱动脚本写 `results/b_line_20260812.jsonl`（未入库，本地 opencode 会话产物——**接管时让 opencode 把驱动脚本本体一并打包**，或按本文 §4 重写一个等价 wrapper）
- **命令形态**：`eval pipeline-replay --samples <batch.json> --auto-advance`（或 python -m 直调，以 opencode 交接的实际命令行为准）
- **输入**：每条 story 一个 gold 目录 `sandbox/gold/tapd-<full_tapd_id>/`（内含构造 PRD，已修复+清洗后的版本）
- **清单**：`dataset/b_batch50_20260812.json`（50 条 {src, tapd_id} + 排除列表 + 种子 42）
- **断点**：逐条 append `results/b_line_20260812.jsonl`，重跑跳过已完成键
- **LLM**：全程 opencode-go（`https://opencode.ai/zen/go/v1` + deepseek-v4-flash）；`STORY_LLM_BASE_URL` 等 env 与 path-A 冒烟一致（`~/story-eval/eval.env`）
- **沙箱**：每条独立 workspace（sandbox/ws/），STORY_HOME 隔离；101 上无 hc-all 全量——**B 线 replay 不依赖 hc-all 仓库**（agent 在沙箱 workspace 内工作，PRD 已构造进 gold 目录），跨服务 9 条不受影响

## 4. 交接物清单（本地 → 101）

| 物 | 本地路径 | 101 建议落点 | 说明 |
|---|---|---|---|
| 批次清单 | `packages/eval/dataset/b_batch50_20260812.json` | `~/story-eval/dataset/` | git main 已有，pull 即可 |
| 断点 jsonl | `packages/eval/results/b_line_20260812.jsonl` | `~/story-eval/results/` | **不上 git**（results 大文件惯例），scp 直传 |
| gold 目录 ×50 | `packages/eval/sandbox/gold/tapd-*`（50 个） | `~/story-eval/sandbox/gold/` | scp 打包；含修复后 PRD |
| B 线驱动脚本 | opencode 会话内的 ad-hoc wrapper | 同上 | 交接时导出；含看门狗 3600s + 断点逻辑 + b_line jsonl 写入 |
| eval 代码 | git main | pull | ZCode 侧已同步 |

**数据边界记账**：50 条 gold PRD（含 TAPD 需求正文/证据文本）随本交接离开内网上 101（用户自有机器，有效期一年）。本地 opencode 需在 `b_batch50_20260812.json` 追加 `"pushed_to_101": "2026-08-12"` 审计字段——curated 纪律闭环。

## 5. 接管步骤（ZCode 侧）

1. `git pull`（拿 b_batch50 清单 + 本交接文档）；scp 接收断点 jsonl + gold 目录 + 驱动脚本
2. 环境：`source ~/story-eval/eval.env`；确认 `EVAL_DATASET_DIR` 指向接收的 dataset；**pre-flight 打印 base_url 确认 Go 端点**（铁律）
3. **单 runner 确认**：本地 runner 已停（opencode 侧先停后传）——45 条绝不能两边同时跑
4. 起跑：断点续跑（已完成 5 条自动跳过）；建议先 `--only` 单跑 1 条验证链路，再放量
5. 托管：runner 挂 systemd user unit（参照 story-serve.service 的做法，Restart=on-failure）或 nohup + ZCode 自选；不再需要本地那套 b_watch.py 创可贴
6. 跑完：最终统计（b_final_stats.py 口径：tapd 去重保留最新 + 修复前旧行单列不计完成率）append 进 `results/iteration4_20260812.md` §5

## 6. 纪律（与本地一致，逐条有效）

- judge 三元组：Go 端点 only；放量前 pre-flight；跑完 grep api.deepseek.com 必须 0 命中
- 连续 3 条 infra 失败自动暂停并报告，不硬闯
- **A 线结论已出（系统性保守）**：B 线 gate 判决可按「保守偏向」解读，但最终统计仍只罗列数字 + 归因，不为 story 质量下结论
- declare 卡闸样本：统计时按「profile 契约问题 vs agent 行为问题」二分归因，不只给计数
- 101 绝不碰内网：不需要也不允许从 101 访问 TAPD/hc-all 内网资源（B 线输入已全部随包提供）

## 7. 结果回流

- **摘要层**：跑完把最终统计写 `results/nightly/b_line_final_2026081X.md`（小文件），commit 推 main——本地 pull 即见；注意 results/ 部分文件 gitignored，`results/nightly/` 需显式 `git add -f` 或调整 .gitignore 白名单
- **明细层**：b_line jsonl 全量留 101，需要分析时本地 scp 拉取
- **叙事层**：kimi 负责把里程碑数字追加进 `docs/eval-journey-20260804-20260812.md` 附录——ZCode 只需推 git，附录由本地维护

## 8. 验收（迁移完成判定）

- [ ] 101 上首条（`--only`）跑通，pre-flight 确认 Go 端点
- [ ] 断点确认：已完成 5 条被跳过，从第 6 条续跑
- [ ] 本地 runner + b_watch 已停（单 runner）
- [ ] `pushed_to_101` 审计字段已写
- [ ] systemd/nohup 托管生效，runner 存活可观测（心跳日志）

## 9. 未决（不在本交接范围，主跑完后回本地议题池）

- 迭代 5 议题：conformance 措辞与分数分离（A 线）/ 薄 PRD agent 行为定义 / 看门狗分档 / declare 契约归因
- nightly 常驻化正式迁移：B 线跑完后，每晚批次直接在 101 起跑（replay_set 扩展规范见 iteration-4 设计文档 §4.1）
