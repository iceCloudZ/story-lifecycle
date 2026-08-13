# B 线最终统计 — 2026-08-13/14（101 服务器 44 条续跑完成）

> 口径：`b_final_stats`（tapd 去重保留最新行；修复前旧数据单列不计入完成率）
> 数据源：101 `~/story-lifecycle/packages/eval/results/b_line_20260812.jsonl`（124 行，明细层留 101）
> 跑批：systemd `b-line-runner`，44 条断点续跑（5 条已完成跳过），NRestarts=0 全程零崩溃

## 总览

- 总行数 **124**，去重后 **50** 条（新行 49 / 修复前旧数据 1）
- 主口径 cls 分布：**ok 11 ／ active_stall 31 ／ confirm_gate_stall 7**
- **真完成率：11/49 = 22%**（旧数据 1 条 ok 单列，不计入）
- **watchdog_timeout（长尾强杀）：0** —— 本地时代 5 条长尾样本重跑后全部收敛为 stall/ok，60min 看门狗在 101 未触发
- 耗时：ok 样本 123~1406s；stall 样本 797~1765s

## ok 明细（11 条）

| tapd | 耗时 | done 回执 |
|---|---|---|
| 1065487 | 384s | verify |
| 1065587 | 270s | verify |
| 1065601 | 123s | —（无 declare 直接判过，边缘样本） |
| 1067189 | 792s | verify |
| 1065518 | 799s | verify |
| 1068441 | 1034s | design+verify |
| 1057808 | 632s | design+verify |
| 1066915 | 791s | design+verify |
| 1039270 | 1406s | design+verify |
| 1034681 | 395s | verify |
| 1063104 | 1242s | design+verify |

## stall 归因二分（38 条，只列数字不下质量结论）

**profile 契约问题（成果物落地/declare 齐，卡在流程闸或判定层）——26 条：**

- confirm_gate_stall **7** 条（story=paused 等人确认）：3 条 verify 两阶段成果物全齐卡「上线/结项」尾闸（1067429/1024096/1036126）；4 条 design 完卡确认闸（1064888/1065008/1038496/1065460）
- active_stall 中 design+verify done 双齐的 **19** 条：成果物全部落地但 story 未终态化——verify judge LLM 瞬态失败 → escalate fallback → 驱动循环 12 轮耗尽（判定层可靠性，非 agent 行为）

**agent 行为问题（done 缺失）——12 条：**

- active@design 无任何 done 的 **3** 条（1036077/1057251/1061721）：薄 PRD 拒写类
- design-only 的 **9** 条（含薄 PRD 已标注的 1023315/1065560）：design 完未产出 verify

## 遗留（迭代 5 素材）

- 38 条 stall 在断点逻辑中不算完成，任何后续 pass 会重跑（等迭代 5 归因修复后重放）
- verify judge LLM 瞬态失败是 active_stall 主因之一（冒烟时 3 连败、当晚同配置多次通过）——判定层超时/重试策略待议
- agent 泄漏：3 个孤儿 agent 在 unit 退出时被 cgroup 清理兜底回收；runner sweep 对「独立会话（start_new_session）agent」的根治方案待做
